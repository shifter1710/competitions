from pydantic import BaseModel


class ReportSliceRow(BaseModel):
    """Строка отчёта для срезов вне «студента» (замечание №19).

    slice_value — значение поля записи (группа, институт, курс, вид спорта,
    уровень или год), по которому сгруппированы метрики. Группировка всегда
    по данным записи — исторический факт, а не текущий профиль студента
    (docs/data-model-decisions.md «Расширение отчётов»).
    """

    slice_value: str | int
    count_participation: int
    count_wins: int
    count_prizes: int
