import hmac
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from unittest.mock import Mock
from urllib.parse import quote
from urllib.parse import urlencode

import pandas as pd
import pytest
from sanic_testing.testing import SanicTestClient

from src.auth import hash_password
from src.main import app
from src.main import create_auth_cookie_value
from src.main import normalize_position
from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.models.http.student_info import StudentInfo
from src.settings import settings


def get_xlsx_headers(content: bytes) -> list[str]:
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(content), read_only=True)
    sheet = workbook.worksheets[0]
    row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))
    return [str(value) for value in row if value is not None]


def get_xlsx_rows(content: bytes) -> list[tuple]:
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(content), read_only=True)
    sheet = workbook.worksheets[0]
    return list(sheet.iter_rows(values_only=True))


def get_auth_headers(role: str = 'admin') -> dict[str, str]:
    username = settings.auth_admin_username
    if role == 'editor':
        username = settings.auth_editor_username
    if role == 'viewer':
        username = settings.auth_viewer_username or 'viewer'
    cookie = create_auth_cookie_value(username=username, role=role)
    return {'cookie': f'{settings.auth_cookie_name}={cookie}'}


def csrf_for(headers: dict[str, str]) -> dict[str, str]:
    cookie = headers['cookie'].split('=', 1)[1]
    token = hmac.new(
        settings.auth_secret_key.encode(),
        b'csrf:' + cookie.encode(),
        sha256,
    ).hexdigest()
    return {'csrf_token': token}


def fake_get_user(username: str) -> dict | None:
    if username == 'sportik':
        return {
            'id': 1,
            'username': 'sportik',
            'password_hash': 'x',
            'role': 'athlete',
            'active': 1,
            'pwd_ver': 0,
        }
    accounts = [
        (settings.auth_admin_username, settings.auth_admin_password, 'admin'),
        (settings.auth_editor_username, settings.auth_editor_password, 'editor'),
        (settings.auth_viewer_username, settings.auth_viewer_password, 'viewer'),
    ]
    for account_username, password, role in accounts:
        if username == account_username and password:
            return {
                'id': 1,
                'username': account_username,
                'password_hash': hash_password(password),
                'role': role,
                'active': 1,
                'pwd_ver': 0,
            }
    return None


@pytest.fixture(scope='module')
def client() -> SanicTestClient:
    fake_storage = Mock()
    fake_storage.get_competitions.return_value = []
    fake_storage.get_filtered.return_value = []
    fake_storage.get_custom_fields.return_value = []
    fake_storage.save_competitions.return_value = None
    fake_storage.update_competition.return_value = None
    fake_storage.delete_competition.return_value = None
    fake_storage.create_custom_field.return_value = None
    fake_storage.update_custom_field.return_value = None
    fake_storage.disable_custom_field.return_value = None
    fake_storage.get_competition_review.return_value = None
    fake_storage.get_profile.return_value = {}
    fake_storage.set_profile.return_value = None
    fake_storage.get_name_aliases.return_value = []
    fake_storage.add_name_alias.return_value = None
    fake_storage.count_records_by_student_hash.return_value = 2
    fake_storage.get_student_names.return_value = ['Абрамов Артём Артёмович', 'Иванов Иван Иванович']
    fake_storage.merge_students.return_value = 2
    fake_storage.carry_name_aliases.return_value = 1
    fake_storage.get_attachment.return_value = None
    fake_storage.get_attachments.return_value = []
    fake_storage.create_attachment.return_value = 1
    fake_storage.delete_attachment.return_value = None
    fake_storage.set_competition_review.return_value = None
    fake_storage.get_level_names.return_value = ['внутривузовские', 'межвузовские']
    fake_storage.list_levels.return_value = []
    fake_storage.create_level.return_value = None
    fake_storage.rename_level.return_value = None
    fake_storage.disable_level.return_value = None
    fake_storage.list_catalog.side_effect = lambda category: {
        'sport': ['Бег', 'Лыжи'],
        'institute': ['ИСИ'],
    }.get(category, [])
    fake_storage.list_catalog_all.return_value = []
    fake_storage.add_catalog_value.return_value = None
    fake_storage.get_catalog_value.return_value = None
    fake_storage.hide_catalog_value.return_value = None
    fake_storage.unhide_catalog_value.return_value = None
    fake_storage.delete_catalog_value.return_value = None
    fake_storage.count_records_using.return_value = 0
    fake_storage.get_user.side_effect = fake_get_user
    fake_storage.get_user_by_id.return_value = None
    fake_storage.list_users.return_value = []
    fake_storage.create_user.return_value = None
    fake_storage.set_user_password.return_value = None
    fake_storage.set_user_active.return_value = None
    fake_storage.count_records_by_owner.return_value = 0
    fake_storage.delete_user.return_value = 0
    fake_storage.count_competitions.return_value = 0
    fake_storage.count_attachments.return_value = 0
    fake_storage.delete_all_competitions.return_value = 0
    fake_storage.delete_all_attachments.return_value = 0
    fake_storage.get_competitions_before.return_value = []
    fake_storage.delete_competitions_before.return_value = 0
    fake_storage.get_attachments_for_records.return_value = []
    fake_storage.delete_attachments_for_records.return_value = 0
    fake_storage.vacuum.return_value = None
    fake_storage.get_sport_names.return_value = []
    fake_storage.add_audit_event.return_value = None
    fake_storage.get_audit_events.return_value = []
    fake_storage.count_audit_events.return_value = 0
    fake_storage.list_audit_actions.return_value = []
    app.ctx.storage = fake_storage
    return SanicTestClient(app)


def test_healthcheck(client: SanicTestClient):
    _, response = client.get('/healthcheck')
    assert response.status == 200


def test_get_report_empty(client: SanicTestClient):
    _, response = client.get('/report', headers=get_auth_headers())
    assert response.status == 200


def test_get_report_date(client: SanicTestClient):
    _, response = client.get('/report?date_from=03.02.2021', headers=get_auth_headers())
    assert response.status == 200

    _, response = client.get('/report?date_from=03.02.2021&date_to=04.02.2021', headers=get_auth_headers())
    assert response.status == 200


def test_get_report_position(client: SanicTestClient):
    _, response = client.get('/report?position=>3', headers=get_auth_headers())
    assert response.status == 200

    _, response = client.get('/report?position=<4', headers=get_auth_headers())
    assert response.status == 200


def test_get_report_level(client: SanicTestClient):
    _, response = client.get('/report?level=внутривузовские', headers=get_auth_headers())
    assert response.status == 200


def test_get_report_name(client: SanicTestClient):
    _, response = client.get('/report?name=Карлова', headers=get_auth_headers())
    assert response.status == 200


def test_clean_db_route_removed(client: SanicTestClient):
    # Старый эндпоинт очистки удалён: защищённая версия живёт на /admin/maintenance
    # (docs/data-model-decisions.md). Проверяем через GET: с валидной сессией он
    # доходит до роутера и возвращает 404. POST к несуществующему пути всегда
    # перехватывается глобальным CSRF-middleware (Sanic не парсит form-body для
    # unmatched-маршрутов → 403/401), поэтому 404 по POST недостижим в принципе.
    headers = get_auth_headers(role='admin')
    _, response = client.get('/clean_db', headers=headers, allow_redirects=False)
    assert response.status == 404

    _, response = client.post('/clean_db', headers=headers, data=csrf_for(headers))
    assert response.status == 403


def test_upload_rejects_missing_columns(client: SanicTestClient):
    df = pd.DataFrame([{'ФИО': 'Тест'}])
    file_obj = BytesIO()
    df.to_excel(file_obj, index=False)
    file_obj.seek(0)

    headers = get_auth_headers()
    _, response = client.post(
        '/',
        headers=headers,
        data=csrf_for(headers),
        files={
            'file': (
                'broken.xlsx',
                file_obj.getvalue(),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )
        },
    )
    assert response.status == 400


def test_upload_accepts_text_dates_in_app_format(client: SanicTestClient):
    app.ctx.storage.save_competitions.reset_mock()
    df = pd.DataFrame(
        [
            {
                'ФИО': 'Тестов Тест Тестович',
                'Пол': 'М',
                'Институт': 'ИСИ',
                'Группа': 'ПГС-101',
                'Вид спорта': 'Бег',
                'Дата': '15.03.2026',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Кубок',
                'Место': 1,
                'Курс': 2,
            }
        ]
    )
    file_obj = BytesIO()
    df.to_excel(file_obj, index=False)
    file_obj.seek(0)

    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/',
        headers=headers,
        data=csrf_for(headers),
        files={
            'file': (
                'import.xlsx',
                file_obj.getvalue(),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )
        },
        allow_redirects=False,
    )
    assert response.status == 200
    assert 'Импортировано записей: 1' in response.text
    assert 'дублей' not in response.text
    app.ctx.storage.save_competitions.assert_called_once()
    saved = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert saved.date == datetime(2026, 3, 15)


def test_upload_skips_duplicates(client: SanicTestClient):
    app.ctx.storage.save_competitions.reset_mock()
    app.ctx.storage.get_competitions.return_value = [
        Competition(
            student_id='1',
            student_name='Тестов Тест Тестович',
            student_sex='М',
            institute='ИСИ',
            group='ПГС-101',
            course=2,
            sport='Бег',
            date=datetime(2026, 3, 15),
            level='внутривузовские',
            name='Кубок',
            position=1,
        )
    ]
    df = pd.DataFrame(
        [
            {
                'ФИО': 'Тестов Тест Тестович',
                'Пол': 'М',
                'Институт': 'ИСИ',
                'Группа': 'ПГС-101',
                'Вид спорта': 'Бег',
                'Дата': '15.03.2026',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Кубок',
                'Место': 1,
                'Курс': 2,
            },
            {
                'ФИО': 'Новый Студент',
                'Пол': 'Ж',
                'Институт': 'ИСИ',
                'Группа': 'ПГС-102',
                'Вид спорта': 'Бег',
                'Дата': '16.03.2026',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Кубок',
                'Место': 2,
                'Курс': 1,
            },
        ]
    )
    file_obj = BytesIO()
    df.to_excel(file_obj, index=False)
    file_obj.seek(0)

    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/',
        headers=headers,
        data=csrf_for(headers),
        files={
            'file': (
                'import.xlsx',
                file_obj.getvalue(),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )
        },
    )

    assert response.status == 200
    assert 'Импортировано записей: 1' in response.text
    assert 'Пропущено дублей: 1' in response.text
    saved_rows = app.ctx.storage.save_competitions.call_args[0][0]
    assert len(saved_rows) == 1
    assert saved_rows[0].student_name == 'Новый Студент'
    app.ctx.storage.get_competitions.return_value = []


def test_upload_error_mentions_row_number(client: SanicTestClient):
    df = pd.DataFrame(
        [
            {
                'ФИО': 'Нормальный Студент',
                'Пол': 'М',
                'Институт': 'ИСИ',
                'Группа': 'ПГС-101',
                'Вид спорта': 'Бег',
                'Дата': '15.03.2026',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Кубок',
                'Место': 1,
                'Курс': 2,
            },
            {
                'ФИО': 'Бедный Студент',
                'Пол': 'М',
                'Институт': 'ИСИ',
                'Группа': 'ПГС-101',
                'Вид спорта': 'Бег',
                'Дата': 'не дата',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Кубок',
                'Место': 1,
                'Курс': 2,
            },
        ]
    )
    file_obj = BytesIO()
    df.to_excel(file_obj, index=False)
    file_obj.seek(0)

    headers = get_auth_headers()
    _, response = client.post(
        '/',
        headers=headers,
        data=csrf_for(headers),
        files={
            'file': (
                'import.xlsx',
                file_obj.getvalue(),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )
        },
    )

    assert response.status == 400
    assert 'строка 3' in response.text


def test_editor_can_create_manual_competition(client: SanicTestClient):
    app.ctx.storage.save_competitions.reset_mock()
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Иванов Иван Иванович',
            'student_sex': 'М',
            'institute': 'ИСИ',
            'group': 'ПГС-101',
            'course': '2',
            'sport': 'Легкая атлетика',
            'date': '10.04.2026',
            'level': 'межвузовские',
            'name': 'Весенний кубок',
            'position': '1',
        },
        allow_redirects=False,
    )

    assert response.status == 302
    assert response.headers['location'] == '/'
    app.ctx.storage.save_competitions.assert_called_once()


def test_manual_competition_create_rejects_invalid_date(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/competition',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Иванов Иван Иванович',
            'student_sex': 'М',
            'institute': 'ИСИ',
            'group': 'ПГС-101',
            'course': '2',
            'sport': 'Легкая атлетика',
            'date': '2026-04-10',
            'level': 'межвузовские',
            'name': 'Весенний кубок',
            'position': '1',
        },
        allow_redirects=False,
    )

    assert response.status == 400


def test_editor_can_update_manual_competition(client: SanicTestClient):
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition/123',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Иванов Иван Иванович',
            'student_sex': 'М',
            'institute': 'ИСИ',
            'group': 'ПГС-101',
            'course': '2',
            'sport': 'Легкая атлетика',
            'date': '10.04.2026',
            'level': 'межвузовские',
            'name': 'Весенний кубок',
            'position': '1',
        },
        allow_redirects=False,
    )

    assert response.status == 302
    assert response.headers['location'] == '/'
    app.ctx.storage.update_competition.assert_called_once()
    assert app.ctx.storage.update_competition.call_args[0][0] == 123
    # Правка модератора (editor входит в MODERATOR_ROLES) подтверждает запись.
    app.ctx.storage.set_competition_review.assert_called_once_with(123, 'approved')


def test_moderator_update_pending_competition_approves_it(client: SanicTestClient):
    app.ctx.storage.update_competition.reset_mock()
    app.ctx.storage.set_competition_review.reset_mock()
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition/7',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Иванов Иван Иванович',
            'student_sex': 'М',
            'institute': 'ИСИ',
            'group': 'ПГС-101',
            'course': '2',
            'sport': 'Легкая атлетика',
            'date': '10.04.2026',
            'level': 'межвузовские',
            'name': 'Весенний кубок',
            'position': '1',
        },
        allow_redirects=False,
    )

    assert response.status == 302
    app.ctx.storage.set_competition_review.assert_called_once_with(7, 'approved')


def test_athlete_update_approved_competition_returns_to_pending(client: SanicTestClient):
    app.ctx.storage.update_competition.reset_mock()
    app.ctx.storage.set_competition_review.reset_mock()
    app.ctx.storage.get_competition_review.return_value = {
        'id': 5,
        'review_status': 'approved',
        'owner_id': 1,  # sportik (athlete, id=1) владеет записью
    }
    headers = athlete_headers()
    _, response = client.post(
        '/competition/5',
        headers=headers,
        data={**csrf_for(headers), **ATHLETE_RECORD_DATA},
        allow_redirects=False,
    )

    assert response.status == 302
    app.ctx.storage.update_competition.assert_called_once()
    assert app.ctx.storage.update_competition.call_args[0][0] == 5
    app.ctx.storage.set_competition_review.assert_called_once_with(5, 'pending')
    app.ctx.storage.get_competition_review.return_value = None


def test_admin_can_delete_competition(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/competition/123/delete',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )

    assert response.status == 302
    assert response.headers['location'] == '/'
    app.ctx.storage.delete_competition.assert_called_once_with(123)


def test_editor_cannot_delete_competition(client: SanicTestClient):
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition/abc123/delete',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 403


def test_export_index_omits_dataframe_index_and_created_at(client: SanicTestClient):
    app.ctx.storage.get_competitions.return_value = [
        Competition(
            student_id='1',
            student_name='Test 1',
            student_sex='M',
            institute='Inst',
            group='A',
            course=1,
            sport='Run',
            date=datetime(2024, 1, 1),
            level='межвузовские',
            name='Meet',
            position=1,
        )
    ]

    _, response = client.get('/export/index', headers=get_auth_headers())

    assert response.status == 200
    assert get_xlsx_headers(response.body) == [
        'ФИО',
        'Пол',
        'Институт',
        'Группа',
        'Вид спорта',
        'Дата',
        'Уровень соревнований',
        'Название соревнований',
        'Место',
        'Курс',
    ]


def test_export_index_includes_custom_fields(client: SanicTestClient):
    app.ctx.storage.get_competitions.return_value = [
        Competition(
            student_id='1',
            student_name='Test 1',
            student_sex='M',
            institute='Inst',
            group='A',
            course=1,
            sport='Run',
            date=datetime(2024, 1, 1),
            level='межвузовские',
            name='Meet',
            position=1,
            extra_data={'trainer': 'Coach'},
        )
    ]
    app.ctx.storage.get_custom_fields.return_value = [
        CustomField(field_id=1, key='trainer', label='Тренер', show_in_export=True)
    ]

    _, response = client.get('/export/index', headers=get_auth_headers())

    assert response.status == 200
    assert 'Тренер' in get_xlsx_headers(response.body)


def test_export_report_omits_dataframe_index_and_student_id(client: SanicTestClient):
    app.ctx.storage.get_filtered.return_value = [
        StudentInfo(
            student_id='1',
            student_name='Test 1',
            student_sex='M',
            institute='Inst',
            group='A',
            course=1,
            count_participation=3,
        )
    ]

    _, response = client.get('/export/report', headers=get_auth_headers())

    assert response.status == 200
    assert get_xlsx_headers(response.body) == [
        'ФИО',
        'Пол',
        'Институт',
        'Группа',
        'Курс',
        'Количество участий',
    ]


def make_report_infos() -> list[StudentInfo]:
    return [
        StudentInfo(
            student_id='1',
            student_name='Иванов Иван',
            student_sex='М',
            institute='ИСИ',
            group='А-101',
            course=2,
            count_participation=5,
        ),
        StudentInfo(
            student_id='2',
            student_name='Петров Пётр',
            student_sex='Ж',
            institute='ИМИ',
            group='Б-202',
            course=1,
            count_participation=1,
        ),
    ]


def test_export_report_returns_rows_in_report_order(client: SanicTestClient):
    app.ctx.storage.get_filtered.return_value = make_report_infos()

    _, response = client.get('/export/report', headers=get_auth_headers())

    assert response.status == 200
    assert get_xlsx_rows(response.body) == [
        ('ФИО', 'Пол', 'Институт', 'Группа', 'Курс', 'Количество участий'),
        ('Иванов Иван', 'М', 'ИСИ', 'А-101', 2, 5),
        ('Петров Пётр', 'Ж', 'ИМИ', 'Б-202', 1, 1),
    ]


def test_export_report_with_column_subset(client: SanicTestClient):
    app.ctx.storage.get_filtered.return_value = make_report_infos()

    _, response = client.get(
        '/export/report?' + urlencode({'columns': ['ФИО', 'Количество участий']}, doseq=True),
        headers=get_auth_headers(),
    )

    assert response.status == 200
    assert get_xlsx_rows(response.body) == [
        ('ФИО', 'Количество участий'),
        ('Иванов Иван', 5),
        ('Петров Пётр', 1),
    ]


def test_export_report_accepts_comma_separated_columns(client: SanicTestClient):
    app.ctx.storage.get_filtered.return_value = make_report_infos()

    _, response = client.get(
        '/export/report?columns=' + quote('Группа,ФИО'),
        headers=get_auth_headers(),
    )

    # порядок колонок всегда канонический, как в HTML-отчёте
    assert response.status == 200
    assert get_xlsx_rows(response.body) == [
        ('ФИО', 'Группа'),
        ('Иванов Иван', 'А-101'),
        ('Петров Пётр', 'Б-202'),
    ]


def test_export_report_requires_at_least_one_column(client: SanicTestClient):
    # значение из пробелов: параметр есть, валидных колонок нет — 400
    _, response = client.get('/export/report?columns=%20', headers=get_auth_headers())
    assert response.status == 400

    _, response = client.get('/export/report?columns=' + quote('Нет такой колонки'), headers=get_auth_headers())
    assert response.status == 400


def test_export_report_applies_report_filters(client: SanicTestClient):
    app.ctx.storage.get_filtered.reset_mock()

    _, response = client.get(
        '/export/report?date_from=01.02.2024&name=Иван&position=<4',
        headers=get_auth_headers(),
    )

    assert response.status == 200
    assert app.ctx.storage.get_filtered.call_args[1] == {
        'date_from': '01.02.2024',
        'date_to': None,
        'position': '<4',
        'level': None,
        'name': 'Иван',
        'custom_filters': [],
    }


def test_export_report_available_for_viewer(client: SanicTestClient):
    _, response = client.get(
        '/export/report?' + urlencode({'columns': ['ФИО']}, doseq=True),
        headers=get_auth_headers(role='viewer'),
    )
    assert response.status == 200


def test_reports_page_shows_export_column_panel(client: SanicTestClient):
    _, response = client.get('/reports', headers=get_auth_headers())

    assert response.status == 200
    assert 'report-export-form' in response.text
    assert 'action="/export/report"' in response.text
    for column in ('ФИО', 'Пол', 'Институт', 'Группа', 'Курс', 'Количество участий'):
        assert f'value="{column}"' in response.text


def test_empty_template_includes_custom_fields(client: SanicTestClient):
    app.ctx.storage.get_custom_fields.return_value = [
        CustomField(field_id=1, key='trainer', label='Тренер', show_in_template=True)
    ]

    _, response = client.get('/template/empty.xlsx', headers=get_auth_headers(role='editor'))

    assert response.status == 200
    assert 'Тренер' in get_xlsx_headers(response.body)


def test_admin_can_create_custom_field(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/fields',
        headers=headers,
        data={
            **csrf_for(headers),
            'label': 'Тренер',
            'field_type': 'text',
            'required': 'on',
            'show_in_table': 'on',
            'show_in_export': 'on',
            'show_in_template': 'on',
            'sort_order': '10',
        },
        allow_redirects=False,
    )

    assert response.status == 302
    app.ctx.storage.create_custom_field.assert_called_once()


def test_viewer_cannot_manage_fields(client: SanicTestClient):
    headers = get_auth_headers(role='viewer')
    _, response = client.post(
        '/admin/fields',
        headers=headers,
        data={**csrf_for(headers), 'label': 'Тренер', 'field_type': 'text'},
        allow_redirects=False,
    )
    assert response.status == 403


def test_normalize_position_allows_multiple_digits():
    assert normalize_position('12') == 12


def test_requires_auth_for_main_page(client: SanicTestClient):
    _, response = client.get('/', allow_redirects=False)

    assert response.status == 302
    assert response.headers['location'] == '/login'


def test_login_sets_auth_cookie(client: SanicTestClient):
    _, response = client.post(
        '/login',
        data={
            'username': settings.auth_admin_username,
            'password': settings.auth_admin_password,
        },
        allow_redirects=False,
    )

    assert response.status == 302
    assert response.headers['location'] == '/'
    assert settings.auth_cookie_name in response.headers.get('set-cookie', '')


def test_competition_created_at_default_factory():
    first = Competition(
        student_id='1',
        student_name='Test 1',
        student_sex='M',
        institute='Inst',
        group='A',
        course=1,
        sport='Run',
        date=datetime(2024, 1, 1),
        level='межвузовские',
        name='Meet',
        position=1,
    )
    second = Competition(
        student_id='2',
        student_name='Test 2',
        student_sex='F',
        institute='Inst',
        group='B',
        course=2,
        sport='Jump',
        date=datetime(2024, 1, 2),
        level='внутривузовские',
        name='Cup',
        position=2,
    )

    assert second.created_at >= first.created_at


def test_report_rejects_invalid_date(client: SanicTestClient):
    _, response = client.get('/report?date_from=garbage', headers=get_auth_headers())
    assert response.status == 400


def test_report_rejects_position_without_sign(client: SanicTestClient):
    _, response = client.get('/report?position=5', headers=get_auth_headers())
    assert response.status == 400


def test_report_rejects_position_with_bad_value(client: SanicTestClient):
    _, response = client.get('/report?position=%3Eabc', headers=get_auth_headers())
    assert response.status == 400


def test_update_competition_rejects_non_numeric_id(client: SanicTestClient):
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition/abc',
        headers=headers,
        data={**csrf_for(headers), 'student_name': 'X', 'course': '1', 'date': '10.04.2026'},
        allow_redirects=False,
    )
    assert response.status == 400


def test_delete_custom_field_rejects_non_numeric_id(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/fields/abc/delete',
        headers=headers,
        data=csrf_for(headers),
    )
    assert response.status == 400


def test_update_custom_field_rejects_invalid_sort_order(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/fields/1',
        headers=headers,
        data={**csrf_for(headers), 'label': 'Поле', 'field_type': 'text', 'sort_order': 'abc'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']


def test_update_custom_field_rejects_empty_label(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/fields/1',
        headers=headers,
        data={**csrf_for(headers), 'label': '', 'field_type': 'text'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']


def test_post_without_csrf_token_is_rejected(client: SanicTestClient):
    _, response = client.post(
        '/competition/123/delete',
        headers=get_auth_headers(),
        allow_redirects=False,
    )
    assert response.status == 403


def test_export_index_neutralizes_formula_values(client: SanicTestClient):
    from openpyxl import load_workbook

    app.ctx.storage.get_competitions.return_value = [
        Competition(
            student_id='1',
            student_name='=HYPERLINK("http://evil.example")',
            student_sex='M',
            institute='Inst',
            group='A',
            course=1,
            sport='Run',
            date=datetime(2024, 1, 1),
            level='межвузовские',
            name='Meet',
            position=1,
        )
    ]

    _, response = client.get('/export/index', headers=get_auth_headers())

    assert response.status == 200
    workbook = load_workbook(BytesIO(response.body), read_only=True)
    cell_value = next(workbook.worksheets[0].iter_rows(min_row=2, max_row=2, values_only=True))[0]
    assert cell_value == "'=HYPERLINK(\"http://evil.example\")"


def test_expired_auth_cookie_is_rejected(client: SanicTestClient):
    from time import time as time_time

    expired_cookie = create_auth_cookie_value(
        username=settings.auth_admin_username,
        role='admin',
        issued_at=int(time_time()) - settings.auth_session_ttl_seconds - 10,
    )
    _, response = client.get(
        '/',
        headers={'cookie': f'{settings.auth_cookie_name}={expired_cookie}'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert response.headers['location'] == '/login'


def test_login_rate_limit_locks_out_after_failures(client: SanicTestClient):
    for _ in range(5):
        client.post(
            '/login',
            data={'username': settings.auth_admin_username, 'password': 'wrong'},
            allow_redirects=False,
        )

    _, response = client.post(
        '/login',
        data={
            'username': settings.auth_admin_username,
            'password': settings.auth_admin_password,
        },
        allow_redirects=False,
    )
    assert response.status == 429


def test_admin_can_create_user(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/users',
        headers=headers,
        data={**csrf_for(headers), 'username': 'operator2', 'password': 'secret123', 'role': 'editor'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.create_user.assert_called_once()
    args = app.ctx.storage.create_user.call_args[0]
    assert args[0] == 'operator2'
    assert args[2] == 'editor'


def test_create_user_rejects_short_password(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/users',
        headers=headers,
        data={**csrf_for(headers), 'username': 'operator3', 'password': '123', 'role': 'editor'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']


def test_admin_can_reset_user_password(client: SanicTestClient):
    app.ctx.storage.get_user_by_id.return_value = {
        'id': 7,
        'username': 'someone',
        'password_hash': 'x',
        'role': 'editor',
        'active': 1,
    }
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/users/7/password',
        headers=headers,
        data={**csrf_for(headers), 'password': 'newsecret123'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.set_user_password.assert_called_once()
    app.ctx.storage.get_user_by_id.return_value = None


def test_admin_cannot_deactivate_self(client: SanicTestClient):
    app.ctx.storage.get_user_by_id.return_value = {
        'id': 7,
        'username': settings.auth_admin_username,
        'password_hash': 'x',
        'role': 'admin',
        'active': 1,
    }
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/users/7/active',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']
    app.ctx.storage.set_user_active.assert_not_called()
    app.ctx.storage.get_user_by_id.return_value = None


def delete_user_request(client, headers, user_id, confirm_username):
    return client.post(
        f'/admin/users/{user_id}/delete',
        headers=headers,
        data={**csrf_for(headers), 'confirm_username': confirm_username},
        allow_redirects=False,
    )


def test_admin_cannot_delete_self(client: SanicTestClient):
    app.ctx.storage.get_user_by_id.return_value = {
        'id': 7,
        'username': settings.auth_admin_username,
        'password_hash': 'x',
        'role': 'admin',
        'active': 1,
    }
    app.ctx.storage.delete_user.reset_mock()
    headers = get_auth_headers()
    _, response = delete_user_request(client, headers, 7, settings.auth_admin_username)

    assert response.status == 302
    assert 'admin_error' in response.headers['location']
    app.ctx.storage.delete_user.assert_not_called()
    app.ctx.storage.get_user_by_id.return_value = None


def test_admin_cannot_delete_last_active_admin(client: SanicTestClient):
    app.ctx.storage.get_user_by_id.return_value = {
        'id': 9,
        'username': 'admin2',
        'password_hash': 'x',
        'role': 'admin',
        'active': 1,
    }
    # Единственный активный админ — сама цель удаления.
    app.ctx.storage.list_users.return_value = [{'id': 9, 'username': 'admin2', 'role': 'admin', 'active': 1}]
    app.ctx.storage.delete_user.reset_mock()
    headers = get_auth_headers()
    _, response = delete_user_request(client, headers, 9, 'admin2')

    assert response.status == 302
    assert 'admin_error' in response.headers['location']
    app.ctx.storage.delete_user.assert_not_called()

    # Неактивного админа удалить можно: активных админов останется больше нуля.
    app.ctx.storage.get_user_by_id.return_value['active'] = 0
    _, response = delete_user_request(client, headers, 9, 'admin2')
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.delete_user.assert_called_once()

    app.ctx.storage.get_user_by_id.return_value = None
    app.ctx.storage.list_users.return_value = []


def test_delete_user_rejects_wrong_confirm_username(client: SanicTestClient):
    app.ctx.storage.get_user_by_id.return_value = {
        'id': 7,
        'username': 'operator9',
        'password_hash': 'x',
        'role': 'editor',
        'active': 1,
    }
    app.ctx.storage.list_users.return_value = [
        {'id': 1, 'username': 'admin', 'role': 'admin', 'active': 1},
        {'id': 7, 'username': 'operator9', 'role': 'editor', 'active': 1},
    ]
    app.ctx.storage.delete_user.reset_mock()
    headers = get_auth_headers()

    _, response = delete_user_request(client, headers, 7, 'wrong-login')
    assert response.status == 302
    assert 'admin_error' in response.headers['location']

    _, response = delete_user_request(client, headers, 7, '')
    assert response.status == 302
    assert 'admin_error' in response.headers['location']

    app.ctx.storage.delete_user.assert_not_called()
    app.ctx.storage.get_user_by_id.return_value = None
    app.ctx.storage.list_users.return_value = []


def test_admin_can_delete_user(client: SanicTestClient):
    app.ctx.storage.get_user_by_id.return_value = {
        'id': 7,
        'username': 'operator9',
        'password_hash': 'x',
        'role': 'editor',
        'active': 1,
    }
    app.ctx.storage.list_users.return_value = [
        {'id': 1, 'username': 'admin', 'role': 'admin', 'active': 1},
        {'id': 7, 'username': 'operator9', 'role': 'editor', 'active': 1},
    ]
    app.ctx.storage.delete_user.return_value = 3
    app.ctx.storage.delete_user.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()
    headers = get_auth_headers()

    _, response = delete_user_request(client, headers, 7, 'operator9')

    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    assert 'admin_message' in response.headers['location']
    app.ctx.storage.delete_user.assert_called_once_with(7)
    assert (
        'user_deleted',
        {'target_user_id': 7, 'target_username': 'operator9', 'records_detached': 3},
    ) in audit_calls()

    app.ctx.storage.get_user_by_id.return_value = None
    app.ctx.storage.list_users.return_value = []
    app.ctx.storage.delete_user.return_value = 0


def test_delete_user_rejects_non_numeric_id(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/users/abc/delete',
        headers=headers,
        data=csrf_for(headers),
    )
    assert response.status == 400


def test_delete_user_forbidden_for_non_admin(client: SanicTestClient):
    editor_headers = get_auth_headers(role='editor')
    _, response = delete_user_request(client, editor_headers, 7, 'operator9')
    assert response.status == 403

    viewer_headers = get_auth_headers(role='viewer')
    _, response = delete_user_request(client, viewer_headers, 7, 'operator9')
    assert response.status == 403


def test_editor_can_approve_pending_record(client: SanicTestClient):
    app.ctx.storage.get_competition_review.return_value = {
        'id': 5,
        'review_status': 'pending',
        'owner_id': 2,
    }
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition/5/review/approve',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 302
    app.ctx.storage.set_competition_review.assert_called_with(5, 'approved', '')
    app.ctx.storage.get_competition_review.return_value = None


def test_editor_can_reject_record_with_comment(client: SanicTestClient):
    app.ctx.storage.get_competition_review.return_value = {
        'id': 5,
        'review_status': 'pending',
        'owner_id': 2,
    }
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition/5/review/reject',
        headers=headers,
        data={**csrf_for(headers), 'comment': 'укажите верное место'},
        allow_redirects=False,
    )
    assert response.status == 302
    app.ctx.storage.set_competition_review.assert_called_with(5, 'rejected', 'укажите верное место')
    app.ctx.storage.get_competition_review.return_value = None


def test_viewer_cannot_review(client: SanicTestClient):
    headers = get_auth_headers(role='viewer')
    _, response = client.post(
        '/competition/5/review/approve',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 403


def test_review_rejects_unknown_decision(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/competition/5/review/delete',
        headers=headers,
        data=csrf_for(headers),
    )
    assert response.status == 400


def test_login_verifies_password_hash(client: SanicTestClient):
    from src.main import login_failures

    login_failures.clear()
    _, response = client.post(
        '/login',
        data={'username': settings.auth_editor_username, 'password': settings.auth_editor_password},
        allow_redirects=False,
    )
    assert response.status == 302

    _, response = client.post(
        '/login',
        data={'username': settings.auth_editor_username, 'password': 'definitely-wrong'},
        allow_redirects=False,
    )
    assert response.status == 401


def test_stale_pwd_ver_cookie_is_rejected(client: SanicTestClient):
    stale_cookie = create_auth_cookie_value(
        username=settings.auth_admin_username,
        role='admin',
        pwd_ver=1,
    )
    _, response = client.get(
        '/',
        headers={'cookie': f'{settings.auth_cookie_name}={stale_cookie}'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert response.headers['location'] == '/login'


def test_template_date_column_is_typed(client: SanicTestClient):
    from openpyxl import load_workbook

    _, response = client.get('/template/empty.xlsx', headers=get_auth_headers(role='editor'))
    assert response.status == 200
    workbook = load_workbook(BytesIO(response.body))
    sheet = workbook.worksheets[0]
    date_column = get_xlsx_headers(response.body).index('Дата') + 1
    assert sheet.cell(row=1, column=date_column).number_format == 'DD.MM.YYYY'


def test_import_auto_adds_unknown_level(client: SanicTestClient):
    app.ctx.storage.create_level.reset_mock()
    df = pd.DataFrame(
        [
            {
                'ФИО': 'Лыжников Лыж Лыжович',
                'Пол': 'М',
                'Институт': 'ИСИ',
                'Группа': 'ЛС-101',
                'Вид спорта': 'Лыжи',
                'Дата': '01.02.2026',
                'Уровень соревнований': 'всероссийские',
                'Название соревнований': 'Чемпионат РФ',
                'Место': 5,
                'Курс': 1,
            }
        ]
    )
    file_obj = BytesIO()
    df.to_excel(file_obj, index=False)
    file_obj.seek(0)

    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/',
        headers=headers,
        data=csrf_for(headers),
        files={
            'file': (
                'import.xlsx',
                file_obj.getvalue(),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )
        },
    )

    assert response.status == 200
    app.ctx.storage.create_level.assert_called_once_with('всероссийские')


def test_admin_can_create_level(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/levels',
        headers=headers,
        data={**csrf_for(headers), 'name': 'городские'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.create_level.assert_called_with('городские')


def test_admin_cannot_create_duplicate_level(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/levels',
        headers=headers,
        data={**csrf_for(headers), 'name': 'внутривузовские'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']


def test_admin_catalogs_page_available_for_admin_only(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.get('/admin/catalogs', headers=headers)
    assert response.status == 200
    for title in ('Виды спорта', 'Институты', 'Уровни'):
        assert title in response.text
    assert 'action="/admin/catalogs/sport"' in response.text
    assert 'action="/admin/catalogs/institute"' in response.text
    assert 'action="/admin/levels"' in response.text  # секция уровней перенесена сюда

    _, response = client.get('/admin/catalogs', headers=get_auth_headers(role='editor'))
    assert response.status == 403

    _, response = client.get('/admin/catalogs', headers=get_auth_headers(role='viewer'))
    assert response.status == 403

    _, response = client.get('/admin/catalogs', headers=athlete_headers())
    assert response.status == 403

    _, response = client.get('/admin/catalogs', allow_redirects=False)
    assert response.status == 302
    assert response.headers['location'] == '/login'


def test_old_levels_url_redirects_to_catalogs(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.get('/admin/levels', headers=headers, allow_redirects=False)
    assert response.status == 302
    assert response.headers['location'] == '/admin/catalogs'

    # flash-параметры сохраняются в редиректе (query string как есть)
    _, response = client.get('/admin/levels?admin_message=готово', headers=headers, allow_redirects=False)
    expected_query = urlencode({'admin_message': 'готово'})
    assert response.headers['location'] == f'/admin/catalogs?{expected_query}'


def test_admin_can_add_catalog_value(client: SanicTestClient):
    app.ctx.storage.add_catalog_value.reset_mock()
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/catalogs/sport',
        headers=headers,
        data={**csrf_for(headers), 'value': 'Плавание'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.add_catalog_value.assert_called_once_with('sport', 'Плавание')

    _, response = client.post(
        '/admin/catalogs/sport',
        headers=headers,
        data={**csrf_for(headers), 'value': '   '},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']


def test_catalog_value_actions_forbidden_for_non_admin(client: SanicTestClient):
    editor_headers = get_auth_headers(role='editor')
    for path in ('/admin/catalogs/sport', '/admin/catalogs/sport/5/hide', '/admin/catalogs/sport/5/delete'):
        _, response = client.post(path, headers=editor_headers, data=csrf_for(editor_headers))
        assert response.status == 403, path


def test_catalog_hide_unhide_endpoints(client: SanicTestClient):
    app.ctx.storage.get_catalog_value.return_value = {'id': 5, 'category': 'sport', 'value': 'Лыжи', 'active': 1}
    app.ctx.storage.hide_catalog_value.reset_mock()
    app.ctx.storage.unhide_catalog_value.reset_mock()
    headers = get_auth_headers()

    _, response = client.post(
        '/admin/catalogs/sport/5/hide', headers=headers, data=csrf_for(headers), allow_redirects=False
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.hide_catalog_value.assert_called_once_with(5)

    _, response = client.post(
        '/admin/catalogs/sport/5/unhide', headers=headers, data=csrf_for(headers), allow_redirects=False
    )
    assert response.status == 302
    app.ctx.storage.unhide_catalog_value.assert_called_once_with(5)

    app.ctx.storage.get_catalog_value.return_value = None


def test_catalog_delete_rejects_value_with_records(client: SanicTestClient):
    app.ctx.storage.get_catalog_value.return_value = {'id': 5, 'category': 'sport', 'value': 'Бег', 'active': 1}
    app.ctx.storage.count_records_using.return_value = 3
    app.ctx.storage.delete_catalog_value.reset_mock()
    headers = get_auth_headers()

    _, response = client.post('/admin/catalogs/sport/5/delete', headers=headers, data=csrf_for(headers))
    assert response.status == 400
    app.ctx.storage.delete_catalog_value.assert_not_called()

    app.ctx.storage.count_records_using.return_value = 0
    _, response = client.post(
        '/admin/catalogs/sport/5/delete', headers=headers, data=csrf_for(headers), allow_redirects=False
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.delete_catalog_value.assert_called_once_with(5)

    app.ctx.storage.get_catalog_value.return_value = None
    app.ctx.storage.count_records_using.return_value = 0


def test_create_competition_auto_adds_catalog_values(client: SanicTestClient):
    app.ctx.storage.add_catalog_value.reset_mock()
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Иванов Иван Иванович',
            'student_sex': 'М',
            'institute': 'ФМА',
            'group': 'ПГС-101',
            'course': '2',
            'sport': 'Плавание',
            'date': '10.04.2026',
            'level': 'межвузовские',
            'name': 'Кубок',
            'position': '1',
        },
        allow_redirects=False,
    )
    assert response.status == 302
    added = {(call[0][0], call[0][1]) for call in app.ctx.storage.add_catalog_value.call_args_list}
    assert ('sport', 'Плавание') in added
    assert ('institute', 'ФМА') in added


def test_import_auto_adds_catalog_values(client: SanicTestClient):
    app.ctx.storage.add_catalog_value.reset_mock()
    app.ctx.storage.get_competitions.return_value = []
    df = pd.DataFrame(
        [
            {
                'ФИО': 'Шахматистов Шах Шахович',
                'Пол': 'М',
                'Институт': 'АДИ',
                'Группа': 'ША-101',
                'Вид спорта': 'Шахматы',
                'Дата': '01.02.2026',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Турнир',
                'Место': 2,
                'Курс': 1,
            }
        ]
    )
    file_obj = BytesIO()
    df.to_excel(file_obj, index=False)
    file_obj.seek(0)

    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/',
        headers=headers,
        data=csrf_for(headers),
        files={
            'file': (
                'import.xlsx',
                file_obj.getvalue(),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )
        },
    )
    assert response.status == 200
    added = {(call[0][0], call[0][1]) for call in app.ctx.storage.add_catalog_value.call_args_list}
    assert ('sport', 'Шахматы') in added
    assert ('institute', 'АДИ') in added


def test_index_datalists_show_active_catalog_values(client: SanicTestClient):
    # фикстура: list_catalog('sport') → ['Бег', 'Лыжи'], list_catalog('institute') → ['ИСИ']
    _, response = client.get('/', headers=get_auth_headers(role='editor'))
    assert response.status == 200
    assert '<datalist id="sport-options">' in response.text
    assert '<datalist id="institute-options">' in response.text
    assert 'list="sport-options"' in response.text  # ручная форма ввода
    assert 'list="institute-options"' in response.text
    assert '<option value="Бег"></option>' in response.text
    assert '<option value="Лыжи"></option>' in response.text
    assert '<option value="ИСИ"></option>' in response.text
    called_categories = {call[0][0] for call in app.ctx.storage.list_catalog.call_args_list}
    assert {'sport', 'institute'} <= called_categories


def test_hidden_catalog_value_not_in_datalist(client: SanicTestClient):
    # list_catalog отдаёт только активные значения — скрытое в подсказки не попадает
    original_side_effect = app.ctx.storage.list_catalog.side_effect
    app.ctx.storage.list_catalog.side_effect = lambda category: {'sport': ['Бег']}.get(category, [])
    try:
        _, response = client.get('/', headers=get_auth_headers(role='editor'))
        assert response.status == 200
        assert '<option value="Бег"></option>' in response.text
        assert '<option value="Лыжи"></option>' not in response.text
    finally:
        app.ctx.storage.list_catalog.side_effect = original_side_effect


def test_url_custom_field_validated(client: SanicTestClient):
    app.ctx.storage.get_custom_fields.return_value = [
        CustomField(field_id=1, key='link', label='Ссылка', field_type='url')
    ]
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Иванов Иван Иванович',
            'student_sex': 'М',
            'institute': 'ИСИ',
            'group': 'ПГС-101',
            'course': '2',
            'sport': 'Легкая атлетика',
            'date': '10.04.2026',
            'level': 'межвузовские',
            'name': 'Кубок',
            'position': '1',
            'custom__link': 'ftp://bad.example',
        },
        allow_redirects=False,
    )
    assert response.status == 400

    _, response = client.post(
        '/competition',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Иванов Иван Иванович',
            'student_sex': 'М',
            'institute': 'ИСИ',
            'group': 'ПГС-101',
            'course': '2',
            'sport': 'Легкая атлетика',
            'date': '10.04.2026',
            'level': 'межвузовские',
            'name': 'Кубок',
            'position': '1',
            'custom__link': 'https://example.com/results',
        },
        allow_redirects=False,
    )
    assert response.status == 302
    app.ctx.storage.get_custom_fields.return_value = []


PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'0' * 32


def upload_attachment(client, headers, filename, payload):
    return client.post(
        '/competition/3/attachments',
        headers=headers,
        data=csrf_for(headers),
        files={'file': (filename, payload, 'application/octet-stream')},
    )


def test_attachment_upload_and_download(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    app.ctx.storage.get_competition_review.return_value = {'id': 3, 'review_status': 'approved', 'owner_id': 1}
    app.ctx.storage.create_attachment.return_value = 42
    app.ctx.storage.get_attachment.return_value = {
        'id': 42,
        'record_id': 3,
        'filename': 'diploma.png',
        'stored_name': 'stored.png',
        'content_type': 'image/png',
        'size': len(PNG_BYTES),
    }
    headers = get_auth_headers(role='editor')

    _, response = upload_attachment(client, headers, 'diploma.png', PNG_BYTES)
    assert response.status == 200
    assert (tmp_path / 'files' / '3' / 'stored-name-unused').exists() is False  # имя генерится
    app.ctx.storage.create_attachment.assert_called_once()
    stored_name = app.ctx.storage.create_attachment.call_args[1]['stored_name']
    assert (tmp_path / 'files' / '3' / stored_name).is_file()

    (tmp_path / 'files' / '3' / 'stored.png').write_bytes(PNG_BYTES)
    _, response = client.get('/attachment/42', headers=get_auth_headers())
    assert response.status == 200
    assert response.body == PNG_BYTES

    app.ctx.storage.get_competition_review.return_value = None
    app.ctx.storage.get_attachment.return_value = None


def test_attachment_rejects_wrong_extension(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    app.ctx.storage.get_competition_review.return_value = {'id': 3, 'review_status': 'approved', 'owner_id': 1}
    headers = get_auth_headers(role='editor')

    _, response = upload_attachment(client, headers, 'notes.txt', b'hello')
    assert response.status == 400
    app.ctx.storage.get_competition_review.return_value = None


def test_attachment_rejects_fake_signature(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    app.ctx.storage.get_competition_review.return_value = {'id': 3, 'review_status': 'approved', 'owner_id': 1}
    headers = get_auth_headers(role='editor')

    _, response = upload_attachment(client, headers, 'fake.png', b'%PDF-1.4 not a png')
    assert response.status == 400
    app.ctx.storage.get_competition_review.return_value = None


def test_attachment_delete_removes_file(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    stored = tmp_path / 'files' / '3' / 'stored.png'
    stored.parent.mkdir(parents=True)
    stored.write_bytes(PNG_BYTES)
    app.ctx.storage.get_attachment.return_value = {
        'id': 42,
        'record_id': 3,
        'filename': 'diploma.png',
        'stored_name': 'stored.png',
        'content_type': 'image/png',
        'size': len(PNG_BYTES),
    }
    headers = get_auth_headers()
    _, response = client.post('/attachment/42/delete', headers=headers, data=csrf_for(headers), allow_redirects=False)
    assert response.status == 302
    assert not stored.exists()
    app.ctx.storage.get_attachment.return_value = None


def athlete_headers() -> dict[str, str]:
    cookie = create_auth_cookie_value(username='sportik', role='athlete')
    return {'cookie': f'{settings.auth_cookie_name}={cookie}'}


ATHLETE_RECORD_DATA = {
    'student_name': 'Спортсменов Спорт Спортович',
    'student_sex': 'М',
    'institute': 'ИСИ',
    'group': 'СБ-101',
    'course': '1',
    'sport': 'Бег',
    'date': '10.04.2026',
    'level': 'внутривузовские',
    'name': 'Кубок',
    'position': '2',
}


def test_athlete_create_goes_to_moderation(client: SanicTestClient):
    app.ctx.storage.save_competitions.reset_mock()
    app.ctx.storage.get_competitions.return_value = []
    headers = athlete_headers()
    _, response = client.post(
        '/competition',
        headers=headers,
        data={**csrf_for(headers), **ATHLETE_RECORD_DATA},
        allow_redirects=False,
    )
    assert response.status == 302
    kwargs = app.ctx.storage.save_competitions.call_args[1]
    assert kwargs['review_status'] == 'pending'
    app.ctx.storage.get_competitions.return_value = []


def test_athlete_sees_only_own_records(client: SanicTestClient):
    app.ctx.storage.get_competitions.reset_mock()
    headers = athlete_headers()
    _, response = client.get('/', headers=headers)
    assert response.status == 200
    assert app.ctx.storage.get_competitions.call_args[1] == {'owner_id': 1, 'student_id_hashes': []}


def test_athlete_cannot_update_foreign_record(client: SanicTestClient):
    app.ctx.storage.get_competition_review.return_value = {
        'id': 9,
        'review_status': 'approved',
        'owner_id': 42,
    }
    headers = athlete_headers()
    _, response = client.post(
        '/competition/9',
        headers=headers,
        data={**csrf_for(headers), **ATHLETE_RECORD_DATA},
        allow_redirects=False,
    )
    assert response.status == 403
    app.ctx.storage.get_competition_review.return_value = None


def test_athlete_cannot_import_review_or_report(client: SanicTestClient):
    headers = athlete_headers()
    _, response = client.post(
        '/',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 403

    _, response = client.post(
        '/competition/5/review/approve',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 403

    _, response = client.get('/report', headers=headers)
    assert response.status == 403

    _, response = client.get('/export/index', headers=headers)
    assert response.status == 403

    _, response = client.get('/export/report', headers=headers)
    assert response.status == 403


def test_profile_save_and_fetch(client: SanicTestClient):
    headers = athlete_headers()
    _, response = client.post(
        '/api/profile',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Спортсменов Спорт Спортович',
            'student_sex': 'М',
            'institute': 'ИСИ',
            'group': 'СБ-101',
            'course': '2',
        },
    )
    assert response.status == 200
    app.ctx.storage.set_profile.assert_called_once()
    saved = app.ctx.storage.set_profile.call_args[0][1]
    assert saved['group'] == 'СБ-101'

    _, response = client.get('/api/profile', headers=headers)
    assert response.status == 200


def test_profile_rejects_bad_sex_and_course(client: SanicTestClient):
    headers = athlete_headers()
    _, response = client.post(
        '/api/profile',
        headers=headers,
        data={**csrf_for(headers), 'student_sex': 'другое'},
    )
    assert response.status == 400

    _, response = client.post(
        '/api/profile',
        headers=headers,
        data={**csrf_for(headers), 'course': 'второй'},
    )
    assert response.status == 400


def test_athlete_record_uses_profile_defaults(client: SanicTestClient):
    app.ctx.storage.save_competitions.reset_mock()
    app.ctx.storage.get_profile.return_value = {
        'student_name': 'Спортсменов Спорт Спортович',
        'student_sex': 'М',
        'institute': 'ИСИ',
        'group': 'СБ-101',
        'course': '2',
    }
    headers = athlete_headers()
    _, response = client.post(
        '/competition',
        headers=headers,
        data={**csrf_for(headers), **ATHLETE_RECORD_DATA},
        allow_redirects=False,
    )
    assert response.status == 302
    saved = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert saved.group == 'СБ-101'

    # а если в форме данные есть — профиль не подменяет их
    app.ctx.storage.get_profile.return_value = {
        'student_name': 'Другое ФИО',
    }
    _, response = client.post(
        '/competition',
        headers=headers,
        data={**csrf_for(headers), **ATHLETE_RECORD_DATA},
        allow_redirects=False,
    )
    assert response.status == 302
    saved = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert saved.student_name == ATHLETE_RECORD_DATA['student_name']
    app.ctx.storage.get_profile.return_value = {}


def test_admin_merge_endpoint_preview_and_apply(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/students/merge',
        headers=headers,
        data={**csrf_for(headers), 'from_name': 'Иванова Анна', 'to_name': 'Петрова Анна'},
    )
    assert response.status == 200
    assert response.json['preview'] is True
    assert 'records_to_merge' in response.json

    _, response = client.post(
        '/admin/students/merge',
        headers=headers,
        data={
            **csrf_for(headers),
            'from_name': 'Иванова Анна',
            'to_name': 'Петрова Анна',
            'confirm': 'on',
            'rewrite_names': 'on',
        },
    )
    assert response.status == 200
    app.ctx.storage.merge_students.assert_called_once()
    call = app.ctx.storage.merge_students.call_args
    assert call[1]['new_name'] == 'Петрова Анна'
    app.ctx.storage.carry_name_aliases.assert_called_once_with('Иванова Анна', 'Петрова Анна')
    assert 'aliases_updated' in response.json


def test_admin_can_add_alias_to_user(client: SanicTestClient):
    app.ctx.storage.add_name_alias.reset_mock()
    app.ctx.storage.get_user_by_id.return_value = {'id': 7, 'username': 'anna', 'role': 'athlete'}
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/users/7/alias',
        headers=headers,
        data={**csrf_for(headers), 'name': 'Иванова Анна Петровна'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.add_name_alias.assert_called_once_with(7, 'Иванова Анна Петровна')
    app.ctx.storage.get_user_by_id.return_value = None


def test_profile_save_appends_alias(client: SanicTestClient):
    app.ctx.storage.add_name_alias.reset_mock()
    app.ctx.storage.get_name_aliases.return_value = []
    headers = athlete_headers()
    _, response = client.post(
        '/api/profile',
        headers=headers,
        data={**csrf_for(headers), 'student_name': 'Спортсменов Спорт Спортович'},
    )
    assert response.status == 200
    app.ctx.storage.add_name_alias.assert_called_once_with(1, 'Спортсменов Спорт Спортович')
    app.ctx.storage.get_name_aliases.return_value = []


STUDENT_NAMES = ['Абрамов Артём Артёмович', 'Иванов Иван Иванович']


def test_students_list_for_admin_and_editor(client: SanicTestClient):
    app.ctx.storage.get_student_names.return_value = STUDENT_NAMES
    for role in ('admin', 'editor'):
        _, response = client.get('/api/students', headers=get_auth_headers(role))
        assert response.status == 200
        assert response.json == STUDENT_NAMES


def test_students_list_forbidden_for_viewer_and_athlete(client: SanicTestClient):
    _, response = client.get('/api/students', headers=get_auth_headers('viewer'))
    assert response.status == 403

    _, response = client.get('/api/students', headers=athlete_headers())
    assert response.status == 403


def test_students_lookup_requires_auth_and_role(client: SanicTestClient):
    _, response = client.get(
        '/api/students/lookup?name=Иванов Иван Иванович',
        allow_redirects=False,
    )
    assert response.status == 302
    assert response.headers['location'] == '/login'

    _, response = client.get(
        '/api/students/lookup',
        headers=get_auth_headers('viewer'),
        params={'name': 'Иванов Иван Иванович'},
    )
    assert response.status == 403


def test_students_lookup_exact_match(client: SanicTestClient):
    known_name = 'Иванов Иван Иванович'
    known_hash = sha256(known_name.encode()).hexdigest()
    app.ctx.storage.count_records_by_student_hash.side_effect = lambda value: 1 if value == known_hash else 0
    headers = athlete_headers()
    _, response = client.get(
        '/api/students/lookup',
        headers=headers,
        params={'name': known_name},
    )
    assert response.status == 200
    assert response.json == {'found': True}

    # опечатка или другой регистр — уже другой sha256, совпадения нет
    _, response = client.get(
        '/api/students/lookup',
        headers=headers,
        params={'name': 'Иванов Иван Ивановеч'},
    )
    assert response.status == 200
    assert response.json == {'found': False}

    _, response = client.get(
        '/api/students/lookup',
        headers=headers,
        params={'name': 'иванов иван иванович'},
    )
    assert response.status == 200
    assert response.json == {'found': False}
    app.ctx.storage.count_records_by_student_hash.side_effect = None


def test_students_lookup_requires_name_param(client: SanicTestClient):
    _, response = client.get('/api/students/lookup', headers=get_auth_headers())
    assert response.status == 400


def audit_calls():
    import json as json_module

    return [
        (call[1]['action'], json_module.loads(call[1]['details']))
        for call in app.ctx.storage.add_audit_event.call_args_list
    ]


def test_login_success_writes_audit_event(client: SanicTestClient):
    from src.main import login_failures

    login_failures.clear()
    app.ctx.storage.add_audit_event.reset_mock()
    _, response = client.post(
        '/login',
        data={
            'username': settings.auth_admin_username,
            'password': settings.auth_admin_password,
        },
        allow_redirects=False,
    )
    assert response.status == 302
    app.ctx.storage.add_audit_event.assert_called_once()
    kwargs = app.ctx.storage.add_audit_event.call_args[1]
    assert kwargs['action'] == 'login_success'
    assert kwargs['username'] == settings.auth_admin_username
    assert kwargs['user_id'] == 1


def test_login_failure_writes_audit_event_without_user_id(client: SanicTestClient):
    from src.main import login_failures

    login_failures.clear()
    app.ctx.storage.add_audit_event.reset_mock()
    _, response = client.post(
        '/login',
        data={'username': settings.auth_admin_username, 'password': 'wrong'},
        allow_redirects=False,
    )
    assert response.status == 401
    app.ctx.storage.add_audit_event.assert_called_once()
    kwargs = app.ctx.storage.add_audit_event.call_args[1]
    assert kwargs['action'] == 'login_failed'
    assert kwargs['username'] == settings.auth_admin_username
    assert kwargs['user_id'] is None


def test_audit_failure_does_not_break_login(client: SanicTestClient):
    from src.main import login_failures

    login_failures.clear()
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.add_audit_event.side_effect = RuntimeError('audit db is down')
    _, response = client.post(
        '/login',
        data={
            'username': settings.auth_admin_username,
            'password': settings.auth_admin_password,
        },
        allow_redirects=False,
    )
    app.ctx.storage.add_audit_event.side_effect = None
    assert response.status == 302


def test_merge_writes_audit_event(client: SanicTestClient):
    app.ctx.storage.add_audit_event.reset_mock()
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/students/merge',
        headers=headers,
        data={
            **csrf_for(headers),
            'from_name': 'Иванова Анна',
            'to_name': 'Петрова Анна',
            'confirm': 'on',
            'rewrite_names': 'on',
        },
    )
    assert response.status == 200
    assert (
        'students_merged',
        {
            'from_name': 'Иванова Анна',
            'to_name': 'Петрова Анна',
            'merged': 2,
            'rewrite_names': True,
            'aliases_updated': 1,
        },
    ) in audit_calls()


def test_review_decisions_write_audit_events(client: SanicTestClient):
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.get_competition_review.return_value = {
        'id': 5,
        'review_status': 'pending',
        'owner_id': 2,
    }
    headers = get_auth_headers(role='editor')

    _, response = client.post(
        '/competition/5/review/approve',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 302
    assert ('record_approved', {'record_id': 5, 'comment': ''}) in audit_calls()

    app.ctx.storage.add_audit_event.reset_mock()
    _, response = client.post(
        '/competition/5/review/reject',
        headers=headers,
        data={**csrf_for(headers), 'comment': 'укажите верное место'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert ('record_rejected', {'record_id': 5, 'comment': 'укажите верное место'}) in audit_calls()
    app.ctx.storage.get_competition_review.return_value = None


def test_alias_added_writes_audit_event(client: SanicTestClient):
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.get_user_by_id.return_value = {'id': 7, 'username': 'anna', 'role': 'athlete'}
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/users/7/alias',
        headers=headers,
        data={**csrf_for(headers), 'name': 'Иванова Анна Петровна'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert (
        'alias_added',
        {
            'target_user_id': 7,
            'target_username': 'anna',
            'name': 'Иванова Анна Петровна',
        },
    ) in audit_calls()
    app.ctx.storage.get_user_by_id.return_value = None


def test_password_change_writes_audit_event(client: SanicTestClient):
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.get_user_by_id.return_value = {
        'id': 7,
        'username': 'someone',
        'password_hash': 'x',
        'role': 'editor',
        'active': 1,
    }
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/users/7/password',
        headers=headers,
        data={**csrf_for(headers), 'password': 'newsecret123'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert ('password_changed', {'target_user_id': 7, 'target_username': 'someone'}) in audit_calls()
    app.ctx.storage.get_user_by_id.return_value = None


def test_audit_page_available_for_admin_only(client: SanicTestClient):
    _, response = client.get('/admin/audit', headers=get_auth_headers())
    assert response.status == 200
    assert 'Журнал безопасности' in response.text

    _, response = client.get('/admin/audit', headers=get_auth_headers(role='editor'))
    assert response.status == 403

    _, response = client.get('/admin/audit', headers=get_auth_headers(role='viewer'))
    assert response.status == 403

    _, response = client.get('/admin/audit', headers=athlete_headers())
    assert response.status == 403

    _, response = client.get('/admin/audit', allow_redirects=False)
    assert response.status == 302
    assert response.headers['location'] == '/login'


def test_audit_page_filters_and_pagination(client: SanicTestClient):
    app.ctx.storage.count_audit_events.return_value = 120
    app.ctx.storage.get_audit_events.reset_mock()
    app.ctx.storage.count_audit_events.reset_mock()
    app.ctx.storage.get_audit_events.return_value = []
    app.ctx.storage.list_audit_actions.return_value = ['db_wiped', 'login_success']

    _, response = client.get(
        '/admin/audit?page=2&user=admin&action=login_success&date_from=01.01.2026&date_to=31.01.2026',
        headers=get_auth_headers(),
    )

    assert response.status == 200
    assert 'Стр. 2 из 3' in response.text
    assert 'page=1' in response.text  # стрелка назад с сохранением фильтров
    assert 'page=3' in response.text  # стрелка вперёд
    app.ctx.storage.count_audit_events.assert_called_once_with(
        username='admin', action='login_success', date_from='2026-01-01', date_to='2026-01-31'
    )
    app.ctx.storage.get_audit_events.assert_called_once_with(
        limit=50,
        offset=50,
        username='admin',
        action='login_success',
        date_from='2026-01-01',
        date_to='2026-01-31',
    )
    assert 'login_success' in response.text  # выпадающий фильтр действий

    app.ctx.storage.count_audit_events.return_value = 0
    app.ctx.storage.list_audit_actions.return_value = []


def test_audit_page_rejects_invalid_filter_date(client: SanicTestClient):
    _, response = client.get('/admin/audit?date_from=31.31.2026', headers=get_auth_headers())
    assert response.status == 400


ADMIN_SECTION_PAGES = (
    '/admin/import',
    '/admin/fields',
    '/admin/catalogs',
    '/admin/users',
    '/admin/students',
    '/admin/maintenance',
    '/admin/maintenance/export',
)


def test_admin_hub_shows_all_sections_for_admin(client: SanicTestClient):
    _, response = client.get('/admin', headers=get_auth_headers())
    assert response.status == 200
    for path in ADMIN_SECTION_PAGES + ('/admin/audit',):
        if path == '/admin/maintenance/export':  # хаб ссылается на раздел, не на выгрузку
            continue
        assert path in response.text


def test_admin_hub_shows_only_import_for_editor(client: SanicTestClient):
    _, response = client.get('/admin', headers=get_auth_headers(role='editor'))
    assert response.status == 200
    assert '/admin/import' in response.text
    for path in ('/admin/fields', '/admin/catalogs', '/admin/users', '/admin/students', '/admin/maintenance'):
        assert path not in response.text


def test_admin_hub_access_for_viewer_athlete_and_anonymous(client: SanicTestClient):
    _, response = client.get('/admin', headers=get_auth_headers(role='viewer'))
    assert response.status == 403

    _, response = client.get('/admin', headers=athlete_headers())
    assert response.status == 403

    _, response = client.get('/admin', allow_redirects=False)
    assert response.status == 302
    assert response.headers['location'] == '/login'


def test_admin_section_pages_available_for_admin(client: SanicTestClient):
    headers = get_auth_headers()
    for path in ADMIN_SECTION_PAGES:
        _, response = client.get(path, headers=headers)
        assert response.status == 200, path


def test_admin_import_page_available_for_editor(client: SanicTestClient):
    _, response = client.get('/admin/import', headers=get_auth_headers(role='editor'))
    assert response.status == 200
    assert 'import-form' in response.text
    assert '/template/empty.xlsx' in response.text


def test_admin_sections_forbidden_for_editor(client: SanicTestClient):
    headers = get_auth_headers(role='editor')
    for path in ADMIN_SECTION_PAGES:
        if path == '/admin/import':
            continue
        _, response = client.get(path, headers=headers)
        assert response.status == 403, path


def test_admin_sections_forbidden_for_viewer_and_athlete(client: SanicTestClient):
    for headers in (get_auth_headers(role='viewer'), athlete_headers()):
        for path in ADMIN_SECTION_PAGES:
            _, response = client.get(path, headers=headers)
            assert response.status == 403, path


def test_admin_pages_render_own_markup(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.get('/admin/students', headers=headers)
    assert response.status == 200
    assert 'merge-form' in response.text
    assert 'student-names-list' in response.text

    _, response = client.get('/admin/maintenance', headers=headers)
    assert response.status == 200
    assert '/admin/maintenance/export' in response.text
    assert 'УДАЛИТЬ' in response.text

    _, response = client.get('/admin/users', headers=headers)
    assert response.status == 200
    assert 'student-names-list' in response.text


def test_index_shows_import_link_for_moderators_only(client: SanicTestClient):
    _, response = client.get('/', headers=get_auth_headers(role='editor'))
    assert response.status == 200
    assert 'href="/admin/import"' in response.text

    _, response = client.get('/', headers=athlete_headers())
    assert response.status == 200
    assert 'href="/admin/import"' not in response.text


def wipe_request(
    client,
    headers,
    scope=None,
    mode='scope',
    date_before=None,
    with_attachments=None,
    phrase='УДАЛИТЬ',
    confirm='1',
):
    data = {**csrf_for(headers), 'mode': mode, 'confirm': confirm, 'confirm_phrase': phrase}
    if scope is not None:
        data['scope'] = scope
    if date_before is not None:
        data['date_before'] = date_before
    if with_attachments is not None:
        data['with_attachments'] = with_attachments
    return client.post(
        '/admin/maintenance/wipe',
        headers=headers,
        data=data,
        allow_redirects=False,
    )


def make_wipe_record(record_id, name, month):
    return Competition(
        record_id=str(record_id),
        student_id=f'hash-{record_id}',
        student_name=name,
        student_sex='М',
        institute='ИСИ',
        group='ПГС-101',
        course=2,
        sport='Бег',
        date=datetime(2025, month, 1),
        level='внутривузовские',
        name='Кубок',
        position=1,
    )


def make_wipe_attachment(record_id, stored_name, filename='diploma.png'):
    return {
        'id': record_id * 10,
        'record_id': record_id,
        'filename': filename,
        'stored_name': stored_name,
        'content_type': 'image/png',
        'size': len(PNG_BYTES),
    }


def wipe_events(action):
    return [details for act, details in audit_calls() if act == action]


def test_maintenance_page_shows_status_panel(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    monkeypatch.setattr(main_module.settings, 'database_path', str(tmp_path / 'competitions.sqlite3'))
    (tmp_path / 'competitions.sqlite3').write_bytes(b'x' * 2048)
    (tmp_path / 'competitions.sqlite3-wal').write_bytes(b'y' * 1024)
    backups = tmp_path / 'backups'
    backups.mkdir()
    (backups / 'competitions-20260101-000000.sqlite3.gz').write_bytes(b'z' * 512)

    _, response = client.get('/admin/maintenance', headers=get_auth_headers())

    assert response.status == 200
    assert 'Диск:' in response.text  # полоса + подпись места на диске (№16б)
    assert '2 КБ' in response.text  # файл БД
    assert '1 КБ' in response.text  # WAL
    assert '512 Б' in response.text  # вложения/бэкапы или сам бэкап
    assert 'competitions-20260101-000000.sqlite3.gz' in response.text  # последний бэкап
    assert 'Сделать бэкап сейчас' in response.text
    assert '/admin/maintenance/backup' in response.text
    assert 'Архивы очисток' in response.text


def test_wipe_records_scope(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    app.ctx.storage.get_competitions.return_value = [make_wipe_record(1, 'Первый', 1)]
    app.ctx.storage.delete_all_competitions.reset_mock()
    app.ctx.storage.delete_all_attachments.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.delete_all_competitions.return_value = 5

    _, response = wipe_request(client, get_auth_headers(), 'records')

    assert response.status == 302
    assert response.headers['location'].startswith('/admin/maintenance?')
    app.ctx.storage.delete_all_competitions.assert_called_once()
    app.ctx.storage.delete_all_attachments.assert_not_called()
    events = wipe_events('db_wiped')
    assert events[0]['scope'] == 'records'
    assert events[0]['records_deleted'] == 5
    assert events[0]['attachments_deleted'] == 0
    # архив удалённых записей: только xlsx, без zip (вложения не трогаются)
    assert len(list((tmp_path / 'backups').glob('pre-wipe-*-records.xlsx'))) == 1
    assert not list((tmp_path / 'backups').glob('pre-wipe-*.zip'))
    archive_events = wipe_events('pre_wipe_archive_created')
    assert archive_events[0]['scope'] == 'records'
    assert archive_events[0]['archive_xlsx']
    assert archive_events[0]['archive_zip'] is None
    app.ctx.storage.get_competitions.return_value = []


def test_wipe_attachments_scope_removes_files_only(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    files_dir = tmp_path / 'files' / '3'
    files_dir.mkdir(parents=True)
    (files_dir / 'stored.png').write_bytes(PNG_BYTES)
    keep_me = tmp_path / 'competitions.sqlite3'
    keep_me.write_bytes(b'db')
    app.ctx.storage.get_attachments.return_value = [make_wipe_attachment(3, 'stored.png')]
    app.ctx.storage.delete_all_competitions.reset_mock()
    app.ctx.storage.delete_all_attachments.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.delete_all_attachments.return_value = 2

    _, response = wipe_request(client, get_auth_headers(), 'attachments')

    assert response.status == 302
    app.ctx.storage.delete_all_attachments.assert_called_once()
    app.ctx.storage.delete_all_competitions.assert_not_called()
    assert not (tmp_path / 'files').exists()
    assert keep_me.exists()
    events = wipe_events('db_wiped')
    assert events[0]['scope'] == 'attachments'
    assert events[0]['records_deleted'] == 0
    assert events[0]['attachments_deleted'] == 2
    assert events[0]['freed_bytes'] >= len(PNG_BYTES)
    # архив: только zip вложений, без xlsx (записи не удаляются)
    assert not list((tmp_path / 'backups').glob('pre-wipe-*.xlsx'))
    zip_archives = list((tmp_path / 'backups').glob('pre-wipe-*-attachments.zip'))
    assert len(zip_archives) == 1
    import zipfile as zipfile_module

    with zipfile_module.ZipFile(zip_archives[0]) as archive:
        assert archive.namelist() == ['3/diploma.png']
    app.ctx.storage.get_attachments.return_value = []


def test_wipe_all_scope_creates_xlsx_and_zip_archives(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    record_dir = tmp_path / 'files' / '1'
    record_dir.mkdir(parents=True)
    (record_dir / 'stored.png').write_bytes(PNG_BYTES)
    app.ctx.storage.get_competitions.return_value = [make_wipe_record(1, 'Первый', 1)]
    app.ctx.storage.get_attachments.return_value = [make_wipe_attachment(1, 'stored.png')]
    app.ctx.storage.delete_all_competitions.reset_mock()
    app.ctx.storage.delete_all_attachments.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.delete_all_competitions.return_value = 4
    app.ctx.storage.delete_all_attachments.return_value = 3

    _, response = wipe_request(client, get_auth_headers(), 'all')

    assert response.status == 302
    app.ctx.storage.delete_all_competitions.assert_called_once()
    app.ctx.storage.delete_all_attachments.assert_called_once()
    assert not (tmp_path / 'files').exists()
    events = wipe_events('db_wiped')
    assert events[0]['scope'] == 'all'
    assert events[0]['records_deleted'] == 4
    assert events[0]['attachments_deleted'] == 3
    backups = tmp_path / 'backups'
    assert len(list(backups.glob('pre-wipe-*-all.xlsx'))) == 1
    assert len(list(backups.glob('pre-wipe-*-all.zip'))) == 1
    archive_events = wipe_events('pre_wipe_archive_created')
    assert archive_events[0]['scope'] == 'all'
    assert archive_events[0]['records'] == 1
    assert archive_events[0]['attachments'] == 1
    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.get_attachments.return_value = []


def test_wipe_preview_returns_counts_without_confirm(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    record_dir = tmp_path / 'files' / '3'
    record_dir.mkdir(parents=True)
    (record_dir / 'stored.png').write_bytes(PNG_BYTES)
    app.ctx.storage.get_attachments.return_value = [make_wipe_attachment(3, 'stored.png')]
    app.ctx.storage.delete_all_attachments.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()

    _, response = wipe_request(client, get_auth_headers(), 'attachments', confirm='')

    assert response.status == 200
    payload = response.json
    assert payload['records'] == 0
    assert payload['attachments'] == 1
    assert payload['attachments_size_bytes'] == len(PNG_BYTES)
    assert payload['attachments_size']
    app.ctx.storage.delete_all_attachments.assert_not_called()
    assert wipe_events('db_wiped') == []
    app.ctx.storage.get_attachments.return_value = []


def test_wipe_by_date_deletes_older_records_and_their_attachments(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    monkeypatch.setattr(main_module.settings, 'database_path', str(tmp_path / 'competitions.sqlite3'))
    (tmp_path / 'competitions.sqlite3').write_bytes(b'db')
    for record_id, stored in ((1, 'a.png'), (1, 'b.png'), (2, 'c.png')):
        record_dir = tmp_path / 'files' / str(record_id)
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / stored).write_bytes(PNG_BYTES)
    keep_dir = tmp_path / 'files' / '9'
    keep_dir.mkdir(parents=True)
    (keep_dir / 'keep.png').write_bytes(PNG_BYTES)

    old_records = [make_wipe_record(1, 'Старый', 1), make_wipe_record(2, 'Старая', 2)]
    app.ctx.storage.get_competitions_before.return_value = old_records
    app.ctx.storage.get_attachments_for_records.return_value = [
        make_wipe_attachment(1, 'a.png'),
        make_wipe_attachment(1, 'b.png'),
        make_wipe_attachment(2, 'c.png'),
    ]
    app.ctx.storage.delete_competitions_before.reset_mock()
    app.ctx.storage.delete_attachments_for_records.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.delete_competitions_before.return_value = 2
    app.ctx.storage.delete_attachments_for_records.return_value = 3

    _, response = wipe_request(client, get_auth_headers(), mode='date', date_before='01.06.2025')

    assert response.status == 302
    app.ctx.storage.get_competitions_before.assert_called_once_with(datetime(2025, 6, 1))
    app.ctx.storage.delete_competitions_before.assert_called_once_with(datetime(2025, 6, 1))
    app.ctx.storage.delete_attachments_for_records.assert_called_once_with([1, 2])
    # файлы вложений удалённых записей ушли, чужая запись осталась
    assert not (tmp_path / 'files' / '1').exists()
    assert not (tmp_path / 'files' / '2').exists()
    assert (tmp_path / 'files' / '9' / 'keep.png').exists()
    events = wipe_events('db_wiped')
    assert events[0]['scope'] == 'date'
    assert events[0]['records_deleted'] == 2
    assert events[0]['attachments_deleted'] == 3
    assert events[0]['date_before'] == '01.06.2025'
    assert events[0]['with_attachments'] is True
    assert events[0]['freed_bytes'] >= 3 * len(PNG_BYTES)
    backups = tmp_path / 'backups'
    assert len(list(backups.glob('pre-wipe-*-date.xlsx'))) == 1
    assert len(list(backups.glob('pre-wipe-*-date.zip'))) == 1
    app.ctx.storage.get_competitions_before.return_value = []
    app.ctx.storage.get_attachments_for_records.return_value = []


def test_wipe_by_date_keeps_attachments_when_checkbox_off(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    record_dir = tmp_path / 'files' / '1'
    record_dir.mkdir(parents=True)
    (record_dir / 'a.png').write_bytes(PNG_BYTES)
    app.ctx.storage.get_competitions_before.return_value = [make_wipe_record(1, 'Старый', 1)]
    app.ctx.storage.get_attachments_for_records.reset_mock()
    app.ctx.storage.delete_attachments_for_records.reset_mock()
    app.ctx.storage.delete_competitions_before.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.delete_competitions_before.return_value = 1

    # скрытый маркер 0 без чекбокса 1 — галочка снята
    _, response = wipe_request(client, get_auth_headers(), mode='date', date_before='01.06.2025', with_attachments='0')

    assert response.status == 302
    app.ctx.storage.get_attachments_for_records.assert_not_called()
    app.ctx.storage.delete_attachments_for_records.assert_not_called()
    assert (tmp_path / 'files' / '1' / 'a.png').exists()  # вложения не тронуты
    assert len(list((tmp_path / 'backups').glob('pre-wipe-*-date.xlsx'))) == 1
    assert not list((tmp_path / 'backups').glob('pre-wipe-*.zip'))
    events = wipe_events('db_wiped')
    assert events[0]['with_attachments'] is False
    assert events[0]['attachments_deleted'] == 0
    app.ctx.storage.get_competitions_before.return_value = []


def test_wipe_by_date_preview_counts_affected(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    record_dir = tmp_path / 'files' / '2'
    record_dir.mkdir(parents=True)
    (record_dir / 'c.png').write_bytes(PNG_BYTES)
    app.ctx.storage.get_competitions_before.return_value = [
        make_wipe_record(1, 'Старый', 1),
        make_wipe_record(2, 'Старая', 2),
    ]
    app.ctx.storage.get_attachments_for_records.return_value = [make_wipe_attachment(2, 'c.png')]
    app.ctx.storage.delete_competitions_before.reset_mock()

    _, response = wipe_request(client, get_auth_headers(), mode='date', date_before='01.06.2025', confirm='')

    assert response.status == 200
    payload = response.json
    assert payload['records'] == 2
    assert payload['attachments'] == 1
    assert payload['attachments_size_bytes'] == len(PNG_BYTES)
    app.ctx.storage.delete_competitions_before.assert_not_called()
    app.ctx.storage.get_competitions_before.return_value = []
    app.ctx.storage.get_attachments_for_records.return_value = []


def test_wipe_rejects_wrong_confirm_phrase(client: SanicTestClient):
    app.ctx.storage.delete_all_competitions.reset_mock()
    _, response = wipe_request(client, get_auth_headers(), 'records', phrase='удалить')
    assert response.status == 400
    app.ctx.storage.delete_all_competitions.assert_not_called()


def test_wipe_rejects_unknown_scope_and_mode(client: SanicTestClient):
    _, response = wipe_request(client, get_auth_headers(), 'everything')
    assert response.status == 400

    _, response = wipe_request(client, get_auth_headers(), mode='date', date_before='июнь')
    assert response.status == 400

    _, response = wipe_request(client, get_auth_headers(), mode='yesterday')
    assert response.status == 400


def test_wipe_forbidden_for_non_admin(client: SanicTestClient):
    headers = get_auth_headers(role='editor')
    _, response = wipe_request(client, headers, 'records')
    assert response.status == 403

    viewer_headers = get_auth_headers(role='viewer')
    _, response = client.post(
        '/admin/maintenance/wipe',
        headers=viewer_headers,
        data={**csrf_for(viewer_headers), 'mode': 'scope', 'scope': 'records', 'confirm_phrase': 'УДАЛИТЬ'},
    )
    assert response.status == 403


def make_tmp_database(db_path):
    import sqlite3 as sqlite3_module

    connection = sqlite3_module.connect(db_path)
    connection.execute(
        'CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, '
        'user_id INTEGER, username TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT \'\')'
    )
    connection.commit()
    connection.close()


def test_manual_backup_creates_archive_and_audit_event(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    db_path = tmp_path / 'competitions.sqlite3'
    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    monkeypatch.setattr(main_module.settings, 'database_path', str(db_path))
    make_tmp_database(db_path)
    record_dir = tmp_path / 'files' / '5'
    record_dir.mkdir(parents=True)
    (record_dir / 'stored.png').write_bytes(PNG_BYTES)
    app.ctx.storage.add_audit_event.reset_mock()

    headers = get_auth_headers()
    _, response = client.post(
        '/admin/maintenance/backup',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )

    assert response.status == 302
    assert response.headers['location'].startswith('/admin/maintenance?')
    backups = tmp_path / 'backups'
    db_archives = list(backups.glob('competitions-*.sqlite3.gz'))
    assert len(db_archives) == 1
    assert db_archives[0].stat().st_size > 0
    files_archives = list(backups.glob('competitions-*-files.zip'))
    assert len(files_archives) == 1  # вложения заархивированы тем же кодом, что и скриптом
    events = [details for action, details in audit_calls() if action == 'backup_created']
    assert len(events) == 1
    assert events[0]['source'] == 'manual'
    assert events[0]['archive'] == db_archives[0].name
    assert events[0]['files_archive'] == files_archives[0].name


def test_manual_backup_forbidden_for_editor(client: SanicTestClient):
    headers = get_auth_headers(role='editor')
    _, response = client.post('/admin/maintenance/backup', headers=headers, data=csrf_for(headers))
    assert response.status == 403


def test_download_backup_file(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))
    backups = tmp_path / 'backups'
    backups.mkdir()
    (backups / 'pre-wipe-20260101-000000-date.xlsx').write_bytes(b'xlsx-content')

    _, response = client.get(
        '/admin/maintenance/backups/pre-wipe-20260101-000000-date.xlsx', headers=get_auth_headers()
    )
    assert response.status == 200
    assert response.body == b'xlsx-content'
    assert response.headers['content-type'].startswith(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


def test_download_backup_rejects_traversal_and_missing(client: SanicTestClient, tmp_path, monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module.settings, 'data_folder', str(tmp_path))

    _, response = client.get('/admin/maintenance/backups/..%2F..%2Fcompetitions.sqlite3', headers=get_auth_headers())
    assert response.status in (400, 404)

    _, response = client.get('/admin/maintenance/backups/no-such-file.zip', headers=get_auth_headers())
    assert response.status == 404

    _, response = client.get('/admin/maintenance/backups/.env', headers=get_auth_headers())
    assert response.status in (400, 404)

    headers = get_auth_headers(role='editor')
    _, response = client.get('/admin/maintenance/backups/whatever.zip', headers=headers)
    assert response.status == 403


def test_maintenance_export_for_admin(client: SanicTestClient):
    from openpyxl import load_workbook

    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.get_custom_fields.return_value = []
    app.ctx.storage.list_users.return_value = [
        {'id': 1, 'username': 'admin', 'role': 'admin', 'active': 1, 'name_aliases': ['Иванов Иван']},
    ]
    app.ctx.storage.list_levels.return_value = [{'id': 1, 'name': 'внутривузовские', 'sort_order': 0, 'active': 1}]
    app.ctx.storage.get_sport_names.return_value = ['Бег', 'Лыжи']

    _, response = client.get('/admin/maintenance/export', headers=get_auth_headers())

    assert response.status == 200
    assert response.headers['content-type'].startswith(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    workbook = load_workbook(BytesIO(response.body), read_only=True)
    assert workbook.sheetnames == ['Записи', 'Пользователи', 'Уровни', 'Виды спорта']
    users_sheet = workbook['Пользователи']
    rows = list(users_sheet.iter_rows(values_only=True))
    assert rows[0] == ('Логин', 'Роль', 'Активен', 'Псевдонимы ФИО')
    assert rows[1][0] == 'admin'
    assert rows[1][3] == 'Иванов Иван'
    sports_sheet = workbook['Виды спорта']
    assert [row[0] for row in sports_sheet.iter_rows(min_row=2, values_only=True)] == ['Бег', 'Лыжи']

    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.get_custom_fields.return_value = []
    app.ctx.storage.list_users.return_value = []
    app.ctx.storage.list_levels.return_value = []
    app.ctx.storage.get_sport_names.return_value = []


def test_maintenance_export_forbidden_for_editor(client: SanicTestClient):
    _, response = client.get('/admin/maintenance/export', headers=get_auth_headers(role='editor'))
    assert response.status == 403


def test_audit_page_shows_message_when_log_unavailable(client: SanicTestClient):
    app.ctx.storage.get_audit_events.side_effect = RuntimeError('audit db is down')
    _, response = client.get('/admin/audit', headers=get_auth_headers())
    app.ctx.storage.get_audit_events.side_effect = None

    assert response.status == 200
    assert 'Журнал недоступен' in response.text


def test_old_admin_post_still_works_and_returns_to_section(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/fields',
        headers=headers,
        data={**csrf_for(headers), 'label': 'Тренер', 'field_type': 'text'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert response.headers['location'].startswith('/admin/fields')
