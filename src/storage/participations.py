"""Записи соревнований хранилища Competitions: выборки и серверные фильтры, отчёты, сохранение и импорт, батч
участий события, правка и удаление.

Перенесено из src/storage/sqlite.py без изменения поведения (Architecture v2.1). Контракт примеси: не
создаёт соединение и блокировку (self.connection / self._lock принадлежат SQLiteAdapter), не импортирует
соседние доменные модули src.storage.* (кроме src.storage.helpers), междоменные вызовы — только через
self."""
import json
import sqlite3
from datetime import datetime
from typing import Collection
from typing import Iterable
from typing import Sequence

from src.models.competition import Competition
from src.models.http.report_row import ReportSliceRow
from src.models.http.student_info import StudentInfo
from src.settings import settings
from src.storage.helpers import competition_insert_records
from src.storage.helpers import COMPETITION_INSERT_SQL
from src.storage.helpers import COMPETITION_SELECT_SQL
from src.storage.helpers import dedup_discipline
from src.storage.helpers import dedup_text
from src.storage.helpers import DEFAULT_REPORT_GROUPING
from src.storage.helpers import EventParticipationConflictError
from src.storage.helpers import IDENTITY_MODE_DEFAULT
from src.storage.helpers import participation_content_key
from src.storage.helpers import REPORT_GROUPINGS
from src.storage.helpers import REPORT_METRIC_SELECTS


class ParticipationsMixin:
    def get_competition_by_id(self, record_id: int) -> Competition | None:
        """Одна запись по id — правая сторона разбора конфликта импорта."""
        with self._lock:
            row = self.connection.execute(
                f'{COMPETITION_SELECT_SQL}WHERE id = ?',
                (record_id,),
            ).fetchone()
            return self._row_to_competition(row) if row else None

    def get_competitions(
        self,
        owner_id: int | None = None,
        student_id_hashes: Sequence[str] = (),
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
    ) -> Iterable[Competition]:
        with self._lock:
            scope_clauses, scope_params = self._competition_scope_clauses(
                owner_id, student_id_hashes, student_ref_id=student_ref_id, identity_mode=identity_mode
            )
            where_clause = f'WHERE {" AND ".join(scope_clauses)}\n' if scope_clauses else ''
            query = f'{COMPETITION_SELECT_SQL}{where_clause}ORDER BY date ASC, created_at ASC'
            rows = self.connection.execute(query, scope_params).fetchall()
            return [self._row_to_competition(row) for row in rows]

    @staticmethod
    def _competition_scope_clauses(
        owner_id: int | None,
        student_id_hashes: Sequence[str],
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
    ) -> tuple[list[str], list[object]]:
        """Видимость записей (кабинет атлета, P3 dual-read): свои по owner_id,
        по стабильной связи student_ref_id и — в dual-режиме — по хешам
        привязанных ФИО (легаси). В ref-режиме легаси-ветка не применяется.
        Как раньше: без owner_id ограничение не применяется — модератор
        видит весь реестр."""
        if owner_id is None:
            return [], []
        clauses = ['owner_id = ?']
        params: list[object] = [owner_id]
        if student_ref_id is not None:
            clauses.append('student_ref_id = ?')
            params.append(student_ref_id)
        if identity_mode != 'ref' and student_id_hashes:
            placeholders = ', '.join('?' for _ in student_id_hashes)
            clauses.append(f'student_id IN ({placeholders})')
            params.extend(student_id_hashes)
        return ['(' + ' OR '.join(clauses) + ')'], params

    @staticmethod
    def _multi_in_clause(column: str, values: Sequence[str]) -> tuple[str, list[object]]:
        """IN-условие мульти-фильтра главной: OR внутри одного фильтра.

        Одиночная строка (легаси-вызовы и одиночные значения URL)
        сворачивается в список из одного — «col IN (?)» совпадает с прежним
        «col = ?». Пустая последовательность — фильтр не применяется.
        """
        normalized: tuple[str, ...] = (values,) if isinstance(values, str) else tuple(values)
        if not normalized:
            return '', []
        placeholders = ', '.join('?' for _ in normalized)
        return f'{column} IN ({placeholders})', list(normalized)

    def _competition_filter_clauses(
        self,
        *,
        owner_id: int | None,
        student_id_hashes: Sequence[str] = (),
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
        institute: Sequence[str] = (),
        sport: Sequence[str] = (),
        level: Sequence[str] = (),
        group: Sequence[str] = (),
        review_status: str = '',
        unapproved_only: bool = False,
        date_from: str = '',
        date_to: str = '',
    ) -> tuple[str, list[object]]:
        """Общий WHERE серверных фильтров главной (прототип 02): видимость
        (P3 dual-read: owner / student_ref_id / легаси-хеши) +
        институт/группа/вид спорта/уровень (мульти-выбор: OR внутри фильтра,
        AND между фильтрами), статус проверки и период дат (дд.мм.гггг, как
        в фильтрах отчёта).

        ФИО сюда НЕ входит: SQL LIKE регистронезависим только для ASCII и
        кириллицу не находит — подстрока по ФИО фильтруется в Python
        (casefold) общим путём счётчика и страницы, см.
        _fetch_filtered_competition_rows."""
        clauses, params = self._competition_scope_clauses(
            owner_id, student_id_hashes, student_ref_id=student_ref_id, identity_mode=identity_mode
        )

        for column, values in (
            ('institute', institute),
            ('sport', sport),
            ('level', level),
            ('"group"', group),
        ):
            clause, in_params = self._multi_in_clause(column, values)
            if clause:
                clauses.append(clause)
                params.extend(in_params)
        if review_status:
            clauses.append('review_status = ?')
            params.append(review_status)
        if unapproved_only:
            # Статусы у всех записей, кроме подтверждённых (для колонки «Статус»).
            clauses.append("review_status != 'approved'")

        date_clauses, date_params = self._date_range_clauses(date_from, date_to)
        clauses.extend(date_clauses)
        params.extend(date_params)

        where_clause = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        return where_clause, params

    def _fetch_filtered_competition_rows(
        self,
        *,
        owner_id: int | None,
        student_id_hashes: Sequence[str] = (),
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
        name: str = '',
        institute: Sequence[str] = (),
        sport: Sequence[str] = (),
        level: Sequence[str] = (),
        group: Sequence[str] = (),
        review_status: str = '',
        unapproved_only: bool = False,
        date_from: str = '',
        date_to: str = '',
    ) -> list[sqlite3.Row]:
        """Все строки под фильтрами главной в порядке реестра (date ASC,
        created_at ASC) — ЕДИНЫЙ путь счётчика и страницы, инвариант
        «счётчик == сумме строк по страницам» держится одним кодом.

        ФИО — пост-фильтр в Python: casefold-подстрока находит «Иванов» по
        «иванов»/«ИВАНОВ»/«иваНОВ» (кириллица + Ё/ё), как уже работает
        search_student_suggestions. Полная выгрузка под _lock приемлема в
        текущем масштабе. Побочный эффект смены LIKE → casefold: % и _ в
        запросе теперь литералы, а не wildcard.
        """
        where_clause, params = self._competition_filter_clauses(
            owner_id=owner_id,
            student_id_hashes=student_id_hashes,
            student_ref_id=student_ref_id,
            identity_mode=identity_mode,
            institute=institute,
            sport=sport,
            level=level,
            group=group,
            review_status=review_status,
            unapproved_only=unapproved_only,
            date_from=date_from,
            date_to=date_to,
        )
        query = f'{COMPETITION_SELECT_SQL}{where_clause}\nORDER BY date ASC, created_at ASC'
        with self._lock:
            rows = self.connection.execute(query, params).fetchall()
        needle = (name or '').strip().casefold()
        if not needle:
            return rows
        return [row for row in rows if needle in (row['student_name'] or '').strip().casefold()]

    def count_competitions_filtered(
        self,
        *,
        owner_id: int | None = None,
        student_id_hashes: Sequence[str] = (),
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
        name: str = '',
        institute: Sequence[str] = (),
        sport: Sequence[str] = (),
        level: Sequence[str] = (),
        group: Sequence[str] = (),
        review_status: str = '',
        unapproved_only: bool = False,
        date_from: str = '',
        date_to: str = '',
    ) -> int:
        """Сколько записей видно пользователю с учётом серверных фильтров.

        Счётчик «Показано N из M» главной: M — с фильтром, без пагинации.
        unapproved_only — признак «есть не подтверждённые» (видимость колонки
        «Статус» не должна зависеть от текущей страницы/фильтра).
        """
        return len(
            self._fetch_filtered_competition_rows(
                owner_id=owner_id,
                student_id_hashes=student_id_hashes,
                student_ref_id=student_ref_id,
                identity_mode=identity_mode,
                name=name,
                institute=institute,
                sport=sport,
                level=level,
                group=group,
                review_status=review_status,
                unapproved_only=unapproved_only,
                date_from=date_from,
                date_to=date_to,
            )
        )

    def get_competitions_page(
        self,
        *,
        owner_id: int | None = None,
        student_id_hashes: Sequence[str] = (),
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
        name: str = '',
        institute: Sequence[str] = (),
        sport: Sequence[str] = (),
        level: Sequence[str] = (),
        group: Sequence[str] = (),
        review_status: str = '',
        date_from: str = '',
        date_to: str = '',
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Competition]:
        """Страница реестра с серверной фильтрацией (прототип 02).

        Те же фильтры, что у count_competitions_filtered (общий путь —
        _fetch_filtered_competition_rows); сортировка и порядок как в
        get_competitions (created_at ASC). limit/offset применяются к
        ПОСТ-фильтрованному списку (ФИО — Python-фильтр) — пагинация главной,
        None возвращает всё (совпадение с get_competitions).
        """
        rows = self._fetch_filtered_competition_rows(
            owner_id=owner_id,
            student_id_hashes=student_id_hashes,
            student_ref_id=student_ref_id,
            identity_mode=identity_mode,
            name=name,
            institute=institute,
            sport=sport,
            level=level,
            group=group,
            review_status=review_status,
            date_from=date_from,
            date_to=date_to,
        )
        if limit is None:
            return [self._row_to_competition(row) for row in rows]
        end = offset + limit
        return [self._row_to_competition(row) for row in rows[offset:end]]

    @staticmethod
    def _build_custom_filter_clauses(
        custom_filters: Iterable[tuple[str, str, str]],
    ) -> tuple[list[str], list[object]]:
        clauses = []
        params: list[object] = []
        for key, field_type, value in custom_filters:
            column = 'json_extract(extra_data, \'$."' + key + '"\')'
            if field_type == 'number':
                clauses.append(f'CAST({column} AS INTEGER) = ?')
                params.append(int(value))
            elif field_type == 'date':
                clauses.append(column + ' = ?')
                params.append(value)
            else:
                clauses.append(f'{column} LIKE ?')
                params.append(f'%{value}%')
        return clauses, params

    def get_filtered(
        self,
        date_from: str = '',
        date_to: str = '',
        position: str = '',
        level: str = '',
        name: str = '',
        custom_filters: Iterable[tuple[str, str, str]] = (),
        *,
        institute: str = '',
        group: str = '',
        sport: str = '',
    ) -> list[StudentInfo]:
        """Отчёт по срезу «студент»: группировка по данным записи + метрики."""
        rows = self._fetch_report_rows(
            DEFAULT_REPORT_GROUPING,
            date_from=date_from,
            date_to=date_to,
            position=position,
            level=level,
            name=name,
            institute=institute,
            group=group,
            sport=sport,
            custom_filters=custom_filters,
        )
        return [
            StudentInfo(
                student_id=row['student_id'],
                student_name=row['student_name'],
                student_sex=row['student_sex'],
                institute=row['institute'],
                group=row['group'],
                course=row['course'],
                count_participation=row['count_participation'],
                count_wins=row['count_wins'],
                count_prizes=row['count_prizes'],
            )
            for row in rows
        ]

    def get_grouped_report(
        self,
        group_by: str,
        *,
        date_from: str = '',
        date_to: str = '',
        position: str = '',
        level: str = '',
        name: str = '',
        institute: str = '',
        group: str = '',
        sport: str = '',
        custom_filters: Iterable[tuple[str, str, str]] = (),
    ) -> list[ReportSliceRow]:
        """Отчёт по произвольному срезу: значение поля записи + метрики.

        group_by — ключ REPORT_GROUPINGS, кроме 'student' (у него другая
        форма строки, см. get_filtered). Фильтры те же, что у get_filtered.
        """
        if group_by == DEFAULT_REPORT_GROUPING:
            raise ValueError('Срез «студент» обрабатывает get_filtered')
        if group_by not in REPORT_GROUPINGS:
            raise ValueError(f'Неизвестный срез отчёта: {group_by}')
        rows = self._fetch_report_rows(
            group_by,
            date_from=date_from,
            date_to=date_to,
            position=position,
            level=level,
            name=name,
            institute=institute,
            group=group,
            sport=sport,
            custom_filters=custom_filters,
        )
        return [
            ReportSliceRow(
                slice_value=row['slice_value'],
                count_participation=row['count_participation'],
                count_wins=row['count_wins'],
                count_prizes=row['count_prizes'],
            )
            for row in rows
        ]

    @staticmethod
    def _date_range_clauses(date_from: str, date_to: str) -> tuple[list[str], list[object]]:
        """Условия периода отчёта: границы парсятся форматом settings.date_format.

        Даты-диапазоны (решение владельца 2026-09-13, docs/data-model-
        decisions.md): запись показывается, если ХОТЯ БЫ ОДНА из её дат
        (date_from ИЛИ date_to) попала в диапазон фильтра. Записи без
        date_to ведут себя как раньше — их единственная дата = date.
        """
        clauses = []
        params: list[object] = []
        if date_from:
            bound = datetime.strptime(date_from, settings.date_format).isoformat()
            clauses.append('(date >= ? OR COALESCE(date_to, date) >= ?)')
            params.extend([bound, bound])
        if date_to:
            bound = datetime.strptime(date_to, settings.date_format).isoformat()
            clauses.append('(date <= ? OR COALESCE(date_to, date) <= ?)')
            params.extend([bound, bound])
        return clauses, params

    def _report_filter_clauses(
        self,
        *,
        date_from: str,
        date_to: str,
        position: str,
        level: str,
        name: str,
        institute: str,
        group: str,
        sport: str,
        custom_filters: Iterable[tuple[str, str, str]],
    ) -> tuple[str, list[object]]:
        """Общий WHERE всех фильтров отчёта: период, место, уровень, ФИО,
        институт, группа, вид спорта, кастомные поля. Только approved."""
        filters = ["review_status = 'approved'"]
        params: list[object] = []

        date_clauses, date_params = self._date_range_clauses(date_from, date_to)
        filters.extend(date_clauses)
        params.extend(date_params)

        if position:
            sign = position[0]
            value = int(position[1:])
            if sign == '>':
                filters.append('position > ?')
                params.append(value)
            elif sign == '<':
                filters.append('position < ?')
                params.append(value)

        if level:
            filters.append('level = ?')
            params.append(level)

        if name:
            filters.append('student_name LIKE ?')
            params.append(f'%{name}%')

        if institute:
            filters.append('institute = ?')
            params.append(institute)

        if group:
            filters.append('"group" = ?')
            params.append(group)

        if sport:
            filters.append('sport = ?')
            params.append(sport)

        custom_clauses, custom_params = self._build_custom_filter_clauses(custom_filters)
        filters.extend(custom_clauses)
        params.extend(custom_params)

        where_clause = f'WHERE {" AND ".join(filters)}' if filters else ''
        return where_clause, params

    def _fetch_report_rows(
        self,
        group_by: str,
        *,
        date_from: str,
        date_to: str,
        position: str,
        level: str,
        name: str,
        institute: str,
        group: str,
        sport: str,
        custom_filters: Iterable[tuple[str, str, str]],
    ) -> list[sqlite3.Row]:
        """Агрегирующий запрос отчёта: GROUP BY по данным записи + метрики.

        Один WHERE для всех фильтров отчёта (см. _report_filter_clauses) и
        метрики SUM CASE — SQL считает всё сам, питон по строкам не ходит.
        """
        with self._lock:
            group_columns = REPORT_GROUPINGS[group_by]
            where_clause, params = self._report_filter_clauses(
                date_from=date_from,
                date_to=date_to,
                position=position,
                level=level,
                name=name,
                institute=institute,
                group=group,
                sport=sport,
                custom_filters=custom_filters,
            )
            if group_by == DEFAULT_REPORT_GROUPING:
                select_columns = ',\n'.join(group_columns)
                order_by = 'count_participation ASC, student_name ASC, institute ASC, "group" ASC, course ASC'
            else:
                select_columns = f'{group_columns[0]} AS slice_value'
                order_by = 'count_participation ASC, slice_value ASC'
            rows = self.connection.execute(
                f'''
                SELECT
                    {select_columns},
                    {', '.join(REPORT_METRIC_SELECTS)}
                FROM competitions
                {where_clause}
                GROUP BY
                    {', '.join(group_columns)}
                ORDER BY {order_by}
                ''',
                params,
            ).fetchall()
            return rows

    def save_competitions(
        self,
        competitions: Iterable[Competition],
        review_status: str = 'approved',
        owner_id: int | None = None,
    ):
        with self._lock:
            records = competition_insert_records(
                self._fill_admission_year_snapshots(list(competitions)),
                review_status,
                owner_id,
            )
            self.connection.executemany(COMPETITION_INSERT_SQL, records)
            self.connection.commit()

    def import_competitions(
        self,
        new_competitions: Sequence[Competition],
        *,
        review_status: str = 'approved',
        owner_id: int | None = None,
    ) -> None:
        """Импорт одной транзакцией с одним COMMIT (находка QA №1).

        Записи вставляются, затем справочники (уровни, виды спорта, институты,
        пары институт→группа) пополняются значениями ИЗ ЗАПИСЕЙ — решение
        2026-09-13 (docs/data-model-decisions.md «Переименование значений
        справочников»): раньше справочник сеялся строками файла, включая
        пропущенные дубли, поэтому переименованное с обновлением записей
        значение возвращалось в справочник следующим импортом того же файла.
        Теперь источник — таблица записей: значение возвращается, только если
        оно реально есть в записях. Сбой на любом шаге — откат всего импорта:
        либо все строки, либо ничего (вместе со справочниками).

        `new_competitions` — то, что реально вставляется; дубли отсеивает
        вызывающая сторона до транзакции (split_import_competitions).
        """
        with self._lock:
            try:
                if new_competitions:
                    # Снимок года поступления — ДО синхронизации справочников:
                    # пары институт+группа из этого же импорта ещё не существуют
                    # в учебных данных → у записей остаётся NULL (заполнит
                    # backfill/последующие вставки; наполнять метаданные из
                    # парсера названия автоматически нельзя).
                    filled = self._fill_admission_year_snapshots(list(new_competitions))
                    records = competition_insert_records(filled, review_status, owner_id)
                    self.connection.executemany(COMPETITION_INSERT_SQL, records)
                self._sync_catalogs_from_records()
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def _event_participation_recheck_context(self, event_id: int) -> tuple[set, set, set]:
        """Живые ключи уникальности участий события (P4): (identity-ключи,
        контент-ключи, ключи коллидирующих кастомов). Вызывается ПОД _lock
        внутри батча — повторная проверка видит актуальное состояние, а не
        снимок предпросмотра.

        identity — (карточка, дисциплина) связанного участия; контент — полный
        ключ participation_content_key. Дисциплина/результат существующего
        участия читаются с legacy-фолбэком (P5a dual-write): NULL в базовой
        колонке значит «взять значение активного кастома с коллидирующим
        label из extra_data» — старые записи до P4 писали только туда.
        Ключи коллидирующих кастомов исключаются из customs-части контент-ключа
        у ОБЕИХ сторон сравнения (QA D2: эффективная дисциплина легаси-строки
        в extra_data, вставляемой — в базовой колонке)."""
        collision_labels = ('дисциплина', 'результат')
        collision_keys = [
            (field.key, field.label.strip().casefold())
            for field in self.get_custom_fields()
            if field.label.strip().casefold() in collision_labels
        ]
        collision_custom_keys = {key for key, _ in collision_keys}
        identities: set = set()
        contents: set = set()

        def effective(base, extra, folded_label: str) -> str:
            value = str(base or '').strip()
            if value:
                return value
            for key, label in collision_keys:
                if label == folded_label:
                    fallback = str(extra.get(key) or '').strip()
                    if fallback:
                        return fallback
            return ''

        for participant in self.list_calendar_event_participants(event_id):
            extra = participant.get('extra_data') or {}
            discipline = effective(participant.get('discipline'), extra, 'дисциплина')
            result = effective(participant.get('result'), extra, 'результат')
            if participant.get('student_ref_id') is not None:
                identities.add(('ref', participant['student_ref_id'], dedup_discipline(discipline)))
            contents.add(
                participation_content_key(
                    participant['student_name'],
                    discipline,
                    participant['student_sex'],
                    participant['institute'],
                    participant['group_name'],
                    participant['course'],
                    participant['position'],
                    result,
                    extra,
                    collision_custom_keys,
                )
            )
        return identities, contents, collision_custom_keys

    @staticmethod
    def _event_participation_batch_guard(
        inserts: Sequence[Competition],
        live_identities: set,
        live_contents: set,
        exclude_custom_keys: Collection[str] = (),
    ) -> None:
        """Повторная проверка insert-строк батча участий (P4) против живой БД
        и самого батча (intra-batch first-wins). Поднимает
        EventParticipationConflictError при коллизии: MATCHED — identity
        (карточка + дисциплина); CARDLESS против живой БД — точный контент,
        внутри батча — контент-ключ (ФИО + дисциплина): оба участия одного
        файла с тем же ключом сервер не вставляет (предпросмотр показывает
        их конфликтом строк). exclude_custom_keys — симметричное исключение
        коллидирующих кастомов из customs-части контент-ключа (QA D2,
        см. participation_content_key)."""
        batch_identities: set = set()
        batch_contents: set = set()
        batch_cardless_keys: set = set()
        for competition in inserts:
            content = participation_content_key(
                competition.student_name,
                competition.discipline,
                competition.student_sex,
                competition.institute,
                competition.group,
                competition.course,
                competition.position,
                competition.result,
                competition.extra_data,
                exclude_custom_keys,
            )
            if competition.student_ref_id is not None:
                identity = ('ref', competition.student_ref_id, dedup_discipline(competition.discipline))
                if identity in live_identities or identity in batch_identities:
                    raise EventParticipationConflictError('participation identity already exists')
                batch_identities.add(identity)
            else:
                cardless_key = (dedup_text(competition.student_name), dedup_discipline(competition.discipline))
                if content in live_contents or content in batch_contents:
                    raise EventParticipationConflictError('identical cardless participation already exists')
                if cardless_key in batch_cardless_keys:
                    raise EventParticipationConflictError('cardless participation key already in batch')
                batch_cardless_keys.add(cardless_key)
            batch_contents.add(content)

    def apply_event_participation_batch(
        self,
        event_id: int,
        inserts: Sequence[Competition],
        updates: Sequence[tuple[int, object, str | None]] | None = None,
        *,
        review_status: str = 'approved',
        owner_id: int | None = None,
    ) -> None:
        """Батч участий события одной транзакцией со СТРОГОЙ повторной
        проверкой уникальности (P4, паттерн import_competitions: вставки +
        синхронизация справочников из записей + один COMMIT; любой сбой —
        rollback всего батча вместе со справочниками).

        Повторная проверка под _lock (см. _event_participation_batch_guard)
        — коллизия означает, что предпросмотр устарел: сервер не позволяет
        создать оба повторных участия. `updates` — restricted-обновления
        (record_id, position, result) участий ЭТОГО события той же
        транзакцией."""
        with self._lock:
            try:
                live_identities, live_contents, collision_custom_keys = self._event_participation_recheck_context(
                    event_id
                )
                self._event_participation_batch_guard(inserts, live_identities, live_contents, collision_custom_keys)
                if inserts:
                    filled = self._fill_admission_year_snapshots(list(inserts))
                    records = competition_insert_records(filled, review_status, owner_id)
                    self.connection.executemany(COMPETITION_INSERT_SQL, records)
                for record_id, position, result in updates or ():
                    cursor = self.connection.execute(
                        'UPDATE competitions SET position = ?, result = ? WHERE id = ? AND calendar_event_id = ?',
                        (position, result, int(record_id), int(event_id)),
                    )
                    if not cursor.rowcount:
                        raise EventParticipationConflictError('updated participation not found in event')
                self._sync_catalogs_from_records()
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def update_event_participation_result(self, record_id: int, event_id: int, position, result: str | None) -> int:
        """P4: restricted-обновление места/результата существующего участия
        события — SET только position/result, WHERE id + calendar_event_id:
        снимок участника, custom-поля, вложения, student_ref_id и ссылка на
        событие физически недостижимы этим UPDATE. Возвращает число изменённых
        строк (0 — участие не найдено или принадлежит другому событию)."""
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE competitions SET position = ?, result = ? WHERE id = ? AND calendar_event_id = ?',
                (position, result, int(record_id), int(event_id)),
            )
            self.connection.commit()
            return cursor.rowcount

    def _fill_admission_year_snapshots(self, competitions: list[Competition]) -> list[Competition]:
        """Заполнить NULL-снимки года поступления перед вставкой записей.

        Course/Education Phase A: источник — ТОЛЬКО учебные данные группы
        (строгое разрешение пары институт+группа, resolve_group_admission_
        year). Парсер названий здесь НЕ работает и никогда не будет: он
        предзаполнение формы и backfill-классификация, не доменная истина.
        Уже заданное значение не перезаписывается; пара без учебных данных
        остаётся NULL (фолбэк отображения — легаси course).
        """
        for item in competitions:
            if item.admission_year is None:
                item.admission_year = self.resolve_group_admission_year(item.institute, item.group)
        return competitions

    def _sync_catalogs_from_records(self) -> None:
        """Довести справочники до значений, реально присутствующих в записях.

        Тот же populate, что при инициализации БД: значения сеются SELECT'ом
        из competitions, а не из строк импортируемого файла. Поэтому после
        переименования значения с обновлением записей (rename_catalog_value)
        следующий импорт старое значение НЕ возвращает: в записях его больше
        нет — источника строки справочника нет. Идемпотентно: уникальные
        индексы и INSERT OR IGNORE не дублируют строки и не «оживляют» скрытые.
        """
        self._populate_catalog_values()
        self._populate_catalog_hierarchy_pairs()
        self.connection.execute(
            'INSERT OR IGNORE INTO levels (name, sort_order) '
            "SELECT DISTINCT level, 0 FROM competitions WHERE TRIM(level) != ''"
        )

    def update_competition(self, record_id: str, competition: Competition):
        # Wave 1 P1: discipline/result правятся общим update. calendar_event_id
        # в SET НЕ входит: ссылка на событие — управляемое поле будущих
        # link/unlink (P2), общий update записи её сохраняет.
        with self._lock:
            self.connection.execute(
                '''
                UPDATE competitions
                SET
                    student_id = ?,
                    student_name = ?,
                    student_sex = ?,
                    institute = ?,
                    "group" = ?,
                    course = ?,
                    sport = ?,
                    date = ?,
                    date_to = ?,
                    level = ?,
                    name = ?,
                    position = ?,
                    extra_data = ?,
                    discipline = ?,
                    result = ?
                WHERE id = ?
                ''',
                (
                    competition.student_id,
                    competition.student_name,
                    competition.student_sex,
                    competition.institute,
                    competition.group,
                    competition.course,
                    competition.sport,
                    competition.date.isoformat(),
                    competition.date_to.isoformat() if competition.date_to else None,
                    competition.level,
                    competition.name,
                    competition.position,
                    json.dumps(competition.extra_data, ensure_ascii=False),
                    competition.discipline,
                    competition.result,
                    int(record_id),
                ),
            )
            self.connection.commit()

    def delete_competition(self, record_id: str):
        with self._lock:
            self.connection.execute('DELETE FROM competitions WHERE id = ?', (int(record_id),))
            self.connection.commit()

    def delete_competition_with_attachments(self, record_id: int) -> int:
        """Удалить запись вместе с её вложениями (удаление записи админом).

        Одна транзакция под одной блокировкой: сначала строки вложений,
        потом сама запись. Возвращает число удалённых строк вложений.
        """
        with self._lock:
            cursor = self.connection.execute('DELETE FROM attachments WHERE record_id = ?', (int(record_id),))
            deleted_attachments = cursor.rowcount
            self.connection.execute('DELETE FROM competitions WHERE id = ?', (int(record_id),))
            self.connection.commit()
            return deleted_attachments

    def clean_db(self):
        with self._lock:
            self.connection.execute('DELETE FROM competitions')
            self.connection.commit()

    def count_competitions(self, date_before: datetime | None = None) -> int:
        """Число записей; с date_before — только записи с датой соревнования ДО неё."""
        with self._lock:
            if date_before is None:
                row = self.connection.execute('SELECT COUNT(*) AS total FROM competitions').fetchone()
            else:
                row = self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions WHERE date < ?',
                    (date_before.isoformat(),),
                ).fetchone()
            return row['total']

    def get_competitions_before(self, date_before: datetime) -> list[Competition]:
        """Записи с датой соревнования ДО указанной (для архива перед очисткой).

        Wave 1 P1 + P3: служебные колонки (student_ref_id, discipline,
        result, calendar_event_id) нужны общему _row_to_competition;
        в сам xlsx-архив они НЕ попадают (competition_to_export_row
        вырезает) — ограничение pre-wipe архива см. docs/data-model.md.
        Course/Education Phase A: admission_year — так же внутренняя
        колонка, в архив не попадает (легаси-колонка course остаётся).
        """
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT
                    id, student_id, student_name, student_sex, institute, "group", course,
                    sport, date, date_to, level, name, position, created_at, extra_data,
                    review_status, owner_id, review_comment, student_ref_id,
                    discipline, result, calendar_event_id, admission_year
                FROM competitions
                WHERE date < ?
                ORDER BY created_at ASC
                ''',
                (date_before.isoformat(),),
            ).fetchall()
            return [self._row_to_competition(row) for row in rows]

    def delete_competitions_before(self, date_before: datetime) -> int:
        """Очистка по дате соревнования (docs/data-model-decisions.md). Возвращает число удалённых."""
        with self._lock:
            cursor = self.connection.execute('DELETE FROM competitions WHERE date < ?', (date_before.isoformat(),))
            self.connection.commit()
            return cursor.rowcount

    def delete_all_competitions(self) -> int:
        """Wipe all competition records (admin maintenance action). Returns deleted row count."""
        with self._lock:
            cursor = self.connection.execute('DELETE FROM competitions')
            self.connection.commit()
            return cursor.rowcount

    def get_sport_names(self) -> list[str]:
        """Unique sport names as stored in records, sorted alphabetically."""
        with self._lock:
            rows = self.connection.execute('SELECT DISTINCT sport FROM competitions ORDER BY sport ASC').fetchall()
            return [row['sport'] for row in rows]

    def get_competition_review(self, record_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, review_status, owner_id, student_id, student_ref_id FROM competitions WHERE id = ?',
                (record_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_competition_student_name(self, record_id: int) -> str:
        """ФИО записи по id (для аудита удаления); нет записи — пустая строка."""
        with self._lock:
            row = self.connection.execute(
                'SELECT student_name FROM competitions WHERE id = ?',
                (record_id,),
            ).fetchone()
            return row['student_name'] if row else ''

    def set_competition_review(
        self,
        record_id: int,
        review_status: str,
        review_comment: str = '',
    ) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE competitions
                SET review_status = ?, review_comment = ?
                WHERE id = ?
                """,
                (review_status, review_comment, record_id),
            )
            self.connection.commit()
