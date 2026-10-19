"""Education is derived from the own group and reference date.

The first exact two digits after an ASCII hyphen mean 20YY. Historical
participation uses its start date; current cards use application-local today.
Legacy persisted course/year columns are inert compatibility data.
"""
import re
from datetime import date

# Достоверное правило года поступления по названию группы (СИБADI-вид:
# «АДб-22С1» → 2022). Первое вхождение «ровно две цифры после дефиса»;
# три и больше цифр после дефиса — НЕ год («-2231»), любые другие формы —
# None. Никаких догадок: непонятное название остаётся без года.
ADMISSION_YEAR_RE = re.compile(r'-(\d{2})(?!\d)')

# Год поступления как int в валидации учебных данных группы (storage).
MIN_ADMISSION_YEAR = 1990
MAX_ADMISSION_YEAR = 2100

# Длительность обучения (лет): и дефолт уровня, и override группы.
# Верхняя граница — здравый смысл бакалавриат+магистратура+аспирантура.
MIN_DURATION_YEARS = 1
MAX_DURATION_YEARS = 10


def parse_group_admission_year(group_value) -> int | None:
    """Год поступления из названия группы — только однозначные случаи.

    Первое «ровно 2 цифры после дефиса» → 2000 + число («См-24МА1» →
    2024, «Тестб-23А1» → 2023). Всё остальное — None: нет дефиса, после
    дефиса не 2 цифры («-2231»), «Выпуск», пустая строка, None, мусорные
    значения строкой («nan»). Это источник года поступления; сохранённые старые годы не используются.
    """
    if group_value is None:
        return None
    text = str(group_value).strip()
    if not text:
        return None
    match = ADMISSION_YEAR_RE.search(text)
    if match is None:
        return None
    return 2000 + int(match.group(1))


def parse_group_admission_year_evidence(group_value) -> str | None:
    """Найденный парсером фрагмент («23») для предпросмотра backfill.

    Показывает админу, ИМЕННО ЧТО распознано в названии группы; сам год
    считает parse_group_admission_year. None — ничего не распознано.
    """
    if group_value is None:
        return None
    match = ADMISSION_YEAR_RE.search(str(group_value).strip())
    return match.group(1) if match else None


def academic_year_start(reference_date: date) -> int:
    """Календарный год начала учебного года для даты-ориентира.

    Учебный год начинается 1 сентября: 31.08.2026 → 2025, 01.09.2026 → 2026.
    """
    return reference_date.year if reference_date.month >= 9 else reference_date.year - 1


def academic_year_label(start_year: int) -> str:
    """Отображение учебного года «2026/2027»."""
    return f'{start_year}/{start_year + 1}'


def derive_course(
    admission_year: int | None,
    reference_date: date,
    effective_duration_years: int | None = None,
) -> dict:
    """Производный курс на дату-ориентир.

    Возвращает {'status', 'course', 'academic_year_start', 'probably_finished'}:
    - unknown — год поступления не задан (course/academic_year_start None);
    - future — год поступления позже учебного года даты (course <= 0),
      course None: «0 курс» не существует;
    - ok — course = academic_year_start - admission_year + 1;
    probably_finished — True ТОЛЬКО при известной длительности и
    course > duration; при неизвестной длительности — всегда False.
    """
    if admission_year is None:
        return {
            'status': 'unknown',
            'course': None,
            'academic_year_start': None,
            'probably_finished': False,
        }
    year_start = academic_year_start(reference_date)
    course = year_start - admission_year + 1
    if course <= 0:
        return {
            'status': 'future',
            'course': None,
            'academic_year_start': year_start,
            'probably_finished': False,
        }
    probably_finished = effective_duration_years is not None and course > effective_duration_years
    return {
        'status': 'ok',
        'course': course,
        'academic_year_start': year_start,
        'probably_finished': probably_finished,
    }


def effective_duration_years(override: int | None, level_default: int | None) -> int | None:
    """Эффективная длительность обучения: override ?? default уровня ?? None."""
    return override if override is not None else level_default


def group_course(group_value, reference_date) -> int | None:
    """Pure group/date calculation, also safe as a SQLite scalar function."""
    if isinstance(reference_date, str):
        try:
            reference_date = date.fromisoformat(reference_date[:10])
        except (ValueError, TypeError):
            return None
    if not isinstance(reference_date, date):
        return None
    return derive_course(parse_group_admission_year(group_value), reference_date)['course']


def current_group_course(group_value) -> int | None:
    # Local import avoids periods -> education import cycle.
    from src.periods import today_in_timezone
    from src.settings import settings

    return group_course(group_value, today_in_timezone(settings.app_timezone))


def current_student_values(row) -> dict:
    """Decorate a card without exposing its inert persisted course."""
    values = dict(row)
    values['course'] = current_group_course(values.get('group_name', values.get('group', '')))
    values['admission_year'] = parse_group_admission_year(values.get('group_name', values.get('group', '')))
    return values
