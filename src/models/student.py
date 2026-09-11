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
    course: int = Field(alias='Курс')
