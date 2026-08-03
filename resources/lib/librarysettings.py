import os
import xml.etree.ElementTree as ET

import xbmc
import xbmcaddon
import xbmcvfs

from slyguy import log


ADDON_ID = 'plugin.video.abc_iviewjc'
SETTING_ID = 'library_scope_mode'
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


def _write_disk_setting(setting_id, value):
    path = _settings_file()
    directory = os.path.dirname(path)
    if directory and not xbmcvfs.exists(directory):
        xbmcvfs.mkdirs(directory)

    root = ET.Element('settings')
    if os.path.isfile(path):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            root = ET.Element('settings')

    node = None
    for candidate in root.iter('setting'):
        if candidate.get('id') == setting_id:
            node = candidate
            break

    if node is None:
        node = ET.SubElement(root, 'setting', id=setting_id)

    if 'value' in node.attrib:
        del node.attrib['value']
    node.text = str(value)

    tree = ET.ElementTree(root)
    temp_path = path + '.tmp'
    tree.write(temp_path, encoding='utf-8', xml_declaration=True)
    os.replace(temp_path, path)


def set_scope_index(value):
    """Write, force a disk flush, and verify the selected library scope.

    The actual setting write uses Kodi's Settings class. If Kodi keeps the
    write in memory longer than expected, the add-on profile settings file is
    updated directly so the saved mode survives a restart.
    """
    value = _normalise_index(value)

    addon = xbmcaddon.Addon(ADDON_ID)
    manager = addon.getSettings()
    written = manager.setInt(SETTING_ID, value)
    if written is False:
        raise RuntimeError('Kodi rejected the library mode setting write')

    memory_value = None
    disk_value = None
    for _attempt in range(20):
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

    _write_disk_setting(SETTING_ID, value)
    disk_value = _disk_scope_index()
    if disk_value == value:
        log.info(
            'ABC iView library mode saved after explicit disk persist: '
            'index={} scope={}'.format(value, SCOPE_BY_INDEX[value])
        )
        return value

    raise RuntimeError(
        'Library mode was not committed (requested {}, memory {}, disk {})'
        .format(value, memory_value, disk_value)
    )
