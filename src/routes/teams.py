"""Server-rendered team roster forms, progressively enhanced for searching."""
from sanic import redirect
from sanic import text
from sanic_ext import render

from src.auth import log_audit_event
from src.auth import require_moderator
from src.auth import user_is_athlete
from src.auth import user_is_moderator
from src.records import event_participation_effective_values
from src.routes.calendar import get_calendar_event_or_error
from src.storage.teams import TeamError
from src.web import forbidden
from src.web import get_auth_user
from src.web import get_form_value
from src.web import get_storage
from src.web import jinja_env


HISTORY_ACTIONS = {
    'created': 'Команда создана',
    'renamed': 'Команда переименована',
    'deleted': 'Команда удалена',
    'added': 'Добавлено участие',
    'removed': 'Исключено участие',
    'identity_changed': 'Изменена связь с карточкой',
}
HISTORY_REASONS = {
    'manual': 'Действие оператора',
    'discipline_changed': 'Изменена дисциплина',
    'participation_deleted': 'Удалено участие',
    'event_changed': 'Изменено соревнование',
}


async def team_page(request, event, team=None, error='', values=None):
    storage = get_storage(request.app)
    fields = storage.get_custom_fields()
    members = storage.event_team_member_ids(event['id'], team['id']) if team else set()
    participants = storage.list_calendar_event_participants(event['id'])
    for row in participants:
        row['effective_discipline'] = event_participation_effective_values(row, fields)[0]
    history = storage.event_team_history(event['id'], team['id'] if team else None)
    return await render(
        template_name=jinja_env.get_template('event_team.html'),
        context={
            'request': request,
            'event': event,
            'team': team,
            'members': members,
            'participants': participants,
            'history': history,
            'history_actions': HISTORY_ACTIONS,
            'history_reasons': HISTORY_REASONS,
            'can_manage': user_is_moderator(request),
            'error': error,
            'values': values or {},
        },
        status=400 if error else 200,
    )


def register(app):  # noqa: C901 - route registration, matching other route modules
    @app.get('/calendar/<event_id>/teams')
    async def team_history(request, event_id):
        if user_is_athlete(request):
            return forbidden(request)
        event, error = get_calendar_event_or_error(request, event_id)
        if error:
            return error
        return await team_page(request, event)

    @app.get('/calendar/<event_id>/teams/<team_id:int>')
    async def team_detail(request, event_id, team_id):
        if user_is_athlete(request):
            return forbidden(request)
        event, error = get_calendar_event_or_error(request, event_id)
        if error:
            return error
        team = get_storage(request.app).get_event_team(event['id'], team_id)
        if not team:
            return text('Team not found', status=404)
        return await team_page(request, event, team)

    @app.post('/calendar/<event_id>/teams')
    async def team_create(request, event_id):
        denied = require_moderator(request)
        if denied:
            return denied
        event, error = get_calendar_event_or_error(request, event_id)
        if error:
            return error
        name = get_form_value(request, 'name')
        try:
            team_id = get_storage(request.app).create_event_team(event['id'], name, get_auth_user(request))
        except TeamError as exc:
            return await team_page(request, event, error=str(exc), values={'name': name})
        return redirect(f'/calendar/{event["id"]}/teams/{team_id}')

    @app.post('/calendar/<event_id>/teams/<team_id:int>/<action>')
    async def team_change(request, event_id, team_id, action):
        denied = require_moderator(request)
        if denied:
            return denied
        event, error = get_calendar_event_or_error(request, event_id)
        if error:
            return error
        storage = get_storage(request.app)
        team = storage.get_event_team(event['id'], team_id)
        if team is None:
            return text('Team not found', status=404)
        values = {
            'name': get_form_value(request, 'name'),
            'selected': request.form.getlist('competition_id'),
            'action': action,
        }
        actor = get_auth_user(request)
        try:
            if action == 'rename':
                storage.rename_event_team(event['id'], team_id, values['name'], actor)
            elif action == 'delete':
                storage.delete_event_team(event['id'], team_id, actor)
                log_audit_event(
                    request,
                    'calendar_event_team_deleted',
                    {'event_id': event['id'], 'team_id': team_id, 'team_name': team['name']},
                )
                return redirect(f'/calendar/{event["id"]}#event-teams')
            elif action in ('add', 'remove'):
                storage.change_event_team_members(event['id'], team_id, values['selected'], action, actor)
            else:
                return text('Not found', status=404)
        except TeamError as exc:
            return await team_page(request, event, team, str(exc), values)
        return redirect(f'/calendar/{event["id"]}/teams/{team_id}')
