import xbmc
import xbmcaddon
import xbmcgui

from .librarysettings import (
    ADDON_ID,
    SCOPE_BY_INDEX,
    get_diagnostic_logging,
    get_library_enabled,
    get_scope_index,
    set_diagnostic_logging,
    set_library_enabled,
    set_scope_index,
    queue_library_request,
)



def _label(addon, string_id, fallback):
    value = addon.getLocalizedString(string_id)
    return value or fallback


def _queue_library_action(action, **fields):
    """Queue work durably for the running service."""
    return queue_library_request(action, **fields)


def _save_error(exc):
    xbmc.log(
        '{} - Library configuration save failed: {}'.format(ADDON_ID, exc),
        xbmc.LOGERROR,
    )
    xbmcgui.Dialog().ok(
        'ABC iView+',
        'The library settings could not be saved.\n\n{}'.format(exc),
    )


def main():
    # This script can be launched from Kodi's settings window without an
    # implicit add-on context.  Always identify the add-on explicitly and do
    # not import SlyGuy here.
    addon = xbmcaddon.Addon(ADDON_ID)
    dialog = xbmcgui.Dialog()

    enabled_text = _label(addon, 30040, 'Enabled')
    disabled_text = _label(addon, 30041, 'Disabled')
    done_text = _label(addon, 30042, 'Done')
    diagnostics_text = _label(addon, 30043, 'Diagnostic logging')
    reconcile_text = _label(addon, 30044, 'Apply settings and reconcile now')
    scope_labels = [
        _label(addon, 30035, 'Manual selection only'),
        _label(
            addon,
            30036,
            'Shows with a currently available watched episode',
        ),
        _label(addon, 30037, 'My Watchlist only'),
    ]

    while True:
        enabled = get_library_enabled()
        scope_index = get_scope_index()
        diagnostics = get_diagnostic_logging()
        choices = [
            '{}: {}'.format(
                _label(addon, 30031, 'Enable Library Integration'),
                enabled_text if enabled else disabled_text,
            ),
            '{}: {}'.format(
                _label(addon, 30033, 'Shows to include'),
                scope_labels[scope_index],
            ),
            '{}: {}'.format(
                diagnostics_text,
                enabled_text if diagnostics else disabled_text,
            ),
            reconcile_text,
            done_text,
        ]

        selected = dialog.select(
            _label(addon, 30038, 'Configure Library Integration'),
            choices,
        )
        if selected < 0 or selected == 4:
            return

        if selected == 0:
            new_index = dialog.select(
                _label(addon, 30031, 'Enable Library Integration'),
                [disabled_text, enabled_text],
                preselect=1 if enabled else 0,
            )
            if new_index < 0:
                continue
            try:
                stored = set_library_enabled(new_index == 1)
                _queue_library_action(
                    'library_settings_changed',
                    enabled=stored,
                    scope=SCOPE_BY_INDEX[get_scope_index()],
                )
                dialog.notification(
                    'ABC iView+',
                    'Library setting saved and queued for application',
                    xbmcgui.NOTIFICATION_INFO,
                    3500,
                )
            except Exception as exc:
                _save_error(exc)
            continue

        if selected == 1:
            new_scope = dialog.select(
                _label(addon, 30033, 'Shows to include'),
                scope_labels,
                preselect=scope_index,
            )
            if new_scope < 0:
                continue
            try:
                stored = set_scope_index(new_scope)
                _queue_library_action(
                    'library_scope_changed',
                    index=stored,
                    scope=SCOPE_BY_INDEX[stored],
                )
                dialog.notification(
                    'ABC iView+',
                    'Library mode saved; reconciliation queued',
                    xbmcgui.NOTIFICATION_INFO,
                    4000,
                )
            except Exception as exc:
                _save_error(exc)
            continue

        if selected == 2:
            new_index = dialog.select(
                diagnostics_text,
                [disabled_text, enabled_text],
                preselect=1 if diagnostics else 0,
            )
            if new_index < 0:
                continue
            try:
                set_diagnostic_logging(new_index == 1)
            except Exception as exc:
                _save_error(exc)

            continue

        if selected == 3:
            try:
                _queue_library_action(
                    'library_reconcile_now',
                    scope=SCOPE_BY_INDEX[get_scope_index()],
                )
                dialog.notification(
                    'ABC iView+',
                    'Full library reconciliation queued',
                    xbmcgui.NOTIFICATION_INFO,
                    4000,
                )
            except Exception as exc:
                _save_error(exc)
