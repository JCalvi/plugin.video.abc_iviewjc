"""Event-driven watched-state synchronisation for plugin-browser episodes.

Kodi's native Mark watched, Mark unwatched and ToggleWatched actions commit the
playcount to MyVideos and then invalidate/reload the current video directory.
The next dispatch of the same ABC episode folder is therefore the event.  We
compare one read-only database snapshot with the previous render manifest and
queue only a real playcount transition.

No keymap interception, selected-item polling or Container.Refresh is used.
"""

import json
import sys
import time
from urllib.parse import parse_qsl, urlparse

import xbmc
import xbmcgui

from .pluginwatchdb import read_latest_plugin_episode_rows


WINDOW = xbmcgui.Window(10000)
ADDON_URL_PREFIX = 'plugin://plugin.video.abc_iviewjc/'

PENDING_PROPERTY = 'abc_iviewjc.pending_states_v230'
QUEUE_PROPERTY = 'abc_iviewjc.watch_queue_v230'
LAST_ACTION_PROPERTY = 'abc_iviewjc.last_watch_action_v230'
MANIFEST_PROPERTY = 'abc_iviewjc.plugin_watch_manifest_v230'

PENDING_TTL = 900.0
MANIFEST_TTL = 3600.0
MAX_QUEUE = 50
DUPLICATE_WINDOW = 3.0

_ACTIVE_MANIFEST = None
_ACTIVE_ROWS = {}


def _load_json_property(name, default):
    raw = WINDOW.getProperty(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        WINDOW.clearProperty(name)
        return default


def _save_json_property(name, value):
    WINDOW.setProperty(
        name,
        json.dumps(value, sort_keys=True, separators=(',', ':')),
    )


def _normalise_signature(value):
    if value is None:
        return None
    try:
        return (
            int(value[0]),
            1 if int(value[1] or 0) > 0 else 0,
            str(value[2] or ''),
            str(value[3] or '') if len(value) > 3 else '',
        )
    except Exception:
        return None


def _normalise_folder(value):
    if not value:
        return None
    try:
        parsed = urlparse(str(value))
        pairs = [
            (key, item_value)
            for key, item_value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if key != '_play'
        ]
        return (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip('/'),
            tuple(sorted(pairs)),
        )
    except Exception:
        return str(value)


def current_folder_url():
    """Return the route currently being dispatched."""
    try:
        base = str(sys.argv[0] or '')
        if base.startswith(ADDON_URL_PREFIX):
            query = str(sys.argv[2] or '') if len(sys.argv) > 2 else ''
            return '{}{}'.format(base, query)
    except Exception:
        pass
    return xbmc.getInfoLabel('Container.FolderPath') or ''


def _snapshot_rows():
    try:
        return {
            str(house_number): _normalise_signature(signature)
            for house_number, signature
            in read_latest_plugin_episode_rows().items()
            if _normalise_signature(signature) is not None
        }, True
    except Exception as exc:
        xbmc.log(
            'plugin.video.abc_iviewjc - Unable to read Kodi watched rows: '
            '{}'.format(exc),
            xbmc.LOGWARNING,
        )
        return {}, False


def detect_folder_watch_changes(folder=None, allow_commit_retry=True):
    """Return native watched changes committed since the previous render."""
    manifest = _load_json_property(MANIFEST_PROPERTY, {})
    if not isinstance(manifest, dict) or not manifest.get('items'):
        return []

    age = time.time() - float(manifest.get('created') or 0)
    if age > MANIFEST_TTL:
        WINDOW.clearProperty(MANIFEST_PROPERTY)
        return []

    current_folder = folder or current_folder_url()
    if _normalise_folder(manifest.get('folder')) != _normalise_folder(current_folder):
        return []

    # The watched job commits before it asks Kodi to reload the folder. The
    # bounded retry is only a transaction-settling guard during that dispatch,
    # not a service poll.
    attempts = 5 if allow_commit_retry else 1
    rows = {}
    available = False
    changed = []
    for attempt in range(attempts):
        rows, available = _snapshot_rows()
        if not available:
            if attempt + 1 < attempts:
                xbmc.sleep(50)
                continue
            return []

        changed = []
        for house_number, entry in (manifest.get('items') or {}).items():
            if not isinstance(entry, dict):
                continue
            previous = _normalise_signature(entry.get('signature'))
            current = _normalise_signature(rows.get(str(house_number)))
            if current != previous:
                changed.append((str(house_number), entry, previous, current))

        if changed or attempt + 1 >= attempts:
            break
        xbmc.sleep(50)

    actions = []
    manifest_changed = False
    for house_number, entry, previous, current in changed:
        # Record every new database signature immediately. This prevents a
        # duplicate capture if Kodi performs more than one reload before the
        # current folder has finished rendering its replacement manifest.
        entry['signature'] = list(current) if current is not None else None
        manifest_changed = True

        # A native watched action creates or updates a concrete files row.
        if current is None:
            continue

        target = 1 if int(current[1] or 0) > 0 else 0
        rendered_baseline = 1 if int(
            entry.get('rendered_playcount') or 0
        ) > 0 else 0
        if target == rendered_baseline:
            continue

        item = {
            'key': entry.get('item_key'),
            'show_id': entry.get('show_id'),
            'house_number': house_number,
            'duration': int(entry.get('duration') or 0),
        }
        if not item['show_id'] or not item['house_number']:
            continue

        actions.append({
            'item': item,
            'playcount': target,
            'previous_signature': previous,
            'current_signature': current,
        })
        xbmc.log(
            'plugin.video.abc_iviewjc - Native Kodi watched change captured '
            'for {}: {}'.format(
                house_number,
                'watched' if target else 'unwatched',
            ),
            xbmc.LOGINFO,
        )

    if manifest_changed:
        _save_json_property(MANIFEST_PROPERTY, manifest)

    return actions


def begin_folder_watch_render(folder=None):
    """Start a fresh manifest for the current plugin dispatch.

    Kodi may reuse the Python interpreter between plugin invocations. Reset the
    process-local snapshot explicitly so each folder reload records the rows and
    items that actually belong to that dispatch.
    """
    global _ACTIVE_MANIFEST, _ACTIVE_ROWS

    _ACTIVE_MANIFEST = None
    _ACTIVE_ROWS = {}
    ensure_folder_watch_manifest(folder=folder)


def ensure_folder_watch_manifest(folder=None):
    """Create this dispatch's episode manifest on first rendered episode."""
    global _ACTIVE_MANIFEST, _ACTIVE_ROWS

    if _ACTIVE_MANIFEST is not None:
        return

    rows, available = _snapshot_rows()
    _ACTIVE_ROWS = rows
    _ACTIVE_MANIFEST = {
        'folder': folder or current_folder_url(),
        'created': time.time(),
        'database_available': bool(available),
        'items': {},
    }
    _save_json_property(MANIFEST_PROPERTY, _ACTIVE_MANIFEST)


def get_folder_watch_state(house_number, fallback_playcount):
    """Return ``(effective_playcount, has_kodi_row)`` for this render."""
    ensure_folder_watch_manifest()
    signature = _normalise_signature(_ACTIVE_ROWS.get(str(house_number)))
    if signature is None:
        return 1 if int(fallback_playcount or 0) > 0 else 0, False
    return 1 if int(signature[1] or 0) > 0 else 0, True


def record_folder_watch_item(item, playcount):
    """Record one rendered episode and its current native Kodi signature."""
    global _ACTIVE_MANIFEST

    if not item or not item.get('house_number'):
        return
    ensure_folder_watch_manifest()

    house_number = str(item['house_number'])
    signature = _normalise_signature(_ACTIVE_ROWS.get(house_number))
    _ACTIVE_MANIFEST['items'][house_number] = {
        'item_key': item.get('key'),
        'show_id': item.get('show_id'),
        'house_number': house_number,
        'duration': int(item.get('duration') or 0),
        'rendered_playcount': 1 if int(playcount or 0) > 0 else 0,
        'signature': list(signature) if signature is not None else None,
    }
    _save_json_property(MANIFEST_PROPERTY, _ACTIVE_MANIFEST)


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
    pending[str(item['house_number'])] = {
        'key': item.get('key'),
        'show_id': item.get('show_id'),
        'house_number': str(item.get('house_number')),
        'duration': int(item.get('duration') or 0),
        'playcount': target,
        'source': source,
        'created': time.time(),
        'expires': time.time() + PENDING_TTL,
    }
    _save_json_property(PENDING_PROPERTY, pending)


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


def apply_pending_state(house_number, server_state):
    """Keep Kodi's local choice until ABC reports the same result."""
    state = dict(server_state or {})
    pending = get_pending_state(house_number)
    if not pending:
        return state

    target = 1 if int(pending.get('playcount') or 0) > 0 else 0
    server_playcount = 1 if state.get('done') else 0
    if server_playcount == target:
        clear_pending_state(house_number, expected=target)
        return state

    if target:
        state['done'] = True
        state['progress'] = max(
            1,
            int(pending.get('duration') or state.get('progress') or 1),
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
    if int(attempts or 0) == 0:
        last_action = _load_json_property(LAST_ACTION_PROPERTY, {})
        if (
            last_action.get('house_number') == str(item['house_number'])
            and int(last_action.get('playcount') or 0) == target
            and now - float(last_action.get('created') or 0) < DUPLICATE_WINDOW
        ):
            return False
        _save_json_property(LAST_ACTION_PROPERTY, {
            'house_number': str(item['house_number']),
            'playcount': target,
            'created': now,
            'source': source,
        })

    queue = _load_json_property(QUEUE_PROPERTY, [])
    if not isinstance(queue, list):
        queue = []
    queue = [
        action for action in queue
        if action.get('house_number') != str(item['house_number'])
    ]
    queue.append({
        'key': item.get('key'),
        'show_id': item.get('show_id'),
        'house_number': str(item.get('house_number')),
        'duration': int(item.get('duration') or 0),
        'playcount': target,
        'source': source,
        'created': now,
        'attempts': int(attempts or 0),
        'not_before': float(not_before or 0),
    })
    _save_json_property(QUEUE_PROPERTY, queue[-MAX_QUEUE:])
    return True


def pop_due_action():
    queue = _load_json_property(QUEUE_PROPERTY, [])
    if not isinstance(queue, list) or not queue:
        return None

    now = time.time()
    for index, action in enumerate(queue):
        if float(action.get('not_before') or 0) <= now:
            selected = queue.pop(index)
            _save_json_property(QUEUE_PROPERTY, queue)
            return selected
    return None


def next_action_due_at():
    """Return the next queued ABC write time, or 0 when the queue is empty."""
    queue = _load_json_property(QUEUE_PROPERTY, [])
    if not isinstance(queue, list) or not queue:
        return 0.0

    due = []
    for action in queue:
        try:
            due.append(float(action.get('not_before') or 0))
        except (TypeError, ValueError):
            due.append(0.0)
    if not due:
        return 0.0
    earliest = min(due)
    return earliest if earliest > 0 else time.time()
