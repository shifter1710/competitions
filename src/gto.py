"""Доменная логика ГТО (Готов к труду и обороне), общая для раздела
«Студенты» и кабинета атлета.

Подписи ступеней/статусов и сборка предвычисленных строк для шаблонов:
шаблоны остаются без логики, рендерят готовые {stage_label, age_label,
status_label}. Чистые функции без хранилища (паттерн src/education.py);
константы живут в src/storage/helpers.py и переэкспортированы
src/storage/sqlite.py.
"""
from typing import Sequence

from src.storage.helpers import GTO_STAGES
from src.storage.helpers import GTO_STATUS_LABELS

# Ступень → (римская цифра, возрастная группа); быстрый lookup из кортежа.
_STAGE_BY_NUMBER = {number: (roman, age) for number, roman, age in GTO_STAGES}


def gto_stage_label(stage: int) -> str:
    """Отображение ступени в таблице: «VIII ступень (20–24)». Неизвестный
    номер (невозможен после валидации хранилища) — безопасный фолбэк."""
    entry = _STAGE_BY_NUMBER.get(stage)
    if entry is None:
        return f'{stage} ступень'
    roman, age = entry
    return f'{roman} ступень ({age})'


def gto_stage_age_label(stage: int) -> str:
    """Возрастная группа ступени: «20–24»; пусто — номер вне 1..18."""
    entry = _STAGE_BY_NUMBER.get(stage)
    return entry[1] if entry is not None else ''


def gto_stage_option_label(stage: int) -> str:
    """Опция select'а ступени: «VIII ступень — 20–24 лет». У ступени 1
    возрастная группа уже с «лет» («6–7 лет») — дописывания нет."""
    entry = _STAGE_BY_NUMBER.get(stage)
    if entry is None:
        return f'{stage} ступень'
    roman, age = entry
    suffix = '' if age.endswith('лет') else ' лет'
    return f'{roman} ступень — {age}{suffix}'


def gto_status_label(status: str) -> str:
    """UI-подпись статуса ГТО (неизвестный код — как есть)."""
    return GTO_STATUS_LABELS.get(status, status)


def build_gto_rows(records: Sequence[dict]) -> list[dict]:
    """Строки ГТО для шаблонов (карточка студента и кабинет атлета — один
    порядок и один рендер): {id, year, stage, stage_label, age_label,
    status, status_label} в порядке хранилища (год DESC, ступень ASC,
    id ASC)."""
    return [
        {
            'id': record['id'],
            'year': record['year'],
            'stage': record['stage'],
            'stage_label': gto_stage_label(record['stage']),
            'age_label': gto_stage_age_label(record['stage']),
            'status': record['status'],
            'status_label': gto_status_label(record['status']),
        }
        for record in records
    ]


def build_gto_stage_options() -> list[dict]:
    """Опции select'а ступени: [{'value': 1, 'label': «I ступень — 6–7
    лет»}, …] по возрастанию номера."""
    return [{'value': number, 'label': gto_stage_option_label(number)} for number, _roman, _age in GTO_STAGES]
