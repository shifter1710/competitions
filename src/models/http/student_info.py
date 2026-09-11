from pydantic import ConfigDict
from pydantic import Field

from src.models.student import Student


class StudentInfo(Student):
    model_config = ConfigDict(populate_by_name=True)

    count_participation: int = Field(alias='Количество участий')
