"""Доменная логика карточек студентов, общая для нескольких route-модулей.

Разбор формы карточки, нормализация значений импорта (пол/курс), тексты
ошибок привязки записей и подсказки справочников для строк импорта.
Используется разделом «Студенты» и календарём (создание/привязка студентов
из участников события). Перенесено из src/routes/students.py без изменения
поведения (Architecture v1, pre-merge gate)."""
from typing import Sequence

from pandas import isna
from sanic import Request

from src.storage.sqlite import SQLiteAdapter
from src.web import get_form_value

# Карточки студентов (Student Identity v1, Phase 1): допустимые значения
# поля «Пол» — только М/Ж или пусто (не указан).
STUDENT_SEX_OPTIONS: frozenset[str] = frozenset({'', 'М', 'Ж'})

# Транслитерация кириллицы для предложения логина (выдача доступа атлету
# с карточки студента): фамилия + инициалы → «иванов_ии». Однозначности
# с паспортной схемой не требуется — это только предзаполнение поля,
# итоговый логин вводит модератор, уникальность проверяет сервер.
TRANSLIT_TABLE: dict[str, str] = {
    'а': 'a',
    'б': 'b',
    'в': 'v',
    'г': 'g',
    'д': 'd',
    'е': 'e',
    'ё': 'e',
    'ж': 'zh',
    'з': 'z',
    'и': 'i',
    'й': 'y',
    'к': 'k',
    'л': 'l',
    'м': 'm',
    'н': 'n',
    'о': 'o',
    'п': 'p',
    'р': 'r',
    'с': 's',
    'т': 't',
    'у': 'u',
    'ф': 'f',
    'х': 'kh',
    'ц': 'ts',
    'ч': 'ch',
    'ш': 'sh',
    'щ': 'shch',
    'ъ': '',
    'ы': 'y',
    'ь': '',
    'э': 'e',
    'ю': 'yu',
    'я': 'ya',
}


def suggest_login(full_name: str) -> str:
    """Предложение логина из ФИО: транслит фамилии (до 24 символов) + «_» +
    инициалы имени и отчества. «Иванов Иван Иванович» → «ivanov_ii».

    Чистая функция без I/O: только предзаполнение формы выдачи доступа,
    занятость проверяет отдельно find_free_username. Из транслита и латиницы
    остаются только [a-z0-9] (дефис и прочее отбрасывается); слова, из
    которых не выжило ни одного символа, не участвуют; если не выжило
    ничего — «student».
    """
    words = []
    for part in (full_name or '').lower().split():
        transliterated = ''.join(TRANSLIT_TABLE.get(char, char) for char in part)
        word = ''.join(char for char in transliterated if 'a' <= char <= 'z' or char.isdigit())
        if word:
            words.append(word)
    surname = words[0][:24] if words else ''
    initials = ''.join(word[0] for word in words[1:])
    if surname and initials:
        return f'{surname}_{initials}'
    return surname or 'student'


def parse_student_form(request: Request) -> tuple[dict, str | None]:
    """Поля карточки студента из формы. None-ошибка — текст для редиректа.

    Поле формы группы называется «group» (HTML-конвенция) и маппится в
    group_name хранилища. Все значения — свободный текст со strip().
    """
    full_name = get_form_value(request, 'full_name').strip()
    sex = get_form_value(request, 'sex').strip()
    institute = get_form_value(request, 'institute').strip()
    group_name = get_form_value(request, 'group').strip()
    course = get_form_value(request, 'course').strip()
    if not full_name:
        return {}, 'Укажите ФИО студента.'
    if sex not in STUDENT_SEX_OPTIONS:
        return {}, 'Пол может быть «М», «Ж» или не указан.'
    return {
        'full_name': full_name,
        'sex': sex,
        'institute': institute,
        'group_name': group_name,
        'course': course,
    }, None


def reconcile_student_error(storage: SQLiteAdapter, student_id: int, code: str) -> str:
    """Flash-текст ошибки привязки по коду хранилища (карточка/активность)."""
    student = storage.get_student_by_id(student_id)
    if student is None:
        return 'Студент не найден.'
    if code == 'student_inactive':
        return f'Студент «{student["full_name"]}» неактивен: привязка возможна только к активным студентам.'
    return 'Студент не найден.'


def reconcile_link_error(
    storage: SQLiteAdapter,
    record_ids: list[int],
    student_id: int,
    code: str,
) -> str:
    """Flash-текст ошибки привязки записей (одиночной и массовой)."""
    if code in ('student_not_found', 'student_inactive'):
        return reconcile_student_error(storage, student_id, code)
    if code == 'records_not_found':
        return 'Запись не найдена.'
    if len(record_ids) == 1:
        return f'Запись №{record_ids[0]} уже привязана к студенту.'
    return 'Ничего не привязано: часть выбранных записей уже привязана. Обновите список и повторите.'


def normalize_import_course(value) -> str:
    """Курс из ячейки Excel как строка: 2.0 → «2», NaN/None → «».

    Числовые ячейки pandas отдаёт float'ами («2» в Excel → 2.0); целые
    выводятся без дробной части, остальное (текст, нецелые) — как str().
    """
    if value is None or isna(value):
        return ''
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def normalize_student_sex(value: str) -> str | None:
    """Пол из ячейки Excel в каноническом виде: '' → '', м/м → «М», ж/ж → «Ж».

    upper() в Python работает с кириллицей («м» → «М»); латинские m/M не
    совпадают с кириллическими «М»/«Ж» и отвергаются естественно. Всё
    остальное — None (ошибка строки, а не молчаливая правка).
    """
    if value == '':
        return ''
    if value.upper() == 'М':
        return 'М'
    if value.upper() == 'Ж':
        return 'Ж'
    return None


def student_file_group_institutes(rows: Sequence[dict]) -> dict[str, list[dict]]:
    """Карта группа → институты из строк файла импорта (по самой сессии).

    Ключ — casefold(strip(группа)); значение — институты этой группы,
    уникальные по casefold, в порядке появления (сохраняется первое
    написание). Строки без группы или института не участвуют; статус строки
    не важен — карта строится из ВСЕХ строк сессии при каждом рендере,
    поэтому правка строки-«донора» меняет подсказки остальных естественно.
    """
    institutes: dict[str, list[dict]] = {}
    for row in rows:
        group = (row['group'] or '').strip()
        institute = (row['institute'] or '').strip()
        if not group or not institute:
            continue
        bucket = institutes.setdefault(group.casefold(), [])
        if not any(item['cf'] == institute.casefold() for item in bucket):
            bucket.append({'cf': institute.casefold(), 'raw': institute})
    return institutes


def canonical_group_in_institute(storage: SQLiteAdapter, institute: str, group: str) -> str:
    """Каноническое написание группы внутри института (уникальность группы —
    по паре институт+группа); без изменений, если пара справочнику неизвестна."""
    institute_row = storage.find_catalog_row('institute', institute)
    if institute_row is None:
        return group
    canonical = storage.find_catalog_canonical('group', group, parent_id=institute_row['id'])
    return canonical if canonical else group


def student_catalog_group_hints(
    storage: SQLiteAdapter,
    hints: dict,
    group: str,
    file_groups: dict[str, list[dict]] | None = None,
) -> dict:
    """Институт не указан, группа указана: институт — из ТЕКУЩЕГО файла
    (file_groups, если группа встречается в нём ровно с одним институтом),
    иначе из справочника (если группа принадлежит ровно одному институту);
    иначе предупреждение без изменения значений.

    Файл важнее справочника: импорт списков одной группы обычно идёт из
    файла факультета. Расхождение со справочником — предупреждение, значение
    остаётся файловым.
    """
    file_entry = (file_groups or {}).get(group.casefold())
    if file_entry is not None:
        if len(file_entry) > 1:
            hints['warnings'].append('Институт не определён: в файле группа относится к разным институтам')
            return hints
        raw_institute = file_entry[0]['raw']
        institute_value = storage.find_catalog_canonical('institute', raw_institute) or raw_institute
        hints['institute'] = institute_value
        hints['institute_autofilled'] = True
        hints['institute_source'] = 'file'
        hints['group'] = canonical_group_in_institute(storage, institute_value, group)
        catalog_owner = storage.find_unique_group_institute(group)
        if catalog_owner and catalog_owner.casefold() != institute_value.casefold():
            hints['warnings'].append('Требует внимания: справочник относит группу к другому институту')
        return hints
    institute_value = storage.find_unique_group_institute(group)
    if not institute_value:
        hints['warnings'].append(
            'Институт не определён: группа отсутствует в справочнике или относится к нескольким институтам'
        )
        return hints
    hints['institute'] = institute_value
    hints['institute_autofilled'] = True
    hints['institute_source'] = 'catalog'
    hints['group'] = canonical_group_in_institute(storage, institute_value, group)
    return hints


def student_catalog_pair_hints(storage: SQLiteAdapter, hints: dict, institute: str, group: str) -> dict:
    """Институт и группа указаны: канонизация обоих по справочнику; группа,
    однозначно принадлежащая ДРУГОМУ институту — предупреждение, значения
    не переписываются."""
    canonical_institute = storage.find_catalog_canonical('institute', institute)
    if canonical_institute:
        hints['institute'] = canonical_institute
        hints['group'] = canonical_group_in_institute(storage, canonical_institute, group)
    owner = storage.find_unique_group_institute(group)
    if owner and owner.lower() != hints['institute'].lower():
        hints['warnings'].append('Требует внимания: группа относится к другому институту')
    return hints


def student_catalog_hints(
    storage: SQLiteAdapter,
    institute: str,
    group: str,
    file_groups: dict[str, list[dict]] | None = None,
) -> dict:
    """Подсказки справочников для строки импорта студентов — ТОЛЬКО ЧТЕНИЕ.

    Возвращает значения (возможно канонизированные по регистру), признак
    автозаполнения института (institute_source: 'file' — из текущего файла,
    'catalog' — из справочника) и предупреждения. Импорт студентов НИЧЕГО
    не пишет в справочники (в отличие от импорта записей соревнований).
    """
    hints = {
        'institute': institute,
        'group': group,
        'institute_autofilled': False,
        'institute_source': None,
        'warnings': [],
    }

    if not institute and group:
        return student_catalog_group_hints(storage, hints, group, file_groups=file_groups)
    if institute and group:
        return student_catalog_pair_hints(storage, hints, institute, group)
    if institute:
        canonical_institute = storage.find_catalog_canonical('institute', institute)
        if canonical_institute:
            hints['institute'] = canonical_institute
    return hints
