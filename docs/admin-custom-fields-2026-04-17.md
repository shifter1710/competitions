## Admin custom fields and operator role

### Added on 2026-04-17

- A new `editor` role was introduced for data entry.
- `admin` keeps access to field management, destructive actions, and all editor capabilities.
- `viewer` remains read-only.

### Editor capabilities

- Download the empty Excel template from `/template/empty.xlsx`
- Import Excel files
- Add records manually
- Edit existing records

### Admin capabilities

- Create custom fields
- Change custom field labels, types, order, and visibility flags
- Disable custom fields
- Delete competition records
- Clean the whole database

### Data model

- `competitions.extra_data` stores custom field values as JSON
- `custom_fields` stores dynamic field definitions

### Supported custom field types

- `text`
- `number`
- `date`

### Live validation done

- Python modules compile successfully
- SQLite schema migration works on existing databases
- Service rebuilt and started successfully with Docker
- `/healthcheck` returns `200 OK`
- Admin UI shows field management and template download
- Editor UI shows template download but hides field management
