import os
import time
import xml.etree.ElementTree as ET

import xbmc
import xbmcaddon
import xbmcvfs

from slyguy import log


ADDON_ID = 'plugin.video.abc_iviewjc'
SETTING_ID = 'library_scope_mode'
FLUSH_SETTING_ID = '_settings_flush_token'
DEFAULT_INDEX = 1

SCOPE_MANUAL = 'manual'
SCOPE_WATCHED = 'watched'
SCOPE_WATCHLIST = 'watchlist'
SCOPE_BY_INDEX = {
    0: SCOPE_MANUAL,
    1: SCOPE_WATCHED,
    2: SCOPE_WATCHLIST,
}


def _normalise_index(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_INDEX
    return value if value in SCOPE_BY_INDEX else DEFAULT_INDEX


def get_scope_index():
    """Read the scope through Kodi's modern Settings wrapper."""
    try:
        addon = xbmcaddon.Addon(ADDON_ID)
        manager = addon.getSettings()
        return _normalise_index(manager.getInt(SETTING_ID))
    except Exception as exc:
        log.warning(
            'ABC iView could not read the saved library mode; using watched '
            'default: {}'.format(exc)
        )
        return DEFAULT_INDEX


def get_scope():
    return SCOPE_BY_INDEX.get(get_scope_index(), SCOPE_WATCHED)


def _settings_file():
    addon = xbmcaddon.Addon(ADDON_ID)
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    return os.path.join(profile, 'settings.xml')


def _disk_scope_index():
    """Read the value Kodi has actually committed to profile/settings.xml."""
    path = _settings_file()
    if not os.path.isfile(path):
        return None

    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None

    for node in root.iter('setting'):
        if node.get('id') != SETTING_ID:
            continue
        raw = node.get('value')
        if raw is None:
            raw = node.text
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return value if value in SCOPE_BY_INDEX else None
    return None


def set_scope_index(value):
    """Write, force a disk flush, and verify the selected library scope.

    The actual setting write uses the Kodi 20+ Settings class. A changing,
    hidden legacy string setting is then written solely to force Kodi's add-on
    settings buffer to disk. Success is reported only after both a fresh
    Settings object and profile/settings.xml contain the requested value.
    """
    value = _normalise_index(value)

    addon = xbmcaddon.Addon(ADDON_ID)
    manager = addon.getSettings()
    written = manager.setInt(SETTING_ID, value)
    if written is False:
        raise RuntimeError('Kodi rejected the library mode setting write')

    # An unchanged empty dummy value may be optimised away, so use a changing
    # token to guarantee that the legacy writer has something to commit.
    flush_token = str(int(time.time() * 1000000))
    flushed = addon.setSettingString(FLUSH_SETTING_ID, flush_token)
    if flushed is False:
        raise RuntimeError('Kodi rejected the settings flush write')

    memory_value = None
    disk_value = None
    for _attempt in range(50):
        try:
            fresh = xbmcaddon.Addon(ADDON_ID).getSettings()
            memory_value = _normalise_index(fresh.getInt(SETTING_ID))
        except Exception:
            memory_value = None

        disk_value = _disk_scope_index()
        if memory_value == value and disk_value == value:
            log.info(
                'ABC iView library mode saved and verified: index={} scope={}'
                .format(value, SCOPE_BY_INDEX[value])
            )
            return value
        xbmc.sleep(100)

    raise RuntimeError(
        'Library mode was not committed (requested {}, memory {}, disk {})'
        .format(value, memory_value, disk_value)
    )
