"""Справочники хранилища Competitions: настройки базовых полей, кастомные поля, уровни и значения справочников с
иерархией институт→группа.

Перенесено из src/storage/sqlite.py без изменения поведения (Architecture v2.1). Контракт примеси: не
создаёт соединение и блокировку (self.connection / self._lock принадлежат SQLiteAdapter), не импортирует
соседние доменные модули src.storage.* (кроме src.storage.helpers), междоменные вызовы — только через
self."""
from datetime import datetime

from src.models.custom_field import CustomField
from src.storage.helpers import CATALOG_CATEGORIES
from src.storage.helpers import RENAME_CATEGORIES


class CatalogsMixin:
    def get_field_settings(self) -> dict[str, dict]:
        """Настройки базовых полей: key -> {'value_type', 'required'}."""
        with self._lock:
            rows = self.connection.execute('SELECT key, value_type, required FROM field_settings').fetchall()
            return {row['key']: {'value_type': row['value_type'], 'required': bool(row['required'])} for row in rows}

    def update_field_settings(self, entries: dict[str, tuple[str, bool]]) -> None:
        """Сохранить настройки базовых полей одной транзакцией.

        entries: key -> (value_type, required). Строки upsert'ятся: у поля
        без строки в таблице дефолт заменяется явной настройкой.
        """
        with self._lock:
            for key, (value_type, required) in entries.items():
                self.connection.execute(
                    '''
                    INSERT INTO field_settings (key, value_type, required)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_type = excluded.value_type, required = excluded.required
                    ''',
                    (key, value_type, int(required)),
                )
            self.connection.commit()

    def get_custom_fields(self, include_inactive: bool = False) -> list[CustomField]:
        with self._lock:
            if include_inactive:
                rows = self.connection.execute(
                    '''
                    SELECT *
                    FROM custom_fields
                    ORDER BY active DESC, sort_order ASC, label ASC
                    '''
                ).fetchall()
            else:
                rows = self.connection.execute(
                    '''
                    SELECT *
                    FROM custom_fields
                    WHERE active = 1
                    ORDER BY sort_order ASC, label ASC
                    '''
                ).fetchall()
            return [self._row_to_custom_field(row) for row in rows]

    def create_custom_field(
        self,
        key: str,
        label: str,
        field_type: str,
        required: bool,
        show_in_table: bool,
        show_in_export: bool,
        show_in_template: bool,
        sort_order: int,
        link_target: str | None = None,
    ) -> None:
        with self._lock:
            self.connection.execute(
                '''
                INSERT INTO custom_fields (
                    key,
                    label,
                    field_type,
                    required,
                    show_in_table,
                    show_in_export,
                    show_in_template,
                    sort_order,
                    link_target
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    key,
                    label,
                    field_type,
                    int(required),
                    int(show_in_table),
                    int(show_in_export),
                    int(show_in_template),
                    sort_order,
                    link_target,
                ),
            )
            self.connection.commit()

    def update_custom_field(
        self,
        field_id: int,
        label: str,
        field_type: str,
        required: bool,
        show_in_table: bool,
        show_in_export: bool,
        show_in_template: bool,
        sort_order: int,
        active: bool,
        link_target: str | None = None,
    ) -> None:
        with self._lock:
            self.connection.execute(
                '''
                UPDATE custom_fields
                SET
                    label = ?,
                    field_type = ?,
                    required = ?,
                    show_in_table = ?,
                    show_in_export = ?,
                    show_in_template = ?,
                    sort_order = ?,
                    active = ?,
                    link_target = ?
                WHERE id = ?
                ''',
                (
                    label,
                    field_type,
                    int(required),
                    int(show_in_table),
                    int(show_in_export),
                    int(show_in_template),
                    sort_order,
                    int(active),
                    link_target,
                    field_id,
                ),
            )
            self.connection.commit()

    def disable_custom_field(self, field_id: int) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE custom_fields SET active = 0 WHERE id = ?',
                (field_id,),
            )
            self.connection.commit()

    def get_level_names(self, include_inactive: bool = False) -> list[str]:
        with self._lock:
            if include_inactive:
                rows = self.connection.execute('SELECT name FROM levels ORDER BY sort_order ASC, name ASC').fetchall()
            else:
                rows = self.connection.execute(
                    'SELECT name FROM levels WHERE active = 1 ORDER BY sort_order ASC, name ASC'
                ).fetchall()
            return [row['name'] for row in rows]

    def list_levels(self) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, name, sort_order, active FROM levels ORDER BY active DESC, sort_order ASC, name ASC'
            ).fetchall()
            return [dict(row) for row in rows]

    def create_level(self, name: str, sort_order: int = 0) -> None:
        with self._lock:
            self.connection.execute(
                'INSERT INTO levels (name, sort_order) VALUES (?, ?)',
                (name, sort_order),
            )
            self.connection.commit()

    def hard_delete_level(self, level_id: int) -> None:
        """Физическое удаление уровня (№20): допустимо только для уровня
        без записей — проверка на стороне роута."""
        with self._lock:
            self.connection.execute('DELETE FROM levels WHERE id = ?', (level_id,))
            self.connection.commit()

    def disable_level(self, level_id: int) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE levels SET active = 0 WHERE id = ?',
                (level_id,),
            )
            self.connection.commit()

    def list_catalog(self, category: str) -> list[str]:
        """Активные значения справочника (подсказки для форм), по алфавиту."""
        with self._lock:
            rows = self.connection.execute(
                'SELECT value FROM catalog_values WHERE category = ? AND active = 1 ORDER BY value ASC',
                (category,),
            ).fetchall()
            return [row['value'] for row in rows]

    def list_catalog_all(self, category: str) -> list[dict]:
        """Все значения справочника (для админки): активные сверху, по алфавиту."""
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, category, value, parent_id, active FROM catalog_values '
                'WHERE category = ? ORDER BY active DESC, value ASC',
                (category,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_catalog_tree(self) -> list[dict]:
        """Институты с их группами для страницы «Справочники».

        Каждый институт — запись list_catalog_all('institute') с counters
        records_count (все записи с этим институтом) и списком groups
        (записи категории group с parent_id на институт, у каждой свой
        records_count — по паре институт+группа).
        """
        with self._lock:
            institutes = self.list_catalog_all('institute')
            group_rows = self.connection.execute(
                'SELECT id, category, value, parent_id, active FROM catalog_values '
                "WHERE category = 'group' ORDER BY active DESC, value ASC"
            ).fetchall()
        groups_by_parent: dict[int, list[dict]] = {}
        for row in group_rows:
            groups_by_parent.setdefault(row['parent_id'], []).append(dict(row))
        tree = []
        for institute in institutes:
            groups = groups_by_parent.get(institute['id'], [])
            for group in groups:
                group['records_count'] = self.count_records_using(
                    'group', group['value'], parent_value=institute['value']
                )
            tree.append(
                {
                    **institute,
                    'records_count': self.count_records_using('institute', institute['value']),
                    'groups': groups,
                }
            )
        return tree

    def get_group_options_by_institute(self) -> dict[str, list[str]]:
        """Активные группы по институтам для подсказок форм: институт → группы.

        Скрытые группы (active = 0) в подсказки не попадают; институт
        присутствует в карте, даже если скрыт сам, — его группы остаются
        подсказкой при точно введённом названии института.
        """
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT institutes.value AS institute, groups.value AS "group"
                FROM catalog_values AS groups
                JOIN catalog_values AS institutes ON institutes.id = groups.parent_id
                WHERE groups.category = 'group' AND groups.active = 1
                ORDER BY institutes.value ASC, groups.value ASC
                '''
            ).fetchall()
        options: dict[str, list[str]] = {}
        for row in rows:
            options.setdefault(row['institute'], []).append(row['group'])
        return options

    def find_catalog_row(
        self,
        category: str,
        value: str,
        *,
        parent_id: int | None = None,
    ) -> dict | None:
        """Запись справочника по значению без учёта регистра (№1.1).

        Для группы (parent_id задан) поиск среди детей этого родителя:
        уникальность группы — по паре (институт, группа). Возвращает первую
        подходящую строку или None.
        """
        value = value.strip()
        if not value:
            return None
        # Сравнение регистра — в Python: lower() в SQLite работает только
        # с ASCII и не приводит кириллицу (ИСИ/иси).
        with self._lock:
            if parent_id is None:
                rows = self.connection.execute(
                    'SELECT id, category, value, parent_id, active FROM catalog_values '
                    'WHERE category = ? AND parent_id IS NULL ORDER BY id ASC',
                    (category,),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    'SELECT id, category, value, parent_id, active FROM catalog_values '
                    'WHERE category = ? AND parent_id = ? ORDER BY id ASC',
                    (category, parent_id),
                ).fetchall()
        for row in rows:
            if row['value'].lower() == value.lower():
                return dict(row)
        return None

    def find_catalog_canonical(
        self,
        category: str,
        value: str,
        *,
        parent_id: int | None = None,
    ) -> str | None:
        """Каноническое написание значения справочника без учёта регистра (№1.1)."""
        row = self.find_catalog_row(category, value, parent_id=parent_id)
        return row['value'] if row else None

    def find_unique_group_institute(self, group: str) -> str | None:
        """Институт для группы №19а (docs/feedback-live.md).

        Если имя группы (без учёта регистра) встречается ровно у одного
        института в иерархии справочников — возвращается каноническое имя
        института. Неизвестная группа или одно имя в разных институтах —
        None (институт остаётся пустым, свободный ввод не ломаем).
        Скрытые группы (active = 0) не участвуют.
        """
        group = group.strip()
        if not group:
            return None
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT institutes.value AS institute, groups.value AS "group"
                FROM catalog_values AS groups
                JOIN catalog_values AS institutes ON institutes.id = groups.parent_id
                WHERE groups.category = 'group' AND groups.active = 1
                ORDER BY institutes.value ASC, groups.value ASC
                '''
            ).fetchall()
        matches = {row['institute'] for row in rows if row['group'].lower() == group.lower()}
        if len(matches) == 1:
            return matches.pop()
        return None

    def find_level_canonical(self, name: str) -> str | None:
        """Каноническое написание уровня без учёта регистра (№1.1).

        Уровень живёт в levels, а не в catalog_values — поэтому отдельный
        поиск. Каноническое написание имеет приоритет над нижнекейсовой
        нормализацией записи (см. apply_catalog_canonical_values).
        """
        name = name.strip()
        if not name:
            return None
        # Сравнение регистра — в Python (lower() в SQLite не берёт кириллицу).
        with self._lock:
            rows = self.connection.execute(
                'SELECT name FROM levels ORDER BY id ASC',
            ).fetchall()
        for row in rows:
            if row['name'].lower() == name.lower():
                return row['name']
        return None

    def add_catalog_value(self, category: str, value: str, parent_id: int | None = None) -> None:
        """Добавить значение в справочник; дубликат игнорируется без ошибки.

        Для групп parent_id указывает на запись института; уникальность пары
        (parent, value) держит дочерний частичный индекс.
        """
        value = value.strip()
        if not value:
            return
        with self._lock:
            self.connection.execute(
                'INSERT OR IGNORE INTO catalog_values (category, value, parent_id, active, created_at) '
                'VALUES (?, ?, ?, 1, ?)',
                (category, value, parent_id, datetime.utcnow().isoformat()),
            )
            self.connection.commit()

    def ensure_catalog_pair(self, institute: str, group: str) -> None:
        """Пара институт→группа из записи/импорта попадает в иерархию справочника.

        Институт обязан существовать (создаётся при отсутствии), группа кладётся
        с parent_id на него; повторная пара игнорируется. Плоские (без
        родителя) значения не трогаются — см. docs/data-model-decisions.md.
        """
        institute = institute.strip()
        group = group.strip()
        if not institute or not group:
            return
        with self._lock:
            self.add_catalog_value('institute', institute)
            parent = self.connection.execute(
                'SELECT id FROM catalog_values ' "WHERE category = 'institute' AND value = ? AND parent_id IS NULL",
                (institute,),
            ).fetchone()
            if parent is None:
                return
            self.connection.execute(
                'INSERT OR IGNORE INTO catalog_values (category, value, parent_id, active, created_at) '
                "VALUES ('group', ?, ?, 1, ?)",
                (group, parent['id'], datetime.utcnow().isoformat()),
            )
            self.connection.commit()

    def get_catalog_value(self, value_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, category, value, parent_id, active FROM catalog_values WHERE id = ?',
                (value_id,),
            ).fetchone()
            return dict(row) if row else None

    def hide_catalog_value(self, value_id: int) -> None:
        with self._lock:
            self.connection.execute('UPDATE catalog_values SET active = 0 WHERE id = ?', (value_id,))
            self.connection.commit()

    def unhide_catalog_value(self, value_id: int) -> None:
        with self._lock:
            self.connection.execute('UPDATE catalog_values SET active = 1 WHERE id = ?', (value_id,))
            self.connection.commit()

    def delete_catalog_value(self, value_id: int) -> None:
        with self._lock:
            self.connection.execute('DELETE FROM catalog_values WHERE id = ?', (value_id,))
            self.connection.commit()

    def rename_catalog_value(
        self,
        category: str,
        old_value: str,
        new_value: str,
        *,
        parent_value: str | None = None,
        update_records: bool = True,
    ) -> int:
        """Переименовать значение справочника одной транзакцией.

        Решение 2026-09-13 (docs/data-model-decisions.md «Переименование
        значений справочников»): записи хранят значения текстом, поэтому
        справочник и записи меняются вместе — иначе справочник и записи
        расходятся, а следующий импорт возвращает старое значение (значения
        сеются из записей). update_records=False — переименовать только
        справочник (осознанное расхождение, например для архивных значений).

        Уровень меняется в levels, остальные категории — в catalog_values;
        группа — по паре институт+группа (parent_value — её институт),
        одноимённые группы других институтов не затрагиваются. Возвращает
        число обновлённых записей (0 при update_records=False). Конфликт
        нового имени с существующим значением категории (для группы — в том
        же институте) или отсутствие старого — ValueError без изменений.
        """
        if category not in RENAME_CATEGORIES:
            raise ValueError(f'Неизвестная категория справочника: {category}')
        old_value = old_value.strip()
        new_value = new_value.strip()
        parent_value = (parent_value or '').strip()
        if not old_value or not new_value:
            raise ValueError('Значение не может быть пустым')
        if old_value == new_value:
            raise ValueError('Новое имя совпадает со старым')

        with self._lock:
            try:
                if category == 'level':
                    self._rename_level_row(old_value, new_value)
                else:
                    self._rename_catalog_row(category, old_value, new_value, parent_value)
                updated = 0
                if update_records:
                    if category == 'group':
                        cursor = self.connection.execute(
                            'UPDATE competitions SET "group" = ? WHERE "group" = ? AND institute = ?',
                            (new_value, old_value, parent_value),
                        )
                    else:
                        cursor = self.connection.execute(
                            f'UPDATE competitions SET {category} = ? WHERE {category} = ?',
                            (new_value, old_value),
                        )
                    updated = cursor.rowcount
                self.connection.commit()
                return updated
            except BaseException:
                self.connection.rollback()
                raise

    def _rename_level_row(self, old_value: str, new_value: str) -> None:
        """Строка таблицы levels: конфликт по UNIQUE(name), затем UPDATE."""
        conflict = self.connection.execute('SELECT 1 FROM levels WHERE name = ?', (new_value,)).fetchone()
        if conflict is not None:
            raise ValueError(f'Уровень «{new_value}» уже существует')
        cursor = self.connection.execute('UPDATE levels SET name = ? WHERE name = ?', (new_value, old_value))
        if not cursor.rowcount:
            raise ValueError(f'Уровень «{old_value}» не найден')

    def _rename_catalog_row(self, category: str, old_value: str, new_value: str, parent_value: str) -> None:
        """Строка catalog_values: плоские значения по (category, value),
        группы — по паре (parent_id института, value)."""
        if category == 'group':
            if not parent_value:
                raise ValueError('Для переименования группы нужен её институт')
            parent = self.connection.execute(
                'SELECT id FROM catalog_values ' "WHERE category = 'institute' AND parent_id IS NULL AND value = ?",
                (parent_value,),
            ).fetchone()
            if parent is None:
                raise ValueError(f'Институт «{parent_value}» не найден')
            conflict = self.connection.execute(
                "SELECT 1 FROM catalog_values WHERE category = 'group' AND value = ? AND parent_id = ?",
                (new_value, parent['id']),
            ).fetchone()
            if conflict is not None:
                raise ValueError(f'Группа «{new_value}» уже есть в институте «{parent_value}»')
            cursor = self.connection.execute(
                'UPDATE catalog_values SET value = ? ' "WHERE category = 'group' AND value = ? AND parent_id = ?",
                (new_value, old_value, parent['id']),
            )
        else:
            conflict = self.connection.execute(
                'SELECT 1 FROM catalog_values WHERE category = ? AND value = ? AND parent_id IS NULL',
                (category, new_value),
            ).fetchone()
            if conflict is not None:
                raise ValueError(f'Значение «{new_value}» уже есть в справочнике')
            cursor = self.connection.execute(
                'UPDATE catalog_values SET value = ? WHERE category = ? AND value = ? AND parent_id IS NULL',
                (new_value, category, old_value),
            )
        if not cursor.rowcount:
            raise ValueError(f'Значение «{old_value}» не найдено')

    def count_records_using(self, category: str, value: str, parent_value: str | None = None) -> int:
        """Сколько записей соревнований содержат это значение справочника.

        Для группы parent_value — её институт: считаются записи с парой
        институт+группа (одна и та же группа в разных институтах — разные
        записи справочника). Без parent_value считается плоское вхождение.
        """
        # 'level' не входит в CATALOG_CATEGORIES, но таблица уровней своя —
        # для счётчика на странице справочников колонка записей та же.
        if category not in (*CATALOG_CATEGORIES, 'level', 'group'):
            return 0
        with self._lock:
            if category == 'group' and parent_value:
                row = self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions ' 'WHERE "group" = ? AND institute = ?',
                    (value, parent_value),
                ).fetchone()
            else:
                # "group" — ключевое слово SQL, колонка только в кавычках
                column = '"group"' if category == 'group' else category
                row = self.connection.execute(
                    f'SELECT COUNT(*) AS total FROM competitions WHERE {column} = ?',
                    (value,),
                ).fetchone()
            return row['total']

    def count_child_groups(self, institute_row_id: int) -> int:
        """Сколько групп справочника прикреплено к институту (включая скрытые)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM catalog_values ' "WHERE category = 'group' AND parent_id = ?",
                (institute_row_id,),
            ).fetchone()
            return row['total']

    def count_students_using_group_pair(self, group: str, institute: str) -> int:
        """Сколько карточек студентов содержат пару институт+группа
        (счётчик для подтверждения переноса группы между институтами)."""
        group = group.strip()
        institute = institute.strip()
        if not group or not institute:
            return 0
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM students WHERE institute = ? AND group_name = ?',
                (institute, group),
            ).fetchone()
            return row['total']

    def move_catalog_group(self, group_id: int, target_institute_id: int) -> dict:
        """Перенести группу в другой институт одной транзакцией.

        Решение 2026-09-22 (docs/data-model-decisions.md «Перенос группы в
        другой институт»): в отличие от переименования, карточки студентов
        обновляются ВМЕСТЕ с записями — перенос чинит неверную привязку
        (группа ошибочно числилась за старым институтом), а актуальные
        данные карточки обязаны совпадать со справочником.

        Отбор строк — по точной паре строк (институт+группа), как у
        переименования/count_records_using: одноимённые группы других
        институтов и строки без института не затрагиваются. Возвращает
        {'students_updated', 'competitions_updated'}. Ошибки (группа/
        институт не найдены, тот же институт, конфликт имени в целевом
        институте) — ValueError без изменений.
        """
        with self._lock:
            try:
                group_row = self.connection.execute(
                    "SELECT id, value, parent_id FROM catalog_values WHERE id = ? AND category = 'group'",
                    (group_id,),
                ).fetchone()
                if group_row is None or group_row['parent_id'] is None:
                    raise ValueError('Группа или институт не найдены')
                target = self.connection.execute(
                    "SELECT id, value FROM catalog_values WHERE id = ? AND category = 'institute'",
                    (target_institute_id,),
                ).fetchone()
                if target is None:
                    raise ValueError('Группа или институт не найдены')
                if target['id'] == group_row['parent_id']:
                    raise ValueError('Группа уже относится к выбранному институту')
                conflict = self.find_catalog_row('group', group_row['value'], parent_id=target['id'])
                if conflict is not None:
                    raise ValueError('В целевом институте уже есть такая группа')
                old_parent = self.connection.execute(
                    'SELECT value FROM catalog_values WHERE id = ?',
                    (group_row['parent_id'],),
                ).fetchone()
                if old_parent is None:
                    raise ValueError('Группа или институт не найдены')
                self.connection.execute(
                    'UPDATE catalog_values SET parent_id = ? WHERE id = ?',
                    (target['id'], group_id),
                )
                competitions_cursor = self.connection.execute(
                    'UPDATE competitions SET institute = ? WHERE institute = ? AND "group" = ?',
                    (target['value'], old_parent['value'], group_row['value']),
                )
                students_cursor = self.connection.execute(
                    'UPDATE students SET institute = ?, updated_at = ? WHERE institute = ? AND group_name = ?',
                    (target['value'], datetime.utcnow().isoformat(), old_parent['value'], group_row['value']),
                )
                self.connection.commit()
                return {
                    'students_updated': students_cursor.rowcount,
                    'competitions_updated': competitions_cursor.rowcount,
                }
            except BaseException:
                self.connection.rollback()
                raise
