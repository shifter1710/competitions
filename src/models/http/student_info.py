from pydantic import Field

from src.models.student import Student


class StudentInfo(Student):
    count_participation: int = Field(alias='Количество участий')
