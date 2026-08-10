"""Read Kodi's native watched state for plugin list items.

Kodi's standard ToggleWatched action stores the selected plugin URL in the
video database even when the item is not a scanned library episode.  The
background service can therefore observe the normal Kodi action without
replacing the user's keymap or creating a custom GUI window.
"""

import sqlite3
from urllib.parse import parse_qsl, urlparse



ADDON_URL_PREFIX = 'plugin://plugin.video.abc_iviewjc/'


def normalise_plugin_url(value):
    """Return a stable comparison key for a Kodi plugin URL."""
    if not value:
        return None

    try:
        parsed = urlparse(str(value))
        return (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip('/'),
            tuple(sorted(parse_qsl(
                parsed.query,
                keep_blank_values=True,
            ))),
        )
    except Exception:
        return str(value)


def combine_kodi_file_path(directory, filename):
    """Rebuild the full URL represented by Kodi's path/files tables."""
    directory = str(directory or '')
    filename = str(filename or '')
    if not directory:
        return filename
    if not filename:
        return directory
    return '{}{}'.format(directory, filename)


def _row_signature(row):
    return (
        int(row['idFile']),
        1 if int(row['playCount'] or 0) > 0 else 0,
        str(row['lastPlayed'] or ''),
    )


def read_plugin_watch_signature(database_path, plugin_url):
    """Return ``(idFile, playcount, lastPlayed)`` for *plugin_url*.

    ``None`` means Kodi has not yet written a file-history row for this URL.
    The connection is query-only; this add-on never edits Kodi's database here.
    """
    if not database_path or not plugin_url:
        return None

    target = normalise_plugin_url(plugin_url)
    connection = sqlite3.connect(str(database_path), timeout=1.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only = ON')

        # The common case is an exact byte-for-byte match.  It avoids scanning
        # historical plugin rows on every selected-episode check.
        row = connection.execute(
            '''
            SELECT f.idFile, f.playCount, f.lastPlayed,
                   p.strPath, f.strFileName
              FROM files AS f
              JOIN path AS p ON p.idPath = f.idPath
             WHERE (p.strPath || f.strFileName) = ?
             ORDER BY f.idFile DESC
             LIMIT 1
            ''',
            (str(plugin_url),),
        ).fetchone()
        if row is not None:
            return _row_signature(row)

        # Kodi can canonicalise escaping or split the path at a slightly
        # different point.  Compare normalised URLs as a narrow fallback.
        rows = connection.execute(
            '''
            SELECT f.idFile, f.playCount, f.lastPlayed,
                   p.strPath, f.strFileName
              FROM files AS f
              JOIN path AS p ON p.idPath = f.idPath
             WHERE p.strPath LIKE ?
             ORDER BY f.idFile DESC
            ''',
            (ADDON_URL_PREFIX + '%',),
        ).fetchall()
        for candidate in rows:
            full_path = combine_kodi_file_path(
                candidate['strPath'],
                candidate['strFileName'],
            )
            if normalise_plugin_url(full_path) == target:
                return _row_signature(candidate)

        return None
    finally:
        connection.close()
