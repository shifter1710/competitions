from pydantic import ConfigDict
from pydantic import Field

from src.models.student import Student


class StudentInfo(Student):
    model_config = ConfigDict(populate_by_name=True)

    # Метрики отчёта (замечание №19): участия, победы (1 место), призовые
    # (1-3 место). Все три считаются в SQL агрегирующим запросом.
    count_participation: int = Field(alias='Участий')
    count_wins: int = Field(default=0, alias='Побед')
    count_prizes: int = Field(default=0, alias='Призовых')
