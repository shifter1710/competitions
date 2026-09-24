from datetime import datetime

from pydantic import ConfigDict
from pydantic import Field

from src.models.student import Student


class Competition(Student):
    model_config = ConfigDict(populate_by_name=True)

    record_id: str | None = Field(default=None, alias='_id')
    sport: str = Field(alias='Вид спорта')
    date: datetime = Field(alias='Дата')
    # Даты-диапазоны (решение 2026-09-13, docs/data-model-decisions.md
    # «Даты-диапазоны: визуально одно, под капотом два»): date — НАЧАЛО
    # (колонка не переименовывается), date_to — опциональный конец;
    # NULL = однодневное.
    date_to: datetime | None = Field(default=None, alias='Дата по')
    level: str = Field(alias='Уровень соревнований')
    name: str = Field(alias='Название соревнований')
    # Лёгкий реестр полей: Место тоже настраивается как text (строка вместо
    # числа); дефолт (number) — прежнее int-поведение.
    position: int | str = Field(alias='Место')
    created_at: datetime = Field(default_factory=datetime.utcnow, alias='Время создания записи (UTC)')
    extra_data: dict[str, str] = Field(default_factory=dict)
    review_status: str = Field(default='approved', alias='Статус проверки')
    review_comment: str = Field(default='', alias='Комментарий проверки')
    # Стабильная связь записи с карточкой студента (Student Identity v1).
    # Пишется ТОЛЬКО при явном выборе/создании Student (ручное добавление
    # участника события, импорт участников события); существующие записи
    # остаются NULL. Runtime (авторизация атлета, отчёты, легаси-merge)
    # по-прежнему работает по legacy-ключу sha256(ФИО) — Phase 3 не начата.
    student_ref_id: int | None = Field(default=None)
