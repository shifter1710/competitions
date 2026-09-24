"""Общая тестовая фикстура расширенного отчёта (замечание №19).

Данные подобраны так, чтобы метрики пересчитывались руками: каждое место
записи видно в EXPECTED-константах тестов (src/main_test.py, src/storage_test.py).
Козлов выступал за два института — историчность: срезы группируют по данным
записи, а не по профилю.
"""
import sys
from datetime import datetime

from src.models.competition import Competition

# Каждый запрос SanicTestClient выполняет app.run → повторная финализация
# signal-роутера Sanic, и таблица сигналов прирастает дублем (взаимодействие
# sanic-testing/sanic-ext, на проде не проявляется — там один старт).
# Сгенерированное дерево диспетчера глубжеет с каждым дублем, и на большом
# наборе HTTP-тестов ast.fix_missing_locations упирается в дефолтный лимит
# рекурсии. Тестовый процесс — единственное место, где запросов тысячи,
# поэтому лимит поднимаем здесь, а не в приложении.
sys.setrecursionlimit(10000)


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
