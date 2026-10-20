"""REP-01/08/09: fictional history, real SQL, applied HTML/XLSX and JS regression."""
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import pytest
from openpyxl import load_workbook

from src import main_test as helpers
from src.conftest import make_report_record
from src.conftest import seed_fixture_levels
from src.main import app
from src.main_test import fake_get_user
from src.main_test import get_auth_headers
from src.main_test import get_report_xlsx_rows
from src.routes.reports import build_report_display_groups
from src.storage.sqlite import SQLiteAdapter


client = helpers.client


def seed_metrics_history(storage):
    """Synthetic history for regression and independent local browser QA."""
    first = storage.create_student('Тестовый Исторический', 'М', 'Институт А', 'А-1', '1')
    twin = storage.create_student('Тестовый Исторический', 'М', 'Институт Б', 'Б-1', '2')
    events = [
        storage.create_calendar_event('Тестовый кубок', f'2026-10-0{day}T00:00:00', None, 'Тестовый уровень', 'Бег', [])
        for day in (1, 2)
    ]
    states = [
        (first, 'Тестовый Исторический', 'Институт А', 'А-1', 1, 3),
        (first, 'Тестовый Исторический', 'Институт А', 'А-2', 1, 2),
        (first, 'Тестовый Исторический', 'Институт А', 'А-1', 2, 1),
        (first, 'Тестовый Исторический', 'Институт Б', 'А-1', 1, 1),
        (first, 'Тестовый Исторический', 'Институт Б', 'А-3', 3, 1),
        (first, 'Тестовое Другое Имя', 'Институт А', 'А-1', 1, 1),
        (twin, 'Тестовый Исторический', 'Институт А', 'А-1', 1, 1),
        (None, 'Тестовая История', 'Институт А', 'А-1', 1, 2),
        (999999, 'Тестовая Нет Карточки', 'Институт А', 'А-1', 1, 1),
        (999999, 'Тестовая Нет Карточки', 'Институт А', 'А-2', 2, 1),
        (first, ' ', 'Институт А', 'А-1', 1, 1),
        (first, ' ', 'Институт А', 'А-2', 2, 1),
    ]
    records = []
    for ref, name, institute, group, course, count in states:
        for _ in range(count):
            record = make_report_record(
                name,
                'М',
                institute,
                group,
                course,
                'Бег',
                datetime(2026, 10, 1),
                'Тестовый уровень',
                len(records) % 5,
                extra={'test_category': 'A'},
                student_ref_id=ref,
                admission_year=2025,
            )
            record.calendar_event_id = (events[0], events[1], None)[len(records) % 3]
            records.append(record)
    storage.save_competitions(records)
    return first, twin, events


@pytest.fixture
def metrics_history(client, tmp_path):
    storage = SQLiteAdapter(str(tmp_path / 'synthetic-metrics.sqlite3'))
    seed_fixture_levels(storage, 'Тестовый уровень')
    first, twin, events = seed_metrics_history(storage)
    previous = app.ctx.storage
    storage.get_user = fake_get_user
    app.ctx.storage = storage
    try:
        yield client, storage, first, twin, events
    finally:
        app.ctx.storage = previous
        storage.connection.close()


def test_metrics_and_safe_visual_blocks_preserve_raw_history(metrics_history):
    _, storage, first, twin, _ = metrics_history
    summary = storage.get_report_summary()
    raw = storage.get_filtered()
    groups = build_report_display_groups(raw, 'student')
    assert summary.count_participation == sum(row.count_participation for row in raw) == 16
    assert len(raw) == 12 and len(groups) == 9
    assert summary.count_unique_participants == 5  # orphan ref remains the existing identity rule
    assert summary.count_events == 2 and summary.count_unlinked_participations == 5
    assert sorted(map(len, groups)) == [1] * 8 + [4]
    combined = next(group for group in groups if len(group) == 4)
    assert {row.student_ref_id for row in combined} == {first}
    assert {row.student_name for row in combined} == {'Тестовый Исторический'}
    assert any(group[0].student_ref_id == twin for group in groups)
    assert sum(group[0].student_ref_id is None for group in groups) == 4
    assert len([group for group in groups if group[0].student_name == ' ']) == 2
    assert Counter(id(row) for group in groups for row in group) == Counter(map(id, raw))
    assert build_report_display_groups([], 'student') == []


@pytest.mark.parametrize(
    'filters,expected',
    [
        ({}, (2, 5, 16)),
        ({'event_id': ['1']}, (1, 0, 6)),
        ({'event_id': ['1', '2']}, (2, 0, 11)),
        ({'event_id': ['9999999']}, (0, 0, 0)),
        ({'institute': ['Институт Б']}, (2, 0, 2)),
        ({'group': ['А-3']}, (1, 0, 1)),
        ({'sport': ['Плавание']}, (0, 0, 0)),
        ({'level': ['Другой']}, (0, 0, 0)),
        ({'course': '3'}, (0, 0, 0)),
        ({'admission_year': 'none'}, (2, 5, 16)),
        ({'name': 'ДРУГОЕ'}, (0, 1, 1)),
        ({'date_from': '01.01.2027'}, (0, 0, 0)),
        ({'date_to': '30.09.2026'}, (0, 0, 0)),
        ({'position': '0'}, (2, 1, 4)),
        ({'custom_filters': [('test_category', 'text', 'B')]}, (0, 0, 0)),
    ],
)
def test_event_metrics_keep_all_filter_contracts(metrics_history, filters, expected):
    summary = metrics_history[1].get_report_summary(**filters)
    assert (summary.count_events, summary.count_unlinked_participations, summary.count_participation) == expected


@pytest.mark.parametrize('role', ['admin', 'editor', 'viewer'])
def test_html_xlsx_grouping_metrics_and_safe_links(metrics_history, role):
    browser, storage, first, twin, _ = metrics_history
    query = urlencode({'period': 'all', 'sport': 'Бег'})
    _, screen = browser.get('/reports?' + query, headers=get_auth_headers(role=role))
    _, export = browser.get('/export/report?' + query, headers=get_auth_headers(role=role))
    assert screen.status == export.status == 200
    html = screen.text
    body = re.search(r'<tbody>(.*?)</tbody>', html, re.S).group(1)
    assert len(re.findall(r'<tr\b', body)) == 12
    assert re.findall(r'data-column-key="index">(\d+)', body) == list(map(str, range(1, 13)))
    names = re.findall(r'<th scope="row".*?</th>', body, re.S)
    assert len(names) == 9
    assert len([cell for cell in names if 'rowspan="4"' in cell]) == 1
    assert f'href="/admin/people/{first}?' in body and f'href="/admin/people/{twin}?' in body
    assert 'href="/admin/people/999999?' not in body
    assert 'report-col-count_participation report-student-' in body
    assert 'Блоков студентов: 9' in html
    assert 'Участий без связи с событием: 5' in html
    assert len(re.findall(r'class="report-stat"', html)) == 6
    assert 'data-column-key="index" data-sort-type="none"' in html
    xlsx = get_report_xlsx_rows(export.content, 'ФИО')
    assert len(xlsx[1:-1]) == 12
    assert [tuple(row[:5]) + tuple(row[5:]) for row in xlsx[1:-1]] == [
        (
            row.student_name,
            row.student_sex,
            row.institute,
            row.group,
            row.course,
            row.count_participation,
            row.count_wins,
            row.count_prizes,
        )
        for row in storage.get_filtered()
    ]
    assert sum(row[-3] for row in xlsx[1:-1]) == 16
    sheet = list(load_workbook(BytesIO(export.content)).active.values)
    headings = next(index for index, row in enumerate(sheet) if row[0] == 'Метрики:')
    assert 'Соревнований' in sheet[headings]
    event_column = sheet[headings].index('Соревнований')
    assert sheet[headings + 1][event_column] == 2
    assert any(row[1:3] == ('Участий без связи с событием:', 5) for row in sheet)


def test_table_javascript_regression():
    node = shutil.which('node') or shutil.which('nodejs')
    if not node:
        pytest.skip('Node is required for the JavaScript regression (available in GitHub CI)')
    script = Path(__file__).parents[1] / 'scripts' / 'test_reports_table.cjs'
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True)
