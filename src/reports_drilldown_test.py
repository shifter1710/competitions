"""REP-04: synthetic record-level oracle, not merely matching navigation links."""
import html
import re
from datetime import datetime
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlsplit

import pytest

from src import main_test as helpers
from src.conftest import make_report_record
from src.conftest import seed_fixture_levels
from src.main import app
from src.main_test import get_auth_headers
from src.routes.reports import get_report_rows
from src.storage.sqlite import SQLiteAdapter


client = helpers.client
METRICS = {'participations': 'count_participation', 'wins': 'count_wins', 'prizes': 'count_prizes'}
SLICES = ('student', 'group', 'institute', 'course', 'admission_year', 'sport', 'level', 'year')


def seed_drilldown_history(storage):
    """Fictional baseline also reusable for isolated browser QA."""
    storage.create_custom_field('test_tag', 'Тестовая категория', 'text', False, True, True, True, 0)
    first = storage.create_student('Тестовый Участник', 'М', 'Институт А', 'СИН-24', '1')
    twin = storage.create_student('Тестовый Участник', 'М', 'Институт Б', 'СИН-23', '2')
    events = [
        storage.create_calendar_event('Тестовый кубок', f'2025-10-0{day}', None, 'Учебный', 'Бег', []) for day in (1, 2)
    ]
    for event_id in events:
        assert storage.set_calendar_event_results_confirmation(event_id, True, 1) is None
    states = [
        (first, 'СИН-24', 'Институт А', 'Бег', 2025, 4, events[0]),
        (first, 'СИН-24', 'Институт А', 'Бег', 2025, 1, events[0]),
        (first, 'СИН-24', 'Институт А', 'Бег', 2025, 2, events[1]),
        (first, 'СИН-23', 'Институт Б', 'Плавание', 2025, 3, events[1]),
        (first, 'СИН-24', 'Институт А', 'Бег', 2026, 1, events[0]),
        (twin, 'СИН-24', 'Институт А', 'Бег', 2025, 1, events[0]),
        (None, 'СИН-24', 'Институт А', 'Бег', 2025, 1, None),
        (None, 'СИН-24', 'Институт А', 'Бег', 2025, 0, None),
        (999999, 'СИН-24', 'Институт А', 'Бег', 2025, 1, None),
        (999999, 'СИН-24', 'Институт А', 'Бег', 2025, 2, None),
        (first, '', 'Институт А', 'Бег', 2025, 0, None),
        (first, '', 'Институт А', 'Бег', 2025, 4, None),
    ]
    for index, (ref, group, institute, sport, year, position, event) in enumerate(states):
        row = make_report_record(
            'Тестовый Участник',
            'М',
            institute,
            group,
            99,
            sport,
            datetime(year, 10, 1),
            'Учебный',
            position,
            extra={'test_tag': 'A' if index % 2 else 'B'},
            student_ref_id=ref,
        )
        row.discipline = f'Дисциплина {index}'
        row.calendar_event_id = event
        storage.save_competitions([row])
    for status in ('pending', 'rejected'):
        row = make_report_record(
            'Тестовый Неподтверждённый',
            'М',
            'Институт А',
            'СИН-24',
            99,
            'Бег',
            datetime(2025, 10, 1),
            'Учебный',
            1,
            student_ref_id=first,
        )
        storage.save_competitions([row], review_status=status)
    return first, twin, events


@pytest.fixture
def drilldown_history(client, monkeypatch):
    from src.settings import settings

    monkeypatch.setattr(settings, 'auth_viewer_username', 'synthetic-viewer')
    monkeypatch.setattr(settings, 'auth_viewer_password', 'synthetic-viewer-only')
    storage = SQLiteAdapter(':memory:')
    seed_fixture_levels(storage, 'Учебный')
    seed_drilldown_history(storage)
    original = app.ctx.storage

    def synthetic_user(username):
        role = (
            'athlete'
            if username == 'sportik'
            else {
                settings.auth_admin_username: 'admin',
                settings.auth_editor_username: 'editor',
                settings.auth_viewer_username: 'viewer',
            }.get(username)
        )
        return (
            dict(id=1, username=username, password_hash='synthetic-unused', role=role, active=1, pwd_ver=0)
            if role
            else None
        )

    storage.get_user = synthetic_user
    app.ctx.storage = storage
    try:
        yield client, storage
    finally:
        app.ctx.storage = original
        storage.connection.close()


def selected_ids(storage, **kwargs):
    total, rows, _ = storage.get_report_drilldown(**kwargs)
    ids = {int(row.record_id) for row in rows}
    assert total == len(ids)
    return ids


def metric_ids(ids, metric):
    positions = {1: 4, 2: 1, 3: 2, 4: 3, 5: 1, 6: 1, 7: 1, 8: 0, 9: 1, 10: 2, 11: 0, 12: 4}
    return {
        record_id
        for record_id in ids
        if metric == 'participations'
        or (positions[record_id] == 1 if metric == 'wins' else 1 <= positions[record_id] <= 3)
    }


@pytest.mark.parametrize('metric', METRICS)
@pytest.mark.parametrize(
    'filters,expected',
    [
        ({}, set(range(1, 13))),
        ({'event_id': ['1', '2']}, {1, 2, 3, 4, 5, 6}),
        ({'institute': ['Институт А', 'Институт Б'], 'sport': ['Бег']}, set(range(1, 13)) - {4}),
        ({'group': ['СИН-23', 'СИН-24'], 'sport': ['Бег', 'Плавание']}, set(range(1, 11))),
        ({'course': '2', 'admission_year': '2024'}, {1, 2, 3, 6, 7, 8, 9, 10}),
        ({'date_from': '01.09.2025', 'date_to': '31.08.2026'}, set(range(1, 13)) - {5}),
        ({'custom_filters': [('test_tag', 'text', 'A')]}, {2, 4, 6, 8, 10, 12}),
        ({'position': '0'}, {8, 11}),
        ({'position': '>3'}, {1, 12}),
        ({'position': '<4'}, {2, 3, 4, 5, 6, 7, 9, 10}),
        ({'event_id': ['999999']}, set()),
        ({'name': 'НЕПОДТВЕРЖДЁННЫЙ'}, set()),
    ],
)
def test_summary_record_oracle(drilldown_history, metric, filters, expected):
    storage = drilldown_history[1]
    ids = selected_ids(storage, metric=metric, **filters)
    assert ids == metric_ids(expected, metric)
    assert len(ids) == getattr(storage.get_report_summary(**filters), METRICS[metric])


@pytest.mark.parametrize('slice_key', SLICES)
@pytest.mark.parametrize('metric', METRICS)
def test_every_slice_partition_matches_summary_and_row_counts(drilldown_history, slice_key, metric):
    storage = drilldown_history[1]
    summary_ids = selected_ids(storage, metric=metric)
    union = set()
    for row in get_report_rows(storage, slice_key, {}):
        ids = selected_ids(storage, metric=metric, slice_key=slice_key, anchor_id=row.drilldown_record_id)
        assert len(ids) == getattr(row, METRICS[metric])
        assert not union.intersection(ids)
        union.update(ids)
        assert 'drilldown_record_id' not in row.model_dump()
    assert union == summary_ids


def test_exact_historical_student_identity_and_null_groups(drilldown_history):
    storage = drilldown_history[1]
    assert selected_ids(storage, metric='participations', slice_key='student', anchor_id=1) == {1, 2, 3}
    assert selected_ids(storage, metric='wins', slice_key='student', anchor_id=1) == {2}
    for anchor in (4, 5, 6, 7, 8):
        assert selected_ids(storage, metric='participations', slice_key='student', anchor_id=anchor) == {anchor}
    assert selected_ids(storage, metric='participations', slice_key='student', anchor_id=9) == {9, 10}
    for anchor in (11, 12):
        assert selected_ids(storage, metric='participations', slice_key='group', anchor_id=anchor) == {11, 12}
        assert selected_ids(storage, metric='participations', slice_key='course', anchor_id=anchor) == {11, 12}
    for anchor in (13, 14, 99999):
        assert not selected_ids(storage, metric='participations', slice_key='student', anchor_id=anchor)
    assert not selected_ids(storage, metric='wins', slice_key='student', anchor_id=1, course='3')
    storage.connection.execute('DELETE FROM competitions WHERE id = 1')
    storage.connection.commit()
    assert not selected_ids(storage, metric='participations', slice_key='student', anchor_id=1)


@pytest.mark.parametrize('role', ['admin', 'editor', 'viewer'])
@pytest.mark.parametrize('slice_key', SLICES)
def test_links_open_exact_records_and_preserve_report(drilldown_history, role, slice_key):
    browser, storage = drilldown_history
    query = urlencode(
        [
            ('period', '2025-2026'),
            ('sport', 'Бег'),
            ('sport', 'Плавание'),
            ('event_id', '1'),
            ('event_id', '2'),
            ('group_by', slice_key),
        ]
    )
    _, report = browser.get('/reports?' + query, headers=get_auth_headers(role))
    assert report.status == 200
    links = re.findall(r'<a class="report-metric-link" href="([^"]+)"[^>]*>(\d+)</a>', report.text)
    assert links
    # All generated links carry valid selectors; execute summary and a row
    # (the storage oracle above checks every row and every metric independently).
    assert all(int(count) > 0 for _, count in links)
    row_link = next(link for link in links if 'report_anchor=' in link[0] and 'report_metric=wins' in link[0])
    for raw_url, count in [links[0], row_link]:
        url = html.unescape(raw_url)
        params = parse_qs(urlsplit(url).query)
        assert params['sport'] == ['Бег', 'Плавание']
        _, registry = browser.get(url, headers=get_auth_headers(role))
        assert registry.status == 200
        assert f'Найдено записей: {count}' in registry.text
        assert 'Только подтверждённые' in registry.text
        assert 'id="index-filter-name"' not in registry.text
        ids = {int(value) for value in re.findall(r'<tr[^>]*data-record-id="(\d+)"', registry.text)}
        filters = dict(date_from='01.09.2025', date_to='31.08.2026', sport=['Бег', 'Плавание'], event_id=['1', '2'])
        expected = selected_ids(
            storage,
            metric=params['report_metric'][0],
            slice_key=params.get('report_slice', [''])[0],
            anchor_id=int(params['report_anchor'][0]) if 'report_anchor' in params else None,
            **filters,
        )
        assert ids == expected
        back = re.search(r'href="([^"]+)">Вернуться к отчёту', registry.text).group(1)
        restored = parse_qs(urlsplit(html.unescape(back)).query)
        original = parse_qs(query)
        if slice_key == 'student':
            original.pop('group_by')  # Existing canonical Reports default.
        assert restored == original


@pytest.mark.parametrize(
    'query',
    [
        'report_metric=nope',
        'report_slice=student',
        'report_anchor=1',
        'report_metric=wins&report_slice=nope&report_anchor=1',
        'report_metric=wins&report_slice=student',
        'report_metric=wins&report_anchor=1',
        'report_metric=wins&report_slice=student&report_anchor=0',
        'report_metric=wins&report_slice=student&report_anchor=-1',
        'report_metric=wins&report_slice=student&report_anchor=9999999999999999999999999',
        'report_metric=wins&report_slice=student&report_anchor=1%20OR%201=1',
        'report_metric=wins&event_id=evil',
        'report_metric=wins&group_by=evil',
        'report_metric=wins&period=custom&date_from=bad',
    ],
)
def test_invalid_urls_are_explicit_400(drilldown_history, query):
    _, response = drilldown_history[0].get('/?' + query, headers=get_auth_headers('viewer'))
    assert response.status == 400


@pytest.mark.parametrize('query', ['report_metric=wins', 'report_metric=evil&report_anchor=bad'])
def test_athlete_rejected_before_analytics_queries(drilldown_history, monkeypatch, query):
    from src.auth import create_auth_cookie_value
    from src.settings import settings

    browser, storage = drilldown_history

    def forbidden_query(*args, **kwargs):
        raise AssertionError('Athlete must not reach Reports storage')

    monkeypatch.setattr(storage, 'get_report_drilldown', forbidden_query)
    monkeypatch.setattr(storage, 'get_custom_fields', forbidden_query)
    cookie = create_auth_cookie_value(username='sportik', role='athlete')
    _, response = browser.get('/?' + query, headers={'cookie': f'{settings.auth_cookie_name}={cookie}'})
    assert response.status == 403


def test_paging_sort_refresh_safe_return_and_empty(drilldown_history):
    browser, storage = drilldown_history
    row = make_report_record(
        'Тестовый Пагинация', 'М', 'Институт А', 'СИН-24', 99, 'Бег', datetime(2025, 10, 1), 'Учебный', 1
    )
    storage.save_competitions([row.model_copy(update={'discipline': f'Тест {index}'}) for index in range(40)])
    query = urlencode(
        [
            ('period', 'all'),
            ('report_metric', 'wins'),
            ('sport', 'Бег'),
            ('sport', 'Плавание'),
            ('sort', 'student_name'),
            ('order', 'desc'),
            ('per_page', '25'),
            ('page', '2'),
            ('return', 'https://invalid.example/evil'),
        ]
    )
    _, response = browser.get('/?' + query, headers=get_auth_headers('viewer'))
    assert response.status == 200
    ids = [int(value) for value in re.findall(r'<tr[^>]*data-record-id="(\d+)"', response.text)]
    total, expected, _ = storage.get_report_drilldown(
        metric='wins',
        sport=['Бег', 'Плавание'],
        sort_key=lambda row: row['student_name'].casefold(),
        sort_reverse=True,
        limit=25,
        offset=25,
    )
    assert ids == [int(row.record_id) for row in expected]
    assert f'Найдено записей: {total}' in response.text
    for value in re.findall(r'(?:href|data-query)="([^"]+)"', response.text):
        decoded = html.unescape(value)
        if 'report_metric=' in decoded:
            params = parse_qs(urlsplit(decoded).query if decoded.startswith('/') else decoded)
            assert params['sport'] == ['Бег', 'Плавание']
            assert params['report_metric'] == ['wins']
    assert 'href="https://invalid.example/evil"' not in response.text
    _, empty = browser.get(
        '/?period=all&report_metric=wins&report_slice=student&report_anchor=99999', headers=get_auth_headers('viewer')
    )
    assert empty.status == 200
    assert 'Найдено записей: 0' in empty.text
    assert 'Больше не доступна' in empty.text


def test_custom_result_filters_intersect_and_zero_metrics_stay_plain(drilldown_history):
    browser, _ = drilldown_history
    query = urlencode({'period': 'all', 'position': '0', 'custom__test_tag': 'A'})
    _, report = browser.get('/reports?' + query, headers=get_auth_headers('viewer'))
    assert report.status == 200
    links = re.findall(r'<a class="report-metric-link" href="([^"]+)"[^>]*>(\d+)</a>', report.text)
    assert links
    assert all('report_metric=participations' in url and int(count) > 0 for url, count in links)
    _, records = browser.get('/?' + query + '&report_metric=participations', headers=get_auth_headers('viewer'))
    assert records.status == 200
    assert {int(value) for value in re.findall(r'<tr[^>]*data-record-id="(\d+)"', records.text)} == {8}
    assert 'Тестовая категория: A' in records.text
    _, wins = browser.get('/?' + query + '&report_metric=wins', headers=get_auth_headers('viewer'))
    assert wins.status == 200
    assert 'Найдено записей: 0' in wins.text
