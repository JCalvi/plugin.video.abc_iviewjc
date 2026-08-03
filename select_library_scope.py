import json
import time

import xbmc
import xbmcaddon
import xbmcgui

from resources.lib.librarysettings import (
    ADDON_ID,
    SCOPE_BY_INDEX,
    get_scope_index,
    set_scope_index,
)


REQUESTS_PROPERTY = 'abc_iviewjc.library_requests'
MAX_REQUESTS = 200


def _label(addon, string_id, fallback):
    value = addon.getLocalizedString(string_id)
    return value or fallback


def _queue_library_action(action, **fields):
    """Queue work for the running service without importing SlyGuy."""
    window = xbmcgui.Window(10000)
    try:
        requests = json.loads(window.getProperty(REQUESTS_PROPERTY) or '[]')
    except Exception:
        requests = []
    if not isinstance(requests, list):
        requests = []

    request = {'action': action, 'created': time.time()}
    request.update(fields)
    requests.append(request)
    window.setProperty(
        REQUESTS_PROPERTY,
        json.dumps(
            requests[-MAX_REQUESTS:],
            sort_keys=True,
            separators=(',', ':'),
        ),
    )


def main():
    # RunScript from Kodi's settings window has no implicit add-on context.
    # Always identify this add-on explicitly and import no context-dependent
    # SlyGuy modules from this script.
    addon = xbmcaddon.Addon(ADDON_ID)
    labels = [
        _label(addon, 30035, 'Manual selection only'),
        _label(
            addon,
            30036,
            'Shows with a currently available watched episode',
        ),
        _label(addon, 30037, 'My Watchlist only'),
    ]

    selected = xbmcgui.Dialog().select(
        _label(addon, 30033, 'Shows to include'),
        labels,
        preselect=get_scope_index(),
    )
    if selected < 0:
        return

    try:
        stored = set_scope_index(selected)
    except Exception as exc:
        xbmc.log(
            'plugin.video.abc_iviewjc - Library mode save failed: {}'.format(exc),
            xbmc.LOGERROR,
        )
        xbmcgui.Dialog().ok(
            'ABC iView',
            'The library mode could not be saved.\n\n{}'.format(exc),
        )
        return

    _queue_library_action(
        'library_scope_changed',
        index=stored,
        scope=SCOPE_BY_INDEX[stored],
    )
    xbmcgui.Dialog().notification(
        'ABC iView',
        'Library mode saved: {}'.format(labels[stored]),
        time=4000,
        sound=False,
    )


if __name__ == '__main__':
    main()
