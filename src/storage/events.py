"""Календарь соревнований хранилища Competitions: события с guard'ом дублей, участники, явные связки «запись →
событие» и массовое связывание.

Перенесено из src/storage/sqlite.py без изменения поведения (Architecture v2.1). Контракт примеси: не
создаёт соединение и блокировку (self.connection / self._lock принадлежат SQLiteAdapter), не импортирует
соседние доменные модули src.storage.* (кроме src.storage.helpers), междоменные вызовы — только через
self."""
import json
from datetime import datetime
from typing import Sequence

from src.storage.helpers import CALENDAR_LINK_PREVIEW_CAP
from src.storage.helpers import CalendarEventDuplicateError
from src.storage.helpers import event_identity_key


class EventsMixin:
    # ---- Календарь соревнований (волна A, docs/feedback-live.md №23) ----
    #
    # Событие календаря — план (пресет): название + период + уровень +
    # вид спорта + ссылка. Участники — обычные записи реестра, совпадающие
    # с пресетом по (name, date, date_to); записи хранят даты в ISO, пустой
    # date_to = однодневное (совпадение по COALESCE с обеих сторон).

    @staticmethod
    def _calendar_preset_match_sql(alias: str = 'c') -> str:
        return (
            f'({alias}.name = e.name'
            f' AND {alias}.date = e.date'
            f" AND COALESCE({alias}.date_to, '') = COALESCE(e.date_to, ''))"
        )

    def _calendar_events_starting_on(self, date) -> list:
        """Кандидаты guard'а дублей: события с той же датой начала — узкий
        предфильтр substr(date,1,10), финальное сравнение идёт ключом в
        Python. Вызывается под _lock (внутри check+write guard-методов)."""
        return self.connection.execute(
            'SELECT id, name, date, date_to, sport, level FROM calendar_events' ' WHERE substr(date, 1, 10) = ?',
            ((date or '')[:10],),
        ).fetchall()

    def _find_calendar_event_duplicate(self, name, date, date_to, sport, level, exclude_id=None) -> dict | None:
        """Точный дубль события по event_identity_key (под _lock): норма
        названия + обе даты (день, без времени) + sport/level. exclude_id
        выкидывает само событие — правка «на себя» дублем не считается."""
        key = event_identity_key(name, date, date_to, sport, level)
        for row in self._calendar_events_starting_on(date):
            if exclude_id is not None and row['id'] == exclude_id:
                continue
            row_key = event_identity_key(row['name'], row['date'], row['date_to'], row['sport'], row['level'])
            if row_key == key:
                return dict(row)
        return None

    def find_similar_calendar_event(self, name, date, date_to, sport, level, exclude_id=None) -> dict | None:
        """Похожее событие для НЕблокирующего предупреждения роута создания
        (2026-09-27): то же нормализованное название и дата начала, но
        ДРУГОЙ identity-ключ (отличаются date_to/sport/level) — вероятно,
        дубль, но гарантии нет, поэтому создание не блокируется. Только
        чтение (SQLite serialized + одно соединение — достаточно);
        возвращает первое найденное {id, name, date, date_to, sport, level}
        или None."""
        key = event_identity_key(name, date, date_to, sport, level)
        for row in self._calendar_events_starting_on(date):
            if exclude_id is not None and row['id'] == exclude_id:
                continue
            row_key = event_identity_key(row['name'], row['date'], row['date_to'], row['sport'], row['level'])
            if row_key[0] == key[0] and row_key != key:
                return dict(row)
        return None

    def _insert_event_links(self, event_id: int, links: Sequence[tuple[str, str]]) -> None:
        """Вставить строки ссылок события (Multiple Event Links).

        Без собственного commit: вызывается под _lock внутри транзакции
        создателя (create/update). sort_order 0..N-1 — порядок строк формы,
        чтение упорядочивает по (sort_order, id). Пары уже провалидированы
        роутом (label/url непустые, схема http/https, кап 20).
        """
        created_at = datetime.utcnow().isoformat()
        for sort_order, (label, url) in enumerate(links):
            self.connection.execute(
                'INSERT INTO calendar_event_links (calendar_event_id, label, url, sort_order, created_at)'
                ' VALUES (?, ?, ?, ?, ?)',
                (event_id, label, url, sort_order, created_at),
            )

    def _calendar_event_links(self, event_id: int) -> list[dict]:
        """Ссылки события по (sort_order, id) — детерминированный порядок
        строк формы (вызывается под _lock)."""
        rows = self.connection.execute(
            'SELECT id, label, url, sort_order FROM calendar_event_links'
            ' WHERE calendar_event_id = ? ORDER BY sort_order ASC, id ASC',
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def create_calendar_event(
        self,
        name: str,
        date: str,
        date_to: str | None,
        level: str,
        sport: str,
        links: Sequence[tuple[str, str]],
    ) -> int:
        """Новое событие календаря с 0..N ссылками и guard'ом точных дублей
        (2026-09-27).

        Проверка и INSERT — под одним _lock (атомарный check+write): событие
        с тем же event_identity_key уже есть → CalendarEventDuplicateError
        (existing — найденный дубль), запись не выполнялась, отката не
        нужно. Существующие дубли БД и повторные create того же события
        через raw SQL guard не трогает — блокируются только новые INSERT
        через этот метод. Legacy-колонка url не пишется (DEFAULT '')."""
        with self._lock:
            duplicate = self._find_calendar_event_duplicate(name, date, date_to, sport, level)
            if duplicate is not None:
                raise CalendarEventDuplicateError(duplicate)
            cursor = self.connection.execute(
                'INSERT INTO calendar_events (name, date, date_to, level, sport, created_at)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                (name, date, date_to, level, sport, datetime.utcnow().isoformat()),
            )
            event_id = cursor.lastrowid
            self._insert_event_links(event_id, links)
            self.connection.commit()
            return event_id

    def get_calendar_event(self, event_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, name, date, date_to, level, sport, url, created_at, '
                'regulation_filename, regulation_stored_name FROM calendar_events WHERE id = ?',
                (event_id,),
            ).fetchone()
            if row is None:
                return None
            event = dict(row)
            event['links'] = self._calendar_event_links(event_id)
            return event

    def set_calendar_regulation(self, event_id: int, filename: str, stored_name: str) -> None:
        """Прикрепить/заменить файл положения события (колонки + имя файла)."""
        with self._lock:
            self.connection.execute(
                'UPDATE calendar_events SET regulation_filename = ?, regulation_stored_name = ? WHERE id = ?',
                (filename, stored_name, event_id),
            )
            self.connection.commit()

    def clear_calendar_regulation(self, event_id: int) -> None:
        """Отвязать файл положения события (обе колонки в NULL)."""
        with self._lock:
            self.connection.execute(
                'UPDATE calendar_events SET regulation_filename = NULL, regulation_stored_name = NULL WHERE id = ?',
                (event_id,),
            )
            self.connection.commit()

    def update_calendar_event(
        self,
        event_id: int,
        name: str,
        date: str,
        date_to: str | None,
        level: str,
        sport: str,
        links: Sequence[tuple[str, str]],
    ) -> int:
        """Правка события + полная замена ссылок + синхронизация связанных
        записей одной транзакцией.

        Event Model, Wave 1 P2: событие — владелец полей участия
        name/date/date_to/sport/level, правка пресета переносится в записи
        со ссылкой calendar_event_id (NULL-legacy-строки не трогаются —
        ссылки у них нет). Сбой любого шага — откат всего (паттерн
        import_competitions). Возвращает synced — число синхронизированных
        записей (rowcount UPDATE). Ссылки — full-replace: DELETE + INSERT
        (sort_order 0..N-1), 0 строк = легитимные 0 ссылок; url в sync НЕ
        входит и не входил. Сами ссылки тоже в той же транзакции — сбой
        откатывает и их.

        Guard дублей (2026-09-27): правка НЕ на себя, совпадающая с чужим
        событием по event_identity_key → CalendarEventDuplicateError внутри
        try (существующий rollback-путь) — 0 изменений события, ссылок и
        связанных записей, синхронизация не происходит.
        """
        with self._lock:
            try:
                duplicate = self._find_calendar_event_duplicate(name, date, date_to, sport, level, exclude_id=event_id)
                if duplicate is not None:
                    raise CalendarEventDuplicateError(duplicate)
                self.connection.execute(
                    'UPDATE calendar_events'
                    ' SET name = ?, date = ?, date_to = ?, level = ?, sport = ?'
                    ' WHERE id = ?',
                    (name, date, date_to, level, sport, event_id),
                )
                self.connection.execute('DELETE FROM calendar_event_links WHERE calendar_event_id = ?', (event_id,))
                self._insert_event_links(event_id, links)
                cursor = self.connection.execute(
                    'UPDATE competitions'
                    ' SET name = ?, date = ?, date_to = ?, sport = ?, level = ?'
                    ' WHERE calendar_event_id = ?',
                    (name, date, date_to, sport, level, event_id),
                )
                synced = cursor.rowcount
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
            return synced

    def count_calendar_event_participants(self, event_id: int) -> int:
        """Блокировщик удаления события (консервативный OR, решение A).

        Считаются записи, связанные ссылкой calendar_event_id, ИЛИ
        совпадающие с пресетом (name + date + date_to): legacy-NULL-строки
        по пресету из участников страницы события уже исчезли (id-first
        reading), но молчаливое удаление события с записями-двойниками
        пресета хуже ложного блокирующего отказа — владелец решает случай
        вручную (unlink/перелинковка в будущих фазах).
        """
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM calendar_events e, competitions c'
                f' WHERE e.id = ? AND (c.calendar_event_id = e.id OR {self._calendar_preset_match_sql()})',
                (event_id,),
            ).fetchone()
            return row['total']

    def delete_calendar_event(self, event_id: int) -> None:
        """Удалить событие вместе со строками его ссылок (один commit).
        Guard участников живёт в роуте и не меняется."""
        with self._lock:
            self.connection.execute('DELETE FROM calendar_event_links WHERE calendar_event_id = ?', (event_id,))
            self.connection.execute('DELETE FROM calendar_events WHERE id = ?', (event_id,))
            self.connection.commit()

    def list_calendar_events(self, sport: str = '') -> list[dict]:
        """Все события по хронологии; рядом — счётчики участников по ссылке
        calendar_event_id (P2, id-first; NULL-legacy-строки пресета не
        считаются): participant_count — все связанные записи реестра,
        no_result_count — из них с position = 0 («без результата»). Каждая
        строка несёт links — ссылки события по (sort_order, id)."""
        with self._lock:
            params: list[object] = []
            where = ''
            if sport:
                where = 'WHERE e.sport = ?'
                params.append(sport)
            rows = self.connection.execute(
                f'''
                SELECT
                    e.id,
                    e.name,
                    e.date,
                    e.date_to,
                    e.level,
                    e.sport,
                    e.url,
                    e.created_at,
                    COUNT(c.id) AS participant_count,
                    SUM(CASE WHEN c.position = 0 THEN 1 ELSE 0 END) AS no_result_count
                FROM calendar_events e
                LEFT JOIN competitions c ON c.calendar_event_id = e.id
                {where}
                GROUP BY e.id
                ORDER BY e.date ASC, e.name ASC
                ''',
                params,
            ).fetchall()
            events = [
                {
                    'id': row['id'],
                    'name': row['name'],
                    'date': row['date'],
                    'date_to': row['date_to'],
                    'level': row['level'],
                    'sport': row['sport'],
                    'url': row['url'],
                    'created_at': row['created_at'],
                    'participant_count': row['participant_count'] or 0,
                    'no_result_count': row['no_result_count'] or 0,
                }
                for row in rows
            ]
            links_by_event: dict[int, list[dict]] = {}
            if events:
                placeholders = ', '.join('?' for _ in events)
                link_rows = self.connection.execute(
                    'SELECT id, calendar_event_id, label, url, sort_order FROM calendar_event_links'
                    f' WHERE calendar_event_id IN ({placeholders})'
                    ' ORDER BY calendar_event_id ASC, sort_order ASC, id ASC',
                    tuple(event['id'] for event in events),
                ).fetchall()
                for link_row in link_rows:
                    link = dict(link_row)
                    links_by_event.setdefault(link.pop('calendar_event_id'), []).append(link)
            for event in events:
                event['links'] = links_by_event.get(event['id'], [])
            return events

    def list_calendar_event_participants(self, event_id: int) -> list[dict]:
        """Записи реестра — участники события по ссылке calendar_event_id.

        Event Model, Wave 1 P2 (id-first): участники события — только явно
        связанные записи; NULL-legacy-строки, совпадающие с пресетом по
        случайности, на странице события не показываются (счётчик удаления
        при этом консервативно учитывает и их — OR, см.
        count_calendar_event_participants).
        Сначала с результатом, затем «ждут результата», внутри — по ФИО.
        student_ref_id — для предпросмотра импорта участников (поиск уже
        существующих участий той же карточки). P4: discipline/result/extra_data
        (распарсенный JSON) — аддитивно, для классификации повторов импорта
        (эффективные значения участия с legacy-фолбэком в extra_data).
        """
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT
                    c.id AS record_id,
                    c.student_name,
                    c.student_sex,
                    c.institute,
                    c."group" AS group_name,
                    c.course,
                    c.position,
                    c.student_ref_id,
                    c.discipline,
                    c.result,
                    c.extra_data
                FROM competitions c
                WHERE c.calendar_event_id = ?
                ORDER BY
                    CASE WHEN c.position = 0 THEN 1 ELSE 0 END,
                    c.student_name ASC
                ''',
                (event_id,),
            ).fetchall()
            return [{**dict(row), 'extra_data': json.loads(row['extra_data'] or '{}')} for row in rows]

            return [dict(row) for row in rows]

    # ---- P5b: явные связки «запись → событие» (Registry ↔ Calendar) ----
    #
    # Явное связывание существующей NULL-записи со событием календаря:
    # ссылка calendar_event_id + синхронизация 5 event-owned полей
    # (name/date/date_to/sport/level) — тот же состав, что у edit-sync
    # update_calendar_event. Правило гонок: guarded UPDATE
    # (WHERE calendar_event_id IS NULL) — запись, которую успел связать
    # кто-то другой, не перезаписывается (already_linked), существующая
    # ссылка не меняется.

    def link_participation_to_event(self, record_id: int, event_id: int) -> tuple[dict | None, str | None]:
        """Связать запись со существующим событием + синхронизация полей.

        Одна транзакция под _lock (паттерн update_calendar_event): сбой
        любого шага — откат всего. Возвращает (event, None) при успехе или
        (None, код ошибки): record_not_found / event_not_found /
        already_linked (запись уже связана — её ссылка и поля не тронуты).
        """
        with self._lock:
            try:
                record = self.connection.execute(
                    'SELECT id, calendar_event_id FROM competitions WHERE id = ?',
                    (record_id,),
                ).fetchone()
                if record is None:
                    return None, 'record_not_found'
                event = self.connection.execute(
                    'SELECT * FROM calendar_events WHERE id = ?',
                    (event_id,),
                ).fetchone()
                if event is None:
                    return None, 'event_not_found'
                cursor = self.connection.execute(
                    'UPDATE competitions'
                    ' SET calendar_event_id = ?, name = ?, date = ?, date_to = ?, sport = ?, level = ?'
                    ' WHERE id = ? AND calendar_event_id IS NULL',
                    (
                        event_id,
                        event['name'],
                        event['date'],
                        event['date_to'],
                        event['sport'],
                        event['level'],
                        record_id,
                    ),
                )
                if cursor.rowcount == 0:
                    # Между SELECT и UPDATE запись успел связать другой
                    # запрос — откатываем (SELECT читает снапшот неявной
                    # транзакции) и отказываем без перезаписи.
                    self.connection.rollback()
                    return None, 'already_linked'
                self.connection.commit()
                return dict(event), None
            except BaseException:
                self.connection.rollback()
                raise

    def create_calendar_event_and_link(
        self,
        *,
        name: str,
        date: str,
        date_to: str | None,
        level: str,
        sport: str,
        links: Sequence[tuple[str, str]],
        record_id: int,
    ) -> tuple[int | None, str | None]:
        """Создать событие календаря (со ссылками) и сразу связать с ним запись.

        Атомарно (одна транзакция): INSERT события + ссылки (без собственного
        commit, паттерн create_calendar_event) + guarded UPDATE записи
        с синхронизацией 5 полей. Проверки записи (существует + ещё NULL)
        выполняются ДО вставки, чтобы не оставлять событий-сирот; гонку на
        UPDATE закрывает rowcount (=0 → already_linked, откат всего).
        Возвращает (event_id, None) или (None, record_not_found /
        already_linked).

        Guard дублей (2026-09-27, ДОКУМЕНТИРОВАННОЕ расширение кортежа):
        точный дубль по event_identity_key уже существует →
        (existing_event_id, 'duplicate_event') — слот event_id несёт id
        СУЩЕСТВУЮЩЕГО события (роут показывает его в подсказке), новое
        событие НЕ создаётся и запись НЕ линкуется. Проверка — до INSERT,
        записей не было, откат не нужен.
        """
        with self._lock:
            try:
                record = self.connection.execute(
                    'SELECT id, calendar_event_id FROM competitions WHERE id = ?',
                    (record_id,),
                ).fetchone()
                if record is None:
                    return None, 'record_not_found'
                if record['calendar_event_id'] is not None:
                    return None, 'already_linked'
                duplicate = self._find_calendar_event_duplicate(name, date, date_to, sport, level)
                if duplicate is not None:
                    return int(duplicate['id']), 'duplicate_event'
                cursor = self.connection.execute(
                    'INSERT INTO calendar_events (name, date, date_to, level, sport, created_at)'
                    ' VALUES (?, ?, ?, ?, ?, ?)',
                    (name, date, date_to, level, sport, datetime.utcnow().isoformat()),
                )
                event_id = cursor.lastrowid
                self._insert_event_links(event_id, links)
                linked = self.connection.execute(
                    'UPDATE competitions'
                    ' SET calendar_event_id = ?, name = ?, date = ?, date_to = ?, sport = ?, level = ?'
                    ' WHERE id = ? AND calendar_event_id IS NULL',
                    (event_id, name, date, date_to, sport, level, record_id),
                )
                if linked.rowcount == 0:
                    self.connection.rollback()
                    return None, 'already_linked'
                self.connection.commit()
                return event_id, None
            except BaseException:
                self.connection.rollback()
                raise

    def count_participations_without_event(self) -> int:
        """Дешёвый счётчик записей без ссылки на событие (карточка
        «Связывание с календарём» на странице обслуживания)."""
        with self._lock:
            return int(
                self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions WHERE calendar_event_id IS NULL'
                ).fetchone()['total']
            )

    def calendar_link_backfill_preview(self) -> dict:
        """Read-only предпросмотр массового связывания NULL-записей.

        Предикат — ровно семантика стартового P0-backfill
        (_calendar_preset_match_sql: name + date + COALESCE(date_to, '')).
        Группы: matched (ровно один кандидат-событие), ambiguous (больше
        одного — вручную, автоматики не будет), unmatched (ноль).
        Списки capped (CALENDAR_LINK_PREVIEW_CAP) — счётчики при этом
        полные. Значения полей для диффа «после связывания» маршрут
        берёт из события сам (get_calendar_event).
        """
        with self._lock:
            predicate = self._calendar_preset_match_sql('c')
            rows = self.connection.execute(
                f'''
                SELECT
                    c.id AS record_id,
                    c.student_name,
                    c.name,
                    c.date,
                    c.date_to,
                    c.sport,
                    c.level,
                    (SELECT COUNT(*) FROM calendar_events e WHERE {predicate}) AS candidate_count
                FROM competitions c
                WHERE c.calendar_event_id IS NULL
                ORDER BY c.date ASC, c.id ASC
                '''
            ).fetchall()
            already_linked = int(
                self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions WHERE calendar_event_id IS NOT NULL'
                ).fetchone()['total']
            )
            matched: list[dict] = []
            ambiguous: list[dict] = []
            unmatched: list[dict] = []
            for row in rows:
                base = {
                    'record_id': row['record_id'],
                    'student_name': row['student_name'],
                    'name': row['name'],
                    'date': row['date'],
                    'date_to': row['date_to'],
                    'sport': row['sport'],
                    'level': row['level'],
                }
                # Кандидаты строки — тот же предикат пресета, что и в
                # подсчёте выше (синхронно с _calendar_preset_match_sql):
                # коррелированная форма по id записи, один источник
                # семантики совпадения.
                candidate_where = f'competitions.id = ? AND {self._calendar_preset_match_sql("competitions")}'
                if row['candidate_count'] == 1:
                    if len(matched) < CALENDAR_LINK_PREVIEW_CAP:
                        event = self.connection.execute(
                            f'SELECT e.id AS event_id, e.name AS event_name '
                            f'FROM competitions, calendar_events e WHERE {candidate_where}',
                            (row['record_id'],),
                        ).fetchone()
                        matched.append({**base, 'event_id': event['event_id'], 'event_name': event['event_name']})
                elif row['candidate_count'] > 1:
                    if len(ambiguous) < CALENDAR_LINK_PREVIEW_CAP:
                        candidates = self.connection.execute(
                            f'SELECT e.name AS event_name '
                            f'FROM competitions, calendar_events e WHERE {candidate_where}',
                            (row['record_id'],),
                        ).fetchall()
                        ambiguous.append(
                            {
                                **base,
                                'candidate_count': row['candidate_count'],
                                'candidate_names': [candidate['event_name'] for candidate in candidates],
                            }
                        )
                elif len(unmatched) < CALENDAR_LINK_PREVIEW_CAP:
                    unmatched.append(base)
            matched_total = sum(1 for row in rows if row['candidate_count'] == 1)
            ambiguous_total = sum(1 for row in rows if row['candidate_count'] > 1)
            return {
                'counters': {
                    'considered': len(rows),
                    'matched': matched_total,
                    'unmatched': len(rows) - matched_total - ambiguous_total,
                    'ambiguous': ambiguous_total,
                    'already_linked': already_linked,
                },
                'matched': matched,
                'ambiguous': ambiguous,
                'unmatched': unmatched,
            }

    def apply_calendar_link_backfill(self) -> tuple[dict[str, int], list[dict]]:
        """Массовое связывание matched-записей (одна транзакция).

        В батч входят ТОЛЬКО строки с единственным пресет-кандидатом
        (=1) — ambiguous не линкуются никогда, unmatched нечего линковать.
        Для каждой строки — guarded UPDATE с синхронизацией 5 полей:
        rowcount = 0 (запись успели связать между предпросмотром и apply)
        — пропуск (skipped), не откат пакета. Возвращает счётчики
        {matched, linked, skipped} и список связанных
        [{record_id, event_id, event_name}].
        """
        with self._lock:
            predicate = self._calendar_preset_match_sql('c')
            pairs = self.connection.execute(
                f'''
                SELECT
                    c.id AS record_id,
                    (SELECT e.id FROM calendar_events e WHERE {predicate}) AS event_id,
                    (SELECT e.name FROM calendar_events e WHERE {predicate}) AS event_name
                FROM competitions c
                WHERE c.calendar_event_id IS NULL
                  AND (SELECT COUNT(*) FROM calendar_events e WHERE {predicate}) = 1
                ORDER BY c.id ASC
                '''
            ).fetchall()
            counters = {'matched': len(pairs), 'linked': 0, 'skipped': 0}
            items: list[dict] = []
            try:
                for pair in pairs:
                    event = self.connection.execute(
                        'SELECT name, date, date_to, sport, level FROM calendar_events WHERE id = ?',
                        (pair['event_id'],),
                    ).fetchone()
                    cursor = self.connection.execute(
                        'UPDATE competitions'
                        ' SET calendar_event_id = ?, name = ?, date = ?, date_to = ?, sport = ?, level = ?'
                        ' WHERE id = ? AND calendar_event_id IS NULL',
                        (
                            pair['event_id'],
                            event['name'],
                            event['date'],
                            event['date_to'],
                            event['sport'],
                            event['level'],
                            pair['record_id'],
                        ),
                    )
                    if cursor.rowcount == 0:
                        # Кто-то связал запись после предпросмотра — не
                        # ломаем пакет, считаем пропуском.
                        counters['skipped'] += 1
                        continue
                    counters['linked'] += 1
                    items.append(
                        {
                            'record_id': pair['record_id'],
                            'event_id': pair['event_id'],
                            'event_name': pair['event_name'],
                        }
                    )
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
            return counters, items
