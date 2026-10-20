"""Общая тестовая фикстура расширенного отчёта (замечание №19).

Данные подобраны так, чтобы метрики пересчитывались руками: каждое место
записи видно в EXPECTED-константах тестов (src/main_test.py, src/storage_test.py).
Козлов выступал за два института — историчность: срезы группируют по данным
записи, а не по профилю.
"""
from datetime import datetime

import pytest

from src.http_scenarios import fresh_application
from src.http_scenarios import ScenarioClient
from src.models.competition import Competition


@pytest.fixture
def http_client(request, monkeypatch):
    """Fresh app/state per scenario; defer startup until storage fixtures finish."""
    from src import auth
    from src import main
    from src import main_test
    from src.routes.auth import login_failures
    from src.routes.calendar import event_import_sessions
    from src.routes.students import student_import_sessions

    mutable_state = (login_failures, event_import_sessions, student_import_sessions, auth._last_seen_touch)
    previous_state = [dict(state) for state in mutable_state]
    for state in mutable_state:
        state.clear()
    try:
        with fresh_application() as application, monkeypatch.context() as aliases:
            aliases.setattr(main, 'app', application)
            aliases.setattr(main_test, 'app', application)
            if hasattr(request.module, 'app'):
                aliases.setattr(request.module, 'app', application)
            client = ScenarioClient(application)
            try:
                yield client
            finally:
                client.close()
    finally:
        for state, previous in zip(mutable_state, previous_state):
            state.clear()
            state.update(previous)


class FailingStudentsDeleteConnection:
    """Прокси sqlite3-соединения для теста отката delete_student: все
    запросы проходят в реальное соединение, но DELETE FROM students
    падает — сбой ВНУТРИ транзакции, между DELETE псевдонимов и DELETE
    самой карточки. Проверяет, что rollback оставляет карточку и её
    псевдонимы на месте."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        if sql.strip().startswith('DELETE FROM students'):
            raise RuntimeError('Injected failure between alias and student deletes')
        return self._connection.execute(sql, parameters)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()


def make_report_record(
    student_name: str,
    student_sex: str,
    institute: str,
    group: str,
    course: int,
    sport: str,
    date: datetime,
    level: str,
    position: int,
    extra: dict | None = None,
    student_ref_id: int | None = None,
    admission_year: int | None = None,
) -> Competition:
    return Competition(
        student_id=f'id-{student_name}',
        student_name=student_name,
        student_sex=student_sex,
        institute=institute,
        group=group,
        course=course,
        sport=sport,
        date=date,
        level=level,
        name='Кубок',
        position=position,
        extra_data=extra or {},
        # Reports v2: стабильная связь с карточкой студента — источник
        # identity-выражения уникальных метрик и ключа среза «студент».
        student_ref_id=student_ref_id,
        # Reports v2: явный снимок года поступления (для фильтра/среза);
        # по умолчанию None — как у существующих записей без снимка.
        admission_year=admission_year,
    )


def make_report_fixture() -> list[Competition]:
    """Одобренные записи с известными метриками.

    Руками (срез «студент», группировка по ФИО+пол+институт+группа+курс):
      Иванов  (ИСИ, ПГС-101, 1): 3 участия, 1 победа (1-я запись), 2 призовых
      Петров  (ИМИ, СБ-202, 2):  2 участия, 1 победа (5-я запись), 2 призовых
      Козлов  (ИСИ, ПГС-101, 1): 1 участие, 1 победа, 1 призовое
      Козлов  (ИМИ, ТД-303, 2):  1 участие, 0 побед, 1 призовое — другой
                                 институт в записи, строка отдельная
      Сидоров (ИСИ, ПГС-102, 3): 1 участие, 0 побед, 0 призовых
    """
    return [
        make_report_record(
            'Иванов Иван',
            'М',
            'ИСИ',
            'ПГС-101',
            1,
            'Бег',
            datetime(2024, 3, 1),
            'внутривузовские',
            1,
            {'trainer': 'Смит'},
        ),
        make_report_record('Иванов Иван', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2024, 4, 1), 'внутривузовские', 3),
        make_report_record('Иванов Иван', 'М', 'ИСИ', 'ПГС-101', 1, 'Лыжи', datetime(2025, 2, 1), 'межвузовские', 5),
        make_report_record('Петров Пётр', 'Ж', 'ИМИ', 'СБ-202', 2, 'Бег', datetime(2024, 5, 1), 'межвузовские', 2),
        make_report_record('Петров Пётр', 'Ж', 'ИМИ', 'СБ-202', 2, 'Лыжи', datetime(2025, 3, 1), 'внутривузовские', 1),
        make_report_record(
            'Сидоров Сидор', 'М', 'ИСИ', 'ПГС-102', 3, 'Шахматы', datetime(2023, 11, 1), 'внутривузовские', 4
        ),
        make_report_record(
            'Козлов Кирилл', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2023, 5, 1), 'внутривузовские', 1
        ),
        make_report_record('Козлов Кирилл', 'М', 'ИМИ', 'ТД-303', 2, 'Бег', datetime(2025, 6, 1), 'межвузовские', 2),
    ]


def make_report_unapproved_fixture() -> list[tuple[str, Competition]]:
    """Записи, которых в отчёте быть не должно (не approved)."""
    return [
        (
            'pending',
            make_report_record(
                'Наумов Наум', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2024, 6, 1), 'внутривузовские', 2
            ),
        ),
        (
            'rejected',
            make_report_record(
                'Наумов Наум', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2024, 7, 1), 'внутривузовские', 1
            ),
        ),
    ]


def make_report_position_fixture() -> list[Competition]:
    """Одобренные записи с местами 0–5 — классы фильтра места (P1 fix).

    Место 0 — «Без результата» (пустое поле при вводе). Все в одной группе,
    чтобы срез по группе показывал только фильтр места. Даты: места 0–2 —
    учебный год 2024/2025, места 3–5 — 2025/2026 (комбинации с периодом).

    Руками по классам:
      «Победа» (<2):      Первов (1);
      «Призовое» (<4):    Второв (2), Первов (1), Третьяков (3);
      «Не призовое» (>3): Пятов (5), Четвериков (4);
      «Без результата» (0): Нулев (0).
    """
    return [
        make_report_record(
            'Нулев Никита', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2024, 10, 1), 'внутривузовские', 0
        ),
        make_report_record(
            'Первов Павел', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2024, 11, 5), 'внутривузовские', 1
        ),
        make_report_record(
            'Второв Виктор', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2025, 2, 10), 'внутривузовские', 2
        ),
        make_report_record(
            'Третьяков Тимофей', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2025, 9, 15), 'внутривузовские', 3
        ),
        make_report_record(
            'Четвериков Чеслав', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2025, 10, 20), 'внутривузовские', 4
        ),
        make_report_record('Пятов Пётр', 'М', 'ИСИ', 'ПГС-101', 1, 'Бег', datetime(2026, 1, 15), 'внутривузовские', 5),
    ]


def make_report_identity_fixture() -> list[Competition]:
    """Записи с identity-разнообразием для тестов Reports v2 (P1/P2).

    Руками (срез «студент», ключ = identity + ФИО + пол + институт + группа
    + курс):
      Одинцова Ольга (ref 11): Гр-111 ×2 (1 место, 3 место) и Гр-222 ×1
        (2 место) — один человек в двух исторических группах: 3 участия,
        2 строки среза, но 1 уникальный участник выборки;
      Двойникова Дарья (ref 22): Гр-111, 5 место;
      Двойникова Дарья (ref 23): Гр-111, 1 место — полное тёзка с тем же
        снимком (ФИО/пол/институт/группа/курс), другой ref — 2 строки;
      Легасиев Ларион (без ref): Гр-222, место 0 «Без результата» —
        легаси-запись без связи, своя строка и свой identity.
    """
    return [
        make_report_record(
            'Одинцова Ольга',
            'Ж',
            'ИПТ',
            'Гр-111',
            1,
            'Бег',
            datetime(2024, 3, 1),
            'внутривузовские',
            1,
            student_ref_id=11,
        ),
        make_report_record(
            'Одинцова Ольга',
            'Ж',
            'ИПТ',
            'Гр-111',
            1,
            'Бег',
            datetime(2024, 4, 1),
            'внутривузовские',
            3,
            student_ref_id=11,
        ),
        make_report_record(
            'Одинцова Ольга',
            'Ж',
            'ИПТ',
            'Гр-222',
            2,
            'Лыжи',
            datetime(2025, 5, 1),
            'межвузовские',
            2,
            student_ref_id=11,
        ),
        make_report_record(
            'Двойникова Дарья',
            'Ж',
            'ИПТ',
            'Гр-111',
            1,
            'Бег',
            datetime(2024, 6, 1),
            'внутривузовские',
            5,
            student_ref_id=22,
        ),
        make_report_record(
            'Двойникова Дарья',
            'Ж',
            'ИПТ',
            'Гр-111',
            1,
            'Бег',
            datetime(2024, 7, 1),
            'внутривузовские',
            1,
            student_ref_id=23,
        ),
        make_report_record(
            'Легасиев Ларион', 'М', 'ИПТ', 'Гр-222', 2, 'Шахматы', datetime(2025, 8, 1), 'внутривузовские', 0
        ),
    ]


def seed_fixture_levels(storage, *names):
    """Explicit administrative catalog for synthetic write fixtures."""
    existing = storage.get_level_names(include_inactive=True)
    for name in names or ('внутривузовские', 'межвузовские'):
        if name not in existing:
            storage.create_level(name)
            existing.append(name)
