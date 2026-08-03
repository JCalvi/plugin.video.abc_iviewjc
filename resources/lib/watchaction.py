import json
import sys
import time
from urllib.parse import parse_qsl, urlparse

import xbmc
import xbmcgui

from .diagnostics import diagnostic_message


class _DiagnosticLog(object):
    def info(self, message):
        prefix = 'IVIEW114FIX '
        if message.startswith(prefix):
            message = message[len(prefix):]
        diagnostic_message('IVIEW115FIX', message)


log = _DiagnosticLog()


WINDOW = xbmcgui.Window(10000)

CANDIDATE_PROPERTY = 'abc_iviewjc.watch_candidate'
PENDING_PROPERTY = 'abc_iviewjc.pending_states'
QUEUE_PROPERTY = 'abc_iviewjc.watch_queue'
LAST_ACTION_PROPERTY = 'abc_iviewjc.last_watch_action'

CANDIDATE_TTL = 8.0
PENDING_TTL = 900.0
MAX_QUEUE = 50
DUPLICATE_WINDOW = 3.0


def _load_json_property(name, default):
    raw = WINDOW.getProperty(name)
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except Exception:
        WINDOW.clearProperty(name)
        return default
    return value


def _save_json_property(name, value):
    WINDOW.setProperty(
        name,
        json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
        ),
    )


def _normalise_url(value):
    if not value:
        return None
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip('/'),
            tuple(sorted(parse_qsl(
                parsed.query,
                keep_blank_values=True,
            ))),
        )
    except Exception:
        return value


def current_folder_url():
    folder = xbmc.getInfoLabel('Container.FolderPath') or ''
    if folder:
        return folder

    try:
        query = sys.argv[2] if len(sys.argv) > 2 else ''
        return '{}{}'.format(sys.argv[0], query)
    except Exception:
        return ''


def set_context_candidate(item, playcount, folder):
    if not item or not item.get('house_number'):
        return

    candidate = {
        'key': item.get('key'),
        'show_id': item.get('show_id'),
        'house_number': item.get('house_number'),
        'duration': int(item.get('duration') or 0),
        'baseline': 1 if int(playcount or 0) > 0 else 0,
        'folder': folder or '',
        'created': time.time(),
    }
    _save_json_property(CANDIDATE_PROPERTY, candidate)
    log.info(
        'IVIEW114FIX candidate_set house_number={} baseline={} '
        'folder={}'.format(
            candidate['house_number'],
            candidate['baseline'],
            candidate['folder'],
        )
    )


def mark_context_closed():
    candidate = _load_json_property(CANDIDATE_PROPERTY, {})
    if not candidate:
        return

    candidate['closed'] = time.time()
    _save_json_property(CANDIDATE_PROPERTY, candidate)
    log.info(
        'IVIEW114FIX candidate_closed house_number={}'.format(
            candidate.get('house_number')
        )
    )


def clear_context_candidate():
    WINDOW.clearProperty(CANDIDATE_PROPERTY)


def _load_pending():
    pending = _load_json_property(PENDING_PROPERTY, {})
    if not isinstance(pending, dict):
        pending = {}

    now = time.time()
    changed = False
    for house_number in list(pending):
        state = pending.get(house_number) or {}
        if float(state.get('expires') or 0) <= now:
            del pending[house_number]
            changed = True

    if changed:
        _save_json_property(PENDING_PROPERTY, pending)

    return pending


def set_pending_state(item, playcount, source):
    if not item or not item.get('house_number'):
        return

    target = 1 if int(playcount or 0) > 0 else 0
    pending = _load_pending()
    pending[item['house_number']] = {
        'key': item.get('key'),
        'show_id': item.get('show_id'),
        'house_number': item.get('house_number'),
        'duration': int(item.get('duration') or 0),
        'playcount': target,
        'source': source,
        'created': time.time(),
        'expires': time.time() + PENDING_TTL,
    }
    _save_json_property(PENDING_PROPERTY, pending)
    log.info(
        'IVIEW114FIX pending_set house_number={} playcount={} '
        'source={}'.format(
            item['house_number'],
            target,
            source,
        )
    )


def get_pending_state(house_number):
    if not house_number:
        return None
    return _load_pending().get(str(house_number))


def clear_pending_state(house_number, expected=None):
    if not house_number:
        return

    pending = _load_pending()
    state = pending.get(str(house_number))
    if not state:
        return

    if expected is not None:
        expected = 1 if int(expected or 0) > 0 else 0
        if int(state.get('playcount') or 0) != expected:
            return

    del pending[str(house_number)]
    _save_json_property(PENDING_PROPERTY, pending)
    log.info(
        'IVIEW114FIX pending_cleared house_number={} expected={}'.format(
            house_number,
            expected,
        )
    )


def apply_pending_state(house_number, server_state):
    """Use Kodi's latest local choice until iView reports the same state."""
    state = dict(server_state or {})
    pending = get_pending_state(house_number)
    if not pending:
        return state

    target = 1 if int(pending.get('playcount') or 0) > 0 else 0
    server_playcount = 1 if state.get('done') else 0

    if server_playcount == target:
        clear_pending_state(house_number, expected=target)
        return state

    log.info(
        'IVIEW114FIX pending_override house_number={} server={} '
        'local={}'.format(
            house_number,
            server_playcount,
            target,
        )
    )

    if target:
        state['done'] = True
        state['progress'] = max(
            1,
            int(
                pending.get('duration')
                or state.get('progress')
                or 1
            ),
        )
    else:
        state['done'] = False
        state['progress'] = 0

    return state


def enqueue_action(item, playcount, source, attempts=0, not_before=0):
    if not item or not item.get('house_number'):
        return False

    target = 1 if int(playcount or 0) > 0 else 0
    now = time.time()

    # The context-refresh handshake and the legacy playcount transition can
    # both observe the same successful Kodi action. Keep the first and suppress
    # only an identical second state for the same episode within three seconds.
    if int(attempts or 0) == 0:
        last_action = _load_json_property(
            LAST_ACTION_PROPERTY,
            {},
        )
        if (
            last_action.get('house_number') == item['house_number']
            and int(last_action.get('playcount') or 0) == target
            and now - float(last_action.get('created') or 0)
                < DUPLICATE_WINDOW
        ):
            log.info(
                'IVIEW114FIX action_duplicate_suppressed '
                'house_number={} playcount={} source={}'.format(
                    item['house_number'],
                    target,
                    source,
                )
            )
            return False

        _save_json_property(
            LAST_ACTION_PROPERTY,
            {
                'house_number': item['house_number'],
                'playcount': target,
                'created': now,
                'source': source,
            },
        )

    queue = _load_json_property(QUEUE_PROPERTY, [])
    if not isinstance(queue, list):
        queue = []

    # Retain only the newest requested state for each episode.
    queue = [
        action for action in queue
        if action.get('house_number') != item['house_number']
    ]
    queue.append({
        'key': item.get('key'),
        'show_id': item.get('show_id'),
        'house_number': item.get('house_number'),
        'duration': int(item.get('duration') or 0),
        'playcount': target,
        'source': source,
        'created': time.time(),
        'attempts': int(attempts or 0),
        'not_before': float(not_before or 0),
    })
    queue = queue[-MAX_QUEUE:]
    _save_json_property(QUEUE_PROPERTY, queue)

    log.info(
        'IVIEW114FIX action_queued house_number={} playcount={} '
        'source={} attempts={}'.format(
            item['house_number'],
            target,
            source,
            attempts,
        )
    )
    return True


def pop_due_action():
    queue = _load_json_property(QUEUE_PROPERTY, [])
    if not isinstance(queue, list) or not queue:
        return None

    now = time.time()
    selected_index = None
    for index, action in enumerate(queue):
        if float(action.get('not_before') or 0) <= now:
            selected_index = index
            break

    if selected_index is None:
        return None

    action = queue.pop(selected_index)
    _save_json_property(QUEUE_PROPERTY, queue)
    return action


def consume_context_refresh(folder=None):
    """Convert Kodi's automatic post-toggle folder rebuild into one action.

    Kodi refreshes the current plugin folder after its built-in Mark watched /
    Mark unwatched command. The candidate was captured while that item's
    context menu was open, before the local playcount could be overwritten by
    the folder rebuild.
    """
    candidate = _load_json_property(CANDIDATE_PROPERTY, {})
    if not candidate:
        return None

    age = time.time() - float(candidate.get('created') or 0)
    if age > CANDIDATE_TTL:
        log.info(
            'IVIEW114FIX candidate_expired house_number={} age={:.3f}'.format(
                candidate.get('house_number'),
                age,
            )
        )
        clear_context_candidate()
        return None

    current_folder = folder or current_folder_url()
    if (
        _normalise_url(candidate.get('folder'))
        != _normalise_url(current_folder)
    ):
        return None

    clear_context_candidate()

    target = 0 if int(candidate.get('baseline') or 0) > 0 else 1
    item = {
        'key': candidate.get('key'),
        'show_id': candidate.get('show_id'),
        'house_number': candidate.get('house_number'),
        'duration': int(candidate.get('duration') or 0),
    }

    set_pending_state(
        item,
        target,
        source='context_refresh',
    )
    enqueue_action(
        item,
        target,
        source='context_refresh',
    )

    log.info(
        'IVIEW114FIX candidate_consumed house_number={} baseline={} '
        'target={} age={:.3f}'.format(
            item['house_number'],
            candidate.get('baseline'),
            target,
            age,
        )
    )

    return {
        'item': item,
        'playcount': target,
    }
