"""Reports repeat-aware filters: fictional fixture, real SQLite and HTML/XLSX."""
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlencode

import pytest

from src import main_test as helpers
from src.conftest import make_report_record
from src.conftest import seed_fixture_levels
from src.main import app
from src.main_test import fake_get_user
from src.main_test import get_auth_headers
from src.main_test import get_report_cells
from src.main_test import get_report_footer
from src.main_test import get_report_xlsx_rows
from src.routes.reports import build_report_chips
from src.routes.reports import build_report_event_options
from src.routes.reports import collect_applied_report_query
from src.storage.sqlite import SQLiteAdapter


client = helpers.client


@pytest.fixture
def multi_reports(client, tmp_path):
    storage = SQLiteAdapter(str(tmp_path / 'synthetic-reports.sqlite3'))
    seed_fixture_levels(storage, 'Уровень А', 'Уровень Б')
    event_ids = [
        storage.create_calendar_event('Тестовый кубок', f'2025-10-{day:02}T00:00:00', None, 'Уровень А', sport, [])
        for day, sport in ((1, 'Бег'), (2, 'Шахматы'), (3, 'Плавание'))
    ]
    for event_id in event_ids:
        assert storage.set_calendar_event_results_confirmation(event_id, True, 1) is None
    data = [
        ('Альфа', 'Институт А', 'А-1', 'Бег', 'Уровень А', event_ids[0], 1),
        ('Бета', 'Институт Б', 'Б-1', 'Шахматы', 'Уровень Б', event_ids[1], 3),
        ('Гамма', 'Институт В', 'В-1', 'Плавание', 'Уровень А', event_ids[2], 4),
        ('Дельта', 'Институт А', 'А-1', 'Бег', 'Уровень А', None, 0),
    ]
    for label, institute, group, sport, level, event, position in data:
        record = make_report_record(
            f'Тестовый студент {label}', 'М', institute, group, 1, sport, datetime(2025, 10, 1), level, position
        )
        record.calendar_event_id = event
        storage.save_competitions([record])
    previous = app.ctx.storage
    storage.get_user = fake_get_user
    app.ctx.storage = storage
    try:
        yield client, storage, event_ids
    finally:
        app.ctx.storage = previous
        storage.connection.close()


def test_or_within_and_between_and_scalar_compatibility(multi_reports):
    _, storage, ids = multi_reports
    filters = dict(
        institute=['Институт А', 'Институт Б'],
        group=['А-1', 'Б-1'],
        sport=['Бег', 'Шахматы'],
        level=['Уровень А', 'Уровень Б'],
        event_id=[str(ids[0]), str(ids[1])],
    )
    assert storage.get_report_summary(**filters).count_participation == 2
    assert sum(row.count_participation for row in storage.get_grouped_report('sport', **filters)) == 2
    assert len(storage.get_filtered(**filters)) == 2
    assert storage.get_report_summary(institute='Институт А').count_participation == 2
    assert storage.get_report_summary(event_id=[str(ids[0])]).count_participation == 1
    assert storage.get_report_summary(event_id=[]).count_participation == 4
    assert storage.get_report_summary(event_id=['9999999']).count_participation == 0
    assert storage.get_report_summary(institute=["' OR 1=1 --"]).count_participation == 0


@pytest.mark.parametrize(
    'slice_key', ['student', 'group', 'institute', 'course', 'admission_year', 'sport', 'level', 'year']
)
def test_html_and_xlsx_same_repeated_selection(multi_reports, slice_key):
    browser, _, ids = multi_reports
    query = urlencode(
        [
            ('period', 'all'),
            ('group_by', slice_key),
            ('institute', 'Институт А'),
            ('institute', 'Институт Б'),
            ('sport', 'Бег'),
            ('sport', 'Шахматы'),
            ('event_id', str(ids[0])),
            ('event_id', str(ids[1])),
        ]
    )
    _, screen = browser.get('/reports?' + query, headers=get_auth_headers())
    _, export = browser.get('/export/report?' + query, headers=get_auth_headers())
    assert screen.status == export.status == 200
    first_column = {
        'student': 'ФИО',
        'group': 'Группа',
        'institute': 'Институт',
        'course': 'Курс',
        'admission_year': 'Год поступления',
        'sport': 'Вид спорта',
        'level': 'Уровень соревнований',
        'year': 'Год',
    }[slice_key]
    xlsx = get_report_xlsx_rows(export.content, first_column)
    # HTML includes index column; every row, including total, matches the exported metric values.
    screen_rows = get_report_cells(screen.text)
    assert len(screen_rows) == len(xlsx) - 2
    assert [row[-3:] if slice_key == 'student' else row[-5:] for row in screen_rows] == [
        [str(value) for value in row[-3:] if value is not None]
        if slice_key == 'student'
        else [str(value) for value in row[-5:]]
        for row in xlsx[1:-1]
    ]
    metric_count = 3 if slice_key == 'student' else 5
    assert get_report_footer(screen.text)[-metric_count:] == [str(value) for value in xlsx[-1][-metric_count:]]
    assert screen.text.count(f'<input type="hidden" name="event_id" value="{ids[0]}">') == 1
    assert f'№{ids[0]}' not in screen.text and f'№{ids[1]}' not in screen.text


def test_chips_remove_only_one_value_and_repeat_roundtrip():
    entries = collect_applied_report_query(
        {'period': ['all'], 'institute': ['Б', '', 'А', 'А'], 'event_id': ['2', '1'], 'sport': ['Бег']}, []
    )
    assert [value for key, value in entries if key == 'institute'] == ['А', 'Б']
    chips = build_report_chips(entries, [])
    chip = next(chip for chip in chips if chip['label'] == 'Институт' and chip['value'] == 'А')
    remaining = parse_qs(chip['remove_url'].partition('?')[2])
    assert remaining['institute'] == ['Б']
    assert remaining['event_id'] == ['1', '2']


def test_stale_event_empty_selection_invalid_event(multi_reports):
    browser, _, ids = multi_reports
    _, stale = browser.get('/reports?period=all&event_id=9999999', headers=get_auth_headers())
    assert stale.status == 200 and 'недоступно' in stale.text
    assert not get_report_cells(stale.text)
    _, outside = browser.get(f'/reports?period=2020-2021&event_id={ids[0]}', headers=get_auth_headers())
    assert outside.status == 200 and f'value="{ids[0]}"' in outside.text and not get_report_cells(outside.text)
    _, unrestricted = browser.get('/report?event_id=&institute=', headers=get_auth_headers())
    assert len(get_report_cells(unrestricted.text)) == 4  # All records, including unlinked history
    for value in ('0', '-1', '1 OR 1=1', '1.5'):
        query = urlencode({'period': 'all', 'event_id': value})
        _, screen = browser.get('/reports?' + query, headers=get_auth_headers())
        _, export = browser.get('/export/report?' + query, headers=get_auth_headers())
        assert 'Некорректный ID' in screen.text and export.status == 400
    # Selected, real event with no suitable records is an honest empty result.
    _, empty = browser.get(f'/reports?period=all&event_id={ids[0]}&sport=Шахматы', headers=get_auth_headers())
    assert not get_report_cells(empty.text)


def test_event_option_metadata_preserves_namesakes_and_missing_selection(multi_reports):
    _, storage, ids = multi_reports
    spanning = storage.create_calendar_event('Демо граница', '2026-08-31', '2026-09-02', '', '', [])
    options = build_report_event_options(storage, ['9999999'])
    same_name = [option for option in options if option['label'] == 'Тестовый кубок']
    assert {option['value'] for option in same_name} == {str(value) for value in ids}
    assert all(option['date_to'] == option['date_from'] for option in same_name)
    assert next(option for option in options if option['value'] == str(spanning))['date_to'] == '2026-09-02'
    assert 'date_from' not in options[-1] and options[-1]['value'] == '9999999'


def test_outside_period_url_keeps_id_and_html_xlsx_empty_result(multi_reports):
    browser, _, ids = multi_reports
    query = urlencode({'period': 'custom', 'date_from': '01.09.2026', 'date_to': '31.10.2026', 'event_id': ids[0]})
    _, screen = browser.get('/reports?' + query, headers=get_auth_headers())
    _, export = browser.get('/export/report?' + query, headers=get_auth_headers())
    assert screen.status == export.status == 200
    assert not get_report_cells(screen.text)
    assert len(get_report_xlsx_rows(export.content, 'ФИО')) == 2  # Heading and total, no broadened rows.
    assert f'name="event_id" value="{ids[0]}" checked' in screen.text
    assert f'<input type="hidden" name="event_id" value="{ids[0]}">' in screen.text
    assert 'data-date-from="2025-10-01" data-date-to="2025-10-01"' in screen.text
    assert 'id="report-event-review-summary"' in screen.text
    assert f'id="report-event-option-{ids[0]}"' in screen.text


@pytest.mark.parametrize('script_name', ['test_report_period_options.cjs', 'test_period_filters.cjs'])
def test_period_javascript_regressions(script_name):
    node = shutil.which('node') or shutil.which('nodejs')
    if not node:
        pytest.skip('Node is required for the JavaScript regression (available in GitHub CI)')
    subprocess.run(
        [node, str(Path(__file__).parents[1] / 'scripts' / script_name)], check=True, capture_output=True, text=True
    )
