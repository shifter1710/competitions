"""GTO identity and award certificate operations, with live server authority."""
import asyncio
import logging
from urllib.parse import quote
from urllib.parse import urlencode

from sanic import text
from sanic.response import raw

from src.auth import log_audit_event
from src.auth import parse_auth_cookie
from src.auth import require_admin
from src.auth import require_moderator
from src.files import detect_attachment_type
from src.gto_files import certificate_identity
from src.gto_files import read_certificate
from src.gto_files import remove_certificate
from src.gto_files import store_certificate
from src.web import build_redirect_with_message
from src.web import forbidden
from src.web import get_form_value
from src.web import get_storage

logger = logging.getLogger(__name__)


def cleanup_certificate(record_id, name):
    try:
        remove_certificate(record_id, name)
    except (OSError, ValueError):
        logger.warning('GTO certificate cleanup failed for award %s', record_id)


def live_moderator(request):
    principal = parse_auth_cookie(request)
    user = get_storage(request.app).get_user_by_id(principal['id']) if principal else None
    return user if user and user['role'] in ('admin', 'editor') else None


def gto_back_url(request, student_id):
    # Deferred import avoids the existing students -> certificate cleanup import.
    from src.routes.students import safe_student_return

    pairs = [('tab', 'gto')]
    return_url = safe_student_return(get_form_value(request, 'return'))
    if return_url:
        pairs.append(('return', return_url))
    # Keep native flash visible at page top; never reintroduce a GTO anchor.
    return f'/admin/people/{student_id}?' + urlencode(pairs)


def register(app):  # noqa: C901 (route assembly follows the existing domain modules)
    @app.post('/admin/people/<student_id:int>/gto/uin')
    async def save_gto_uin(request, student_id):
        error = require_admin(request)
        if error is not None:
            return error
        user = live_moderator(request)
        if not user or user['role'] != 'admin':
            return forbidden(request)
        storage = get_storage(request.app)
        before = storage.get_student_by_id(student_id)
        updated, code = storage.set_student_gto_uin(student_id, get_form_value(request, 'gto_uin'))
        back = gto_back_url(request, student_id)
        if not updated:
            messages = {
                'duplicate_uin': 'Этот УИН уже указан у другого студента. Проверьте значение.',
                'invalid_uin': 'УИН не может содержать управляющие символы.',
                'student_not_found': 'Студент не найден.',
            }
            return build_redirect_with_message(error=messages[code], url=back)
        current = storage.get_student_by_id(student_id)
        log_audit_event(
            request,
            'student_gto_uin_updated',
            {'student_id': student_id, 'changed': {'gto_uin': {'old': before['gto_uin'], 'new': current['gto_uin']}}},
        )
        return build_redirect_with_message(message='УИН сохранён.', url=back)

    @app.post('/admin/people/<student_id:int>/gto/<record_id:int>/certificates')
    async def upload_gto_certificate(request, student_id, record_id):
        error = require_moderator(request)
        if error is not None:
            return error
        user = live_moderator(request)
        if user is None:
            return forbidden(request)
        storage = get_storage(request.app)
        back = gto_back_url(request, student_id)
        award = storage.get_student_gto_record(student_id, record_id)
        if not award or award['status'] not in ('bronze', 'silver', 'gold'):
            return text('Награждение не найдено.', status=404)
        try:
            fields = store_certificate(record_id, request.files.get('file'))
        except ValueError as exception:
            return build_redirect_with_message(error=str(exception), url=back)
        except OSError:
            return build_redirect_with_message(
                error='Не удалось сохранить сертификат. Выберите файл повторно.', url=back
            )
        try:
            certificate_id = storage.create_gto_certificate(student_id, record_id, uploaded_by=user['id'], **fields)
        except Exception:
            cleanup_certificate(record_id, fields['stored_name'])
            return build_redirect_with_message(
                error='Не удалось сохранить сертификат. Выберите файл повторно.', url=back
            )
        if certificate_id is None:
            cleanup_certificate(record_id, fields['stored_name'])
            return text('Награждение не найдено.', status=404)
        log_audit_event(
            request,
            'student_gto_certificate_added',
            {'student_id': student_id, 'record_id': record_id, 'certificate_id': certificate_id},
        )
        return build_redirect_with_message(message='Сертификат прикреплён.', url=back)

    @app.post('/admin/people/<student_id:int>/gto/<record_id:int>/certificates/<certificate_id:int>/delete')
    async def delete_gto_certificate(request, student_id, record_id, certificate_id):
        error = require_moderator(request)
        if error is not None:
            return error
        user = live_moderator(request)
        if user is None:
            return forbidden(request)
        storage = get_storage(request.app)
        document = storage.get_gto_certificate_for_user(student_id, record_id, certificate_id, user['id'])
        if document is None:
            return text('Сертификат не найден.', status=404)
        if get_form_value(request, 'confirm_delete') != 'yes':
            return build_redirect_with_message(
                error='Подтвердите удаление сертификата.', url=gto_back_url(request, student_id)
            )
        if not storage.delete_gto_certificate(student_id, record_id, certificate_id):
            return text('Сертификат не найден.', status=404)
        cleanup_certificate(record_id, document['stored_name'])
        log_audit_event(
            request,
            'student_gto_certificate_deleted',
            {'student_id': student_id, 'record_id': record_id, 'certificate_id': certificate_id},
        )
        return build_redirect_with_message(message='Сертификат удалён.', url=gto_back_url(request, student_id))

    @app.get('/gto/<student_id:int>/awards/<record_id:int>/certificates/<certificate_id:int>')
    async def download_gto_certificate(request, student_id, record_id, certificate_id):
        storage = get_storage(request.app)

        def authorized():
            principal = parse_auth_cookie(request)
            return (
                storage.get_gto_certificate_for_user(student_id, record_id, certificate_id, principal['id'])
                if principal
                else None
            )

        document = authorized()
        if document is None:
            return forbidden(request)
        try:
            body, version = await asyncio.to_thread(read_certificate, record_id, document['stored_name'])
        except (OSError, ValueError):
            return text('Сертификат не найден.', status=404)
        current = authorized()
        if current is None:
            return forbidden(request)
        if current != document:
            return text('Сертификат изменён. Повторите запрос.', status=409)
        try:
            if version != certificate_identity(record_id, current['stored_name']):
                return text('Сертификат изменён. Повторите запрос.', status=409)
        except (OSError, ValueError):
            return text('Сертификат не найден.', status=404)
        if len(body) != current['size'] or detect_attachment_type(body, current['filename']) != current['content_type']:
            return text('Сертификат не найден.', status=404)
        disposition = 'inline' if request.args.get('view') == '1' else 'attachment'
        name = ''.join(c for c in current['filename'] if ord(c) >= 32 and ord(c) != 127)
        return raw(
            body,
            headers={
                'Content-Type': current['content_type'],
                'Content-Disposition': f"{disposition}; filename=certificate; filename*=UTF-8''{quote(name, safe='')}",
                'Cache-Control': 'private, no-store',
                'X-Content-Type-Options': 'nosniff',
                'Content-Security-Policy': "default-src 'none'; sandbox",
            },
        )
