"""Учебные данные хранилища Competitions: уровни образования, учебные
данные групп (год поступления/уровень/длительность), строгий резолв года
поступления по паре институт+группа, снимок года поступления записей и его
backfill.

Course / Education, Phase A. Контракт примеси (Architecture v2.1): не
создаёт соединение и блокировку (self.connection / self._lock принадлежат
SQLiteAdapter), не импортирует соседние доменные модули src.storage.*
(кроме src.storage.helpers), междоменные вызовы — только через self.
"""
import sqlite3
from datetime import datetime

from src.education import derive_course
from src.education import effective_duration_years
from src.education import MAX_ADMISSION_YEAR
from src.education import MAX_DURATION_YEARS
from src.education import MIN_ADMISSION_YEAR
from src.education import MIN_DURATION_YEARS
from src.education import parse_group_admission_year
from src.storage.helpers import CALENDAR_LINK_PREVIEW_CAP

# Посев уровней образования (решение владельца Phase A): без хардкода
# длительностей — default_duration_years NULL, значения задаёт админ.
DEFAULT_EDUCATION_LEVELS: tuple[str, ...] = (
    'Бакалавриат',
    'Специалитет',
    'Магистратура',
    'Аспирантура',
)


class EducationMixin:
    # ---- Уровни образования ----

    def _populate_default_education_levels(self) -> None:
        """Посев базовых уровней образования (Course/Education Phase A).

        Паттерн _populate_field_settings_defaults: UNIQUE(name) +
        INSERT OR IGNORE — повторные старты не дублируют, изменения админа
        (переименование не предусмотрено, но скрытие/длительность) не
        сбрасываются. default_duration_years NULL — длительности не
        хардкодятся (решение владельца Phase A).
        """
        now = datetime.utcnow().isoformat()
        for name in DEFAULT_EDUCATION_LEVELS:
            self.connection.execute(
                'INSERT OR IGNORE INTO education_levels (name, default_duration_years, active, created_at, updated_at) '
                'VALUES (?, NULL, 1, ?, ?)',
                (name, now, now),
            )

    def list_education_levels(self, include_inactive: bool = False) -> list[dict]:
        """Уровни образования: активные сверху, по алфавиту."""
        with self._lock:
            if include_inactive:
                rows = self.connection.execute(
                    'SELECT id, name, default_duration_years, active, created_at, updated_at '
                    'FROM education_levels ORDER BY active DESC, name ASC'
                ).fetchall()
            else:
                rows = self.connection.execute(
                    'SELECT id, name, default_duration_years, active, created_at, updated_at '
                    'FROM education_levels WHERE active = 1 ORDER BY name ASC'
                ).fetchall()
            return [dict(row) for row in rows]

    def get_education_level(self, level_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, name, default_duration_years, active, created_at, updated_at '
                'FROM education_levels WHERE id = ?',
                (level_id,),
            ).fetchone()
            return dict(row) if row else None

    def _assert_valid_duration(self, value, *, field: str) -> None:
        if value is None:
            return
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f'{field} должно быть целым числом или пусто')
        if not MIN_DURATION_YEARS <= value <= MAX_DURATION_YEARS:
            raise ValueError(f'{field} должно быть от {MIN_DURATION_YEARS} до {MAX_DURATION_YEARS} лет')

    def create_education_level(self, name: str, default_duration_years: int | None = None) -> int:
        """Создать уровень образования; возвращает id.

        Имя непустое, уникально без учёта регистра; длительность — NULL или
        int в допустимом диапазоне. Ошибки — ValueError, ничего не создаётся.
        """
        name = (name or '').strip()
        if not name:
            raise ValueError('Название уровня обязательно')
        self._assert_valid_duration(default_duration_years, field='Длительность по умолчанию')
        now = datetime.utcnow().isoformat()
        with self._lock:
            # Регистровое сравнение — в Python: lower() в SQLite работает
            # только с ASCII и не приводит кириллицу (как find_catalog_row).
            existing = self.connection.execute('SELECT name FROM education_levels').fetchall()
            if any(row['name'].strip().casefold() == name.casefold() for row in existing):
                raise ValueError(f'Уровень «{name}» уже есть')
            cursor = self.connection.execute(
                'INSERT INTO education_levels (name, default_duration_years, active, created_at, updated_at) '
                'VALUES (?, ?, 1, ?, ?)',
                (name, default_duration_years, now, now),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def update_education_level(self, level_id: int, default_duration_years: int | None) -> None:
        """Изменить длительность по умолчанию уровня (inline-редактирование)."""
        self._assert_valid_duration(default_duration_years, field='Длительность по умолчанию')
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE education_levels SET default_duration_years = ?, updated_at = ? WHERE id = ?',
                (default_duration_years, datetime.utcnow().isoformat(), level_id),
            )
            if not cursor.rowcount:
                self.connection.rollback()
                raise ValueError('Уровень образования не найден')
            self.connection.commit()

    def set_education_level_active(self, level_id: int, active: bool) -> None:
        """Скрыть/показать уровень. Скрытый не выбирается в новых учебных
        данных групп, но остаётся у групп, где уже задан."""
        with self._lock:
            self.connection.execute(
                'UPDATE education_levels SET active = ?, updated_at = ? WHERE id = ?',
                (1 if active else 0, datetime.utcnow().isoformat(), level_id),
            )
            self.connection.commit()

    def count_groups_using_level(self, level_id: int) -> int:
        """Сколько учебных данных групп ссылаются на уровень (включая скрытые)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM group_academic WHERE education_level_id = ?',
                (level_id,),
            ).fetchone()
            return row['total']

    def delete_education_level(self, level_id: int) -> None:
        """Удалить уровень; только когда на него не ссылается ни одна группа."""
        with self._lock:
            references = self.connection.execute(
                'SELECT COUNT(*) AS total FROM group_academic WHERE education_level_id = ?',
                (level_id,),
            ).fetchone()['total']
            if references:
                raise ValueError(f'У уровня есть группы ({references}) — удаление невозможно')
            cursor = self.connection.execute('DELETE FROM education_levels WHERE id = ?', (level_id,))
            if not cursor.rowcount:
                self.connection.rollback()
                raise ValueError('Уровень образования не найден')
            self.connection.commit()

    # ---- Учебные данные групп ----

    def get_group_academic(self, group_catalog_value_id: int) -> dict | None:
        """Учебные данные группы (+ имя уровня, даже если уровень скрыт)."""
        with self._lock:
            row = self.connection.execute(
                '''
                SELECT ga.group_catalog_value_id, ga.education_level_id, ga.admission_year,
                       ga.duration_years_override, ga.source, ga.updated_at,
                       el.name AS education_level_name, el.default_duration_years AS level_default_duration_years
                FROM group_academic AS ga
                LEFT JOIN education_levels AS el ON el.id = ga.education_level_id
                WHERE ga.group_catalog_value_id = ?
                ''',
                (group_catalog_value_id,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_group_academic(
        self,
        group_catalog_value_id: int,
        *,
        education_level_id: int | None = None,
        admission_year: int | None = None,
        duration_years_override: int | None = None,
        source: str = 'manual',
    ) -> None:
        """Сохранить учебные данные группы (одна транзакция).

        Валидация здесь, а не в CHECK-ограничениях (паттерн проекта):
        целевая строка справочника — category='group'; уровень существует
        (может быть скрытым — метаданные переживают деактивацию уровня);
        год поступления — NULL или int в допустимом диапазоне; длительность
        — NULL или int в допустимом диапазоне. Все поля пишутся целиком:
        форма отправляет полный набор, «пусто» = очистить.
        """
        if education_level_id is not None:
            with self._lock:
                level = self.connection.execute(
                    'SELECT id FROM education_levels WHERE id = ?',
                    (education_level_id,),
                ).fetchone()
            if level is None:
                raise ValueError('Уровень образования не найден')
        if admission_year is not None:
            if not isinstance(admission_year, int) or isinstance(admission_year, bool):
                raise ValueError('Год поступления должен быть целым числом или пустым')
            if not MIN_ADMISSION_YEAR <= admission_year <= MAX_ADMISSION_YEAR:
                raise ValueError(f'Год поступления должен быть от {MIN_ADMISSION_YEAR} до {MAX_ADMISSION_YEAR}')
        self._assert_valid_duration(duration_years_override, field='Длительность обучения')
        now = datetime.utcnow().isoformat()
        with self._lock:
            try:
                group_row = self.connection.execute(
                    "SELECT id FROM catalog_values WHERE id = ? AND category = 'group'",
                    (group_catalog_value_id,),
                ).fetchone()
                if group_row is None:
                    raise ValueError('Группа не найдена в справочнике')
                self.connection.execute(
                    '''
                    INSERT INTO group_academic (
                        group_catalog_value_id, education_level_id, admission_year,
                        duration_years_override, source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_catalog_value_id) DO UPDATE SET
                        education_level_id = excluded.education_level_id,
                        admission_year = excluded.admission_year,
                        duration_years_override = excluded.duration_years_override,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    ''',
                    (
                        group_catalog_value_id,
                        education_level_id,
                        admission_year,
                        duration_years_override,
                        source,
                        now,
                    ),
                )
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def delete_group_academic(self, group_catalog_value_id: int) -> None:
        """Удалить учебные данные группы (idempotent: нет строки — ок)."""
        with self._lock:
            self.connection.execute(
                'DELETE FROM group_academic WHERE group_catalog_value_id = ?',
                (group_catalog_value_id,),
            )
            self.connection.commit()

    def list_group_academic_with_context(self) -> list[dict]:
        """Все учебные данные групп: значение группы + имя института-родителя."""
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT ga.group_catalog_value_id, ga.education_level_id, ga.admission_year,
                       ga.duration_years_override, ga.source, ga.updated_at,
                       groups.value AS group_value, institutes.value AS institute_value,
                       el.name AS education_level_name
                FROM group_academic AS ga
                JOIN catalog_values AS groups ON groups.id = ga.group_catalog_value_id
                LEFT JOIN catalog_values AS institutes ON institutes.id = groups.parent_id
                LEFT JOIN education_levels AS el ON el.id = ga.education_level_id
                ORDER BY institutes.value ASC, groups.value ASC
                '''
            ).fetchall()
            return [dict(row) for row in rows]

    def _resolve_group_catalog_row(self, institute: str, group: str) -> dict | None:
        """Строка справочника группы по СТРОГОЙ паре институт+группа.

        Институт пустой/неизвестный или пара не существует — None; групповое
        имя без института не резолвится (одноимённые группы разных институтов
        сосуществуют). Возвращает строку каталога или None.
        """
        institute_text = (institute or '').strip()
        group_text = (group or '').strip()
        if not institute_text or not group_text:
            return None
        with self._lock:
            institute_row = self.find_catalog_row('institute', institute_text)
            if institute_row is None:
                return None
            return self.find_catalog_row('group', group_text, parent_id=institute_row['id'])

    def resolve_group_admission_year(self, institute: str, group: str) -> int | None:
        """Год поступления пары институт+группа из учебных данных группы.

        Только строгое разрешение пары по справочнику; фолбэка на парсер
        названий здесь НЕТ и не будет (парсер — предзаполнение формы и
        классификация backfill, не доменная истина). Нет пары в справочнике,
        нет учебных данных или не задан год — None.
        """
        row = self._resolve_group_catalog_row(institute, group)
        if row is None:
            return None
        with self._lock:
            academic = self.connection.execute(
                'SELECT admission_year FROM group_academic WHERE group_catalog_value_id = ?',
                (row['id'],),
            ).fetchone()
        if academic is None or academic['admission_year'] is None:
            return None
        return int(academic['admission_year'])

    def get_student_education_context(self, institute: str, group: str) -> dict | None:
        """Учебный контекст для карточки студента: уровень, год поступления,
        эффективная длительность.

        None — пара институт+группа не резолвится в строку справочника ИЛИ у
        группы нет строки group_academic (карточка «Образование» не
        рендерится). Курс считаем в роуте (src.education.derive_course).
        """
        row = self._resolve_group_catalog_row(institute, group)
        if row is None:
            return None
        with self._lock:
            academic = self.connection.execute(
                '''
                SELECT ga.education_level_id, ga.admission_year, ga.duration_years_override,
                       el.name AS education_level_name, el.default_duration_years AS level_default_duration_years
                FROM group_academic AS ga
                LEFT JOIN education_levels AS el ON el.id = ga.education_level_id
                WHERE ga.group_catalog_value_id = ?
                ''',
                (row['id'],),
            ).fetchone()
        if academic is None:
            return None
        duration = effective_duration_years(
            academic['duration_years_override'], academic['level_default_duration_years']
        )
        return {
            'level_name': academic['education_level_name'],
            'admission_year': academic['admission_year'],
            'duration_years_override': academic['duration_years_override'],
            'level_default_duration_years': academic['level_default_duration_years'],
            'effective_duration_years': duration,
            'group_value': row['value'],
        }

    # ---- Снимок года поступления записей: счётчик и backfill ----

    def count_participations_without_admission_year(self) -> int:
        """Дешёвый счётчик записей без снимка года поступления (карточка
        «Годы поступления (записи)» на странице обслуживания)."""
        with self._lock:
            return int(
                self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions WHERE admission_year IS NULL'
                ).fetchone()['total']
            )

    def _classify_admission_year_row(self, row: sqlite3.Row) -> tuple[str, int | None, str | None]:
        """Классификация одной NULL-записи: (категория, год, причина/подсказка).

        SAFE — парсер названия группы однозначен И курс на дату записи
        положителен (год поступления не позже соревнования). MANUAL — год
        определяется, но применять нельзя (future), ИЛИ парсер не уверен,
        а пара институт+группа известна справочнику (сюда попадает и
        «Выпуск» — автозаполнения не будет никогда). UNRESOLVED — парсер
        не уверен и пары в справочнике нет (мусор: nan, пусто, не-каталог).
        """
        admission_year = parse_group_admission_year(row['group'])
        if admission_year is not None:
            reference_date = datetime.fromisoformat(row['date']).date()
            if derive_course(admission_year, reference_date)['status'] == 'ok':
                return 'safe', admission_year, None
            return (
                'manual',
                admission_year,
                'год поступления позже даты соревнования — проверьте вручную',
            )
        if self._resolve_group_catalog_row(row['institute'], row['group']) is not None:
            return (
                'manual',
                None,
                'год из названия не определяется; группа есть в справочнике — '
                'задайте год поступления в учебных данных группы',
            )
        return 'unresolved', None, None

    def group_admission_backfill_preview(self) -> dict:
        """Read-only предпросмотр заполнения admission_year у NULL-записей.

        Каждая строка классифицируется по СВОЕМУ снимку группы (значение в
        самой записи), не по текущим учебным данным справочника. Списки
        capped (CALENDAR_LINK_PREVIEW_CAP), счётчики — полные.
        """
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT id, student_name, institute, "group", date
                FROM competitions
                WHERE admission_year IS NULL
                ORDER BY date ASC, id ASC
                '''
            ).fetchall()
            already_filled = int(
                self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions WHERE admission_year IS NOT NULL'
                ).fetchone()['total']
            )
            safe: list[dict] = []
            manual: list[dict] = []
            unresolved: list[dict] = []
            counters = {'considered': len(rows), 'safe': 0, 'manual': 0, 'unresolved': 0}
            for row in rows:
                category, year, reason = self._classify_admission_year_row(row)
                counters[category] += 1
                base = {
                    'record_id': row['id'],
                    'student_name': row['student_name'],
                    'institute': row['institute'],
                    'group': row['group'],
                    'date': row['date'],
                }
                if category == 'safe':
                    if len(safe) < CALENDAR_LINK_PREVIEW_CAP:
                        safe.append({**base, 'admission_year': year})
                elif category == 'manual':
                    if len(manual) < CALENDAR_LINK_PREVIEW_CAP:
                        manual.append({**base, 'reason': reason})
                elif len(unresolved) < CALENDAR_LINK_PREVIEW_CAP:
                    unresolved.append(base)
            return {
                'counters': {**counters, 'already_filled': already_filled},
                'safe': safe,
                'manual': manual,
                'unresolved': unresolved,
            }

    def apply_group_admission_backfill(self) -> dict[str, int]:
        """Заполнить admission_year ТОЛЬКО у SAFE-записей (одна транзакция).

        Классификация пересчитывается в момент применения (предпросмотр мог
        устареть); каждая строка — guarded UPDATE `WHERE id = ? AND
        admission_year IS NULL`: запись, которую успели заполнить, —
        пропуск (skipped), не откат пакета. Возвращает {'matched',
        'applied', 'skipped'}.
        """
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT id, student_name, institute, "group", date
                FROM competitions
                WHERE admission_year IS NULL
                ORDER BY id ASC
                '''
            ).fetchall()
            counters = {'matched': 0, 'applied': 0, 'skipped': 0}
            try:
                for row in rows:
                    category, year, _reason = self._classify_admission_year_row(row)
                    if category != 'safe':
                        continue
                    counters['matched'] += 1
                    cursor = self.connection.execute(
                        'UPDATE competitions SET admission_year = ? WHERE id = ? AND admission_year IS NULL',
                        (year, row['id']),
                    )
                    if cursor.rowcount:
                        counters['applied'] += 1
                    else:
                        # Кто-то заполнил запись между предпросмотром и apply —
                        # не ломаем пакет, считаем пропуском.
                        counters['skipped'] += 1
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
            return counters
