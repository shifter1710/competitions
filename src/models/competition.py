from datetime import datetime

from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from src.education import group_course
from src.education import parse_group_admission_year
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
    # остаются NULL. Runtime: кабинет атлета и права на запись с P3 Runtime
    # Identity читают эту связь (dual: связь + легаси-хеш, ref: только связь,
    # режим app_settings.identity_mode); отчёты, выгрузки и легаси-merge
    # работают по legacy-ключу sha256(ФИО) как раньше.
    student_ref_id: int | None = Field(default=None)
    # Event Model, Wave 1 P0/P1 (целевая архитектура): дисциплина и результат
    # участия + явная ссылка на событие календаря. Колонки существуют и
    # проводятся через storage (insert/select/update), но runtime их НЕ
    # читает: пользовательское поведение в этой волне не меняется.
    # Participation-identity будущих фаз = (calendar_event_id, student_ref_id,
    # discipline); calendar_event_id управляется link/unlink (P2), общий
    # update записи его сохраняет.
    discipline: str | None = Field(default=None)
    result: str | None = Field(default=None)
    calendar_event_id: int | None = Field(default=None)
    # Computed presentation value, never a persisted source snapshot.
    admission_year: int | None = Field(default=None)

    @model_validator(mode='before')
    @classmethod
    def education_from_source_facts(cls, values):
        if isinstance(values, dict):
            values = dict(values)
            group = values.get('Группа', values.get('group', ''))
            reference_date = values.get('Дата', values.get('date'))
            course = group_course(group, reference_date)
            values['Курс'] = course
            values['course'] = course
            values['admission_year'] = parse_group_admission_year(group)
        return values
