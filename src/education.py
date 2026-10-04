"""Учебные данные: год поступления, уровень образования и производный курс.

Course / Education, Phase A (docs/data-model-decisions.md «Курс и уровень
образования»): чистые функции без обращений к БД и Sanic — хранилище и роуты
только вызывают их. Контракты:

- Год поступления НИКОГДА не выводится из уровня образования или группы
  иначе как парсером-ПОДСКАЗКОЙ (parse_group_admission_year): доменная
  истина — явное значение в учебных данных группы (group_academic) или
  снимок записи (competitions.admission_year).
- Учебный год начинается 1 сентября: academic_year_start(1 сентября) уже
  новый год, 31 августа — ещё прошлый.
- course = academic_year_start - admission_year + 1; course <= 0 — год
  поступления позже даты (future), производить «0 курс» нельзя.
- «Предположительно обучение завершено» — только когда длительность
  известна и course > duration; авто-архирования нет и в этой фазе не будет.
"""
import re
from datetime import date

# Парсер-подсказка года поступления по названию группы (СИБADI-вид:
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
    значения строкой («nan»). Это ПОДСКАЗКА для предзаполнения формы и
    классификации backfill, НЕ доменная истина.
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
