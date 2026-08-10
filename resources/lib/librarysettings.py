"""Durable settings for ABC iView library integration.

SlyGuy CommonSettings owns the normal add-on settings lifecycle and may migrate
or remove the standard profile/settings.xml file.  The library integration
settings therefore live in their own small JSON document beside the existing
library state files.  Writes are atomic and every setter verifies a fresh read.
"""

import json
import os
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON_ID = 'plugin.video.abc_iviewjc'
LIBRARY_WAKE_PROPERTY = 'abc_iviewjc.library_wake_v231'
# Fresh installs start in manual mode so enabling Library Integration cannot
# unexpectedly populate a user's TV library. Existing installations keep the
# value already stored in library_config.json.
DEFAULT_INDEX = 0

SCOPE_MANUAL = 'manual'
SCOPE_WATCHED = 'watched'
SCOPE_WATCHLIST = 'watchlist'
SCOPE_BY_INDEX = {
    0: SCOPE_MANUAL,
    1: SCOPE_WATCHED,
    2: SCOPE_WATCHLIST,
}

_CONFIG_VERSION = 2
_DEFAULTS = {
    'version': _CONFIG_VERSION,
    'library_integration': True,
    'library_scope_mode': DEFAULT_INDEX,
    'diagnostic_logging': False,
    # Monotonically increasing cross-process change token. The service uses
    # this to apply settings changes even when Kodi window properties are not
    # shared with a separately launched settings script.
    'revision': 0,
}
_LOCK = threading.RLock()


def _profile_path():
    addon = xbmcaddon.Addon(ADDON_ID)
    return xbmcvfs.translatePath(addon.getAddonInfo('profile'))


PROFILE = _profile_path()
CONFIG_FILE = os.path.join(PROFILE, 'library_config.json')
REQUESTS_DIR = os.path.join(PROFILE, 'library_requests')


def _normalise_index(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_INDEX
    return value if value in SCOPE_BY_INDEX else DEFAULT_INDEX


def _normalise_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ('1', 'true', 'yes', 'on', 'enabled'):
            return True
        if value in ('0', 'false', 'no', 'off', 'disabled', ''):
            return False
    return bool(default)


def _normalise(data):
    result = dict(_DEFAULTS)
    if isinstance(data, dict):
        result.update(data)
    result['version'] = _CONFIG_VERSION
    result['library_integration'] = _normalise_bool(
        result.get('library_integration'), True
    )
    result['library_scope_mode'] = _normalise_index(
        result.get('library_scope_mode')
    )
    result['diagnostic_logging'] = _normalise_bool(
        result.get('diagnostic_logging'), False
    )
    try:
        result['revision'] = max(0, int(result.get('revision') or 0))
    except (TypeError, ValueError):
        result['revision'] = 0
    return result


def _read_unlocked():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as handle:
            return _normalise(json.load(handle))
    except FileNotFoundError:
        return dict(_DEFAULTS)
    except (OSError, ValueError, TypeError) as exc:
        xbmc.log(
            '{} - Could not read {}; using defaults: {}'.format(
                ADDON_ID, CONFIG_FILE, exc
            ),
            xbmc.LOGWARNING,
        )
        return dict(_DEFAULTS)


def _write_unlocked(data):
    data = _normalise(data)
    profile = os.path.dirname(CONFIG_FILE)
    os.makedirs(profile, exist_ok=True)

    payload = json.dumps(
        data,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    ) + '\n'
    temp_path = '{}.{}.{}.tmp'.format(
        CONFIG_FILE,
        os.getpid(),
        int(time.time() * 1000000),
    )

    try:
        with open(temp_path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp_path, CONFIG_FILE)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

    fresh = _read_unlocked()
    for key in (
        'library_integration',
        'library_scope_mode',
        'diagnostic_logging',
        'revision',
    ):
        if fresh.get(key) != data.get(key):
            raise RuntimeError(
                'Library configuration verification failed for {}'.format(key)
            )
    return fresh


def get_config():
    with _LOCK:
        return dict(_read_unlocked())


def _set_value(key, value):
    with _LOCK:
        data = _read_unlocked()
        old_value = data.get(key)
        data[key] = value
        if old_value != value:
            data['revision'] = int(data.get('revision') or 0) + 1
        saved = _write_unlocked(data)
        xbmc.log(
            '{} - Saved library configuration: {}={} revision={}'.format(
                ADDON_ID,
                key,
                saved.get(key),
                saved.get('revision'),
            ),
            xbmc.LOGINFO,
        )
        return saved.get(key)


def queue_library_request(action, **fields):
    """Persist a service request as its own atomic file.

    Kodi may run a settings action in a separate Python interpreter. Window
    properties are not reliable enough for that boundary, so each request is
    placed on disk and survives interpreter exits and Kodi restarts.
    """
    if not action:
        return False
    os.makedirs(REQUESTS_DIR, exist_ok=True)
    created = time.time()
    request = {
        'action': str(action),
        'created': created,
    }
    request.update(fields)
    name = '{:020d}-{}-{}.json'.format(
        time.time_ns(),
        os.getpid(),
        threading.get_ident(),
    )
    path = os.path.join(REQUESTS_DIR, name)
    temp_path = path + '.tmp'
    payload = json.dumps(
        request,
        sort_keys=True,
        separators=(',', ':'),
    )
    with open(temp_path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(payload)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(temp_path, path)

    # The request file is the durable source of truth. This shared-window
    # token is only an immediate wake signal for the already-running service,
    # allowing it to remain completely idle between real events.
    try:
        xbmcgui.Window(10000).setProperty(
            LIBRARY_WAKE_PROPERTY,
            '{}:{}:{}'.format(time.time_ns(), os.getpid(), action),
        )
    except Exception:
        pass

    return True


def has_library_requests():
    """Return True when durable service requests are waiting on disk.

    This is used only by the service's low-frequency safety check. Normal
    request delivery still uses the immediate shared-window wake token.
    """
    try:
        return any(
            name.endswith('.json')
            for name in os.listdir(REQUESTS_DIR)
        )
    except (FileNotFoundError, OSError):
        return False


def pop_library_requests(limit=200):
    """Return and remove queued cross-process library requests."""
    try:
        names = sorted(
            name for name in os.listdir(REQUESTS_DIR)
            if name.endswith('.json')
        )[:max(1, int(limit or 1))]
    except FileNotFoundError:
        return []
    except OSError as exc:
        xbmc.log(
            '{} - Could not list library request queue: {}'.format(
                ADDON_ID, exc
            ),
            xbmc.LOGWARNING,
        )
        return []

    requests = []
    for name in names:
        path = os.path.join(REQUESTS_DIR, name)
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                request = json.load(handle)
            if isinstance(request, dict):
                requests.append(request)
            os.remove(path)
        except FileNotFoundError:
            continue
        except Exception as exc:
            xbmc.log(
                '{} - Could not consume library request {}: {}'.format(
                    ADDON_ID, path, exc
                ),
                xbmc.LOGWARNING,
            )
            try:
                os.replace(path, path + '.bad')
            except OSError:
                pass
    return requests


def get_library_enabled():
    return bool(get_config()['library_integration'])


def set_library_enabled(value):
    return bool(_set_value('library_integration', bool(value)))


def get_scope_index():
    return _normalise_index(get_config()['library_scope_mode'])


def set_scope_index(value):
    return _normalise_index(
        _set_value('library_scope_mode', _normalise_index(value))
    )


def get_scope():
    return SCOPE_BY_INDEX.get(get_scope_index(), SCOPE_WATCHED)


def get_diagnostic_logging():
    return bool(get_config()['diagnostic_logging'])


def set_diagnostic_logging(value):
    return bool(_set_value('diagnostic_logging', bool(value)))
