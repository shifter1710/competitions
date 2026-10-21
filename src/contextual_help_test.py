"""Contextual help uses fictional page contexts and no persistent application data."""
import shutil
import subprocess
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.web import jinja_env_sync


class HelpDocument(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.help_depth = 0
        self.stack = []
        self.help_text = []
        self.visible_text = []
        self.help_controls = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        is_help = tag == 'details' and 'context-help' in attrs.get('class', '').split()
        if tag not in ('input', 'meta', 'link', 'br', 'hr', 'img'):
            self.stack.append((tag, is_help))
        if is_help:
            assert 'open' not in attrs, 'Help must arrive closed before JavaScript loads'
            assert 'ui-hint' in attrs['class'].split()
            self.help_depth += 1
        if tag == 'summary' and self.help_depth:
            self.help_controls.append(attrs)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            _, is_help = self.stack.pop()
            if is_help:
                self.help_depth -= 1

    def handle_data(self, data):
        (self.help_text if self.help_depth else self.visible_text).append(data)


def render_page(name, **context):
    request = SimpleNamespace(ctx=SimpleNamespace(auth_user=None), args={}, path='/')
    return jinja_env_sync.get_template(name).render(
        request=request,
        csrf_token=lambda _: 'synthetic-csrf',
        format_date_range=lambda *_: '01.01.2026',
        **context,
    )


@pytest.mark.parametrize(
    'template, optional, essential',
    [
        ('admin_import.html', 'Год поступления и курс рассчитываются', 'Неизвестное значение отменяет весь импорт'),
        ('admin_people_import.html', 'Если институт не указан', 'Загрузка файла'),
        ('calendar_event_import.html', 'Лишние колонки в файле игнорируются', 'Синтетический турнир'),
        ('admin_reconcile_create.html', 'Год поступления и курс рассчитываются', 'Созданный студент сразу привяжется'),
    ],
)
def test_optional_explanations_are_closed_help_but_required_action_state_stays_visible(template, optional, essential):
    markup = render_page(
        template,
        is_admin=True,
        source='record',
        record_id=987,
        record={},
        form={},
        back_url='/admin/people',
        event={'id': 987, 'name': 'Синтетический турнир', 'date_label': '01.01.2026'},
    )
    document = HelpDocument(markup)
    assert optional in ''.join(document.help_text)
    assert optional not in ''.join(document.visible_text)
    assert essential in ''.join(document.visible_text)
    assert document.help_controls
    assert all(control.get('aria-label', '').startswith('Справка: ') for control in document.help_controls)


def test_import_confirmation_state_does_not_become_optional_admin_description():
    document = HelpDocument(
        render_page(
            'admin_people_import_preview.html',
            filename='synthetic-students.xlsx',
            rows=[],
            pending=[],
            groups={},
            total=0,
            ready_count=0,
            review_count=0,
            error_count=0,
        )
    )
    assert 'synthetic-students.xlsx' in ''.join(document.visible_text)


def test_macro_escapes_text_and_remains_native_without_javascript():
    macro = jinja_env_sync.get_template('macros/context_help.html').module.help
    markup = str(macro('Безопасная справка', '<script>synthetic</script>'))
    assert '&lt;script&gt;' in markup
    assert '<script>' not in markup
    assert '<details' in markup and '<summary' in markup
    HelpDocument(markup)


def test_global_help_setting_is_applied_before_styles_and_shared_script_is_loaded():
    markup = render_page('login.html')
    assert markup.index('localStorage.getItem("hints")') < markup.index('/static/styles/main.css')
    assert 'document.documentElement.classList.add("hints-off")' in markup
    assert markup.count('hints-toggle-button') == 1
    assert '/static/scripts/contextual-help.js' in markup


def test_record_to_calendar_optional_links_help_preserves_creation_consequence():
    document = HelpDocument(
        render_page(
            'competition_link.html',
            record_id=987,
            record={'name': 'Синтетический турнир', 'date': datetime(2026, 1, 1), 'level': '', 'sport': ''},
            filters={'name': 'NoSuchSyntheticEvent'},
            candidates=[],
            new_form_open=True,
            level_options=[],
            sport_options=[],
        )
    )
    optional = 'Необязательно: положение, фото, регламент'
    assert optional in ''.join(document.help_text)
    assert optional not in ''.join(document.visible_text)
    assert 'Соревнование создастся в календаре и сразу свяжется с этой записью.' in ''.join(document.visible_text)
    assert 'NoSuchSyntheticEvent' not in ''.join(document.help_text)


def test_import_queue_course_help_preserves_overwrite_and_conflict_warnings():
    document = HelpDocument(
        render_page(
            'admin_import_queue.html',
            queue_entries=[
                {
                    'id': 987,
                    'created_at': '2026-01-01T12:00:00',
                    'candidate': [],
                    'matched': {'record_id': 654},
                    'edit_values': {},
                }
            ],
            level_options=[],
        )
    )
    optional = 'Год поступления и курс рассчитываются по группе после сохранения.'
    assert optional in ''.join(document.help_text)
    assert optional not in ''.join(document.visible_text)
    visible = ''.join(document.visible_text)
    assert 'перезапишет эту запись полями кандидата' in visible
    assert 'Определяет конфликт — редактировать нельзя.' in visible
    assert 'исключит его из всех команд' in visible


def test_contextual_help_javascript_survives_dynamic_registry_replacement():
    node = shutil.which('node') or shutil.which('nodejs')
    if not node:
        pytest.skip('Node is required for the JavaScript regression (available in GitHub CI)')
    script = Path(__file__).parents[1] / 'scripts' / 'test_contextual_help.cjs'
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True)
