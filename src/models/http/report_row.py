from pydantic import BaseModel
from pydantic import Field


class ReportSliceRow(BaseModel):
    """Строка отчёта для срезов вне «студента» (замечание №19, Reports v2).

    slice_value — значение поля записи (группа, институт, курс, вид спорта,
    уровень, год или год поступления), по которому сгруппированы метрики.
    Группировка всегда по данным записи — исторический факт, а не текущий
    профиль студента (docs/data-model-decisions.md «Расширение отчётов»).
    None — группа без значения (например, admission_year IS NULL),
    отображается как «Не указан».

    Reports v2: уникальные участники/призёры считаются ВНУТРИ группы по
    identity-выражению (student_ref_id ?? id записи).
    """

    drilldown_record_id: int | None = Field(default=None, exclude=True)
    slice_value: str | int | None
    count_participation: int
    count_wins: int
    count_prizes: int
    count_unique_participants: int = 0
    count_unique_prizers: int = 0


class ReportSummary(BaseModel):
    """Сводка отчёта по всей отфильтрованной выборке (Reports v2).

    Отдельный агрегатный запрос по тому же WHERE без GROUP BY — источник
    карточек сводки и строки «Итого». Инвариант: уникальные метрики здесь
    НЕ равны сумме уникальных по группам среза (один студент в двух группах
    среза = 2 строки таблицы, но 1 уникальный участник выборки).
    """

    count_events: int = 0
    count_unlinked_participations: int = 0
    count_participation: int = 0
    count_wins: int = 0
    count_prizes: int = 0
    count_unique_participants: int = 0
    count_unique_prizers: int = 0
