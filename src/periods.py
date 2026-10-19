"""Shared academic-year and custom-date filters for reports and viewing history."""
import re
from datetime import date
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from src.education import academic_year_start
from src.settings import settings


def today_in_timezone(timezone_name: str) -> date:
    """Calendar autofill uses an explicit application zone, independent of host/browser."""
    return datetime.now(ZoneInfo(timezone_name)).date()


def period_param(args: dict, key: str) -> str:
    value = args.get(key, '')
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ''
    return str(value) if value is not None else ''


REPORT_PERIOD_ALL = 'all'
REPORT_PERIOD_CUSTOM = 'custom'
REPORT_PERIOD_YEAR_RE = re.compile(r'(\d{4})-(\d{4})')


def current_report_period_value() -> str:
    """Текущий учебный год как значение селекта периода (форма по умолчанию)."""
    start = academic_year_start(date.today())
    return f'{start}-{start + 1}'


def build_report_period_options(data_years: Sequence[int]) -> list[dict[str, str]]:
    """Опции селекта периода: годы из данных по убыванию (текущий учебный
    год всегда есть — он дефолт формы), затем «Всё время» и «Произвольный
    диапазон…»."""
    current_start = academic_year_start(date.today())
    years = sorted({*data_years, current_start}, reverse=True)
    options = [{'value': f'{year}-{year + 1}', 'label': f'{year}/{year + 1} (учебный год)'} for year in years]
    options.append({'value': REPORT_PERIOD_ALL, 'label': 'Всё время'})
    options.append({'value': REPORT_PERIOD_CUSTOM, 'label': 'Произвольный диапазон…'})
    return options


def _valid_academic_year(match) -> bool:
    return match is not None and 1 <= int(match.group(1)) <= 9998 and int(match.group(2)) == int(match.group(1)) + 1


def _period_reversed(date_from: str, date_to: str) -> bool:
    return datetime.strptime(date_from, settings.date_format) > datetime.strptime(date_to, settings.date_format)


def validate_report_period(args: dict) -> str | None:
    """Валидация периода и дат фильтра: формат учебного года, dd.mm.yyyy,
    обе даты обязательны для произвольного диапазона."""
    period = (period_param(args, 'period') or '').strip()
    if period and period not in (REPORT_PERIOD_ALL, REPORT_PERIOD_CUSTOM):
        match = REPORT_PERIOD_YEAR_RE.fullmatch(period)
        if not _valid_academic_year(match):
            return f'Некорректный период: {period} (формат ГГГГ-ГГГГ, например 2026-2027)'

    for key in ('date_from', 'date_to'):
        value = period_param(args, key)
        if value:
            try:
                datetime.strptime(value, settings.date_format)
            except ValueError:
                return f'Некорректная дата в фильтре ({key}): {value}'

    if period == REPORT_PERIOD_CUSTOM:
        if not (period_param(args, 'date_from') or '').strip() or not (period_param(args, 'date_to') or '').strip():
            return 'Для произвольного периода укажите обе даты: от и до'
    date_from, date_to = resolve_report_period(args)
    if date_from and date_to and _period_reversed(date_from, date_to):
        return 'Начало периода не может быть позже окончания'
    return None


def resolve_report_period(args: dict) -> tuple[str, str]:
    """Эффективные границы периода из GET-параметров.

    period=ГГГГ-ГГГГ разворачивается в 01.09.ГГГГ – 31.08.следующего года
    (учебный год начинается 1 сентября, src/education.py); all — без
    фильтра дат (прямые даты игнорируются); custom — из date_from/date_to.
    Без period прямые даты работают как раньше (легаси-ссылки). Валидация
    значений уже выполнена validate_report_filters.
    """
    period = (period_param(args, 'period') or '').strip()
    date_from = (period_param(args, 'date_from') or '').strip()
    date_to = (period_param(args, 'date_to') or '').strip()
    if period == REPORT_PERIOD_ALL:
        return '', ''
    if period == REPORT_PERIOD_CUSTOM:
        return date_from, date_to
    if period:
        match = REPORT_PERIOD_YEAR_RE.fullmatch(period)
        return f'01.09.{match.group(1)}', f'31.08.{match.group(2)}'
    return date_from, date_to


def report_period_label(args: dict) -> str:
    """Человекочитаемая метка периода для чипа и шапки XLSX.

    args — плоский словарь «параметр → значение» (например, dict(entries)
    применённого запроса).
    """
    period = (period_param(args, 'period') or '').strip()
    date_from = (period_param(args, 'date_from') or '').strip()
    date_to = (period_param(args, 'date_to') or '').strip()
    if period == REPORT_PERIOD_ALL:
        return 'Всё время'
    if period == REPORT_PERIOD_CUSTOM:
        return f'{date_from} – {date_to}'
    if period:
        match = REPORT_PERIOD_YEAR_RE.fullmatch(period)
        return f'Учебный год {match.group(1)}/{match.group(2)}' if match else period
    if date_from and date_to:
        return f'{date_from} – {date_to}'
    if date_from:
        return f'с {date_from}'
    if date_to:
        return f'по {date_to}'
    return 'Все даты'


def academic_years_for_ranges(rows: Sequence[dict]) -> list[int]:
    """Include each school year touched by stored ISO date ranges."""
    years = set()
    for row in rows:
        first = academic_year_start(date.fromisoformat(row['date'][:10]))
        last = academic_year_start(date.fromisoformat((row.get('date_to') or row['date'])[:10]))
        years.update(range(max(first, 1), min(last, 9998) + 1))
    return sorted(years, reverse=True)


def period_filter_context(args: dict, data_years: Sequence[int]) -> dict:
    """Viewing filters default to all dates; legacy date URLs remain editable."""
    period = (period_param(args, 'period') or '').strip()
    date_from = (period_param(args, 'date_from') or '').strip()
    date_to = (period_param(args, 'date_to') or '').strip()
    if not period:
        period = REPORT_PERIOD_CUSTOM if date_from or date_to else REPORT_PERIOD_ALL
    options = build_report_period_options(data_years)
    options.sort(key=lambda item: item['value'] != REPORT_PERIOD_ALL)
    if period not in {item['value'] for item in options}:
        options.insert(0, {'value': period, 'label': period.replace('-', '/') + ' (учебный год)'})
    return {
        'period_value': period,
        'period_options': options,
        'period_date_from': date_from,
        'period_date_to': date_to,
        'period_active': period != REPORT_PERIOD_ALL,
        'period_label': report_period_label(args),
    }
