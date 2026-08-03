import xbmc
import xbmcaddon
import xbmcgui

from resources.lib.librarysettings import (
    ADDON_ID,
    SCOPE_BY_INDEX,
    get_scope_index,
    set_scope_index,
)


def _label(addon, string_id, fallback):
    value = addon.getLocalizedString(string_id)
    return value or fallback


def _request_library_scope_change(stored):
    try:
        from resources.lib.libraryintegration import request_library_action

        request_library_action(
            'library_scope_changed',
            index=stored,
            scope=SCOPE_BY_INDEX[stored],
        )
    except Exception as exc:
        xbmc.log(
            'ABC iView could not queue library scope refresh: {}'.format(exc),
            xbmc.LOGWARNING,
        )


def main():
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

    current = get_scope_index()
    selected = xbmcgui.Dialog().select(
        _label(addon, 30033, 'Shows to include'),
        labels,
        preselect=current,
    )
    if selected < 0:
        return

    try:
        stored = set_scope_index(selected)
    except Exception as exc:
        xbmcgui.Dialog().ok(
            'ABC iView',
            'The library mode could not be saved.\n\n{}'.format(exc),
        )
        return

    _request_library_scope_change(stored)
    xbmcgui.Dialog().notification(
        'ABC iView',
        'Library mode saved: {}'.format(labels[stored]),
        time=4000,
        sound=False,
    )


if __name__ == '__main__':
    main()
