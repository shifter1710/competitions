"""Competition-level policy shared by all write paths (no catalog writes)."""
import math


class InvalidCompetitionLevelError(ValueError):
    """A nonempty level is absent from the active administrative catalog."""


def resolve_competition_level(storage, value, *, existing=None):
    """Keep an unchanged historical snapshot; otherwise use an active value.

    Empty means absence (the existing field-required setting remains in force).
    Only the caller's persisted record may supply ``existing``.
    """
    raw = '' if value is None or isinstance(value, float) and math.isnan(value) else str(value)
    if existing is not None and raw == existing:
        return existing
    normalized = raw.strip().lower()
    if not normalized:
        return ''
    for canonical in storage.get_level_names():
        if canonical.strip().lower() == normalized:
            return canonical
    raise InvalidCompetitionLevelError(
        f'Уровень «{raw.strip()}» отсутствует в действующем справочнике. '
        'Выберите доступный уровень; добавить его может администратор.'
    )
