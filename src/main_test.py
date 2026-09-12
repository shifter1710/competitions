import hmac
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from unittest.mock import Mock

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
    fake_storage.clean_db.return_value = None
    fake_storage.create_custom_field.return_value = None
    fake_storage.update_custom_field.return_value = None
    fake_storage.disable_custom_field.return_value = None
    fake_storage.get_competition_review.return_value = None
    fake_storage.get_profile.return_value = {}
    fake_storage.set_profile.return_value = None
    fake_storage.get_name_aliases.return_value = []
    fake_storage.add_name_alias.return_value = None
    fake_storage.count_records_by_student_hash.return_value = 2
    fake_storage.merge_students.return_value = 2
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
    fake_storage.get_user.side_effect = fake_get_user
    fake_storage.get_user_by_id.return_value = None
    fake_storage.list_users.return_value = []
    fake_storage.create_user.return_value = None
    fake_storage.set_user_password.return_value = None
    fake_storage.set_user_active.return_value = None
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


def test_clean_db_post(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post('/clean_db', headers=headers, data=csrf_for(headers))
    assert response.status == 200


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


def test_viewer_cannot_clean_db(client: SanicTestClient):
    headers = get_auth_headers(role='viewer')
    _, response = client.post('/clean_db', headers=headers, data=csrf_for(headers))

    assert response.status == 403


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
