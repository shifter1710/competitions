"""Тесты чистых функций учебных данных (Course / Education, Phase A).

Все названия групп и ФИО синтетические: формы повторяют встречающиеся
паттерны («<код>-<2 цифры><суффикс>»), но не копируют реальные значения.
"""
from datetime import date

import pytest

from src.education import academic_year_label
from src.education import academic_year_start
from src.education import derive_course
from src.education import effective_duration_years
from src.education import parse_group_admission_year
from src.education import parse_group_admission_year_evidence


# ---- Парсер года поступления: корпус распознаваемых форм (синтетика) ----


@pytest.mark.parametrize(
    ('group_value', 'expected_year'),
    [
        ('Тестб-22А1', 2022),  # форма «АДб-22С1» — код, дефис, 2 цифры, суффикс
        ('Прб-25Т1', 2025),  # форма «ПТС-25Т1»
        ('Смт-24МА1', 2024),  # форма «См-24МА1» — многосимвольный суффикс
        (' БИ-22Э1 ', 2022),  # пробелы по краям — strip
        ('Т-группа-23А1', 2023),  # два дефиса: год после второго
        ('Хх-21', 2021),  # две цифры в конце названия
        ('Хх-99Б2', 2099),  # верхняя граница двузначных индексов
    ],
)
def test_parse_group_admission_year_recognized(group_value, expected_year):
    assert parse_group_admission_year(group_value) == expected_year


@pytest.mark.parametrize(
    'group_value',
    [
        'Выпуск',  # архивное значение без года
        'выпуск 25',  # без дефиса перед цифрами
        'Тестб-2231А1',  # 3+ цифр после дефиса — НЕ год
        'Тестб-2А1',  # одна цифра после дефиса
        'Тестб-А22С1',  # цифры не сразу после дефиса
        'Тестб без дефиса 22',  # дефиса нет вообще
        'nan',  # мусор из импорта строкой
        '',  # пусто
        '   ',  # только пробелы
        None,  # NULL
        2022,  # не строка — тоже None (в парсер может прийти что угодно)
    ],
)
def test_parse_group_admission_year_never_guesses(group_value):
    assert parse_group_admission_year(group_value) is None


def test_parse_group_admission_year_evidence_matches_parser():
    assert parse_group_admission_year_evidence('Тестб-23А1') == '23'
    assert parse_group_admission_year_evidence('Выпуск') is None
    assert parse_group_admission_year_evidence('Тестб-2231А1') is None
    assert parse_group_admission_year_evidence(None) is None


# ---- Граница учебного года: 31 августа / 1 сентября ----


def test_academic_year_start_boundary():
    assert academic_year_start(date(2026, 8, 31)) == 2025
    assert academic_year_start(date(2026, 9, 1)) == 2026
    assert academic_year_start(date(2026, 12, 31)) == 2026
    assert academic_year_start(date(2026, 1, 1)) == 2025


def test_academic_year_label():
    assert academic_year_label(2026) == '2026/2027'


def test_derive_course_before_and_after_september_first():
    # 31.08.2026: учебный год 2025/2026, поступившие в 2022 — 4 курс;
    # длительность 4 — обучение НЕ завершено.
    before = derive_course(2022, date(2026, 8, 31), effective_duration_years=4)
    assert before == {
        'status': 'ok',
        'course': 4,
        'academic_year_start': 2025,
        'probably_finished': False,
    }
    # 01.09.2026: тот же студент — уже 5 курс, длительность превышена.
    after = derive_course(2022, date(2026, 9, 1), effective_duration_years=4)
    assert after == {
        'status': 'ok',
        'course': 5,
        'academic_year_start': 2026,
        'probably_finished': True,
    }


def test_derive_course_first_year():
    result = derive_course(2026, date(2026, 10, 3))
    assert result['status'] == 'ok'
    assert result['course'] == 1
    assert result['probably_finished'] is False


def test_derive_course_future_admission():
    # Год поступления позже даты соревнования: курса нет, «0 курс» не выдаём.
    result = derive_course(2027, date(2026, 10, 3), effective_duration_years=4)
    assert result == {
        'status': 'future',
        'course': None,
        'academic_year_start': 2026,
        'probably_finished': False,
    }


def test_derive_course_unknown_admission():
    result = derive_course(None, date(2026, 10, 3))
    assert result == {
        'status': 'unknown',
        'course': None,
        'academic_year_start': None,
        'probably_finished': False,
    }


def test_derive_course_without_duration_never_finished():
    # Длительность неизвестна — «завершено» невозможно ни при каком курсе.
    result = derive_course(2015, date(2026, 10, 3), effective_duration_years=None)
    assert result['status'] == 'ok'
    assert result['course'] == 12
    assert result['probably_finished'] is False


def test_derive_course_duration_boundary_not_finished():
    # Курс РАВЕН длительности — последний курс, обучение не завершено.
    result = derive_course(2022, date(2026, 10, 3), effective_duration_years=5)
    assert result['course'] == 5
    assert result['probably_finished'] is False


# ---- Цепочка эффективной длительности ----


@pytest.mark.parametrize(
    ('override', 'level_default', 'expected'),
    [
        (4, 5, 4),  # override сильнее дефолта уровня
        (None, 5, 5),  # без override — дефолт уровня
        (None, None, None),  # не известно ничего
        (3, None, 3),  # override при отсутствии дефолта
    ],
)
def test_effective_duration_years_fallback_chain(override, level_default, expected):
    assert effective_duration_years(override, level_default) == expected
