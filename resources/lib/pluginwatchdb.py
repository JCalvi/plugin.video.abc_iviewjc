"""Read Kodi's native watched rows for ABC iView plugin episodes.

Kodi stores watched state against the playable plugin URL in MyVideos*.db.
SlyGuy adds its private ``_play=1`` query argument when rendering playable
items, so full-URL string matching is unnecessarily fragile.  These helpers
identify a row by the stable ABC house number embedded in a versioned playable
URL instead.

All SQLite connections are query-only.  This module never writes Kodi's
video database.
"""

import glob
import html
import os
import re
import sqlite3
from urllib.parse import parse_qs, urlparse

import xbmcvfs


ADDON_ID = 'plugin.video.abc_iviewjc'
ADDON_URL_PREFIX = 'plugin://{}/'.format(ADDON_ID)
WATCH_MARKER_KEY = 'iview_watch'
WATCH_MARKER_VALUE = '4'


def _first(query, name, default=''):
    values = query.get(name)
    return values[0] if values else default


def parse_plugin_episode(value, require_marker=True):
    """Return stable episode identity from a playable ABC plugin URL.

    Query ordering, URL escaping and SlyGuy's ``_play`` routing flag do not
    affect the identity.  ``None`` means the value is not one of this
    version's playable episode URLs.
    """
    if not value:
        return None

    try:
        raw = html.unescape(str(value))
        parsed = urlparse(raw)
        if parsed.scheme.lower() != 'plugin':
            return None
        if parsed.netloc.lower() != ADDON_ID:
            return None

        query = parse_qs(parsed.query, keep_blank_values=True)
        marker = _first(query, WATCH_MARKER_KEY)
        if require_marker and marker != WATCH_MARKER_VALUE:
            return None

        house_number = _first(query, 'house_number')
        source_url = _first(query, 'url')
        if not house_number and source_url:
            source_path = urlparse(source_url).path
            if '/video/' in source_path:
                house_number = source_path.rstrip('/').rsplit('/', 1)[-1]

        house_number = str(house_number or '').strip()
        if not house_number:
            return None

        return {
            'house_number': house_number,
            'show_id': str(_first(query, 'show_id') or '').strip(),
            'marker': str(marker or ''),
            'url': raw,
        }
    except Exception:
        return None


def combine_kodi_file_path(directory, filename):
    """Return likely full URL candidates for one Kodi path/files row."""
    directory = str(directory or '')
    filename = str(filename or '')
    candidates = []

    for value in (
        '{}{}'.format(directory, filename),
        directory,
        filename,
        '{}/{}'.format(directory.rstrip('/'), filename.lstrip('/'))
            if directory and filename else '',
    ):
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _row_signature(row, full_url):
    return (
        int(row['idFile']),
        1 if int(row['playCount'] or 0) > 0 else 0,
        str(row['lastPlayed'] or ''),
        str(full_url or ''),
    )


def _connect_query_only(database_path):
    connection = sqlite3.connect(str(database_path), timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only = ON')
    return connection


def read_plugin_episode_rows(database_path):
    """Return newest Kodi signature for each marked ABC house number.

    The query deliberately examines both halves of Kodi's path/files split.
    This makes it independent of exactly where Kodi splits a plugin URL.
    """
    if not database_path:
        return {}

    connection = _connect_query_only(database_path)
    try:
        addon_pattern = '%{}%'.format(ADDON_ID)
        marker_pattern = '%{}={}%'.format(
            WATCH_MARKER_KEY,
            WATCH_MARKER_VALUE,
        )
        rows = connection.execute(
            '''
            SELECT f.idFile, f.playCount, f.lastPlayed,
                   p.strPath, f.strFileName
              FROM files AS f
              JOIN path AS p ON p.idPath = f.idPath
             WHERE p.strPath LIKE ?
                OR f.strFileName LIKE ?
                OR p.strPath LIKE ?
                OR f.strFileName LIKE ?
             ORDER BY f.idFile DESC
            ''',
            (
                addon_pattern,
                addon_pattern,
                marker_pattern,
                marker_pattern,
            ),
        ).fetchall()

        result = {}
        for row in rows:
            identity = None
            full_url = ''
            for candidate in combine_kodi_file_path(
                row['strPath'],
                row['strFileName'],
            ):
                parsed = parse_plugin_episode(candidate, require_marker=True)
                if parsed:
                    identity = parsed
                    full_url = candidate
                    break

            if not identity:
                continue

            house_number = identity['house_number']
            # Rows are newest-first. The first row for an episode is the one
            # Kodi most recently created or updated for this URL generation.
            if house_number not in result:
                result[house_number] = _row_signature(row, full_url)
        return result
    finally:
        connection.close()


def latest_kodi_video_database():
    """Return Kodi's newest local MyVideos*.db path."""
    database_dir = xbmcvfs.translatePath('special://database/')
    candidates = glob.glob(os.path.join(database_dir, 'MyVideos*.db'))
    if not candidates:
        return None

    def version(path):
        match = re.search(r'MyVideos(\d+)\.db$', os.path.basename(path), re.I)
        return int(match.group(1)) if match else -1

    return max(candidates, key=version)


def read_latest_plugin_episode_rows():
    database_path = latest_kodi_video_database()
    if not database_path:
        return {}
    return read_plugin_episode_rows(database_path)
