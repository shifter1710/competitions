"""Bounded Telegram delivery and persistent incident deduplication, without Sanic."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from src.backup_offsite import age_seconds
from src.health import health_view
from src.ops_state import atomic_json
from src.ops_state import locked
from src.ops_state import read_json
from src.ops_state import update_component
from src.ops_state import utcnow


def retry_delay(payload):
    """Telegram's optional rate-limit fields are untrusted JSON, never exceptions."""
    if not isinstance(payload, dict):
        return 1
    parameters = payload.get('parameters')
    if not isinstance(parameters, dict):
        return 1
    value = parameters.get('retry_after', 1)
    if isinstance(value, bool):
        return 1
    try:
        return min(30, max(1, int(value)))
    except (ValueError, TypeError, OverflowError):
        return 1


def send_message(token, chat_id, message, *, timeout=5, retries=2, opener=None, sleeper=time.sleep):
    opener = opener or urllib.request.urlopen
    body = urllib.parse.urlencode({'chat_id': chat_id, 'text': message}).encode()
    for attempt in range(max(0, min(retries, 3)) + 1):
        delay = 1
        try:
            request = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage', data=body)
            with opener(request, timeout=max(1, min(timeout, 10))) as response:
                result = json.loads(response.read(65536))
            if isinstance(result, dict) and result.get('ok') is True:
                return True
            delay = retry_delay(result)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    delay = retry_delay(json.loads(exc.read(65536)))
                except (OSError, ValueError, TypeError):
                    pass
        except (OSError, ValueError, TimeoutError):
            pass  # Never log an exception: it can include the bot token URL.
        if attempt < min(retries, 3):
            sleeper(delay)
    return False


def notify(state_dir, *, enabled=False, token='', chat_id='', timeout=5, retries=2, sender=send_message):  # noqa: C901
    with locked(Path(state_dir) / 'alerts.lock'):
        if not enabled or not token or not chat_id:
            update_component(state_dir, 'alerts', {'status': 'UNKNOWN', 'code': 'disabled'})
            return
        state_path = Path(state_dir) / 'alert-incidents.json'
        incidents = read_json(state_path)
        cards = {card['key']: card for card in health_view(state_dir)['cards'] if card['key'] != 'alerts'}
        failed = False
        last_sent = None
        for key, card in cards.items():
            old = incidents.get(key, {})
            active = card['status'] in {'CRITICAL', 'WARNING'}
            signature = card['status'] + ':' + card['message']
            recovery = not active and card['status'] == 'OK' and old.get('active') is True
            new = active and (old.get('signature') != signature or not old.get('active'))
            reminder = (
                active
                and old.get('active')
                and (age_seconds(old.get('sent_at')) or 0) >= 24 * 3600
                and old.get('reminders', 0) < 2
            )
            # A failed delivery is not marked sent; retry on next scheduled run.
            retry = active and old.get('pending') is True
            if not (new or reminder or recovery or retry):
                continue
            if age_seconds(old.get('attempted_at')) is not None and age_seconds(old['attempted_at']) < 15 * 60:
                continue
            message = (
                'Competitions — Backup Alert\nStatus: '
                + ('RECOVERED' if recovery else card['status'])
                + '\nComponent: '
                + card['title']
                + '\nProblem: '
                + ('Работа восстановлена' if recovery else card['message'])
                + '\nDetected: '
                + utcnow()
            )
            try:
                success = sender(token, chat_id, message, timeout=timeout, retries=retries) is True
            except Exception:
                # A transport/parser failure must still persist an undelivered incident.
                success = False
            old.update(attempted_at=utcnow(), pending=not success)
            if success:
                old.update(
                    active=active,
                    signature=signature,
                    sent_at=utcnow(),
                    reminders=(old.get('reminders', 0) + 1) if reminder else 0,
                )
                last_sent = old['sent_at']
            else:
                failed = True
            incidents[key] = old
        # A never-delivered incident that has already resolved needs no recovery message.
        for key, old in incidents.items():
            if old.get('pending') and not old.get('active') and cards.get(key, {}).get('status') == 'OK':
                old['pending'] = False
        failed = any(item.get('pending') for item in incidents.values())
        atomic_json(state_path, incidents)
        value = {'status': 'CRITICAL' if failed else 'OK', 'code': 'delivery_failed' if failed else 'ok'}
        if last_sent:
            value['last_sent_at'] = last_sent
        update_component(state_dir, 'alerts', value)
