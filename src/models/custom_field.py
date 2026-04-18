from pydantic import BaseModel


class CustomField(BaseModel):
    field_id: int
    key: str
    label: str
    field_type: str = 'text'
    required: bool = False
    show_in_table: bool = True
    show_in_export: bool = True
    show_in_template: bool = True
    sort_order: int = 0
    active: bool = True
