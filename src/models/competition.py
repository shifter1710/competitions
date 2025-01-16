from datetime import datetime

from pydantic import Field

from src.models.student import Student


class Competition(Student):
    sport: str = Field(alias='Вид спорта')
    date: datetime = Field(alias='Дата')
    level: str = Field(alias='Уровень соревнований')
    name: str = Field(alias='Название соревнований')
    position: int = Field(alias='Место')
    created_at: datetime = Field(default=datetime.utcnow(), alias='Время создания записи (UTC)')

    class Config:
        allow_population_by_field_name = True
