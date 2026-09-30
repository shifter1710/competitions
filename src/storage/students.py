"""Карточки студентов хранилища Competitions: создание, псевдонимы ФИО, сопоставление записей и аккаунтов с
карточками, слияние легаси-хешей.

Перенесено из src/storage/sqlite.py без изменения поведения (Architecture v2.1). Контракт примеси: не
создаёт соединение и блокировку (self.connection / self._lock принадлежат SQLiteAdapter), не импортирует
соседние доменные модули src.storage.* (кроме src.storage.helpers), междоменные вызовы — только через
self."""
import sqlite3
from datetime import datetime
from typing import Sequence

from src.storage.helpers import STUDENT_SEX_VALUES
from src.storage.helpers import STUDENT_SORT_COLUMNS


class StudentsMixin:
    # ---- Карточки студентов (Student Identity v1, Phase 1 — фундамент) ----
    #
    # Карточка — АКТУАЛЬНЫЕ данные студента (ФИО, пол, институт, группа,
    # курс) и его псевдонимы ФИО. Связи записей/аккаунтов с карточкой
    # (student_ref_id) заполняет только ручное сопоставление (Phase 2);
    # импорт/отчёты/merge работают по легаси-ключу sha256(ФИО) как раньше.
    # Физического удаления студентов нет — только active=0.

    def create_student(
        self,
        full_name: str,
        sex: str,
        institute: str,
        group_name: str,
        course: str,
    ) -> int:
        """Создать карточку студента; возвращает id новой строки."""
        with self._lock:
            now = datetime.utcnow().isoformat()
            cursor = self.connection.execute(
                'INSERT INTO students (full_name, sex, institute, group_name, course, active, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, 1, ?, ?)',
                (full_name, sex, institute, group_name, course, now, now),
            )
            self.connection.commit()
            return cursor.lastrowid

    def create_students(self, students: Sequence[tuple[str, str, str, str, str]]) -> list[int]:
        """Массовое создание карточек ОДНОЙ транзакцией (импорт, Phase 2.5).

        Паттерн import_competitions: блокировка держится всю вставку, строки
        вставляются построчно (нужен lastrowid каждой), сбой на любой строке
        откатывает всё — «либо все, либо ничего». Возвращает id новых строк
        в порядке входных строк.
        """
        with self._lock:
            try:
                now = datetime.utcnow().isoformat()
                ids: list[int] = []
                for full_name, sex, institute, group_name, course in students:
                    cursor = self.connection.execute(
                        'INSERT INTO students '
                        '(full_name, sex, institute, group_name, course, active, created_at, updated_at) '
                        'VALUES (?, ?, ?, ?, ?, 1, ?, ?)',
                        (full_name, sex, institute, group_name, course, now, now),
                    )
                    ids.append(cursor.lastrowid)
                self.connection.commit()
                return ids
            except BaseException:
                self.connection.rollback()
                raise

    def get_student_by_id(self, student_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, full_name, sex, institute, group_name, course, active, merged_into_id, '
                'created_at, updated_at FROM students WHERE id = ?',
                (student_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_students(self, search: str = '', sort: str = '', order: str = '') -> list[dict]:
        """Все карточки (активные И неактивные — контракт страницы «Студенты»).

        Дефолтный порядок: активные сверху, внутри — по алфавиту ФИО, id ASC
        (тёзки не переставляются между перезагрузками).

        search — подстрока ФИО ИЛИ любого псевдонима (strip + casefold).
        Сравнение — в Python: lower() в SQLite не берёт кириллицу (тот же
        подход, что search_student_candidates); пустой/пробельный запрос —
        без фильтра.

        sort/order — сортировка таблицы «Студенты»: sort ∈ ключей
        STUDENT_SORT_COLUMNS, order ∈ {'asc', 'desc'}. Пустые институт/группа
        всегда внизу В ОБЕИХ направлениях; вторичный порядок — full_name ASC,
        id ASC. Некорректные/пустые значения — дефолтный порядок.
        """
        target = (search or '').strip().casefold()
        # Сортировка применяется только полной валидной парой sort+order;
        # некорректное/отсутствующее любое из них — дефолтный порядок.
        column = STUDENT_SORT_COLUMNS.get(sort or '') if order in ('asc', 'desc') else ''
        direction = 'DESC' if order == 'desc' else 'ASC'
        with self._lock:
            if column == 'full_name':
                order_by = f'{column} {direction}, id ASC'
            elif column:
                # Пустые институт/группа — всегда внизу: фикс-терм «пусто»
                # ASC впереди, сама колонка меняет направление.
                order_by = f"({column} IS NULL OR {column} = '') ASC, " f'{column} {direction}, full_name ASC, id ASC'
            else:
                order_by = 'active DESC, full_name ASC, id ASC'
            rows = [
                dict(row)
                for row in self.connection.execute(
                    f'SELECT id, full_name, sex, institute, group_name, course, active, merged_into_id, '
                    f'created_at, updated_at FROM students ORDER BY {order_by}'
                ).fetchall()
            ]
            if not target:
                return rows
            aliases = self.connection.execute(
                'SELECT student_id, name FROM student_aliases ORDER BY student_id ASC, name ASC'
            ).fetchall()
        # Подстрока в любом псевдониме карточки — тоже совпадение (словарь
        # id → найдено; порядок строк aliases детерминирован, но для
        # подстрочного поиска порядок псевдонимов не важен).
        alias_match: set[int] = set()
        for row in aliases:
            if target in (row['name'] or '').strip().casefold():
                alias_match.add(row['student_id'])
        return [
            row for row in rows if target in (row['full_name'] or '').strip().casefold() or row['id'] in alias_match
        ]

    def update_student(
        self,
        student_id: int,
        full_name: str,
        sex: str,
        institute: str,
        group_name: str,
        course: str,
    ) -> bool:
        """Обновить актуальные данные карточки (updated_at меняется).
        Возвращает False, если студента с таким id нет."""
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE students SET full_name = ?, sex = ?, institute = ?, group_name = ?, course = ?, '
                'updated_at = ? WHERE id = ?',
                (full_name, sex, institute, group_name, course, datetime.utcnow().isoformat(), student_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def set_student_active(self, student_id: int, active: bool) -> bool:
        """Включить/выключить карточку (скрытие из поиска/импорта; полное
        удаление пустой карточки — delete_student)."""
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE students SET active = ? WHERE id = ?',
                (int(active), student_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def delete_student(self, student_id: int) -> tuple[str, dict[str, int]]:
        """Полное удаление карточки студента (hard delete, только admin).

        Удаляет карточку и её псевдонимы ФИО ОДНОЙ транзакцией — «либо всё,
        либо ничего». Блокируется при ЛЮБЫХ связях карточки (записи
        соревнований, аккаунты, слитые в неё карточки): в этом случае не
        меняется ни одна строка. Записи соревнований вместе с карточкой
        не удаляются — их отвязывают отдельно.

        Аккаунты считаются по student_ref_id без фильтра роли: link_user /
        relink_user пишут связь только athlete-аккаунтам, но счётчик без
        фильтра накрывает и любые легаси-строки с тем же ref.

        Возвращает (код, счётчики блокеров):
        'ok' — удалено (счётчики пустые);
        'not_found' — карточки с таким id нет;
        'blocked' — {'records': N, 'athlete_users': M, 'merged_children': K}.
        """
        with self._lock:
            try:
                row = self.connection.execute(
                    'SELECT id FROM students WHERE id = ?',
                    (int(student_id),),
                ).fetchone()
                if row is None:
                    return 'not_found', {}
                blockers = {
                    'records': self.connection.execute(
                        'SELECT COUNT(*) AS total FROM competitions WHERE student_ref_id = ?',
                        (int(student_id),),
                    ).fetchone()['total'],
                    'athlete_users': self.connection.execute(
                        'SELECT COUNT(*) AS total FROM users WHERE student_ref_id = ?',
                        (int(student_id),),
                    ).fetchone()['total'],
                    'merged_children': self.connection.execute(
                        'SELECT COUNT(*) AS total FROM students WHERE merged_into_id = ?',
                        (int(student_id),),
                    ).fetchone()['total'],
                }
                if any(blockers.values()):
                    return 'blocked', blockers
                self.connection.execute(
                    'DELETE FROM student_aliases WHERE student_id = ?',
                    (int(student_id),),
                )
                self.connection.execute('DELETE FROM students WHERE id = ?', (int(student_id),))
                self.connection.commit()
                return 'ok', {}
            except BaseException:
                self.connection.rollback()
                raise

    def add_student_alias(self, student_id: int, name: str) -> bool:
        """Добавить псевдоним ФИО; False — пустое имя, нет такого студента
        или дубль пары (student_id, name) по UNIQUE."""
        name = name.strip()
        if not name:
            return False
        with self._lock:
            if self.get_student_by_id(student_id) is None:
                return False
            try:
                self.connection.execute(
                    'INSERT INTO student_aliases (student_id, name, created_at) VALUES (?, ?, ?)',
                    (student_id, name, datetime.utcnow().isoformat()),
                )
                self.connection.commit()
            except sqlite3.IntegrityError:
                self.connection.rollback()
                return False
            return True

    def remove_student_alias(self, student_id: int, alias_id: int) -> bool:
        """Удалить псевдоним в пределах карточки: WHERE id = ? AND student_id = ?."""
        with self._lock:
            cursor = self.connection.execute(
                'DELETE FROM student_aliases WHERE id = ? AND student_id = ?',
                (alias_id, student_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def list_student_aliases(self, student_id: int) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, student_id, name, created_at FROM student_aliases WHERE student_id = ? ORDER BY id',
                (student_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    # ---- Сопоставление данных (Student Identity v1, Phase 2). ----
    #
    # Ручное заполнение стабильных связей student_ref_id существующих
    # записей/аккаунтов с карточками. Кандидаты — ТОЛЬКО предложения:
    # ничего не связывается автоматически. Привязка меняет ТОЛЬКО
    # student_ref_id: снимки данных в записях и легаси-ключ
    # student_id (sha256 ФИО) не трогаются; с P3 связанная запись
    # появляется в кабинете атлета (dual — вдобавок к легаси-хешу,
    # ref — вместо него; отчёты/выгрузки по-прежнему только легаси).

    def count_student_reconciliation(self) -> dict:
        """Сводные счётчики сопоставления: всего/связано/без связи."""
        with self._lock:
            records_total = self.connection.execute('SELECT COUNT(*) AS total FROM competitions').fetchone()['total']
            records_linked = self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE student_ref_id IS NOT NULL'
            ).fetchone()['total']
            athlete_users_total = self.connection.execute(
                "SELECT COUNT(*) AS total FROM users WHERE role = 'athlete'"
            ).fetchone()['total']
            athlete_users_linked = self.connection.execute(
                "SELECT COUNT(*) AS total FROM users WHERE role = 'athlete' AND student_ref_id IS NOT NULL"
            ).fetchone()['total']
        return {
            'records_total': records_total,
            'records_linked': records_linked,
            'records_unlinked': records_total - records_linked,
            'athlete_users_total': athlete_users_total,
            'athlete_users_linked': athlete_users_linked,
            'athlete_users_unlinked': athlete_users_total - athlete_users_linked,
        }

    def count_unlinked_competitions(self, search: str = '') -> int:
        """Число записей без карточки студента (с тем же поиском, что список)."""
        with self._lock:
            params: list[object] = []
            where = 'WHERE student_ref_id IS NULL'
            if search:
                where += ' AND student_name LIKE ?'
                params.append(f'%{search}%')
            row = self.connection.execute(
                f'SELECT COUNT(*) AS total FROM competitions {where}',
                params,
            ).fetchone()
            return row['total']

    def list_unlinked_competitions(self, search: str = '', limit: int = 50, offset: int = 0) -> list[dict]:
        """Записи без карточки студента: по ФИО, затем по дате, затем по id."""
        with self._lock:
            params: list[object] = []
            where = 'WHERE student_ref_id IS NULL'
            if search:
                where += ' AND student_name LIKE ?'
                params.append(f'%{search}%')
            rows = self.connection.execute(
                f'''
                SELECT
                    id, student_name, student_sex, institute, "group", course,
                    sport, date, date_to, name, level, position, review_status
                FROM competitions
                {where}
                ORDER BY student_name ASC, date ASC, id ASC
                LIMIT ? OFFSET ?
                ''',
                [*params, int(limit), int(offset)],
            ).fetchall()
            return [dict(row) for row in rows]

    def find_student_candidates(self, name: str) -> list[dict]:
        """Кандидаты-карточки для ФИО из записи/профиля — ТОЛЬКО предложения.

        Точное совпадение БЕЗ учёта регистра (strip + casefold) по full_name
        или псевдониму; только активные карточки. Сравнение — в Python:
        lower() в SQLite не знает кириллицы (как и справочники — см.
        find_catalog_row / _known_athletes). Одна строка на карточку:
        совпадение по full_name приоритетнее совпадения по псевдониму.
        Порядок — по алфавиту ФИО.
        """
        target = (name or '').strip().casefold()
        if not target:
            return []
        with self._lock:
            students = self.connection.execute(
                'SELECT id, full_name, sex, institute, group_name, course '
                'FROM students WHERE active = 1 ORDER BY full_name ASC, id ASC'
            ).fetchall()
            aliases = self.connection.execute(
                '''
                SELECT a.student_id, a.name
                FROM student_aliases a
                JOIN students s ON s.id = a.student_id
                WHERE s.active = 1
                ORDER BY a.student_id ASC, a.name ASC
                '''
            ).fetchall()
        # Первый подходящий псевдоним карточки (порядок a.name ASC —
        # детерминированный выбор при регистровых вариантах одного псевдонима)
        matching_alias: dict[int, str] = {}
        for row in aliases:
            if row['student_id'] not in matching_alias and (row['name'] or '').strip().casefold() == target:
                matching_alias[row['student_id']] = row['name']
        full_rows = []
        alias_rows = []
        for row in students:
            if (row['full_name'] or '').strip().casefold() == target:
                full_rows.append(row)
            elif row['id'] in matching_alias:
                alias_rows.append(row)
        candidates: list[dict] = [
            {
                'student_id': row['id'],
                'full_name': row['full_name'],
                'sex': row['sex'],
                'institute': row['institute'],
                'group_name': row['group_name'],
                'course': row['course'],
                'match_type': 'full_name',
                'alias_name': None,
            }
            for row in full_rows
        ]
        candidates.extend(
            {
                'student_id': row['id'],
                'full_name': row['full_name'],
                'sex': row['sex'],
                'institute': row['institute'],
                'group_name': row['group_name'],
                'course': row['course'],
                'match_type': 'alias',
                'alias_name': matching_alias[row['id']],
            }
            for row in alias_rows
        )
        return candidates

    def search_student_candidates(self, query: str, limit: int = 8) -> list[dict]:
        """Подстрочный поиск активных карточек («Найти студента» в предпросмотре
        импорта участников события) — ТОЛЬКО предложения.

        Совпадение — ПОДСТРОКА query (strip + casefold) в full_name ИЛИ в любом
        псевдониме карточки; одна строка на карточку (при обоих совпадениях —
        match_type='full_name'). Порядок: совпадения по ФИО, затем по
        псевдониму, внутри — по (full_name, id). Формат ответа — как у
        find_student_candidates. Сравнение — в Python (lower() в SQLite не
        берёт кириллицу); без fuzzy/ё-е/транслитерации. Пустой/пробельный
        запрос → []. Не влияет на search_student_suggestions и
        /api/athletes/search (точные кандидаты — find_student_candidates).
        """
        target = (query or '').strip().casefold()
        if not target:
            return []
        with self._lock:
            students = self.connection.execute(
                'SELECT id, full_name, sex, institute, group_name, course '
                'FROM students WHERE active = 1 ORDER BY full_name ASC, id ASC'
            ).fetchall()
            aliases = self.connection.execute(
                '''
                SELECT a.student_id, a.name
                FROM student_aliases a
                JOIN students s ON s.id = a.student_id
                WHERE s.active = 1
                ORDER BY a.student_id ASC, a.name ASC
                '''
            ).fetchall()
        # Первый подходящий псевдоним карточки (порядок a.name ASC —
        # детерминированный выбор, как в find_student_candidates).
        matching_alias: dict[int, str] = {}
        for row in aliases:
            if row['student_id'] not in matching_alias and target in (row['name'] or '').strip().casefold():
                matching_alias[row['student_id']] = (row['name'] or '').strip()
        full_rows = []
        alias_rows = []
        for row in students:
            if target in (row['full_name'] or '').strip().casefold():
                full_rows.append(row)
            elif row['id'] in matching_alias:
                alias_rows.append(row)
        candidates: list[dict] = [
            {
                'student_id': row['id'],
                'full_name': row['full_name'],
                'sex': row['sex'],
                'institute': row['institute'],
                'group_name': row['group_name'],
                'course': row['course'],
                'match_type': 'full_name',
                'alias_name': None,
            }
            for row in full_rows
        ]
        candidates.extend(
            {
                'student_id': row['id'],
                'full_name': row['full_name'],
                'sex': row['sex'],
                'institute': row['institute'],
                'group_name': row['group_name'],
                'course': row['course'],
                'match_type': 'alias',
                'alias_name': matching_alias[row['id']],
            }
            for row in alias_rows
        )
        return candidates[: int(limit)]

    def link_competitions(self, record_ids: Sequence[int], student_id: int) -> tuple[int, str | None]:
        """Атомарно привязать записи к карточке: либо все, либо ничего.

        Проверки и UPDATE — под одной блокировкой, без изменений при любой
        ошибке. Возвращает (число привязанных, код ошибки или None):
        student_not_found / student_inactive / records_not_found /
        already_linked.
        """
        ids = sorted({int(record_id) for record_id in record_ids})
        if not ids:
            return 0, 'records_not_found'
        with self._lock:
            student = self.connection.execute(
                'SELECT active FROM students WHERE id = ?',
                (int(student_id),),
            ).fetchone()
            if student is None:
                return 0, 'student_not_found'
            if not student['active']:
                return 0, 'student_inactive'
            placeholders = ', '.join('?' for _ in ids)
            found = self.connection.execute(
                f'SELECT COUNT(*) AS total FROM competitions WHERE id IN ({placeholders})',
                ids,
            ).fetchone()['total']
            if found != len(ids):
                return 0, 'records_not_found'
            unlinked = self.connection.execute(
                f'SELECT COUNT(*) AS total FROM competitions '
                f'WHERE id IN ({placeholders}) AND student_ref_id IS NULL',
                ids,
            ).fetchone()['total']
            if unlinked != len(ids):
                return 0, 'already_linked'
            cursor = self.connection.execute(
                f'UPDATE competitions SET student_ref_id = ? '
                f'WHERE id IN ({placeholders}) AND student_ref_id IS NULL',
                [int(student_id), *ids],
            )
            self.connection.commit()
            return cursor.rowcount, None

    def create_student_and_link_participation(
        self,
        *,
        event_id: int,
        record_id: int,
        full_name: str,
        sex: str,
        institute: str,
        group_name: str,
        course: str,
    ) -> tuple[int | None, str | None]:
        """Создать карточку студента и сразу привязать к ней cardless-участие
        события («Создать и связать» со страницы участника) ОДНОЙ транзакцией.

        Паттерн create_calendar_event_and_link: проверки и оба шага — под
        self._lock в одной транзакции (try/commit/except: rollback; raise);
        сбой на любом шаге откатывает всё, карточка-сирота невозможна — в
        отличие от последовательных create_student + link_competitions, где
        гонка already_linked оставляла пустую карточку. Возвращает
        (student_id, None) при успехе или (None, код ошибки) при отказе
        (0 изменений в любом случае): record_not_found / event_mismatch /
        already_linked / invalid_full_name / invalid_sex.

        Проверки внутри транзакции:
        - участие существует, принадлежит событию (calendar_event_id =
          event_id) и ещё без карточки — ДО вставки, чтобы не создавать
          студента ради отказа;
        - поля карточки валидны (ФИО непустое после strip; пол '', «М»,
          «Ж» — ровно parse_student_form роута): ревалидация закрывает
          расхождение «форма проверена в роуте → create здесь»;
        - гонку «SELECT → INSERT → UPDATE» закрывает guarded UPDATE:
          rowcount = 0 → already_linked, откат и карточки, и связи.

        P4 matched-duplicate guard (event_participation_matched_duplicate_
        guard) здесь НЕ применяется, и это осознанно: инвариант (событие,
        карточка, нормализованная дисциплина) сравнивает участия
        СУЩЕСТВУЮЩИХ карточек, а у создаваемой карточки id ещё нет ни в одном
        участии — её будущий student_ref_id не пересекается с чужими
        identity, и после привязки у новой карточки ровно одно участие (сама
        привязываемая запись). Дубль против существующих карточек закрывают
        link-existing (guard в роуте POST .../link) и импорт; сценарий
        «после create связь не нужна» (прецедент admin_reconcile_create_
        record) неприменим — связь здесь и есть цель операции, отказа без
        карточки не существует.
        """
        with self._lock:
            try:
                record = self.connection.execute(
                    'SELECT id, student_ref_id, calendar_event_id FROM competitions WHERE id = ?',
                    (int(record_id),),
                ).fetchone()
                if record is None:
                    return None, 'record_not_found'
                if record['calendar_event_id'] != int(event_id):
                    return None, 'event_mismatch'
                if record['student_ref_id'] is not None:
                    return None, 'already_linked'
                values = {
                    'full_name': (full_name or '').strip(),
                    'sex': (sex or '').strip(),
                    'institute': (institute or '').strip(),
                    'group_name': (group_name or '').strip(),
                    'course': (course or '').strip(),
                }
                if not values['full_name']:
                    return None, 'invalid_full_name'
                if values['sex'] not in STUDENT_SEX_VALUES:
                    return None, 'invalid_sex'
                now = datetime.utcnow().isoformat()
                cursor = self.connection.execute(
                    'INSERT INTO students '
                    '(full_name, sex, institute, group_name, course, active, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?, ?, 1, ?, ?)',
                    (
                        values['full_name'],
                        values['sex'],
                        values['institute'],
                        values['group_name'],
                        values['course'],
                        now,
                        now,
                    ),
                )
                student_id = cursor.lastrowid
                linked = self.connection.execute(
                    'UPDATE competitions SET student_ref_id = ? ' 'WHERE id = ? AND student_ref_id IS NULL',
                    (student_id, int(record_id)),
                )
                if linked.rowcount == 0:
                    self.connection.rollback()
                    return None, 'already_linked'
                self.connection.commit()
                return student_id, None
            except BaseException:
                self.connection.rollback()
                raise

    def unlink_competition(self, record_id: int) -> int | None:
        """Снять связь записи с карточкой; прежний student_ref_id (или None,
        если записи нет либо связи и так не было)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT student_ref_id FROM competitions WHERE id = ?',
                (int(record_id),),
            ).fetchone()
            if row is None or row['student_ref_id'] is None:
                return None
            self.connection.execute(
                'UPDATE competitions SET student_ref_id = NULL WHERE id = ?',
                (int(record_id),),
            )
            self.connection.commit()
            return row['student_ref_id']

    def relink_competition(self, record_id: int, student_id: int) -> tuple[int | None, str | None]:
        """Перепривязать запись на другую карточку (можно и с NULL — тогда
        это привязка). Возвращает (прежний ref или None, ошибка или None)."""
        with self._lock:
            student = self.connection.execute(
                'SELECT active FROM students WHERE id = ?',
                (int(student_id),),
            ).fetchone()
            if student is None:
                return None, 'student_not_found'
            if not student['active']:
                return None, 'student_inactive'
            row = self.connection.execute(
                'SELECT student_ref_id FROM competitions WHERE id = ?',
                (int(record_id),),
            ).fetchone()
            if row is None:
                return None, 'records_not_found'
            self.connection.execute(
                'UPDATE competitions SET student_ref_id = ? WHERE id = ?',
                (int(student_id), int(record_id)),
            )
            self.connection.commit()
            return row['student_ref_id'], None

    def linked_records_count(self, student_id: int) -> int:
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE student_ref_id = ?',
                (int(student_id),),
            ).fetchone()
            return row['total']

    def list_linked_records(self, student_id: int, limit: int = 50) -> list[dict]:
        """Записи, привязанные к карточке (свежие сверху). student_name —
        снимок из самой записи, а не актуальное ФИО карточки."""
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT id, student_name, sport, date, date_to, name
                FROM competitions
                WHERE student_ref_id = ?
                ORDER BY date DESC, id DESC
                LIMIT ?
                ''',
                (int(student_id), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_competition_student_ref(self, record_id: int) -> tuple[bool, int | None]:
        """(запись существует, её student_ref_id) — для страниц сопоставления."""
        with self._lock:
            row = self.connection.execute(
                'SELECT student_ref_id FROM competitions WHERE id = ?',
                (int(record_id),),
            ).fetchone()
            if row is None:
                return False, None
            return True, row['student_ref_id']

    def count_records_by_student_hash(self, student_id_hash: str) -> int:
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE student_id = ?',
                (student_id_hash,),
            ).fetchone()
            return row['total']

    def get_student_names(self) -> list[str]:
        """Unique student names as stored in records, sorted alphabetically."""
        with self._lock:
            rows = self.connection.execute(
                'SELECT DISTINCT student_name FROM competitions ORDER BY student_name ASC'
            ).fetchall()
            return [row['student_name'] for row in rows]

    def merge_students(self, old_hash: str, new_hash: str, new_name: str | None = None) -> int:
        with self._lock:
            if new_name:
                cursor = self.connection.execute(
                    '''
                    UPDATE competitions
                    SET student_id = ?, student_name = ?
                    WHERE student_id = ?
                    ''',
                    (new_hash, new_name, old_hash),
                )
            else:
                cursor = self.connection.execute(
                    'UPDATE competitions SET student_id = ? WHERE student_id = ?',
                    (new_hash, old_hash),
                )
            self.connection.commit()
            return cursor.rowcount

    def search_student_suggestions(self, query: str, limit: int = 8) -> list[dict]:
        """Активные карточки студентов по подстроке ФИО (автодополнение).

        В отличие от search_athletes источник — таблица students, поэтому
        находятся и студенты БЕЗ истории участий (импорт в раздел Students).
        Сравнение — подстрока на casefold в Python (lower() в SQLite не
        берёт кириллицу, тот же подход, что _known_athletes). Порядок —
        по алфавиту ФИО, затем по id; неактивные карточки не возвращаются.
        """
        query = (query or '').strip().casefold()
        if not query:
            return []
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, full_name, sex, institute, group_name, course '
                'FROM students WHERE active = 1 ORDER BY full_name ASC, id ASC'
            ).fetchall()
        suggestions: list[dict] = []
        for row in rows:
            name = (row['full_name'] or '').strip()
            if not name or query not in name.casefold():
                continue
            suggestions.append(
                {
                    'student_id': row['id'],
                    'name': name,
                    'sex': (row['sex'] or '').strip(),
                    'institute': (row['institute'] or '').strip(),
                    'group': (row['group_name'] or '').strip(),
                    'course': '' if row['course'] is None else str(row['course']).strip(),
                }
            )
            if len(suggestions) >= limit:
                break
        return suggestions
