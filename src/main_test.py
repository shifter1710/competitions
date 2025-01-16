import pytest
from sanic_testing.testing import SanicTestClient

from src.main import app


@pytest.fixture
def client() -> SanicTestClient:
    client = SanicTestClient(app)
    return client


def test_healthcheck(client: SanicTestClient):
    _, response = client.get('/healthcheck')
    assert response.status == 200


def test_get_report_empty(client: SanicTestClient):
    _, response = client.get('/report')
    assert response.status == 200


def test_get_report_date(client: SanicTestClient):
    _, response = client.get('/report?date_from=03.02.2021')
    assert response.status == 200

    _, response = client.get('/report?date_from=03.02.2021&date_to=04.02.2021')
    assert response.status == 200


def test_get_report_position(client: SanicTestClient):
    _, response = client.get('/report?position=>3')
    assert response.status == 200

    _, response = client.get('/report?position=<4')
    assert response.status == 200


def test_get_report_level(client: SanicTestClient):
    _, response = client.get('/report?level=внутривузовские')
    assert response.status == 200


def test_get_report_name(client: SanicTestClient):
    _, response = client.get('/report?name=Карлова')
    assert response.status == 200
