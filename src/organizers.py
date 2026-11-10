"""ORG-01: stable organizer metadata and single-line input contract."""
import unicodedata

from pandas import isna

ORGANIZER_LABEL = 'Организатор'
ORGANIZER_MAX_LENGTH = 255
# ':' is outside normalize_custom_field_key's word-character grammar.
ORGANIZER_CUSTOM_COLUMN_KEY = 'custom:organizer'


def validate_organizer(raw) -> str:
    if raw is None or isna(raw):
        return ''
    value = str(raw)
    if any(unicodedata.category(char) in {'Cc', 'Zl', 'Zp'} for char in value):
        raise ValueError(f'Поле "{ORGANIZER_LABEL}" должно содержать одну строку без управляющих символов')
    value = value.strip()
    if len(value) > ORGANIZER_MAX_LENGTH:
        raise ValueError(f'Поле "{ORGANIZER_LABEL}" не должно превышать {ORGANIZER_MAX_LENGTH} символов')
    return value


def organizer_custom_collision(field) -> bool:
    return field.label.strip().casefold() == ORGANIZER_LABEL.casefold()


def custom_field_column_key(field) -> str:
    """Keep a pre-existing custom key distinct from the new fixed table key."""
    return ORGANIZER_CUSTOM_COLUMN_KEY if field.key == 'organizer' else field.key
