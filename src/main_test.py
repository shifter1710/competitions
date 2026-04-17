from datetime import datetime
from io import BytesIO
from unittest.mock import Mock
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pandas as pd
import pytest
from sanic_testing.testing import SanicTestClient

from src.main import app
from src.main import create_auth_cookie_value
from src.main import normalize_position
from src.models.competition import Competition
from src.models.http.student_info import StudentInfo
from src.settings import settings


SPREADSHEET_NS = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def get_xlsx_headers(content: bytes) -> list[str]:
    with ZipFile(BytesIO(content)) as archive:
        shared_strings_xml = archive.read('xl/sharedStrings.xml')
        sheet_xml = archive.read('xl/worksheets/sheet1.xml')

    shared_strings_root = ET.fromstring(shared_strings_xml)
    shared_strings = [
        ''.join(node.itertext())
        for node in shared_strings_root.findall('main:si', SPREADSHEET_NS)
    ]

    sheet_root = ET.fromstring(sheet_xml)
    header_row = sheet_root.find('main:sheetData/main:row[@r="1"]', SPREADSHEET_NS)
    assert header_row is not None

    headers = []
    for cell in header_row.findall('main:c', SPREADSHEET_NS):
        value = cell.find('main:v', SPREADSHEET_NS)
        assert value is not None
        headers.append(shared_strings[int(value.text)])

    return headers


def get_auth_headers(role: str = 'admin') -> dict[str, str]:
    username = settings.auth_admin_username
    if role == 'viewer':
        username = settings.auth_viewer_username or 'viewer'
    cookie = create_auth_cookie_value(username=username, role=role)
    return {'cookie': f'{settings.auth_cookie_name}={cookie}'}


@pytest.fixture
def client() -> SanicTestClient:
    fake_storage = Mock()
    fake_storage.get_competitions.return_value = []
    fake_storage.get_filtered.return_value = []
    fake_storage.save_competitions.return_value = None
    fake_storage.update_competition.return_value = None
    fake_storage.delete_competition.return_value = None
    fake_storage.clean_db.return_value = None
    app.ctx.storage = fake_storage
    client = SanicTestClient(app)
    return client


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
    _, response = client.post('/clean_db', headers=get_auth_headers())
    assert response.status == 200


def test_upload_rejects_missing_columns(client: SanicTestClient):
    df = pd.DataFrame([{'ФИО': 'Тест'}])
    file_obj = BytesIO()
    df.to_excel(file_obj, index=False)
    file_obj.seek(0)

    _, response = client.post('/',
                              headers=get_auth_headers(),
                              files={'file': ('broken.xlsx', file_obj.getvalue(),
                                              'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
    assert response.status == 400


def test_manual_competition_create_success(client: SanicTestClient):
    _, response = client.post(
        '/competition',
        headers=get_auth_headers(),
        data={
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
    _, response = client.post(
        '/competition',
        headers=get_auth_headers(),
        data={
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


def test_manual_competition_update_success(client: SanicTestClient):
    _, response = client.post(
        '/competition/abc123',
        headers=get_auth_headers(),
        data={
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


def test_manual_competition_delete_success(client: SanicTestClient):
    _, response = client.post(
        '/competition/abc123/delete',
        headers=get_auth_headers(),
        allow_redirects=False,
    )

    assert response.status == 302
    assert response.headers['location'] == '/'
    app.ctx.storage.delete_competition.assert_called_once_with('abc123')


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
    _, response = client.post('/clean_db', headers=get_auth_headers(role='viewer'))

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
