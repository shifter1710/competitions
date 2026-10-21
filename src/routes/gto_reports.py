"""Independent staff GTO report. Participation Reports v2 remains unchanged."""
import asyncio

from openpyxl.utils.exceptions import IllegalCharacterError
from sanic import text
from sanic.response import raw
from sanic_ext import render

from src.auth import parse_auth_cookie
from src.auth import require_staff
from src.gto import build_gto_stage_options
from src.gto_report import build_gto_report
from src.gto_report import build_gto_xlsx
from src.storage.gto import GTO_AWARDS
from src.storage.helpers import GTO_STATUS_LABELS
from src.web import forbidden
from src.web import get_storage
from src.web import jinja_env


def gto_report_staff_error(request):
    error = require_staff(request)
    if error is not None:
        return error
    principal = parse_auth_cookie(request)
    user = get_storage(request.app).get_user_by_id(principal['id']) if principal else None
    return None if user and user['role'] in ('admin', 'editor', 'viewer') else forbidden(request)


def register(app):
    @app.get('/reports/gto')
    async def gto_report(request):
        error = gto_report_staff_error(request)
        if error is not None:
            return error
        report = build_gto_report(get_storage(request.app).list_gto_report_awards(), request.args)
        response = await render(
            template_name=jinja_env.get_template('gto_report.html'),
            context={
                'request': request,
                'gto': report,
                'stages': build_gto_stage_options(),
                'awards': [{'value': status, 'label': GTO_STATUS_LABELS[status]} for status in GTO_AWARDS],
                'stage_values': {str(value) for value in range(1, 19)},
                'award_values': GTO_AWARDS,
            },
            status=400 if report['error'] else 200,
        )
        error = gto_report_staff_error(request)
        if error is not None:
            return error
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @app.get('/reports/gto/export.xlsx')
    async def export_gto_report(request):
        error = gto_report_staff_error(request)
        if error is not None:
            return error
        report = build_gto_report(get_storage(request.app).list_gto_report_awards(), request.args)
        if report['error']:
            return text(report['error'], status=400)
        try:
            body = await asyncio.to_thread(build_gto_xlsx, report)
        except (ValueError, IllegalCharacterError):
            return text('Данные нельзя без потерь записать в XLSX: проверьте длину и символы текста.', status=400)
        error = gto_report_staff_error(request)
        if error is not None:
            return error
        return raw(
            body,
            headers={
                'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'Content-Disposition': 'attachment; filename="gto-awards.xlsx"',
                'Cache-Control': 'private, no-store',
                'X-Content-Type-Options': 'nosniff',
            },
        )
