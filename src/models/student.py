from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class Student(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    student_id: str = Field(alias='Код студента')
    student_name: str = Field(alias='ФИО')
    student_sex: str = Field(alias='Пол')
    institute: str = Field(alias='Институт')
    group: str = Field(alias='Группа')
    # Лёгкий реестр полей (решение 2026-09-13, docs/data-model-decisions.md):
    # Курс настраивается как text — хранит строку («Выпускник 2025/26»).
    # pydantic в smart-режиме приводит числовые строки к int, поэтому
    # числовые записи не меняют тип.
    course: int | str = Field(alias='Курс')
