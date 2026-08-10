"""Helpers for classifying Kodi VideoLibrary.OnUpdate notifications.

Kodi's native ToggleWatched action can be used both in a generated Kodi library
and while browsing this plugin.  Library rows have a positive database episode
ID; plugin list items commonly report no ID, zero, or a negative sentinel.
"""

import json


def parse_notification_payload(data):
    """Return a dictionary for a JSON notification payload."""
    if isinstance(data, dict):
        return data
    try:
        payload = json.loads(data) if data else {}
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def notification_item(payload):
    item = payload.get('item') if isinstance(payload, dict) else None
    return item if isinstance(item, dict) else {}


def positive_episode_id(payload):
    """Return a real Kodi episode database ID, or zero for plugin items."""
    item = notification_item(payload)
    if item.get('type') != 'episode':
        return 0
    try:
        episode_id = int(item.get('id'))
    except (TypeError, ValueError):
        return 0
    return episode_id if episode_id > 0 else 0


def notification_playcount(payload):
    """Return the requested playcount when Kodi supplied it."""
    if not isinstance(payload, dict):
        return None
    value = payload.get('playcount')
    if value is None:
        value = notification_item(payload).get('playcount')
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
