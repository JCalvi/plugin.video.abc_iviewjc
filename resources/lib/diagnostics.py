import json
import threading
import time

import xbmc
import xbmcaddon


ADDON_ID = 'plugin.video.abc_iviewjc'

_CACHE_SECONDS = 1.0
_STATE_LOCK = threading.Lock()
_STATE_EXPIRES = 0.0
_STATE = (False, False, False)


def _addon_diagnostics_enabled():
    try:
        addon = xbmcaddon.Addon(ADDON_ID)
        return bool(
            addon.getSettings().getBool('diagnostic_logging')
        )
    except Exception:
        try:
            addon = xbmcaddon.Addon(ADDON_ID)
            return (
                addon.getSetting('diagnostic_logging')
                .strip()
                .lower()
                in ('1', 'true', 'yes', 'on')
            )
        except Exception:
            return False


def _kodi_debug_enabled():
    """Read Kodi's global Enable debug logging setting."""
    request = {
        'jsonrpc': '2.0',
        'method': 'Settings.GetSettingValue',
        'params': {'setting': 'debug.showloginfo'},
        'id': 1,
    }

    try:
        response = json.loads(
            xbmc.executeJSONRPC(json.dumps(request))
        )
        return bool(
            response.get('result', {}).get('value')
        )
    except Exception:
        try:
            return bool(
                xbmc.getCondVisibility(
                    'System.GetBool(debug.showloginfo)'
                )
            )
        except Exception:
            return False


def diagnostic_state(force=False):
    """Return (enabled, add-on switch, Kodi global debug)."""
    global _STATE_EXPIRES, _STATE

    now = time.time()
    with _STATE_LOCK:
        if not force and now < _STATE_EXPIRES:
            return _STATE

        addon_enabled = _addon_diagnostics_enabled()
        kodi_enabled = _kodi_debug_enabled()
        _STATE = (
            addon_enabled or kodi_enabled,
            addon_enabled,
            kodi_enabled,
        )
        _STATE_EXPIRES = now + _CACHE_SECONDS
        return _STATE


def diagnostics_enabled():
    return diagnostic_state()[0]


def redact(value, key=''):
    """Remove credentials and bound very large values before logging."""
    key_lower = str(key or '').lower()

    if any(
        token in key_lower
        for token in (
            'access_token',
            'refreshtoken',
            'refresh_token',
            'authorization',
            'signature',
            'password',
            'secret',
        )
    ):
        return '<redacted>'

    if key_lower == 'email':
        return '<redacted>'

    if isinstance(value, dict):
        return {
            str(child_key): redact(child_value, child_key)
            for child_key, child_value in value.items()
        }

    if isinstance(value, (list, tuple)):
        limit = 100
        result = [
            redact(child_value)
            for child_value in value[:limit]
        ]
        if len(value) > limit:
            result.append(
                '<{} additional values omitted>'.format(
                    len(value) - limit
                )
            )
        return result

    if isinstance(value, str) and len(value) > 4000:
        return (
            value[:4000]
            + '<{} characters omitted>'.format(
                len(value) - 4000
            )
        )

    return value


def _format_value(value):
    value = redact(value)
    try:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(
                value,
                sort_keys=True,
                separators=(',', ':'),
                default=str,
            )
        return str(value)
    except Exception:
        return repr(value)


def diagnostic_message(prefix, message):
    enabled, addon_enabled, _kodi_enabled = diagnostic_state()
    if not enabled:
        return

    # The add-on switch must remain useful at Kodi's normal log level.
    level = xbmc.LOGINFO if addon_enabled else xbmc.LOGDEBUG
    xbmc.log(
        '{} - {} {}'.format(
            ADDON_ID,
            prefix,
            message,
        ).rstrip(),
        level,
    )


def diagnostic_event(prefix, event, **fields):
    if not diagnostics_enabled():
        return

    parts = [
        '{}={}'.format(key, _format_value(fields[key]))
        for key in sorted(fields)
    ]
    diagnostic_message(
        prefix,
        '{} {}'.format(
            event,
            ' '.join(parts),
        ).rstrip(),
    )
