from datetime import datetime

from pydantic import ConfigDict
from pydantic import Field

from src.models.student import Student


class Competition(Student):
    model_config = ConfigDict(populate_by_name=True)

    record_id: str | None = Field(default=None, alias='_id')
    sport: str = Field(alias='Вид спорта')
    date: datetime = Field(alias='Дата')
    level: str = Field(alias='Уровень соревнований')
    name: str = Field(alias='Название соревнований')
    position: int = Field(alias='Место')
    created_at: datetime = Field(default_factory=datetime.utcnow, alias='Время создания записи (UTC)')
    extra_data: dict[str, str] = Field(default_factory=dict)
    review_status: str = Field(default='approved', alias='Статус проверки')
    review_comment: str = Field(default='', alias='Комментарий проверки')
