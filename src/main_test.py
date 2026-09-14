import hmac
import json
import re
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from unittest.mock import ANY
from unittest.mock import Mock
from urllib.parse import quote
from urllib.parse import unquote_plus
from urllib.parse import urlencode

import pandas as pd
import pytest
from sanic_testing.testing import SanicTestClient

from src.auth import hash_password
from src.conftest import make_report_fixture
from src.conftest import make_report_unapproved_fixture
from src.main import app
from src.main import build_competition
from src.main import competition_duplicate_key
from src.main import competition_to_export_row
from src.main import create_auth_cookie_value
from src.main import format_date_range
from src.main import normalize_position
from src.main import parse_date_value
from src.main import split_import_competitions
from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.models.http.student_info import StudentInfo
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter


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
    # Серверные фильтры/пагинация главной (прототип 02)
    fake_storage.get_competitions_page.return_value = []
    fake_storage.count_competitions_filtered.return_value = 0
    fake_storage.get_filtered.return_value = []
    fake_storage.get_custom_fields.return_value = []
    fake_storage.save_competitions.return_value = None
    fake_storage.import_competitions.return_value = None
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
    fake_storage.disable_level.return_value = None
    fake_storage.rename_catalog_value.return_value = 0
    fake_storage.list_catalog.side_effect = lambda category: {
        'sport': ['Бег', 'Лыжи'],
        'institute': ['ИСИ'],
    }.get(category, [])
    fake_storage.list_catalog_all.return_value = []
    fake_storage.list_catalog_tree.return_value = []
    # №1.1: канонизирующие подстановки по умолчанию выключены (None).
    fake_storage.find_catalog_row.return_value = None
    fake_storage.find_catalog_canonical.return_value = None
    fake_storage.find_level_canonical.return_value = None
    # №19а: автозаполнение института по группе по умолчанию выключено.
    fake_storage.find_unique_group_institute.return_value = None
    # №23доп: резолвер атлета по умолчанию ничего не знает.
    fake_storage.search_athletes.return_value = []
    fake_storage.find_athlete_fields.return_value = {}
    fake_storage.get_group_options_by_institute.return_value = {}
    fake_storage.ensure_catalog_pair.return_value = None
    fake_storage.count_child_groups.return_value = 0
    fake_storage.add_catalog_value.return_value = None
    fake_storage.get_catalog_value.return_value = None
    fake_storage.hide_catalog_value.return_value = None
    fake_storage.unhide_catalog_value.return_value = None
    fake_storage.delete_catalog_value.return_value = None
    fake_storage.count_records_using.return_value = 0
    fake_storage.touch_user_seen.return_value = False
    fake_storage.set_user_last_login.return_value = None
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
    # Лёгкий реестр полей + очередь конфликтов импорта (волна 3)
    fake_storage.get_field_settings.return_value = {}
    fake_storage.update_field_settings.return_value = None
    fake_storage.count_import_queue.return_value = 0
    fake_storage.list_import_queue.return_value = []
    fake_storage.get_import_queue_entry.return_value = None
    fake_storage.add_import_queue_entry.return_value = 1
    fake_storage.set_import_queue_status.return_value = None
    fake_storage.get_competition_by_id.return_value = None
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
    app.ctx.storage.import_competitions.reset_mock()
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
    app.ctx.storage.import_competitions.assert_called_once()
    saved = app.ctx.storage.import_competitions.call_args[0][0][0]
    assert saved.date == datetime(2026, 3, 15)


def test_upload_skips_duplicates(client: SanicTestClient):
    app.ctx.storage.import_competitions.reset_mock()
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
    saved_rows = app.ctx.storage.import_competitions.call_args[0][0]
    assert len(saved_rows) == 1
    assert saved_rows[0].student_name == 'Новый Студент'
    app.ctx.storage.get_competitions.return_value = []


def test_upload_error_mentions_row_number(client: SanicTestClient):
    app.ctx.storage.import_competitions.reset_mock()
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
    # Атомарность импорта: невалидная строка — ничего не сохраняется вовсе.
    app.ctx.storage.import_competitions.assert_not_called()


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
            count_wins=1,
            count_prizes=2,
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
        'Участий',
        'Побед',
        'Призовых',
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
            count_wins=2,
            count_prizes=3,
        ),
        StudentInfo(
            student_id='2',
            student_name='Петров Пётр',
            student_sex='Ж',
            institute='ИМИ',
            group='Б-202',
            course=1,
            count_participation=1,
            count_wins=0,
            count_prizes=0,
        ),
    ]


def test_export_report_returns_rows_in_report_order(client: SanicTestClient):
    app.ctx.storage.get_filtered.return_value = make_report_infos()

    _, response = client.get('/export/report', headers=get_auth_headers())

    assert response.status == 200
    assert get_xlsx_rows(response.body) == [
        ('ФИО', 'Пол', 'Институт', 'Группа', 'Курс', 'Участий', 'Побед', 'Призовых'),
        ('Иванов Иван', 'М', 'ИСИ', 'А-101', 2, 5, 2, 3),
        ('Петров Пётр', 'Ж', 'ИМИ', 'Б-202', 1, 1, 0, 0),
    ]


def test_export_report_with_column_subset(client: SanicTestClient):
    app.ctx.storage.get_filtered.return_value = make_report_infos()

    _, response = client.get(
        '/export/report?' + urlencode({'columns': ['ФИО', 'Участий']}, doseq=True),
        headers=get_auth_headers(),
    )

    assert response.status == 200
    assert get_xlsx_rows(response.body) == [
        ('ФИО', 'Участий'),
        ('Иванов Иван', 5),
        ('Петров Пётр', 1),
    ]


def test_export_report_accepts_legacy_column_name(client: SanicTestClient):
    # Старые закладки с «Количество участий» продолжают работать: имя —
    # алиас «Участий» (замечание №19, обратная совместимость).
    app.ctx.storage.get_filtered.return_value = make_report_infos()

    _, response = client.get(
        '/export/report?' + urlencode({'columns': ['ФИО', 'Количество участий']}, doseq=True),
        headers=get_auth_headers(),
    )

    assert response.status == 200
    assert get_xlsx_rows(response.body) == [
        ('ФИО', 'Участий'),
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
        'institute': '',
        'group': '',
        'sport': '',
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
    for column in ('ФИО', 'Пол', 'Институт', 'Группа', 'Курс', 'Участий', 'Побед', 'Призовых'):
        assert f'value="{column}"' in response.text


def test_reports_page_shows_slice_and_catalog_filters(client: SanicTestClient):
    # Срез и фильтры институт/группа/спорт — на странице фильтров отчёта;
    # группы знают иерархию (институт → его группы), срезы — фиксированный
    # набор из решения по замечанию №19.
    _, response = client.get('/reports', headers=get_auth_headers())

    assert response.status == 200
    assert 'Группировать по' in response.text
    assert 'name="group_by"' in response.text
    assert 'name="institute"' in response.text
    assert 'name="group"' in response.text
    assert 'name="sport"' in response.text
    for option in ('student', 'group', 'institute', 'course', 'sport', 'level', 'year'):
        assert f'value="{option}"' in response.text
    assert 'data-groups-by-institute' in response.text
    assert 'data-columns-by-slice' in response.text


# --- Расширение отчётов (замечание №19) на реальном SQLite-адаптере. ---
# Данные и ручной пересчёт метрик — src/conftest.py.


@pytest.fixture
def reports_client(client: SanicTestClient, tmp_path):
    storage = SQLiteAdapter(str(tmp_path / 'reports.sqlite3'))
    storage.save_competitions(make_report_fixture())
    for review_status, record in make_report_unapproved_fixture():
        storage.save_competitions([record], review_status=review_status, owner_id=9)
    storage.create_custom_field('trainer', 'Тренер', 'text', False, True, True, True, 0)
    previous = getattr(app.ctx, 'storage', None)
    app.ctx.storage = storage
    try:
        yield client
    finally:
        app.ctx.storage = previous


def get_report_cells(html: str) -> list[list[str]]:
    body = re.search(r'<tbody>(.*?)</tbody>', html, re.S)
    if body is None:
        return []
    rows = re.findall(r'<tr>(.*?)</tr>', body.group(1), re.S)
    return [
        [re.sub(r'<[^>]+>', '', cell).strip() for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)] for row in rows
    ]


def test_report_page_student_slice_with_metrics(reports_client: SanicTestClient):
    # Историчность: Козлов в двух институтах — две отдельные строки; метрики
    # Побед/Призовых теперь есть и для среза «студент».
    _, response = reports_client.get('/report', headers=get_auth_headers())

    assert response.status == 200
    assert get_report_cells(response.text) == [
        ['1', 'Козлов Кирилл', 'М', 'ИМИ', 'ТД-303', '2', '1', '0', '1'],
        ['2', 'Козлов Кирилл', 'М', 'ИСИ', 'ПГС-101', '1', '1', '1', '1'],
        ['3', 'Сидоров Сидор', 'М', 'ИСИ', 'ПГС-102', '3', '1', '0', '0'],
        ['4', 'Петров Пётр', 'Ж', 'ИМИ', 'СБ-202', '2', '2', '1', '2'],
        ['5', 'Иванов Иван', 'М', 'ИСИ', 'ПГС-101', '1', '3', '1', '2'],
    ]
    assert 'Участий' in response.text and 'Побед' in response.text and 'Призовых' in response.text


@pytest.mark.parametrize(
    'group_by,header,expected',
    [
        (
            'group',
            'Группа',
            [
                ['1', 'ПГС-102', '1', '0', '0'],
                ['2', 'ТД-303', '1', '0', '1'],
                ['3', 'СБ-202', '2', '1', '2'],
                ['4', 'ПГС-101', '4', '2', '3'],
            ],
        ),
        ('institute', 'Институт', [['1', 'ИМИ', '3', '1', '3'], ['2', 'ИСИ', '5', '2', '3']]),
        ('course', 'Курс', [['1', '3', '1', '0', '0'], ['2', '2', '3', '1', '3'], ['3', '1', '4', '2', '3']]),
        (
            'sport',
            'Вид спорта',
            [['1', 'Шахматы', '1', '0', '0'], ['2', 'Лыжи', '2', '1', '1'], ['3', 'Бег', '5', '2', '5']],
        ),
        (
            'level',
            'Уровень соревнований',
            [['1', 'межвузовские', '3', '0', '2'], ['2', 'внутривузовские', '5', '3', '4']],
        ),
        (
            'year',
            'Год',
            [['1', '2023', '2', '1', '1'], ['2', '2024', '3', '1', '3'], ['3', '2025', '3', '1', '2']],
        ),
    ],
)
def test_report_page_slice_rows(reports_client: SanicTestClient, group_by, header, expected):
    _, response = reports_client.get(f'/report?group_by={group_by}', headers=get_auth_headers())

    assert response.status == 200
    assert f'>{header}</th>' in response.text
    assert get_report_cells(response.text) == expected


def test_report_page_filters_institute_group_sport(reports_client: SanicTestClient):
    # Пара институт+группа по иерархии: остались Иванов и запись Козлова из
    # ИСИ/ПГС-101 (его ИМИ-запись не попадает).
    _, response = reports_client.get(
        '/report?' + urlencode({'institute': 'ИСИ', 'group': 'ПГС-101'}),
        headers=get_auth_headers(),
    )
    assert response.status == 200
    assert get_report_cells(response.text) == [
        ['1', 'Козлов Кирилл', 'М', 'ИСИ', 'ПГС-101', '1', '1', '1', '1'],
        ['2', 'Иванов Иван', 'М', 'ИСИ', 'ПГС-101', '1', '3', '1', '2'],
    ]

    _, response = reports_client.get('/report?sport=' + quote('Шахматы'), headers=get_auth_headers())
    assert response.status == 200
    assert get_report_cells(response.text) == [
        ['1', 'Сидоров Сидор', 'М', 'ИСИ', 'ПГС-102', '3', '1', '0', '0'],
    ]

    # Новый фильтр работает и на другом срезе: группы института ИМИ.
    _, response = reports_client.get(
        '/report?' + urlencode({'group_by': 'group', 'institute': 'ИМИ'}),
        headers=get_auth_headers(),
    )
    assert response.status == 200
    assert get_report_cells(response.text) == [
        ['1', 'ТД-303', '1', '0', '1'],
        ['2', 'СБ-202', '2', '1', '2'],
    ]


def test_report_page_custom_filter_applies_to_slice(reports_client: SanicTestClient):
    _, response = reports_client.get(
        '/report?' + urlencode({'group_by': 'group', 'custom__trainer': 'Смит'}),
        headers=get_auth_headers(),
    )
    assert response.status == 200
    assert get_report_cells(response.text) == [['1', 'ПГС-101', '1', '1', '1']]


def test_report_url_is_reproducible(reports_client: SanicTestClient):
    # Шаримость: прямой GET с срезом и фильтрами даёт те же данные тем же
    # ответом, что и повторный заход по той же ссылке (закладка).
    url = '/report?' + urlencode(
        {'group_by': 'group', 'institute': 'ИСИ', 'date_from': '01.01.2023', 'date_to': '31.12.2025'}
    )
    _, first = reports_client.get(url, headers=get_auth_headers())
    _, second = reports_client.get(url, headers=get_auth_headers())

    assert first.status == second.status == 200
    assert first.text == second.text
    assert get_report_cells(first.text) == [
        ['1', 'ПГС-102', '1', '0', '0'],
        ['2', 'ПГС-101', '4', '2', '3'],
    ]


def test_report_rejects_unknown_slice(client: SanicTestClient):
    _, response = client.get('/report?group_by=bogus', headers=get_auth_headers())
    assert response.status == 400
    assert 'Неизвестный срез' in response.text


def test_report_available_for_viewer_with_slice(reports_client: SanicTestClient):
    _, response = reports_client.get('/report?group_by=year', headers=get_auth_headers(role='viewer'))
    assert response.status == 200
    assert get_report_cells(response.text) == [
        ['1', '2023', '2', '1', '1'],
        ['2', '2024', '3', '1', '3'],
        ['3', '2025', '3', '1', '2'],
    ]


def test_report_page_shows_row_counter(reports_client: SanicTestClient):
    # Композиция отчёта (прототип 06): счётчик «N строк · M участий».
    _, response = reports_client.get('/report', headers=get_auth_headers())

    assert response.status == 200
    # 5 строк-студентов, суммарно 8 участий в фикстуре.
    assert '5 строк · 8 участий' in response.text

    _, response = reports_client.get('/report?institute=' + quote('ИМИ'), headers=get_auth_headers())
    assert response.status == 200
    # Петров (2 участия) + Козлов в записи за ИМИ (1 участие) = 2 строки, 3 участия.
    assert '2 строки · 3 участия' in response.text


def test_report_page_shows_filter_chips(reports_client: SanicTestClient):
    # Каждый активный фильтр — чип с крестиком-сбросом (data-remove-key);
    # позиция и срез — человекочитаемые метки; кастомные поля — по ярлыку.
    params = urlencode(
        {
            'name': 'Иванов',
            'level': 'внутривузовские',
            'position': '<2',
            'group_by': 'sport',
            'custom__trainer': 'Смит',
        }
    )
    _, response = reports_client.get(f'/report?{params}', headers=get_auth_headers())

    assert response.status == 200
    assert 'ФИО: <strong>Иванов</strong>' in response.text
    assert 'Уровень: <strong>внутривузовские</strong>' in response.text
    assert 'Место: <strong>Победа</strong>' in response.text
    assert 'Срез: <strong>Вид спорта</strong>' in response.text
    assert 'Тренер: <strong>Смит</strong>' in response.text
    assert 'data-remove-key="name"' in response.text
    assert 'data-remove-key="custom__trainer"' in response.text


def test_report_page_without_filters_has_no_chips(reports_client: SanicTestClient):
    _, response = reports_client.get('/report', headers=get_auth_headers())
    assert response.status == 200
    assert 'filter-chip' not in response.text


# --- Серверная фильтрация, пагинация и счётчик главной (прототип 02) ---
# Реальный SQLite-адаптер: фильтры GET / — это SQL (storage), а не клиент.
# Набор подобран так, чтобы каждый фильтр и границы страниц считались руками:
# 12 подтверждённых + 2 «на проверке» + 1 «отклонена» = 15 для модератора.


def make_index_record(
    name: str,
    *,
    institute: str = 'ИСИ',
    sport: str = 'Бег',
    level: str = 'внутривузовские',
    date: datetime = datetime(2024, 3, 1),
) -> Competition:
    return Competition(
        student_id=f'id-{name}',
        student_name=name,
        student_sex='М',
        institute=institute,
        group='ГРП-101',
        course=2,
        sport=sport,
        date=date,
        level=level,
        name='Кубок',
        position=1,
    )


def seed_index_records(storage: SQLiteAdapter) -> None:
    approved = [
        make_index_record('Иванов Иван', date=datetime(2024, 3, 1)),
        make_index_record('Иванов Иван', date=datetime(2024, 4, 1)),
        make_index_record('Иванов Иван', date=datetime(2024, 5, 15)),
        make_index_record('Иванов Иван', date=datetime(2024, 9, 10)),
        make_index_record(
            'Петрова Анна', institute='ИМИ', sport='Лыжи', level='межвузовские', date=datetime(2024, 11, 1)
        ),
        make_index_record(
            'Петрова Анна', institute='ИМИ', sport='Лыжи', level='межвузовские', date=datetime(2024, 12, 1)
        ),
        make_index_record('Сидоров Сидор', date=datetime(2025, 1, 10)),
        make_index_record('Сидоров Сидор', date=datetime(2025, 2, 20)),
        make_index_record('Кузнецов Кирилл', institute='ИМИ', sport='Шахматы', date=datetime(2025, 3, 5)),
        make_index_record(
            'Иванов Иван', institute='ИМИ', sport='Лыжи', level='межвузовские', date=datetime(2025, 4, 1)
        ),
        make_index_record('Петрова Анна', sport='Бег', level='межвузовские', date=datetime(2025, 4, 15)),
        make_index_record('Петрова Анна', sport='Бег', level='межвузовские', date=datetime(2025, 4, 25)),
    ]
    storage.save_competitions(approved)
    storage.save_competitions(
        [
            make_index_record('Ждунов Ждун', date=datetime(2025, 5, 1)),
            make_index_record('Ждунов Ждун', date=datetime(2025, 5, 2)),
        ],
        review_status='pending',
        owner_id=9,
    )
    storage.save_competitions(
        [make_index_record('Отклонов Отклон', institute='ИМИ', sport='Лыжи', date=datetime(2025, 5, 3))],
        review_status='rejected',
        owner_id=9,
    )


@pytest.fixture
def index_client(client: SanicTestClient, tmp_path) -> SanicTestClient:
    storage = SQLiteAdapter(str(tmp_path / 'index.sqlite3'))
    seed_index_records(storage)
    previous = getattr(app.ctx, 'storage', None)
    app.ctx.storage = storage
    try:
        yield client
    finally:
        app.ctx.storage = previous


@pytest.fixture
def athlete_index_client(index_client: SanicTestClient) -> SanicTestClient:
    """Атлет sportik с двумя своими записями «на проверке» (owner_id — его id)."""
    storage = app.ctx.storage
    storage.create_user('sportik', hash_password('sportik-pass-123'), 'athlete')
    athlete_id = storage.get_user('sportik')['id']
    storage.save_competitions(
        [
            make_index_record('Атлетов Атлет', date=datetime(2025, 6, 1)),
            make_index_record('Атлетов Атлет', date=datetime(2025, 6, 2)),
        ],
        review_status='pending',
        owner_id=athlete_id,
    )
    return index_client


def get_tbody_rows(html: str) -> list[str]:
    body = re.search(r'<tbody>(.*?)</tbody>', html, re.S)
    if body is None:
        return []
    return re.findall(r'<tr>(.*?)</tr>', body.group(1), re.S)


def get_index_counter(html: str) -> tuple[int, int]:
    match = re.search(r'Показано <strong>(\d+)</strong> из <strong>(\d+)</strong> записей', html)
    assert match is not None, 'счётчик «Показано N из M» не найден'
    return int(match.group(1)), int(match.group(2))


def test_pager_items_edges_and_gaps():
    # Чистая функция пагинации: края (первая/последняя) и окно ±1 вокруг
    # текущей страницы, между непоследовательными номерами — разрыв «…».
    from src.main import pager_items

    assert pager_items(1, 0) == []
    assert pager_items(1, 1) == [{'page': 1}]
    assert pager_items(1, 2) == [{'page': 1}, {'page': 2}]
    assert pager_items(1, 4) == [{'page': 1}, {'page': 2}, {'gap': True}, {'page': 4}]
    assert pager_items(1, 9) == [{'page': 1}, {'page': 2}, {'gap': True}, {'page': 9}]
    assert pager_items(5, 9) == [
        {'page': 1},
        {'gap': True},
        {'page': 4},
        {'page': 5},
        {'page': 6},
        {'gap': True},
        {'page': 9},
    ]
    assert pager_items(9, 9) == [{'page': 1}, {'gap': True}, {'page': 8}, {'page': 9}]


def test_index_shows_counter_and_all_records(index_client: SanicTestClient):
    _, response = index_client.get('/', headers=get_auth_headers())

    assert response.status == 200
    shown, total = get_index_counter(response.text)
    assert (shown, total) == (15, 15)
    assert len(get_tbody_rows(response.text)) == 15
    # Композиция прототипа 02: заголовок с описанием, карточка фильтров, футер.
    assert 'Реестр соревнований' in response.text
    assert 'index-filter-card' in response.text
    assert 'Показывать по' in response.text


def test_index_filter_by_name_substring(index_client: SanicTestClient):
    _, response = index_client.get('/?' + urlencode({'name': 'Иванов'}), headers=get_auth_headers())

    assert response.status == 200
    assert get_index_counter(response.text) == (5, 5)
    assert len(get_tbody_rows(response.text)) == 5


def test_index_filter_by_catalog_exact_values(index_client: SanicTestClient):
    params = urlencode({'institute': 'ИСИ', 'sport': 'Бег', 'level': 'межвузовские'})
    _, response = index_client.get(f'/?{params}', headers=get_auth_headers())

    assert response.status == 200
    # Петрова Анна, ИСИ, Бег, межвузовские — две записи апреля 2025
    assert get_index_counter(response.text) == (2, 2)


def test_index_filter_by_date_range(index_client: SanicTestClient):
    params = urlencode({'date_from': '01.06.2024', 'date_to': '30.06.2024'})
    _, response = index_client.get(f'/?{params}', headers=get_auth_headers())

    assert response.status == 200
    assert get_index_counter(response.text) == (0, 0)
    assert 'Записей с текущим фильтром не найдено' in response.text

    params = urlencode({'date_from': '01.09.2024', 'date_to': '31.12.2024'})
    _, response = index_client.get(f'/?{params}', headers=get_auth_headers())
    # Иванов 10.09.2024 + Петрова 01.11.2024 и 01.12.2024
    assert get_index_counter(response.text) == (3, 3)


def test_index_filter_rejects_invalid_date(index_client: SanicTestClient):
    _, response = index_client.get('/?date_from=31.31.2024', headers=get_auth_headers())

    assert response.status == 400
    assert 'Некорректная дата' in response.text


def test_index_status_filter_moderator_only(index_client: SanicTestClient):
    _, response = index_client.get('/?status=pending', headers=get_auth_headers())
    assert response.status == 200
    assert get_index_counter(response.text) == (2, 2)

    _, response = index_client.get('/?status=rejected', headers=get_auth_headers())
    assert get_index_counter(response.text) == (1, 1)

    _, response = index_client.get('/?status=approved', headers=get_auth_headers())
    assert get_index_counter(response.text) == (12, 12)

    # Незнакомое значение — фильтр не применяется (все 15).
    _, response = index_client.get('/?status=unknown', headers=get_auth_headers())
    assert get_index_counter(response.text) == (15, 15)

    # viewer не модератор: параметр статуса игнорируется, селекта «Статус» нет.
    _, response = index_client.get('/?status=pending', headers=get_auth_headers(role='viewer'))
    assert response.status == 200
    assert get_index_counter(response.text) == (15, 15)
    assert 'id="index-filter-status"' not in response.text


def test_index_filters_kept_in_url_and_form(index_client: SanicTestClient):
    params = {'status': 'approved', 'per_page': 10, 'page': 2}
    _, response = index_client.get('/?' + urlencode(params), headers=get_auth_headers())

    assert response.status == 200
    # Поля формы переотображают применённые фильтры (ссылки шарятся).
    assert '<option value="approved" selected>' in response.text
    assert '<option value="10" selected>' in response.text
    # Селект «Показывать по» держит фильтры без per_page (ставит сам).
    assert f'data-query="{urlencode({"status": "approved"})}"' in response.text
    # Ссылки пагинации несут фильтры и per_page дальше (Jinja экранирует &).
    assert 'href="/?status=approved&amp;per_page=10&amp;page=1"' in response.text

    # Фильтр по ФИО тоже возвращается в поле ввода.
    _, response = index_client.get('/?' + urlencode({'name': 'Иванов'}), headers=get_auth_headers())
    assert 'value="Иванов"' in response.text


def test_index_per_page_default_shows_all(index_client: SanicTestClient):
    _, response = index_client.get('/', headers=get_auth_headers())
    assert get_index_counter(response.text) == (15, 15)
    # Дефолт — 100 (прежнее поведение показывало весь список): страница одна.
    assert '<option value="100" selected>' in response.text
    assert 'class="pager"' not in response.text


def test_index_per_page_splits_and_paginates(index_client: SanicTestClient):
    _, response = index_client.get('/?per_page=10', headers=get_auth_headers())
    assert get_index_counter(response.text) == (10, 15)
    assert len(get_tbody_rows(response.text)) == 10
    assert 'href="/?per_page=10&amp;page=2"' in response.text

    _, response = index_client.get('/?per_page=10&page=2', headers=get_auth_headers())
    assert get_index_counter(response.text) == (5, 15)
    assert len(get_tbody_rows(response.text)) == 5


def test_index_per_page_invalid_falls_back_to_default(index_client: SanicTestClient):
    _, response = index_client.get('/?per_page=7', headers=get_auth_headers())
    assert get_index_counter(response.text) == (15, 15)
    assert '<option value="100" selected>' in response.text


def test_index_page_out_of_range_clamps_to_last(index_client: SanicTestClient):
    _, response = index_client.get('/?per_page=10&page=999', headers=get_auth_headers())
    # Страница за пределами диапазона зажимается на последней (5 записей).
    assert get_index_counter(response.text) == (5, 15)


def test_index_page_invalid_number_falls_back_to_first(index_client: SanicTestClient):
    _, response = index_client.get('/?per_page=10&page=abc', headers=get_auth_headers())
    assert get_index_counter(response.text) == (10, 15)


def test_index_pager_two_pages_navigation(index_client: SanicTestClient):
    _, response = index_client.get('/?per_page=10', headers=get_auth_headers())
    # Первая из двух: «‹» неактивна, ссылка на вторую есть.
    assert 'pager__btn is-disabled' in response.text
    assert 'href="/?per_page=10&amp;page=2"' in response.text

    _, response = index_client.get('/?per_page=10&page=2', headers=get_auth_headers())
    # Вторая страница: 5 записей, активна кнопка «2», «›» неактивна.
    assert get_index_counter(response.text) == (5, 15)
    assert '<span class="pager__btn is-active" aria-current="page">2</span>' in response.text
    assert 'href="/?per_page=10&amp;page=1"' in response.text


def test_index_athlete_sees_own_records_and_ignores_status(athlete_index_client: SanicTestClient):
    _, response = athlete_index_client.get('/', headers=athlete_headers())
    assert response.status == 200
    assert get_index_counter(response.text) == (2, 2)
    # Селекта «Статус» у атлета нет; параметр статуса сервер игнорирует.
    assert 'id="index-filter-status"' not in response.text

    _, response = athlete_index_client.get('/?status=approved', headers=athlete_headers())
    assert get_index_counter(response.text) == (2, 2)


def test_index_athlete_filter_applies_to_own_records(athlete_index_client: SanicTestClient):
    storage = app.ctx.storage
    athlete_id = storage.get_user('sportik')['id']
    storage.save_competitions(
        [make_index_record('Атлетов Атлет', sport='Шахматы', date=datetime(2025, 7, 1))],
        review_status='pending',
        owner_id=athlete_id,
    )

    _, response = athlete_index_client.get('/?' + urlencode({'sport': 'Шахматы'}), headers=athlete_headers())
    assert get_index_counter(response.text) == (1, 1)


def test_export_report_slice_xlsx_rows(reports_client: SanicTestClient):
    _, response = reports_client.get('/export/report?group_by=group', headers=get_auth_headers())

    assert response.status == 200
    assert get_xlsx_rows(response.body) == [
        ('Группа', 'Участий', 'Побед', 'Призовых'),
        ('ПГС-102', 1, 0, 0),
        ('ТД-303', 1, 0, 1),
        ('СБ-202', 2, 1, 2),
        ('ПГС-101', 4, 2, 3),
    ]


def test_export_report_slice_with_column_subset(reports_client: SanicTestClient):
    _, response = reports_client.get(
        '/export/report?group_by=sport&columns=' + quote('Побед,Вид спорта'),
        headers=get_auth_headers(),
    )

    # порядок колонок канонический для среза, неизвестные игнорируются
    assert response.status == 200
    assert get_xlsx_rows(response.body) == [
        ('Вид спорта', 'Побед'),
        ('Шахматы', 0),
        ('Лыжи', 1),
        ('Бег', 2),
    ]


def test_export_report_slice_with_filters_and_year(reports_client: SanicTestClient):
    _, response = reports_client.get(
        '/export/report?' + urlencode({'group_by': 'year', 'institute': 'ИМИ', 'sport': 'Бег'}),
        headers=get_auth_headers(),
    )

    assert response.status == 200
    assert get_xlsx_rows(response.body) == [
        ('Год', 'Участий', 'Побед', 'Призовых'),
        (2024, 1, 0, 1),
        (2025, 1, 0, 1),
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


# --- Присутствие пользователей: last_login_at / last_seen_at (№17) ---


def test_login_records_last_login(client: SanicTestClient):
    app.ctx.storage.set_user_last_login.reset_mock()
    _, response = client.post(
        '/login',
        data={
            'username': settings.auth_admin_username,
            'password': settings.auth_admin_password,
        },
        allow_redirects=False,
    )

    assert response.status == 302
    # фикстура отдаёт админу id = 1
    app.ctx.storage.set_user_last_login.assert_called_once_with(1)


def test_authenticated_requests_touch_last_seen_with_throttle(client: SanicTestClient):
    from src.main import reset_presence_tracking

    reset_presence_tracking()
    app.ctx.storage.touch_user_seen.reset_mock()
    headers = get_auth_headers()

    client.get('/', headers=headers)
    client.get('/', headers=headers)
    # два запроса подряд — один UPDATE благодаря троттлингу (окно 60 с)
    app.ctx.storage.touch_user_seen.assert_called_once_with(1)

    # без сессии активность не фиксируется
    app.ctx.storage.touch_user_seen.reset_mock()
    reset_presence_tracking()
    client.get('/login')
    app.ctx.storage.touch_user_seen.assert_not_called()


def test_admin_users_page_shows_presence(client: SanicTestClient):
    from datetime import timedelta
    from datetime import timezone

    now = datetime.now(timezone.utc)
    app.ctx.storage.list_users.return_value = [
        {
            'id': 1,
            'username': 'admin',
            'role': 'admin',
            'active': 1,
            'name_aliases': [],
            'last_login_at': now.isoformat(),
            'last_seen_at': now.isoformat(),
        },
        {
            'id': 2,
            'username': 'stale',
            'role': 'viewer',
            'active': 1,
            'name_aliases': [],
            'last_login_at': (now - timedelta(hours=2)).isoformat(),
            'last_seen_at': (now - timedelta(minutes=30)).isoformat(),
        },
        {
            'id': 3,
            'username': 'ghost',
            'role': 'viewer',
            'active': 0,
            'name_aliases': [],
            'last_login_at': None,
            'last_seen_at': None,
        },
    ]
    try:
        _, response = client.get('/admin/users', headers=get_auth_headers())
        assert response.status == 200
        # онлайн: зелёная точка
        assert 'presence-dot-online' in response.text
        assert 'онлайн' in response.text
        # был(а): 30 минут назад / —
        assert 'был(а): 30 минут назад' in response.text
        assert 'был(а): —' in response.text
        assert 'Последний вход:' in response.text
    finally:
        app.ctx.storage.list_users.return_value = []


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
    # Само автопополнение уровней — внутри одной транзакции импорта, значения
    # сеются из записей (storage_test: autofills levels and catalogs); здесь —
    # что роут отдаёт импорту вставляемые строки с новым уровнем как есть.
    app.ctx.storage.import_competitions.reset_mock()
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
    inserted_rows = app.ctx.storage.import_competitions.call_args[0][0]
    assert [row.level for row in inserted_rows] == ['всероссийские']


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


def level_hard_delete_mocks(level_id: int = 9, name: str = 'городские', records: int = 0):
    app.ctx.storage.list_levels.return_value = [{'id': level_id, 'name': name, 'sort_order': 0, 'active': 1}]
    app.ctx.storage.count_records_using.return_value = records
    app.ctx.storage.hard_delete_level.reset_mock()


def test_admin_hard_deletes_empty_level(client: SanicTestClient):
    level_hard_delete_mocks()
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/levels/9/hard-delete', headers=headers, data=csrf_for(headers), allow_redirects=False
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.hard_delete_level.assert_called_once_with(9)
    app.ctx.storage.list_levels.return_value = []


def test_admin_hard_delete_rejects_level_with_records(client: SanicTestClient):
    level_hard_delete_mocks(records=3)
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/levels/9/hard-delete', headers=headers, data=csrf_for(headers), allow_redirects=False
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']
    app.ctx.storage.hard_delete_level.assert_not_called()
    app.ctx.storage.list_levels.return_value = []
    app.ctx.storage.count_records_using.return_value = 0


def test_level_hard_delete_forbidden_for_non_admin(client: SanicTestClient):
    level_hard_delete_mocks()
    editor_headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/admin/levels/9/hard-delete',
        headers=editor_headers,
        data=csrf_for(editor_headers),
    )
    assert response.status == 403
    app.ctx.storage.hard_delete_level.assert_not_called()
    app.ctx.storage.list_levels.return_value = []
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
    for path in (
        '/admin/catalogs/sport',
        '/admin/catalogs/sport/5/hide',
        '/admin/catalogs/sport/5/delete',
        '/admin/catalogs/group/5/hide',
        '/admin/catalogs/group/5/delete',
        '/admin/catalogs/institute/7/group',
    ):
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


# --- Переименование значений справочников (решение 2026-09-13) ---


def rename_sport_value_mock(row_id: int = 5, value: str = 'Бег'):
    app.ctx.storage.get_catalog_value.return_value = {
        'id': row_id,
        'category': 'sport',
        'value': value,
        'parent_id': None,
        'active': 1,
    }


def reset_rename_mocks():
    app.ctx.storage.get_catalog_value.return_value = None
    app.ctx.storage.list_catalog_all.return_value = []
    app.ctx.storage.count_records_using.return_value = 0
    app.ctx.storage.list_levels.return_value = []


def test_catalog_rename_preview_returns_records_count(client: SanicTestClient):
    rename_sport_value_mock()
    app.ctx.storage.count_records_using.return_value = 4
    app.ctx.storage.rename_catalog_value.reset_mock()
    headers = get_auth_headers()

    # без confirm — только превью, ничего не выполняется
    _, response = client.post(
        '/admin/catalogs/sport/5/rename',
        headers=headers,
        data={**csrf_for(headers), 'new_name': 'Кросс'},
    )
    assert response.status == 200
    assert response.json['preview'] is True
    assert response.json['records'] == 4
    assert response.json['old'] == 'Бег'
    assert response.json['new'] == 'Кросс'
    app.ctx.storage.rename_catalog_value.assert_not_called()

    reset_rename_mocks()


def test_catalog_rename_executes_with_update_and_writes_audit(client: SanicTestClient):
    rename_sport_value_mock()
    app.ctx.storage.rename_catalog_value.reset_mock()
    app.ctx.storage.rename_catalog_value.return_value = 3
    app.ctx.storage.add_audit_event.reset_mock()
    headers = get_auth_headers()

    _, response = client.post(
        '/admin/catalogs/sport/5/rename',
        headers=headers,
        data={**csrf_for(headers), 'new_name': 'Кросс', 'confirm': 'on', 'update_records': 'on'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.rename_catalog_value.assert_called_once_with(
        'sport', 'Бег', 'Кросс', parent_value=None, update_records=True
    )

    audit_call = app.ctx.storage.add_audit_event.call_args
    assert audit_call[1]['action'] == 'catalog_value_renamed'
    details = json.loads(audit_call[1]['details'])
    assert details == {
        'category': 'sport',
        'old': 'Бег',
        'new': 'Кросс',
        'update_records': True,
        'records_updated': 3,
    }

    reset_rename_mocks()
    app.ctx.storage.rename_catalog_value.return_value = 0


def test_catalog_rename_execution_without_update_records_flag(client: SanicTestClient):
    rename_sport_value_mock()
    app.ctx.storage.rename_catalog_value.reset_mock()
    app.ctx.storage.rename_catalog_value.return_value = 0
    headers = get_auth_headers()

    _, response = client.post(
        '/admin/catalogs/sport/5/rename',
        headers=headers,
        data={**csrf_for(headers), 'new_name': 'Кросс', 'confirm': 'on'},  # галочка снята
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'записи не тронуты' in unquote_plus(response.headers['location'])
    app.ctx.storage.rename_catalog_value.assert_called_once_with(
        'sport', 'Бег', 'Кросс', parent_value=None, update_records=False
    )

    reset_rename_mocks()


def test_catalog_rename_level_uses_levels_table(client: SanicTestClient):
    app.ctx.storage.list_levels.return_value = [
        {'id': 9, 'name': 'городские', 'sort_order': 0, 'active': 1},
        {'id': 10, 'name': 'внутривузовские', 'sort_order': 0, 'active': 1},
    ]
    app.ctx.storage.rename_catalog_value.reset_mock()
    app.ctx.storage.rename_catalog_value.return_value = 2
    headers = get_auth_headers()

    _, response = client.post(
        '/admin/catalogs/level/9/rename',
        headers=headers,
        data={**csrf_for(headers), 'new_name': 'муниципальные', 'confirm': 'on'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.rename_catalog_value.assert_called_once_with(
        'level', 'городские', 'муниципальные', parent_value=None, update_records=False
    )

    reset_rename_mocks()
    app.ctx.storage.rename_catalog_value.return_value = 0


def test_catalog_rename_confirmation_page(client: SanicTestClient):
    rename_sport_value_mock()
    app.ctx.storage.count_records_using.return_value = 7
    headers = get_auth_headers()

    _, response = client.get(f'/admin/catalogs/sport/5/rename?new_name={quote("Кросс")}', headers=headers)
    assert response.status == 200
    assert '«Бег» → «Кросс»' in response.text
    assert 'name="update_records"' in response.text  # галочка «обновить записи»
    assert 'name="confirm"' in response.text

    reset_rename_mocks()


def test_catalog_rename_validations(client: SanicTestClient):
    rename_sport_value_mock()
    app.ctx.storage.list_catalog_all.return_value = [
        {'id': 6, 'category': 'sport', 'value': 'Кросс', 'parent_id': None, 'active': 1}
    ]
    app.ctx.storage.rename_catalog_value.reset_mock()
    headers = get_auth_headers()

    # пустое имя
    _, response = client.post(
        '/admin/catalogs/sport/5/rename',
        headers=headers,
        data={**csrf_for(headers), 'new_name': '   ', 'confirm': 'on'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']

    # то же имя
    _, response = client.post(
        '/admin/catalogs/sport/5/rename',
        headers=headers,
        data={**csrf_for(headers), 'new_name': 'Бег', 'confirm': 'on'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']

    # конфликт с существующим значением той же категории
    _, response = client.post(
        '/admin/catalogs/sport/5/rename',
        headers=headers,
        data={**csrf_for(headers), 'new_name': 'Кросс', 'confirm': 'on'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']
    app.ctx.storage.rename_catalog_value.assert_not_called()

    # неизвестная категория
    _, response = client.post(
        '/admin/catalogs/unknown/5/rename',
        headers=headers,
        data={**csrf_for(headers), 'new_name': 'X', 'confirm': 'on'},
    )
    assert response.status == 404

    reset_rename_mocks()


def test_catalog_rename_group_uses_parent_institute(client: SanicTestClient):
    app.ctx.storage.get_catalog_value.side_effect = lambda value_id: {
        7: {'id': 7, 'category': 'institute', 'value': 'ИСИ', 'parent_id': None, 'active': 1},
        8: {'id': 8, 'category': 'group', 'value': 'ПГС-101', 'parent_id': 7, 'active': 1},
    }.get(value_id)
    app.ctx.storage.list_catalog_all.return_value = []
    app.ctx.storage.rename_catalog_value.reset_mock()
    app.ctx.storage.rename_catalog_value.return_value = 2
    headers = get_auth_headers()

    _, response = client.post(
        '/admin/catalogs/group/8/rename',
        headers=headers,
        data={**csrf_for(headers), 'new_name': 'ПГС-101А', 'confirm': 'on', 'update_records': 'on'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.rename_catalog_value.assert_called_once_with(
        'group', 'ПГС-101', 'ПГС-101А', parent_value='ИСИ', update_records=True
    )
    audit_details = json.loads(app.ctx.storage.add_audit_event.call_args[1]['details'])
    assert audit_details['parent'] == 'ИСИ'

    app.ctx.storage.get_catalog_value.side_effect = None
    reset_rename_mocks()
    app.ctx.storage.rename_catalog_value.return_value = 0


def test_catalog_rename_forbidden_for_non_admin(client: SanicTestClient):
    editor_headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/admin/catalogs/sport/5/rename',
        headers=editor_headers,
        data={**csrf_for(editor_headers), 'new_name': 'Кросс', 'confirm': 'on'},
    )
    assert response.status == 403

    _, response = client.get('/admin/catalogs/sport/5/rename?new_name=Кросс', headers=editor_headers)
    assert response.status == 403


def test_old_level_rename_route_removed(client: SanicTestClient):
    # Старый POST /admin/levels/<id> (rename без каскада) удалён: UI переехал
    # на /admin/catalogs/<category>/<id>/rename. GET несуществующего пути — 404;
    # POST перехватывается CSRF-middleware до роутера (Sanic не парсит form-body
    # для unmatched-маршрутов) — как у удалённого /clean_db.
    headers = get_auth_headers()
    _, response = client.get('/admin/levels/9', headers=headers, allow_redirects=False)
    assert response.status == 404

    _, response = client.post('/admin/levels/9', headers=headers, data=csrf_for(headers), allow_redirects=False)
    assert response.status == 403


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
    # Справочники импорт пополняет из вставленных записей внутри
    # storage.import_competitions (одна транзакция с записями); само
    # пополнение проверено на реальном адаптере в storage_test (autofills
    # levels and catalogs). Здесь — что строки файла со значениями доходят
    # до импорта.
    app.ctx.storage.import_competitions.reset_mock()
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
    inserted_rows = app.ctx.storage.import_competitions.call_args[0][0]
    assert [(row.sport, row.institute) for row in inserted_rows] == [('Шахматы', 'АДИ')]


def test_index_datalists_show_active_catalog_values(client: SanicTestClient):
    # фикстура: list_catalog('sport') → ['Бег', 'Лыжи'], list_catalog('institute') → ['ИСИ']
    # отдельная форма «Добавить запись» убрана (ввод — инлайн-строкой), datalist
    # остаётся в разметке: script.js вешает его на поля инлайн-строки по id
    _, response = client.get('/', headers=get_auth_headers(role='editor'))
    assert response.status == 200
    assert '<datalist id="sport-options">' in response.text
    assert '<datalist id="institute-options">' in response.text
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


# --- Иерархия справочника: институты содержат группы (№15/№4) ---


def test_index_table_carries_group_hints_by_institute(client: SanicTestClient):
    # карта институт → группы отдаётся таблице в data-атрибуте: комбобокс
    # «Группа» фильтрует список выбранным институтом
    record = Competition(
        record_id='1',
        student_id='hash-1',
        student_name='Иванов Иван Иванович',
        student_sex='М',
        institute='ИСИ',
        group='ПГС-101',
        course=2,
        sport='Бег',
        date=datetime(2026, 1, 1),
        level='внутривузовские',
        name='Кубок',
        position=1,
    )
    app.ctx.storage.get_competitions_page.return_value = [record]
    app.ctx.storage.count_competitions_filtered.return_value = 1
    app.ctx.storage.get_group_options_by_institute.return_value = {'ИСИ': ['ПГС-101', 'ПГС-102']}
    try:
        _, response = client.get('/', headers=get_auth_headers(role='editor'))
        assert response.status == 200
        assert 'data-groups-by-institute' in response.text
        # tojson экранирует кириллицу в \u-последовательности — сверяем точный JSON
        import json

        assert json.dumps({'ИСИ': ['ПГС-101', 'ПГС-102']}) in response.text
    finally:
        app.ctx.storage.get_competitions_page.return_value = []
        app.ctx.storage.count_competitions_filtered.return_value = 0
        app.ctx.storage.get_group_options_by_institute.return_value = {}


def test_admin_catalogs_page_shows_institute_hierarchy(client: SanicTestClient):
    app.ctx.storage.list_catalog_tree.return_value = [
        {
            'id': 1,
            'category': 'institute',
            'value': 'ИСИ',
            'parent_id': None,
            'active': 1,
            'records_count': 3,
            'groups': [
                {'id': 11, 'category': 'group', 'value': 'ПГС-101', 'parent_id': 1, 'active': 1, 'records_count': 2},
                {'id': 12, 'category': 'group', 'value': 'ПГС-102', 'parent_id': 1, 'active': 0, 'records_count': 0},
            ],
        }
    ]
    try:
        _, response = client.get('/admin/catalogs', headers=get_auth_headers())
        assert response.status == 200
        assert 'Институты и группы' in response.text
        assert 'ПГС-101' in response.text
        assert 'action="/admin/catalogs/institute/1/group"' in response.text
        assert 'action="/admin/catalogs/group/11/hide"' in response.text
        assert 'action="/admin/catalogs/group/12/unhide"' in response.text
        # удаление — только у группы без записей
        assert 'action="/admin/catalogs/group/12/delete"' in response.text
        assert 'action="/admin/catalogs/group/11/delete"' not in response.text
    finally:
        app.ctx.storage.list_catalog_tree.return_value = []


def test_admin_can_add_group_to_institute(client: SanicTestClient):
    app.ctx.storage.get_catalog_value.return_value = {
        'id': 7,
        'category': 'institute',
        'value': 'ИСИ',
        'parent_id': None,
        'active': 1,
    }
    app.ctx.storage.add_catalog_value.reset_mock()
    headers = get_auth_headers()

    _, response = client.post(
        '/admin/catalogs/institute/7/group',
        headers=headers,
        data={**csrf_for(headers), 'value': 'ПГС-101'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.add_catalog_value.assert_called_once_with('group', 'ПГС-101', parent_id=7)

    _, response = client.post(
        '/admin/catalogs/institute/7/group',
        headers=headers,
        data={**csrf_for(headers), 'value': '   '},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']
    app.ctx.storage.get_catalog_value.return_value = None


def test_group_delete_uses_pair_record_count(client: SanicTestClient):
    app.ctx.storage.get_catalog_value.side_effect = lambda value_id: {
        7: {'id': 7, 'category': 'institute', 'value': 'ИСИ', 'parent_id': None, 'active': 1},
        6: {'id': 6, 'category': 'group', 'value': 'ПГС-101', 'parent_id': 7, 'active': 1},
    }.get(value_id)
    app.ctx.storage.count_records_using.reset_mock()
    app.ctx.storage.count_records_using.return_value = 2
    app.ctx.storage.delete_catalog_value.reset_mock()
    headers = get_auth_headers()

    _, response = client.post('/admin/catalogs/group/6/delete', headers=headers, data=csrf_for(headers))
    assert response.status == 400
    app.ctx.storage.delete_catalog_value.assert_not_called()
    # счётчик — по паре институт+группа, а не по одному названию группы
    app.ctx.storage.count_records_using.assert_called_once_with('group', 'ПГС-101', parent_value='ИСИ')

    app.ctx.storage.count_records_using.return_value = 0
    _, response = client.post(
        '/admin/catalogs/group/6/delete', headers=headers, data=csrf_for(headers), allow_redirects=False
    )
    assert response.status == 302
    app.ctx.storage.delete_catalog_value.assert_called_once_with(6)

    app.ctx.storage.get_catalog_value.side_effect = None
    app.ctx.storage.get_catalog_value.return_value = None
    app.ctx.storage.count_records_using.return_value = 0


def test_institute_delete_blocked_until_groups_removed(client: SanicTestClient):
    app.ctx.storage.get_catalog_value.return_value = {
        'id': 7,
        'category': 'institute',
        'value': 'ИСИ',
        'parent_id': None,
        'active': 1,
    }
    app.ctx.storage.count_records_using.return_value = 0
    app.ctx.storage.count_child_groups.return_value = 2
    app.ctx.storage.delete_catalog_value.reset_mock()
    headers = get_auth_headers()

    _, response = client.post('/admin/catalogs/institute/7/delete', headers=headers, data=csrf_for(headers))
    assert response.status == 400
    assert 'сначала удалите' in response.text
    app.ctx.storage.delete_catalog_value.assert_not_called()

    app.ctx.storage.count_child_groups.return_value = 0
    _, response = client.post(
        '/admin/catalogs/institute/7/delete', headers=headers, data=csrf_for(headers), allow_redirects=False
    )
    assert response.status == 302
    app.ctx.storage.delete_catalog_value.assert_called_once_with(7)

    app.ctx.storage.get_catalog_value.return_value = None
    app.ctx.storage.count_child_groups.return_value = 0


def test_create_competition_auto_adds_catalog_pair(client: SanicTestClient):
    app.ctx.storage.ensure_catalog_pair.reset_mock()
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
    app.ctx.storage.ensure_catalog_pair.assert_called_once_with('ФМА', 'ПГС-101')


def test_import_auto_adds_catalog_pair(client: SanicTestClient):
    # Пара институт→группа из импорта попадает в иерархию справочника тем же
    # батчем (проверено на реальном адаптере в storage_test); здесь — что
    # строки с парой доходят до импорта целиком.
    app.ctx.storage.import_competitions.reset_mock()
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
    inserted_rows = app.ctx.storage.import_competitions.call_args[0][0]
    assert [(row.institute, row.group) for row in inserted_rows] == [('АДИ', 'ША-101')]


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
    headers = athlete_headers()
    _, response = client.get('/', headers=headers)
    assert response.status == 200
    # Реестр читается через страницу с серверными фильтрами/пагинацией:
    # видимость атлета (только свои записи) задаётся owner_id в обоих
    # запросах — счётчик и выборку страницы.
    page_kwargs = app.ctx.storage.get_competitions_page.call_args[1]
    assert page_kwargs['owner_id'] == 1
    assert page_kwargs['student_id_hashes'] == []
    count_kwargs = app.ctx.storage.count_competitions_filtered.call_args[1]
    assert count_kwargs['owner_id'] == 1
    assert count_kwargs['student_id_hashes'] == []


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

    _, response = client.get('/report?group_by=group', headers=headers)
    assert response.status == 403

    _, response = client.get('/export/index', headers=headers)
    assert response.status == 403

    _, response = client.get('/export/report', headers=headers)
    assert response.status == 403

    _, response = client.get('/export/report?group_by=institute', headers=headers)
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


# --- Волна 0 живого фидбека: баг «nan», №1.1 регистронезависимость, №19б аккордеон ---


def make_import_row(**overrides) -> dict:
    row = {
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
    row.update(overrides)
    return row


def upload_xlsx(client: SanicTestClient, rows: list[dict]):
    df = pd.DataFrame(rows)
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
    return response


def test_upload_empty_cells_become_empty_not_nan(client: SanicTestClient):
    # Баг с живого импорта (docs/feedback-live.md «nan»): пустые ячейки Excel
    # (pandas NaN) не должны превращаться в текст «nan» в значениях записи.
    app.ctx.storage.import_competitions.reset_mock()
    app.ctx.storage.get_competitions.return_value = []
    response = upload_xlsx(client, [make_import_row(**{'Пол': None, 'Институт': None, 'Группа': None})])

    assert response.status == 200
    saved = app.ctx.storage.import_competitions.call_args[0][0][0]
    assert saved.student_sex == ''
    assert saved.institute == ''
    assert saved.group == ''
    assert saved.sport == 'Бег'
    app.ctx.storage.get_competitions.return_value = []


def test_upload_empty_level_becomes_empty_not_nan(client: SanicTestClient):
    app.ctx.storage.import_competitions.reset_mock()
    app.ctx.storage.get_competitions.return_value = []
    response = upload_xlsx(client, [make_import_row(**{'Уровень соревнований': None})])

    assert response.status == 200
    saved = app.ctx.storage.import_competitions.call_args[0][0][0]
    assert saved.level == ''
    app.ctx.storage.get_competitions.return_value = []


def test_upload_case_variants_get_canonical_catalog_values(client: SanicTestClient):
    # №1.1: значение, отличающееся от справочника только регистром,
    # заменяется каноническим написанием ещё на входе в запись.
    app.ctx.storage.import_competitions.reset_mock()
    app.ctx.storage.get_competitions.return_value = []

    def fake_canonical(category, value, parent_id=None):
        by_category = {
            'institute': {'иси': 'ИСИ'},
            'sport': {'бег': 'Бег'},
            'group': {'пгс-101': 'ПГС-101'},
        }
        return by_category.get(category, {}).get(value.strip().lower())

    app.ctx.storage.find_catalog_canonical.side_effect = fake_canonical
    app.ctx.storage.find_catalog_row.return_value = {
        'id': 1,
        'category': 'institute',
        'value': 'ИСИ',
        'parent_id': None,
        'active': 1,
    }
    app.ctx.storage.find_level_canonical.return_value = 'внутривузовские'

    response = upload_xlsx(
        client,
        [
            make_import_row(
                **{
                    'Институт': 'иси',
                    'Группа': 'ПГС-101',
                    'Вид спорта': 'БЕГ',
                    'Уровень соревнований': 'Внутривузовские',
                }
            )
        ],
    )

    assert response.status == 200
    saved = app.ctx.storage.import_competitions.call_args[0][0][0]
    assert saved.institute == 'ИСИ'
    assert saved.sport == 'Бег'
    assert saved.group == 'ПГС-101'
    assert saved.level == 'внутривузовские'
    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.find_catalog_canonical.side_effect = None
    app.ctx.storage.find_catalog_canonical.return_value = None
    app.ctx.storage.find_catalog_row.return_value = None
    app.ctx.storage.find_level_canonical.return_value = None


def test_manual_competition_uses_canonical_catalog_values(client: SanicTestClient):
    app.ctx.storage.save_competitions.reset_mock()

    def fake_canonical(category, value, parent_id=None):
        if category == 'institute' and value.lower() == 'иси':
            return 'ИСИ'
        return None

    app.ctx.storage.find_catalog_canonical.side_effect = fake_canonical

    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Новиков Новел Новикович',
            'student_sex': 'М',
            'institute': 'иси',
            'group': 'ПГС-101',
            'sport': 'Бег',
            'date': '20.03.2026',
            'level': 'внутривузовские',
            'name': 'Кубок',
            'position': '2',
            'course': '1',
        },
        allow_redirects=False,
    )
    assert response.status == 302
    saved = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert saved.institute == 'ИСИ'
    app.ctx.storage.find_catalog_canonical.side_effect = None
    app.ctx.storage.find_catalog_canonical.return_value = None


def test_import_autofills_institute_from_unique_group(client: SanicTestClient):
    # №19a: группа известна, институт пуст, группа принадлежит ровно одному
    # институту — институт подставляется каноническим значением.
    app.ctx.storage.import_competitions.reset_mock()
    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.find_unique_group_institute.return_value = 'ИСИ'
    app.ctx.storage.find_catalog_row.return_value = {
        'id': 1,
        'category': 'institute',
        'value': 'ИСИ',
        'parent_id': None,
        'active': 1,
    }
    app.ctx.storage.find_catalog_canonical.return_value = 'ПГС-101-и'

    response = upload_xlsx(
        client,
        [make_import_row(**{'Институт': '', 'Группа': 'пгс-101-и'})],
    )

    assert response.status == 200
    saved = app.ctx.storage.import_competitions.call_args[0][0][0]
    assert saved.institute == 'ИСИ'
    assert saved.group == 'ПГС-101-и'
    app.ctx.storage.find_unique_group_institute.return_value = None
    app.ctx.storage.find_catalog_row.return_value = None
    app.ctx.storage.find_catalog_canonical.return_value = None


def test_import_ambiguous_group_leaves_institute_empty(client: SanicTestClient):
    # №19a: одно имя группы в разных институтах — институт не подставляется.
    app.ctx.storage.import_competitions.reset_mock()
    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.find_unique_group_institute.return_value = None

    response = upload_xlsx(
        client,
        [make_import_row(**{'Институт': '', 'Группа': 'ПГС-101'})],
    )

    assert response.status == 200
    saved = app.ctx.storage.import_competitions.call_args[0][0][0]
    assert saved.institute == ''
    assert saved.group == 'ПГС-101'
    app.ctx.storage.find_unique_group_institute.return_value = None


def test_import_empty_group_keeps_institute_empty(client: SanicTestClient):
    # №19a: группа не указана — автозаполнение не запускается.
    app.ctx.storage.import_competitions.reset_mock()
    app.ctx.storage.get_competitions.return_value = []

    response = upload_xlsx(
        client,
        [make_import_row(**{'Институт': '', 'Группа': ''})],
    )

    assert response.status == 200
    saved = app.ctx.storage.import_competitions.call_args[0][0][0]
    assert saved.institute == ''
    app.ctx.storage.find_unique_group_institute.return_value = None


def test_manual_competition_autofills_institute_from_unique_group(client: SanicTestClient):
    # №19a: ручной ввод — тот же путь build_competition.
    app.ctx.storage.save_competitions.reset_mock()
    app.ctx.storage.find_unique_group_institute.return_value = 'ИСИ'

    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/competition',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Абрамов Артём Абрамович',
            'student_sex': 'М',
            'institute': '',
            'group': 'ПГС-101',
            'sport': 'Бег',
            'date': '20.03.2026',
            'level': 'внутривузовские',
            'name': 'Кубок',
            'position': '2',
            'course': '1',
        },
        allow_redirects=False,
    )
    assert response.status == 302
    saved = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert saved.institute == 'ИСИ'
    app.ctx.storage.find_unique_group_institute.return_value = None


def test_index_renders_url_custom_field_as_link(client: SanicTestClient):
    # №3: url-кастомное поле в таблице — гиперссылка target=_blank;
    # значение с посторонней схемой — обычный текст.
    record = Competition(
        student_id='2',
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
        extra_data={
            'link': 'https://sport.example.com/race',
            'bad': 'javascript:alert(1)',
        },
    )
    app.ctx.storage.get_competitions_page.return_value = [record]
    app.ctx.storage.count_competitions_filtered.return_value = 1
    app.ctx.storage.get_custom_fields.return_value = [
        CustomField(field_id=1, key='link', label='Ссылка на соревнование', field_type='url'),
        CustomField(field_id=2, key='bad', label='Плохая ссылка', field_type='url'),
    ]

    headers = get_auth_headers(role='editor')
    _, response = client.get('/', headers=headers)
    assert response.status == 200
    body = response.text
    expected = (
        '<a href="https://sport.example.com/race" class="link" '
        'rel="noopener noreferrer" target="_blank">https://sport.example.com/race</a>'
    )
    assert expected in body
    assert 'javascript:alert(1)' in body
    assert '<a href="javascript:' not in body
    app.ctx.storage.get_custom_fields.return_value = []
    app.ctx.storage.get_competitions_page.return_value = []
    app.ctx.storage.count_competitions_filtered.return_value = 0


def test_admin_catalog_add_rejects_case_duplicate(client: SanicTestClient):
    # №1.1: ручное добавление регистрового дубля — отказ с подсказкой.
    app.ctx.storage.add_catalog_value.reset_mock()
    app.ctx.storage.find_catalog_canonical.return_value = 'Бег'

    headers = get_auth_headers()
    _, response = client.post(
        '/admin/catalogs/sport',
        headers=headers,
        data={**csrf_for(headers), 'value': 'бег'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']
    assert 'Бег' in unquote_plus(response.headers['location'])
    app.ctx.storage.add_catalog_value.assert_not_called()
    app.ctx.storage.find_catalog_canonical.return_value = None


def test_admin_catalog_add_allows_exact_and_new_values(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/catalogs/sport',
        headers=headers,
        data={**csrf_for(headers), 'value': 'Плавание'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.add_catalog_value.assert_called_with('sport', 'Плавание')


def test_admin_group_add_rejects_case_duplicate_within_institute(client: SanicTestClient):
    app.ctx.storage.add_catalog_value.reset_mock()
    app.ctx.storage.get_catalog_value.return_value = {
        'id': 5,
        'category': 'institute',
        'value': 'ИСИ',
        'parent_id': None,
        'active': 1,
    }
    app.ctx.storage.find_catalog_canonical.return_value = 'ПГС-101'

    headers = get_auth_headers()
    _, response = client.post(
        '/admin/catalogs/institute/5/group',
        headers=headers,
        data={**csrf_for(headers), 'value': 'пгс-101'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']
    app.ctx.storage.add_catalog_value.assert_not_called()
    app.ctx.storage.get_catalog_value.return_value = None
    app.ctx.storage.find_catalog_canonical.return_value = None


def test_admin_level_add_rejects_case_duplicate(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/levels',
        headers=headers,
        data={**csrf_for(headers), 'name': 'Внутривузовские'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' in response.headers['location']
    # Имя приведено к lower до проверки, поэтому «Внутривузовские» ловится
    # как дубль «внутривузовские» (источник регистровых дублей устранён).
    assert unquote_plus(response.headers['location']).startswith('/admin/catalogs?admin_error=')


def test_admin_level_add_normalizes_to_lower(client: SanicTestClient):
    # Уровень всегда хранится в lower (как из импорта); ручное добавление
    # приведено к тому же виду — источник регистровых дублей устранён.
    app.ctx.storage.create_level.reset_mock()
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/levels',
        headers=headers,
        data={**csrf_for(headers), 'name': 'ГОРОДСКИЕ'},
        allow_redirects=False,
    )
    assert response.status == 302
    assert 'admin_error' not in response.headers['location']
    app.ctx.storage.create_level.assert_called_with('городские')


def test_admin_catalogs_institutes_are_collapsed_accordion(client: SanicTestClient):
    # №19б: институты — <details>/<summary> без JS, по умолчанию свёрнуто.
    app.ctx.storage.list_catalog_tree.return_value = [
        {
            'id': 1,
            'category': 'institute',
            'value': 'ИСИ',
            'parent_id': None,
            'active': 1,
            'records_count': 3,
            'groups': [
                {
                    'id': 2,
                    'category': 'group',
                    'value': 'ПГС-101',
                    'parent_id': 1,
                    'active': 1,
                    'records_count': 2,
                }
            ],
        }
    ]
    headers = get_auth_headers()
    _, response = client.get('/admin/catalogs', headers=headers)
    assert response.status == 200
    assert '<details class="catalog-institute' in response.text
    assert '<summary>' in response.text
    assert not re.search(r'<details[^>]*\bopen\b', response.text), 'институты должны быть свёрнуты по умолчанию'
    assert 'ПГС-101' in response.text
    app.ctx.storage.list_catalog_tree.return_value = []


# Даты-диапазоны (решение 2026-09-13, docs/data-model-decisions.md
# «Даты-диапазоны: визуально одно, под капотом два»).


def test_parse_date_value_single_day():
    date, date_to = parse_date_value('25.06.2026')
    assert date == datetime(2026, 6, 25)
    assert date_to is None


def test_parse_date_value_one_month_range():
    date, date_to = parse_date_value('25-27.06.2026')
    assert date == datetime(2026, 6, 25)
    assert date_to == datetime(2026, 6, 27)


def test_parse_date_value_one_year_range():
    date, date_to = parse_date_value('30.01-01.02.2026')
    assert date == datetime(2026, 1, 30)
    assert date_to == datetime(2026, 2, 1)


def test_parse_date_value_full_range():
    date, date_to = parse_date_value('28.12.2025-02.01.2026')
    assert date == datetime(2025, 12, 28)
    assert date_to == datetime(2026, 1, 2)


def test_parse_date_value_rejects_end_before_start():
    with pytest.raises(ValueError):
        parse_date_value('27-25.06.2026')


def test_parse_date_value_rejects_invalid_date():
    with pytest.raises(ValueError):
        parse_date_value('32.06.2026')
    with pytest.raises(ValueError):
        parse_date_value('25-26.13.2026')


def test_format_date_range_variants():
    assert format_date_range(datetime(2026, 6, 25), None) == '25.06.2026'
    assert format_date_range(datetime(2026, 6, 25), datetime(2026, 6, 27)) == '25-27.06.2026'
    assert format_date_range(datetime(2026, 1, 30), datetime(2026, 2, 1)) == '30.01-01.02.2026'
    assert format_date_range(datetime(2025, 12, 28), datetime(2026, 1, 2)) == '28.12.2025-02.01.2026'


def test_manual_competition_accepts_range(client: SanicTestClient):
    _, response = client.post(
        '/competition',
        headers=get_auth_headers(),
        data={
            **csrf_for(get_auth_headers()),
            'student_name': 'Диапазон Дарья',
            'student_sex': 'Ж',
            'institute': 'ИСИ',
            'group': 'ПГС-101',
            'course': '2',
            'sport': 'Легкая атлетика',
            'date': '25-27.06.2026',
            'level': 'межвузовские',
            'name': 'Летний кубок',
            'position': '2',
        },
        allow_redirects=False,
    )

    assert response.status == 302
    competition = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert competition.date == datetime(2026, 6, 25)
    assert competition.date_to == datetime(2026, 6, 27)


def test_manual_competition_rejects_range_end_before_start(client: SanicTestClient):
    _, response = client.post(
        '/competition',
        headers=get_auth_headers(),
        data={
            **csrf_for(get_auth_headers()),
            'student_name': 'Диапазон Дарья',
            'student_sex': 'Ж',
            'institute': 'ИСИ',
            'group': 'ПГС-101',
            'course': '2',
            'sport': 'Легкая атлетика',
            'date': '27-25.06.2026',
            'level': 'межвузовские',
            'name': 'Летний кубок',
            'position': '2',
        },
        allow_redirects=False,
    )

    assert response.status == 400


def test_import_accepts_range_in_date_column(client: SanicTestClient):
    storage = app.ctx.storage
    storage.find_catalog_canonical.return_value = None
    storage.find_level_canonical.return_value = None
    storage.find_catalog_row.return_value = None
    storage.find_unique_group_institute.return_value = None
    competition = build_competition(
        {
            'ФИО': 'Импорт Игорь',
            'Пол': 'М',
            'Институт': 'ИСИ',
            'Группа': 'ПГС-101',
            'Вид спорта': 'Бег',
            'Дата': '28.12.2025-02.01.2026',
            'Уровень соревнований': 'межвузовские',
            'Название соревнований': 'Новогодний кубок',
            'Место': '1',
            'Курс': 2,
        },
        custom_fields=[],
        storage=None,
    )
    assert competition.date == datetime(2025, 12, 28)
    assert competition.date_to == datetime(2026, 1, 2)


def test_export_range_roundtrips_through_import():
    competition = Competition(
        student_id='id-1',
        student_name='Экспорт Елена',
        student_sex='Ж',
        institute='ИСИ',
        group='ПГС-101',
        course=2,
        sport='Бег',
        date=datetime(2026, 1, 30),
        date_to=datetime(2026, 2, 1),
        level='внутривузовские',
        name='Зимний кубок',
        position=1,
    )
    row = competition_to_export_row(competition, [])
    # Компактная строка в существующей колонке «Дата» — без новой колонки.
    assert 'Дата по' not in row
    assert row['Дата'] == '30.01-01.02.2026'

    buffer = BytesIO()
    pd.DataFrame.from_records([row]).to_excel(buffer, index=False)
    buffer.seek(0)
    imported = build_competition(pd.read_excel(buffer).iloc[0].to_dict(), custom_fields=[])

    assert competition_duplicate_key(imported) == competition_duplicate_key(competition)
    assert imported.date == datetime(2026, 1, 30)
    assert imported.date_to == datetime(2026, 2, 1)


def test_duplicate_key_uses_start_date_only():
    existing = Competition(
        student_id='id-1',
        student_name='Дубль Дмитрий',
        student_sex='М',
        institute='ИСИ',
        group='ПГС-101',
        course=2,
        sport='Бег',
        date=datetime(2026, 6, 25),
        date_to=datetime(2026, 6, 27),
        level='внутривузовские',
        name='Кубок',
        position=1,
    )
    incoming = Competition(
        student_id='id-2',
        student_name='Дубль Дмитрий',
        student_sex='М',
        institute='ИСИ',
        group='ПГС-101',
        course=2,
        sport='Бег',
        date=datetime(2026, 6, 25),
        level='внутривузовские',
        name='Кубок',
        position=3,
    )

    new_competitions, skipped, conflicts = split_import_competitions([incoming], [existing])
    assert new_competitions == []
    assert skipped == 1
    assert conflicts == []


# --- Волна 3: лёгкий реестр полей (№2/№7) ---


class FakeSettingsStorage:
    """Минимальный storage для build_competition: только настройки полей
    и «пустые» справочники канонизации."""

    def __init__(self, field_settings=None):
        self._field_settings = field_settings or {}

    def get_field_settings(self):
        return self._field_settings

    def find_catalog_row(self, *args, **kwargs):
        return None

    def find_catalog_canonical(self, *args, **kwargs):
        return None

    def find_level_canonical(self, *args, **kwargs):
        return None

    def find_unique_group_institute(self, *args, **kwargs):
        return None


def make_import_record(**overrides):
    record = {
        'ФИО': 'Реестров Реестр Реестрович',
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
    record.update(overrides)
    return record


def test_field_settings_defaults_lock_name_and_date():
    from src.main import get_base_field_settings

    merged = get_base_field_settings(None)
    assert merged['student_name'] == {'value_type': 'text', 'required': True}
    assert merged['date'] == {'value_type': 'text', 'required': True}
    assert merged['course'] == {'value_type': 'number', 'required': True}


def test_field_settings_locked_fields_survive_bad_storage_rows():
    from src.main import get_base_field_settings

    storage = FakeSettingsStorage(
        {
            'student_name': {'value_type': 'number', 'required': False},
            'date': {'value_type': 'number', 'required': False},
        }
    )
    merged = get_base_field_settings(storage)
    assert merged['student_name'] == {'value_type': 'text', 'required': True}
    assert merged['date'] == {'value_type': 'text', 'required': True}


def test_build_competition_text_course_graduate_case():
    from src.main import BASE_FIELD_SETTING_DEFAULTS

    settings = {
        key: {'value_type': value_type, 'required': required}
        for key, (value_type, required) in BASE_FIELD_SETTING_DEFAULTS.items()
    }
    settings['course'] = {'value_type': 'text', 'required': False}
    storage = FakeSettingsStorage(settings)
    competition = build_competition(
        make_import_record(Курс='Выпускник 2025/26'),
        custom_fields=[],
        storage=storage,
    )
    assert competition.course == 'Выпускник 2025/26'


def test_build_competition_number_course_rejects_graduate_case():
    from src.main import BASE_FIELD_SETTING_DEFAULTS

    settings = {
        key: {'value_type': value_type, 'required': required}
        for key, (value_type, required) in BASE_FIELD_SETTING_DEFAULTS.items()
    }
    settings['course'] = {'value_type': 'number', 'required': True}
    storage = FakeSettingsStorage(settings)
    with pytest.raises(ValueError):
        build_competition(
            make_import_record(Курс='Выпускник 2025/26'),
            custom_fields=[],
            storage=storage,
        )


def test_build_competition_required_text_field_rejects_empty():
    from src.main import BASE_FIELD_SETTING_DEFAULTS

    settings = {
        key: {'value_type': value_type, 'required': required}
        for key, (value_type, required) in BASE_FIELD_SETTING_DEFAULTS.items()
    }
    settings['group'] = {'value_type': 'text', 'required': True}
    storage = FakeSettingsStorage(settings)
    with pytest.raises(ValueError, match='Группа'):
        build_competition(
            make_import_record(Группа=''),
            custom_fields=[],
            storage=storage,
        )


def test_build_competition_name_and_date_always_required():
    storage = FakeSettingsStorage(
        {
            'student_name': {'value_type': 'text', 'required': False},
            'date': {'value_type': 'text', 'required': False},
        }
    )
    with pytest.raises(ValueError, match='ФИО'):
        build_competition(make_import_record(ФИО=''), custom_fields=[], storage=storage)
    with pytest.raises(ValueError, match='Дата'):
        build_competition(make_import_record(Дата=''), custom_fields=[], storage=storage)


def test_save_base_field_settings_updates_and_audits(client: SanicTestClient):
    app.ctx.storage.get_field_settings.return_value = {}
    app.ctx.storage.update_field_settings.reset_mock()
    headers = get_auth_headers(role='admin')
    _, response = client.post(
        '/admin/fields/base',
        headers=headers,
        data={
            **csrf_for(headers),
            'type_course': 'text',
            'required_course': 'on',
            'type_position': 'number',
        },
        allow_redirects=False,
    )
    assert response.status == 302
    app.ctx.storage.update_field_settings.assert_called_once()
    saved = app.ctx.storage.update_field_settings.call_args[0][0]
    assert saved['course'] == ('text', True)
    assert saved['position'] == ('number', False)
    # Заблокированные поля игнорируют форму: обязательность не отключается.
    assert saved['student_name'][1] is True
    assert saved['date'][1] is True


def test_base_field_settings_editor_forbidden(client: SanicTestClient):
    headers = get_auth_headers(role='editor')
    _, response = client.get('/admin/fields', headers=headers)
    assert response.status == 403
    _, response = client.post(
        '/admin/fields/base',
        headers=headers,
        data=csrf_for(headers),
    )
    assert response.status == 403


# --- Волна 3: очередь конфликтов импорта (№6/№8) ---


def test_upload_similar_row_goes_to_queue(client: SanicTestClient):
    app.ctx.storage.get_field_settings.return_value = {}
    app.ctx.storage.import_competitions.reset_mock()
    app.ctx.storage.add_import_queue_entry.reset_mock()
    app.ctx.storage.get_competitions.return_value = [
        Competition(
            record_id='7',
            student_id='1',
            student_name='Похожий Павел',
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
            # Похожая строка: ФИО+дата совпадают, вид спорта другой → очередь.
            {
                'ФИО': 'Похожий Павел',
                'Пол': 'М',
                'Институт': 'ИСИ',
                'Группа': 'ПГС-101',
                'Вид спорта': 'Лыжи',
                'Дата': '15.03.2026',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Кубок',
                'Место': 2,
                'Курс': 2,
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
        allow_redirects=False,
    )
    assert response.status == 200
    assert 'Импортировано записей: 0' in response.text
    assert 'На подтверждение: 1' in response.text
    assert app.ctx.storage.import_competitions.call_args[0][0] == []
    app.ctx.storage.add_import_queue_entry.assert_called_once()
    payload = app.ctx.storage.add_import_queue_entry.call_args[0][0]
    assert payload['sport'] == 'Лыжи'
    assert app.ctx.storage.add_import_queue_entry.call_args[1]['matched_record_id'] == 7


def test_upload_exact_duplicate_still_skipped_as_before(client: SanicTestClient):
    app.ctx.storage.get_field_settings.return_value = {}
    app.ctx.storage.import_competitions.reset_mock()
    app.ctx.storage.add_import_queue_entry.reset_mock()
    app.ctx.storage.get_competitions.return_value = [
        Competition(
            student_id='1',
            student_name='Дубль Дарья',
            student_sex='Ж',
            institute='ИСИ',
            group='ПГС-101',
            course=1,
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
                'ФИО': 'Дубль Дарья',
                'Пол': 'Ж',
                'Институт': 'ИСИ',
                'Группа': 'ПГС-101',
                'Вид спорта': 'Бег',
                'Дата': '15.03.2026',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Кубок',
                'Место': 1,
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
        allow_redirects=False,
    )
    assert response.status == 200
    assert 'Пропущено дублей: 1' in response.text
    assert 'На подтверждение' not in response.text
    assert app.ctx.storage.import_competitions.call_args[0][0] == []
    app.ctx.storage.add_import_queue_entry.assert_not_called()


def test_import_queue_page_admin_only_and_renders(client: SanicTestClient):
    app.ctx.storage.get_field_settings.return_value = {}
    entry_payload = {
        'student_id': 'hash-1',
        'student_name': 'Похожий Павел',
        'student_sex': 'М',
        'institute': 'ИСИ',
        'group': 'ПГС-101',
        'course': 2,
        'sport': 'Лыжи',
        'date': '2026-03-15T00:00:00',
        'date_to': None,
        'level': 'внутривузовские',
        'name': 'Кубок',
        'position': 2,
        'extra_data': {},
        'record_id': None,
        'created_at': '2026-03-01T00:00:00',
        'review_status': 'approved',
        'review_comment': '',
    }
    app.ctx.storage.list_import_queue.return_value = [
        {
            'id': 5,
            'created_at': '2026-03-01T12:00:00',
            'payload_json': json.dumps(entry_payload),
            'payload': entry_payload,
            'status': 'pending',
            'matched_record_id': 1,
            'created_by': 1,
        }
    ]
    matched = Competition(
        student_id='1',
        student_name='Похожий Павел',
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
    app.ctx.storage.get_competition_by_id.return_value = matched

    headers = get_auth_headers(role='admin')
    _, response = client.get('/admin/import-queue', headers=headers)
    assert response.status == 200
    assert 'Похожий Павел' in response.text
    assert 'Лыжи' in response.text

    headers = get_auth_headers(role='editor')
    _, response = client.get('/admin/import-queue', headers=headers)
    assert response.status == 403
    _, response = client.post(
        '/admin/import-queue/5/accept',
        headers=headers,
        data=csrf_for(headers),
    )
    assert response.status == 403


def test_import_queue_accept_inserts_record_and_audits(client: SanicTestClient):
    app.ctx.storage.get_field_settings.return_value = {}
    entry_payload = {
        'student_id': 'hash-2',
        'student_name': 'Принятый Пётр',
        'student_sex': 'М',
        'institute': 'ИСИ',
        'group': 'ПГС-101',
        'course': 2,
        'sport': 'Лыжи',
        'date': '2026-03-15T00:00:00',
        'date_to': None,
        'level': 'внутривузовские',
        'name': 'Кубок',
        'position': 2,
        'extra_data': {},
        'record_id': None,
        'created_at': '2026-03-01T00:00:00',
        'review_status': 'approved',
        'review_comment': '',
    }
    app.ctx.storage.get_import_queue_entry.return_value = {
        'id': 9,
        'created_at': '2026-03-01T12:00:00',
        'payload_json': json.dumps(entry_payload),
        'payload': entry_payload,
        'status': 'pending',
        'matched_record_id': 3,
        'created_by': 2,
    }
    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.save_competitions.reset_mock()
    app.ctx.storage.set_import_queue_status.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()

    headers = get_auth_headers(role='admin')
    _, response = client.post(
        '/admin/import-queue/9/accept',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 302
    app.ctx.storage.save_competitions.assert_called_once()
    saved = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert saved.student_name == 'Принятый Пётр'
    assert saved.sport == 'Лыжи'
    app.ctx.storage.set_import_queue_status.assert_called_once_with(9, 'accepted')
    app.ctx.storage.add_audit_event.assert_called_once()
    audit_kwargs = app.ctx.storage.add_audit_event.call_args[1]
    assert audit_kwargs['action'] == 'import_conflict_resolved'
    assert json.loads(audit_kwargs['details'])['decision'] == 'accepted'


def test_import_queue_skip_marks_skipped_and_audits(client: SanicTestClient):
    app.ctx.storage.get_field_settings.return_value = {}
    app.ctx.storage.get_import_queue_entry.return_value = {
        'id': 11,
        'created_at': '2026-03-01T12:00:00',
        'payload_json': '{}',
        'payload': {},
        'status': 'pending',
        'matched_record_id': 4,
        'created_by': 1,
    }
    app.ctx.storage.set_import_queue_status.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()

    headers = get_auth_headers(role='admin')
    _, response = client.post(
        '/admin/import-queue/11/skip',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 302
    app.ctx.storage.set_import_queue_status.assert_called_once_with(11, 'skipped')
    assert app.ctx.storage.add_audit_event.call_args[1]['action'] == 'import_conflict_resolved'
    assert json.loads(app.ctx.storage.add_audit_event.call_args[1]['details'])['decision'] == 'skipped'


def test_import_queue_replace_updates_existing_record_and_audits(client: SanicTestClient):
    app.ctx.storage.get_field_settings.return_value = {}
    entry_payload = {
        'student_id': 'hash-r',
        'student_name': 'Заменяемый Захар',
        'student_sex': 'М',
        'institute': 'ИСИ',
        'group': 'ПГС-101',
        'course': 2,
        'sport': 'Лыжи',
        'date': '2026-03-15T00:00:00',
        'date_to': None,
        'level': 'внутривузовские',
        'name': 'Кубок',
        'position': 2,
        'extra_data': {},
        'record_id': None,
        'created_at': '2026-03-01T00:00:00',
        'review_status': 'approved',
        'review_comment': '',
    }
    app.ctx.storage.get_import_queue_entry.return_value = {
        'id': 13,
        'created_at': '2026-03-01T12:00:00',
        'payload_json': json.dumps(entry_payload),
        'payload': entry_payload,
        'status': 'pending',
        'matched_record_id': 3,
        'created_by': 2,
    }
    existing = Competition(
        student_id='hash-r',
        student_name='Заменяемый Захар',
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
    app.ctx.storage.get_competition_by_id.return_value = existing
    app.ctx.storage.update_competition.reset_mock()
    app.ctx.storage.set_import_queue_status.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()
    app.ctx.storage.save_competitions.reset_mock()

    headers = get_auth_headers(role='admin')
    _, response = client.post(
        '/admin/import-queue/13/replace',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 302
    # Существующая запись обновляется по месту: record_id сохраняется, вставки нет.
    app.ctx.storage.update_competition.assert_called_once_with(3, ANY)
    replaced = app.ctx.storage.update_competition.call_args[0][1]
    assert replaced.sport == 'Лыжи'
    assert replaced.position == 2
    app.ctx.storage.save_competitions.assert_not_called()
    app.ctx.storage.set_import_queue_status.assert_called_once_with(13, 'replaced')
    audit_kwargs = app.ctx.storage.add_audit_event.call_args[1]
    assert audit_kwargs['action'] == 'import_conflict_resolved'
    details = json.loads(audit_kwargs['details'])
    assert details['decision'] == 'replaced'
    assert details['matched_record_id'] == 3
    assert details['changes']['sport'] == {'old': 'Бег', 'new': 'Лыжи'}
    assert details['changes']['position'] == {'old': '1', 'new': '2'}


def test_import_queue_replace_without_matched_record_rejected(client: SanicTestClient):
    app.ctx.storage.get_field_settings.return_value = {}
    app.ctx.storage.get_import_queue_entry.return_value = {
        'id': 14,
        'created_at': '2026-03-01T12:00:00',
        'payload_json': '{}',
        'payload': {},
        'status': 'pending',
        'matched_record_id': None,
        'created_by': 1,
    }
    app.ctx.storage.update_competition.reset_mock()
    app.ctx.storage.set_import_queue_status.reset_mock()
    headers = get_auth_headers(role='admin')
    _, response = client.post(
        '/admin/import-queue/14/replace',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 400
    app.ctx.storage.update_competition.assert_not_called()
    app.ctx.storage.set_import_queue_status.assert_not_called()


def test_import_queue_edit_accepts_with_edited_values_and_audits(client: SanicTestClient):
    app.ctx.storage.get_field_settings.return_value = {}
    entry_payload = {
        'student_id': 'hash-e',
        'student_name': 'Ручной Роман',
        'student_sex': 'М',
        'institute': 'ИСИ',
        'group': 'ПГС-101',
        'course': 2,
        'sport': 'Лыжи',
        'date': '2026-03-15T00:00:00',
        'date_to': None,
        'level': 'внутривузовские',
        'name': 'Кубок',
        'position': 2,
        'extra_data': {},
        'record_id': None,
        'created_at': '2026-03-01T00:00:00',
        'review_status': 'approved',
        'review_comment': '',
    }
    app.ctx.storage.get_import_queue_entry.return_value = {
        'id': 15,
        'created_at': '2026-03-01T12:00:00',
        'payload_json': json.dumps(entry_payload),
        'payload': entry_payload,
        'status': 'pending',
        'matched_record_id': 3,
        'created_by': 2,
    }
    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.save_competitions.reset_mock()
    app.ctx.storage.set_import_queue_status.reset_mock()
    app.ctx.storage.add_audit_event.reset_mock()

    headers = get_auth_headers(role='admin')
    _, response = client.post(
        '/admin/import-queue/15/edit',
        headers=headers,
        data={
            **csrf_for(headers),
            # №27в: подмена ФИО и даты формой игнорируется — берутся из payload.
            'student_name': 'Подмена ФИО',
            'student_sex': 'Ж',
            'institute': 'ИГДН',
            'group': 'ТД-202',
            'course': '3',
            'sport': 'Шахматы',
            'date': '20.03.2026',
            'level': 'городские',
            'name': 'Кубок edited',
            'position': '1',
        },
        allow_redirects=False,
    )
    assert response.status == 302
    app.ctx.storage.save_competitions.assert_called_once()
    saved = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert saved.student_name == 'Ручной Роман'
    assert saved.student_sex == 'Ж'
    assert saved.institute == 'ИГДН'
    assert saved.group == 'ТД-202'
    assert saved.sport == 'Шахматы'
    assert saved.date == datetime(2026, 3, 15)
    assert saved.date_to is None
    assert saved.level == 'городские'
    assert saved.name == 'Кубок edited'
    assert saved.position == 1
    assert saved.course == 3
    app.ctx.storage.set_import_queue_status.assert_called_once_with(15, 'accepted')
    audit_kwargs = app.ctx.storage.add_audit_event.call_args[1]
    assert audit_kwargs['action'] == 'import_conflict_resolved'
    details = json.loads(audit_kwargs['details'])
    assert details['decision'] == 'accepted'
    assert details['edited'] is True


def test_import_queue_edit_uses_payload_date_range_from_payload(client: SanicTestClient):
    """№27в: диапазон дат кандидата сохраняется из payload, форма дату не меняет."""
    app.ctx.storage.get_field_settings.return_value = {}
    entry_payload = {
        'student_name': 'Диапазон Дарья',
        'student_sex': 'Ж',
        'institute': 'ИСИ',
        'group': 'ПГС-102',
        'course': 1,
        'sport': 'Биатлон',
        'date': '2026-06-25T00:00:00',
        'date_to': '2026-06-27T00:00:00',
        'level': 'областные',
        'name': 'Спартакиада',
        'position': 3,
        'extra_data': {},
        'record_id': None,
        'created_at': '2026-06-01T00:00:00',
        'review_status': 'approved',
        'review_comment': '',
    }
    app.ctx.storage.get_import_queue_entry.return_value = {
        'id': 17,
        'created_at': '2026-06-01T12:00:00',
        'payload_json': json.dumps(entry_payload),
        'payload': entry_payload,
        'status': 'pending',
        'matched_record_id': None,
        'created_by': 2,
    }
    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.save_competitions.reset_mock()
    headers = get_auth_headers(role='admin')
    _, response = client.post(
        '/admin/import-queue/17/edit',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_sex': 'Ж',
            'institute': 'ИСИ',
            'group': 'ПГС-103',
            'course': '2',
            'sport': 'Лёгкая атлетика',
            'date': '01.01.2027',
            'level': 'городские',
            'name': 'Спартакиада edited',
            'position': '5',
        },
        allow_redirects=False,
    )
    assert response.status == 302
    saved = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert saved.student_name == 'Диапазон Дарья'
    assert saved.date == datetime(2026, 6, 25)
    assert saved.date_to == datetime(2026, 6, 27)
    assert saved.group == 'ПГС-103'
    assert saved.sport == 'Лёгкая атлетика'
    assert saved.level == 'городские'
    assert saved.name == 'Спартакиада edited'
    assert saved.position == 5
    assert saved.course == 2


def test_import_queue_edit_course_text_allowed_by_field_settings(client: SanicTestClient):
    """№27в: валидация по field_settings — course type=text принимает «Выпускник 2025/26»."""
    app.ctx.storage.get_field_settings.return_value = {'course': {'value_type': 'text', 'required': True}}
    entry_payload = {
        'student_name': 'Выпускник Влада',
        'student_sex': 'Ж',
        'institute': 'ИСИ',
        'group': 'ПГС-103',
        'course': 'Выпускник 2025/26',
        'sport': 'Волейбол',
        'date': '2026-03-15T00:00:00',
        'date_to': None,
        'level': 'внутривузовские',
        'name': 'Кубок',
        'position': 2,
        'extra_data': {},
        'record_id': None,
        'created_at': '2026-03-01T00:00:00',
        'review_status': 'approved',
        'review_comment': '',
    }
    app.ctx.storage.get_import_queue_entry.return_value = {
        'id': 18,
        'created_at': '2026-03-01T12:00:00',
        'payload_json': json.dumps(entry_payload),
        'payload': entry_payload,
        'status': 'pending',
        'matched_record_id': None,
        'created_by': 2,
    }
    app.ctx.storage.get_competitions.return_value = []
    app.ctx.storage.save_competitions.reset_mock()
    headers = get_auth_headers(role='admin')
    _, response = client.post(
        '/admin/import-queue/18/edit',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_sex': 'Ж',
            'institute': 'ИСИ',
            'group': 'ПГС-103',
            'course': 'Выпускник 2025/26',
            'sport': 'Волейбол',
            'level': 'внутривузовские',
            'name': 'Кубок',
            'position': '2',
        },
        allow_redirects=False,
    )
    assert response.status == 302
    saved = app.ctx.storage.save_competitions.call_args[0][0][0]
    assert saved.course == 'Выпускник 2025/26'


def test_import_queue_edit_rejects_bad_date(client: SanicTestClient):
    """№27в: дата берётся из payload — битая дата payload даёт 400."""
    app.ctx.storage.get_field_settings.return_value = {}
    app.ctx.storage.get_import_queue_entry.return_value = {
        'id': 16,
        'created_at': '2026-03-01T12:00:00',
        'payload_json': '{}',
        'payload': {},
        'status': 'pending',
        'matched_record_id': 3,
        'created_by': 1,
    }
    app.ctx.storage.save_competitions.reset_mock()
    app.ctx.storage.set_import_queue_status.reset_mock()
    headers = get_auth_headers(role='admin')
    _, response = client.post(
        '/admin/import-queue/16/edit',
        headers=headers,
        data={
            **csrf_for(headers),
            'student_name': 'Ручной Роман',
            'student_sex': 'М',
            'institute': 'ИСИ',
            'group': 'ПГС-101',
            'course': '3',
            'sport': 'Шахматы',
            'date': '2026-03-20',
            'level': 'внутривузовские',
            'name': 'Кубок',
            'position': '1',
        },
        allow_redirects=False,
    )
    assert response.status == 400
    app.ctx.storage.save_competitions.assert_not_called()
    app.ctx.storage.set_import_queue_status.assert_not_called()


def test_import_queue_editor_forbidden_on_replace_and_edit(client: SanicTestClient):
    headers = get_auth_headers(role='editor')
    _, response = client.post(
        '/admin/import-queue/5/replace',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 403
    _, response = client.post(
        '/admin/import-queue/5/edit',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )
    assert response.status == 403


def test_split_import_similar_rows_within_file(client: SanicTestClient):
    first = Competition(
        student_id='1',
        student_name='Файл Фёдор',
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
    similar = first.model_copy(update={'sport': 'Лыжи', 'student_id': '2'})
    new_competitions, skipped, conflicts = split_import_competitions([first, similar], [])
    assert [comp.sport for comp in new_competitions] == ['Бег']
    assert skipped == 0
    assert len(conflicts) == 1
    assert conflicts[0][0].sport == 'Лыжи'
    assert conflicts[0][1] is None


# --- №23доп (docs/feedback-live.md): резолвер атлета ---


def get_athlete_headers() -> dict[str, str]:
    cookie = create_auth_cookie_value(username='sportik', role='athlete')
    return {'cookie': f'{settings.auth_cookie_name}={cookie}'}


def test_athletes_search_returns_variants_for_moderator(client: SanicTestClient):
    app.ctx.storage.search_athletes.return_value = [
        {'name': 'Иванов Иван', 'sex': 'М', 'institute': 'ИСИ', 'group': 'ПГС-101', 'course': '1'}
    ]
    headers = get_auth_headers(role='editor')
    _, response = client.get('/api/athletes/search?q=Ива', headers=headers)
    assert response.status == 200
    assert response.json == [{'name': 'Иванов Иван', 'sex': 'М', 'institute': 'ИСИ', 'group': 'ПГС-101', 'course': '1'}]
    args = app.ctx.storage.search_athletes.call_args[0]
    assert args[0] == 'Ива'
    app.ctx.storage.search_athletes.return_value = []


def test_athletes_search_empty_query_returns_empty_list(client: SanicTestClient):
    _, response = client.get('/api/athletes/search?q=', headers=get_auth_headers(role='admin'))
    assert response.status == 200
    assert response.json == []


def test_athletes_search_forbidden_for_athlete(client: SanicTestClient):
    # Полный список ФИО атлету не раскрывается (docs/data-model-decisions.md):
    # 403 и никаких обращений к хранилищу.
    app.ctx.storage.search_athletes.reset_mock()
    _, response = client.get('/api/athletes/search?q=Ива', headers=get_athlete_headers())
    assert response.status == 403
    app.ctx.storage.search_athletes.assert_not_called()


def test_athletes_search_forbidden_for_viewer(client: SanicTestClient):
    _, response = client.get('/api/athletes/search?q=Ива', headers=get_auth_headers(role='viewer'))
    assert response.status == 403


def test_upload_autofills_empty_fields_from_known_athlete(client: SanicTestClient):
    # Строка импорта: ФИО известно, Пол/Институт/Группа/Курс пусты —
    # резолвер подставляет непустые значения (№23доп).
    app.ctx.storage.find_athlete_fields.side_effect = lambda name: (
        {
            'sex': 'Ж',
            'institute': 'ИМИ',
            'group': 'СБ-202',
            'course': '2',
        }
        if name.strip().lower() == 'петрова петра'
        else {}
    )
    df = pd.DataFrame(
        [
            {
                'ФИО': 'Петрова Петра',
                'Пол': '',
                'Институт': '',
                'Группа': '',
                'Вид спорта': 'Бег',
                'Дата': '15.03.2026',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Кубок',
                'Место': 2,
                'Курс': '',
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
    assert 'Импортировано записей: 1' in response.text
    saved_rows = app.ctx.storage.import_competitions.call_args[0][0]
    assert len(saved_rows) == 1
    assert saved_rows[0].student_sex == 'Ж'
    assert saved_rows[0].institute == 'ИМИ'
    assert saved_rows[0].group == 'СБ-202'
    assert saved_rows[0].course == 2
    app.ctx.storage.find_athlete_fields.side_effect = None
    app.ctx.storage.find_athlete_fields.return_value = {}


def test_upload_keeps_filled_fields_and_counts_duplicates_after_autofill(client: SanicTestClient):
    # Заполненные поля строки НЕ перезаписываются; дубль-ключ считается
    # ПОСЛЕ автозаполнения: строка с пустыми полями, совпавшая по ключу
    # с существующей записью после подстановки, пропускается как дубль.
    app.ctx.storage.find_athlete_fields.side_effect = lambda name: (
        {'sex': 'М', 'institute': 'ИСИ', 'group': 'ПГС-101', 'course': '2'}
        if name.strip().lower() == 'иванов иван'
        else {}
    )
    app.ctx.storage.get_competitions.return_value = [
        Competition(
            student_id='1',
            student_name='Иванов Иван',
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
                'ФИО': 'Иванов Иван',
                'Пол': '',
                'Институт': '',
                'Группа': '',
                'Вид спорта': 'Бег',
                'Дата': '15.03.2026',
                'Уровень соревнований': 'внутривузовские',
                'Название соревнований': 'Кубок',
                'Место': 3,
                'Курс': '',
            },
            {
                'ФИО': 'Петров Пётр',
                'Пол': 'М',
                'Институт': '',
                'Группа': '',
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

    app.ctx.storage.find_athlete_fields.side_effect = lambda name: (
        {'sex': 'М', 'institute': 'ИСИ', 'group': 'ПГС-101', 'course': '2'}
        if name.strip().lower() == 'иванов иван'
        else {'sex': 'Ж', 'institute': 'ИМИ', 'group': 'СБ-202', 'course': '9'}
    )
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
    saved_rows = app.ctx.storage.import_competitions.call_args[0][0]
    assert len(saved_rows) == 1
    # Заполненные поля (Пол=М, Курс=1) резолвером с другим ответом (Ж, 9)
    # не перезаписаны; пустые — заполнены.
    row = saved_rows[0]
    assert row.student_name == 'Петров Пётр'
    assert row.student_sex == 'М'
    assert row.course == 1
    assert row.institute == 'ИМИ'
    assert row.group == 'СБ-202'
    app.ctx.storage.find_athlete_fields.side_effect = None
    app.ctx.storage.find_athlete_fields.return_value = {}
    app.ctx.storage.get_competitions.return_value = []


# №24 (docs/feedback-live.md): привязка link-поля к другой колонке.
def test_index_renders_bound_link_in_target_column(client: SanicTestClient):
    """Целевая колонка — гиперссылка на link-поле ЭТОЙ записи; без ссылки —
    текст; сама link-колонка скрыта (d-none)."""
    with_link = Competition(
        student_id='2',
        student_name='Тестов Тест Тестович',
        student_sex='М',
        institute='ИСИ',
        group='ПГС-101',
        course=2,
        sport='Бег',
        date=datetime(2026, 3, 15),
        level='внутривузовские',
        name='Кубок «Весна»',
        position=1,
        extra_data={'comp_link': 'https://sport.example.com/race'},
    )
    without_link = Competition(
        student_id='3',
        student_name='Без Ссылки',
        student_sex='Ж',
        institute='ИСИ',
        group='ПГС-102',
        course=3,
        sport='Бег',
        date=datetime(2026, 3, 16),
        level='внутривузовские',
        name='Кубок «Осень»',
        position=2,
        extra_data={},
    )
    app.ctx.storage.get_competitions_page.return_value = [with_link, without_link]
    app.ctx.storage.count_competitions_filtered.return_value = 2
    app.ctx.storage.get_custom_fields.return_value = [
        CustomField(
            field_id=1,
            key='comp_link',
            label='Ссылка на соревнование',
            field_type='url',
            link_target='name',
        ),
    ]

    headers = get_auth_headers(role='editor')
    _, response = client.get('/', headers=headers)
    assert response.status == 200
    body = response.text
    expected = (
        '<a href="https://sport.example.com/race" class="link" '
        'rel="noopener noreferrer" target="_blank">Кубок «Весна»</a>'
    )
    assert expected in body
    # Запись без ссылки — обычный текст целевой колонки.
    assert '>Кубок «Осень»<' in body
    # Скрытая link-колонка: остаётся в DOM (инлайн-строка читает типы), но d-none.
    assert 'data-column-key="comp_link" data-field-type="url" data-sort-type="url" class="d-none"' in body
    # Не дублируется: у записи со ссылкой отдельная колонка пуста и скрыта.
    assert '<td data-column-key="comp_link" class="d-none">' in body

    app.ctx.storage.get_custom_fields.return_value = []
    app.ctx.storage.get_competitions_page.return_value = []
    app.ctx.storage.count_competitions_filtered.return_value = 0


def test_update_custom_field_saves_link_target(client: SanicTestClient):
    # Старое поле нужно маршруту для сравнения привязки (audit-событие).
    app.ctx.storage.get_custom_fields.return_value = [
        CustomField(field_id=1, key='comp_link', label='Ссылка', field_type='url'),
    ]
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/fields/1',
        headers=headers,
        data={
            **csrf_for(headers),
            'label': 'Ссылка на соревнование',
            'field_type': 'url',
            'sort_order': '0',
            'link_target': 'name',
        },
        allow_redirects=False,
    )

    assert response.status == 302
    _, kwargs = app.ctx.storage.update_custom_field.call_args
    assert kwargs['link_target'] == 'name'
    # Изменение привязки — в audit (field_settings_changed).
    audit_calls = [
        call
        for call in app.ctx.storage.add_audit_event.call_args_list
        if call.kwargs.get('action') == 'field_settings_changed'
    ]
    assert audit_calls, 'ожидалось audit-событие field_settings_changed'
    app.ctx.storage.get_custom_fields.return_value = []


def test_update_custom_field_rejects_unknown_link_target(client: SanicTestClient):
    headers = get_auth_headers()
    _, response = client.post(
        '/admin/fields/1',
        headers=headers,
        data={
            **csrf_for(headers),
            'label': 'Ссылка на соревнование',
            'field_type': 'url',
            'sort_order': '0',
            'link_target': '../../evil',
        },
        allow_redirects=False,
    )

    assert response.status == 302
    _, kwargs = app.ctx.storage.update_custom_field.call_args
    assert kwargs['link_target'] is None
