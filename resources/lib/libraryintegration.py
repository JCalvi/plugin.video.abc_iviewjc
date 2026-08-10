import datetime
import glob
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from slyguy import log

from .api import API
from .diagnostics import diagnostic_event
from .librarymeta import (
    build_episode_records,
    episode_basename,
    episode_nfo,
    link_href,
    plugin_play_url,
    safe_component,
    show_title,
    tvshow_nfo,
)
from .kodiwatchdb import read_plugin_watch_signature
from .librarysettings import (
    ADDON_ID,
    SCOPE_BY_INDEX,
    SCOPE_MANUAL as LIBRARY_SCOPE_MANUAL,
    SCOPE_WATCHED as LIBRARY_SCOPE_WATCHED,
    SCOPE_WATCHLIST as LIBRARY_SCOPE_WATCHLIST,
    get_config,
    get_library_enabled,
    get_scope as library_scope,
    pop_library_requests,
    queue_library_request,
)
from .watchnotification import (
    notification_playcount,
    parse_notification_payload,
    positive_episode_id,
)


ADDON = xbmcaddon.Addon(ADDON_ID)
WINDOW = xbmcgui.Window(10000)

PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
LIBRARY_ROOT = os.path.join(PROFILE, 'library', 'tvshows')
STATE_FILE = os.path.join(PROFILE, 'library_state.json')
MANUAL_SHOWS_FILE = os.path.join(PROFILE, 'manual_library.json')
SOURCE_NAME = 'ABC iView Library'
REQUESTS_PROPERTY = 'abc_iviewjc.library_requests'

REFRESH_INTERVAL = 6 * 60 * 60
BOOTSTRAP_INTERVAL = 24 * 60 * 60
QUEUE_RETRY_LIMIT = 4
MAX_REQUESTS = 200
EPISODE_STATE_MISSING_RETRY = 10 * 60
EPISODE_STATE_MAX_RETRY = 6 * 60 * 60
STATE_VERSION = 12

def library_enabled():
    # Read this directly from Kodi rather than through a long-lived wrapper,
    # so reopening Settings and pressing OK is reflected immediately.
    return get_library_enabled()


def _property_json(name, default):
    raw = WINDOW.getProperty(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        WINDOW.clearProperty(name)
        return default


def request_library_action(action, **fields):
    """Send a durable cross-interpreter request to the service."""
    if not library_enabled():
        return False
    return queue_library_request(action, **fields)


def request_follow_show(show_id, source, house_number=''):
    if not show_id:
        return False
    return request_library_action(
        'follow_show',
        show_id=str(show_id),
        source=str(source or ''),
        house_number=str(house_number or ''),
    )


def request_reconcile_show(show_id, source=''):
    if not show_id:
        return False
    return request_library_action(
        'reconcile_show',
        show_id=str(show_id),
        source=str(source or ''),
    )


def request_episode_state(
    show_id,
    house_number,
    playcount,
    progress=0,
    duration=0,
    source='',
):
    if not show_id or not house_number:
        return False
    return request_library_action(
        'episode_state',
        show_id=str(show_id),
        house_number=str(house_number),
        playcount=1 if int(playcount or 0) > 0 else 0,
        progress=max(0, int(progress or 0)),
        duration=max(0, int(duration or 0)),
        source=str(source or ''),
    )


def _jsonrpc(method, params=None):
    payload = {
        'jsonrpc': '2.0',
        'method': method,
        'id': 1,
    }
    if params is not None:
        payload['params'] = params
    raw = xbmc.executeJSONRPC(json.dumps(payload))
    try:
        response = json.loads(raw)
    except Exception:
        raise RuntimeError('{} returned invalid JSON'.format(method))
    if response.get('error'):
        raise RuntimeError(
            '{} failed: {}'.format(method, response['error'])
        )
    return response.get('result') or {}

def _jsonrpc_batch(calls):
    """Execute a group of JSON-RPC writes in one Kodi request.

    Kodi still reports each changed episode, but sending one batch prevents the
    service from spreading state writes across one-second ticks and repeatedly
    disturbing library-backed Home/Favourites controls.
    """
    payload = []
    for index, call in enumerate(calls or [], 1):
        method, params = call
        item = {
            'jsonrpc': '2.0',
            'method': method,
            'id': index,
        }
        if params is not None:
            item['params'] = params
        payload.append(item)
    if not payload:
        return []

    raw = xbmc.executeJSONRPC(json.dumps(payload))
    try:
        response = json.loads(raw)
    except Exception:
        raise RuntimeError('JSON-RPC batch returned invalid JSON')
    if not isinstance(response, list):
        raise RuntimeError('JSON-RPC batch returned an invalid response')
    errors = [row.get('error') for row in response if row.get('error')]
    if errors:
        raise RuntimeError('JSON-RPC batch failed: {}'.format(errors))
    return response


def _atomic_write(path, data, binary=False):
    """Atomically write only when the file content has actually changed.

    Kodi scans use file timestamps as one of their change signals. Replacing
    every NFO and STRM during a routine catalogue refresh made an unchanged
    library look newly modified and caused unnecessary rescans.
    """
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)

    if binary:
        payload = data if isinstance(data, bytes) else bytes(data)
    else:
        payload = str(data).encode('utf-8')

    try:
        with open(path, 'rb') as existing:
            if existing.read() == payload:
                return False
    except FileNotFoundError:
        pass
    except Exception:
        # A failed comparison must not prevent the intended write.
        pass

    # Keep the temporary file beside the destination so replacement stays
    # atomic. A unique name also prevents overlapping service instances from
    # writing through the same fixed ``.tmp`` path.
    fd, temp = tempfile.mkstemp(
        prefix='.{}.'.format(os.path.basename(path)),
        suffix='.tmp',
        dir=directory or os.curdir,
    )
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass
    return True


def _read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            value = json.load(handle)
        return value
    except Exception:
        return default


def manual_show_ids():
    """Return shows explicitly selected for manual library mode."""
    value = _read_json(MANUAL_SHOWS_FILE, [])
    if isinstance(value, dict):
        value = value.get('show_ids', [])
    if not isinstance(value, list):
        return set()
    return {str(show_id) for show_id in value if show_id}


def is_manual_show(show_id):
    return bool(show_id) and str(show_id) in manual_show_ids()


def set_manual_show(show_id, included):
    """Persist a user's manual add/remove choice and wake the service."""
    if not show_id:
        return False
    show_id = str(show_id)
    selected = manual_show_ids()
    changed = False
    if included and show_id not in selected:
        selected.add(show_id)
        changed = True
    elif not included and show_id in selected:
        selected.remove(show_id)
        changed = True

    if changed:
        _atomic_write(
            MANUAL_SHOWS_FILE,
            json.dumps({'show_ids': sorted(selected)}, indent=2),
        )
        request_library_action(
            'manual_selection_changed',
            show_id=show_id,
            included=bool(included),
        )
    return changed


def _normalise_path(path):
    path = xbmcvfs.translatePath(str(path or ''))
    path = os.path.normpath(path)
    if not path.endswith(('/', '\\')):
        path += os.sep
    return path


def _path_key(path):
    return _normalise_path(path).replace('\\', '/').lower()


class LibraryIntegration(object):
    def __init__(self):
        self.state = _read_json(STATE_FILE, {})
        if not isinstance(self.state, dict):
            self.state = {}

        try:
            previous_version = int(self.state.get('version') or 0)
        except Exception:
            previous_version = 0

        self.state['version'] = STATE_VERSION
        self.state.setdefault('followed', {})
        self.state.setdefault('queue', [])
        self.state.setdefault('episode_states', {})
        self.state.setdefault('last_bootstrap', 0)
        self.state.setdefault('last_refresh', 0)
        self.state.setdefault('qualification_recheck_at', 0)
        self.state.setdefault('source_ready', False)
        self.state.setdefault('setup_error_notified', False)
        self.state.setdefault('library_scope', '')
        self.state.setdefault('library_config_revision', -1)
        self.state.setdefault('library_enabled', None)
        self.state.setdefault('reconcile_reason', '')
        self.state.setdefault('tvshow_map', {})

        # Force a qualification pass after state/schema upgrades so the
        # active mode is applied and stale generated shows are pruned.
        if previous_version < STATE_VERSION:
            self.state['last_bootstrap'] = 0
            self.state['qualification_recheck_at'] = time.time() + 2

        self._enabled_last = None
        self._scope_last = None
        self._next_tick = 0
        self._next_show_sync = 0
        self._scan_needed = False
        self._unscanned_shows = 0
        self._last_files_written = 0
        self._scan_in_progress = False
        self._scan_started = 0
        self._clean_needed = False
        self._clean_in_progress = False
        self._clean_started = 0
        self._owned_clean_active = False
        self._owned_scan_active = False
        self._library_updates = []
        self._expected_updates = {}
        self._expected_tvshow_removals = {}
        self._tvshow_show_map = {
            int(tvshow_id): str(show_id)
            for tvshow_id, show_id in (self.state.get('tvshow_map') or {}).items()
            if str(tvshow_id).isdigit() and show_id
        }
        self._next_tvshow_map_refresh = 0
        self._episode_cache = ({}, 0)
        self._plugin_watch_db_path = ''
        self._plugin_watch_db_error = ''
        self._plugin_watch_db_error_at = 0
        self._save()

    def _diag(self, event, **fields):
        diagnostic_event('IVIEW200LIB', event, **fields)

    def _save(self):
        try:
            _atomic_write(
                STATE_FILE,
                json.dumps(
                    self.state,
                    sort_keys=True,
                    indent=2,
                ),
            )
        except Exception as exc:
            log.warning('Unable to save ABC iView library state: {}'.format(exc))

    def _drain_requests(self):
        # Consume the durable disk queue first. The window-property queue is
        # retained only for compatibility with already-running older plugin
        # interpreters during an in-place upgrade.
        requests = pop_library_requests(limit=MAX_REQUESTS)
        legacy_requests = _property_json(REQUESTS_PROPERTY, [])
        if legacy_requests:
            WINDOW.clearProperty(REQUESTS_PROPERTY)
        if isinstance(legacy_requests, list):
            requests.extend(legacy_requests)
        if not isinstance(requests, list):
            return
        for request in requests:
            if not isinstance(request, dict):
                continue
            action = request.get('action')
            if action == 'follow_show':
                if library_scope() == LIBRARY_SCOPE_WATCHED:
                    self.follow_show(
                        request.get('show_id'),
                        request.get('source'),
                        request.get('house_number'),
                        priority=True,
                    )
                else:
                    self._schedule_qualification_recheck(delay=2)
            elif action == 'episode_state':
                playcount = 1 if int(request.get('playcount') or 0) > 0 else 0
                if playcount:
                    if library_scope() == LIBRARY_SCOPE_WATCHED:
                        self.follow_show(
                            request.get('show_id'),
                            request.get('source'),
                            request.get('house_number'),
                            priority=True,
                        )
                    else:
                        self._schedule_qualification_recheck(delay=2)
                else:
                    self._schedule_qualification_recheck(delay=5)

                self.queue_episode_state(
                    request.get('show_id'),
                    request.get('house_number'),
                    playcount,
                    request.get('progress'),
                    request.get('duration'),
                    request.get('source'),
                )
            elif action == 'reconcile_show':
                self._schedule_qualification_recheck(delay=5)
                # A watchlist/watched qualification change may add or remove a
                # TV show. Refresh once after all generated files, cleaning and
                # scanning have settled; never refresh during each scan.
                self._diag(
                    'qualification_recheck_requested',
                    show_id=request.get('show_id'),
                    source=request.get('source'),
                )
            elif action == 'manual_selection_changed':
                show_id = str(request.get('show_id') or '')
                included = bool(request.get('included'))
                # In manual mode, adding one show can be handled immediately
                # rather than waiting for a full qualification pass over every
                # selected show. The normal reconciliation is still queued so
                # removals and any stale entries are cleaned up reliably.
                if (
                    included
                    and show_id
                    and library_scope() == LIBRARY_SCOPE_MANUAL
                ):
                    self.follow_show(
                        show_id,
                        source='manual_selection',
                        priority=True,
                    )
                self._schedule_qualification_recheck(delay=0)
                self._diag(
                    'manual_selection_changed',
                    show_id=show_id,
                    included=included,
                )
            elif action == 'library_scope_changed':
                self.state['reconcile_reason'] = 'scope_changed'
                self.state['last_bootstrap'] = 0
                self._schedule_qualification_recheck(delay=0)
                self._next_tick = 0
                self._diag(
                    'library_scope_change_requested',
                    scope=request.get('scope'),
                    index=request.get('index'),
                )
            elif action == 'library_settings_changed':
                self.state['reconcile_reason'] = 'settings_changed'
                self.state['last_bootstrap'] = 0
                self._schedule_qualification_recheck(delay=0)
                self._next_tick = 0
                self._diag(
                    'library_settings_change_requested',
                    enabled=request.get('enabled'),
                    scope=request.get('scope'),
                )
            elif action == 'library_reconcile_now':
                self.state['reconcile_reason'] = 'manual_reconcile'
                self.state['last_bootstrap'] = 0
                self._schedule_qualification_recheck(delay=0)
                self._next_tick = 0
                self._diag(
                    'library_reconcile_requested',
                    scope=request.get('scope'),
                )

    def _schedule_qualification_recheck(self, delay=5):
        target = time.time() + max(0, float(delay or 0))
        current = float(
            self.state.get('qualification_recheck_at') or 0
        )
        if not current or target < current:
            self.state['qualification_recheck_at'] = target
            self._save()

    def _pending_watched_episodes_by_show(self):
        result = {}
        for state in self.state.get('episode_states', {}).values():
            if int(state.get('playcount') or 0) <= 0:
                continue
            show_id = str(state.get('show_id') or '')
            house_number = str(state.get('house_number') or '')
            if show_id and house_number:
                result.setdefault(show_id, set()).add(house_number)
        return result

    def _local_watched_episodes_by_show(self):
        """Return watched generated ABC episodes grouped by show."""
        result = {}
        try:
            response = _jsonrpc(
                'VideoLibrary.GetEpisodes',
                {
                    'properties': ['file', 'playcount'],
                    'limits': {'start': 0, 'end': 10000},
                },
            )
        except Exception as exc:
            self._diag(
                'local_watched_query_failed',
                error=repr(exc),
            )
            return result

        root_key = _path_key(LIBRARY_ROOT)
        for details in response.get('episodes') or []:
            if int(details.get('playcount') or 0) <= 0:
                continue
            file_path = str(details.get('file') or '')
            if (
                not file_path
                or not _path_key(file_path).startswith(root_key)
            ):
                continue
            item = self._item_from_library_details(details)
            if not item:
                continue
            show_id = str(item.get('show_id') or '')
            house_number = str(item.get('house_number') or '')
            if show_id and house_number:
                result.setdefault(show_id, set()).add(house_number)

        return result

    @staticmethod
    def _merge_watched_episodes(*mappings):
        result = {}
        for mapping in mappings:
            for show_id, house_numbers in (mapping or {}).items():
                show_id = str(show_id or '')
                if not show_id:
                    continue
                target = result.setdefault(show_id, set())
                target.update(
                    str(value)
                    for value in (house_numbers or set())
                    if value
                )
        return result

    def _currently_available_house_numbers(self, api, show_id):
        """Return the playable episode IDs currently exposed for one show."""
        show = api.get_show(show_id)
        if not show:
            return set()
        show['id'] = str(show.get('id') or show_id)
        series_rows = self._load_all_series(api, show)
        records = build_episode_records(show, series_rows)
        return {
            str(record.get('house_number') or '')
            for record in records
            if record.get('house_number')
        }

    def _show_id_from_tvshow_details(self, details):
        """Return the ABC show id for a Kodi TV-show row owned by this add-on."""
        if not isinstance(details, dict):
            return ''

        file_path = str(details.get('file') or '')
        if not file_path or not _path_key(file_path).startswith(_path_key(LIBRARY_ROOT)):
            return ''

        uniqueid = details.get('uniqueid')
        if isinstance(uniqueid, dict):
            show_id = str(uniqueid.get('abciview') or '')
            if show_id:
                return show_id

        translated_path = xbmcvfs.translatePath(file_path)
        folder_path = os.path.normpath(translated_path)
        folder_name = os.path.basename(folder_path)
        match = re.search(r'\[ABC ([^\]]+)\]$', folder_name, re.I)
        if match:
            return str(match.group(1) or '')

        manifest = _read_json(
            os.path.join(folder_path, '.abc_iview.json'),
            {},
        )
        if isinstance(manifest, dict):
            return str(manifest.get('show_id') or '')
        return ''

    def _store_tvshow_map(self):
        stored = {
            str(tvshow_id): str(show_id)
            for tvshow_id, show_id in self._tvshow_show_map.items()
            if show_id
        }
        if stored != (self.state.get('tvshow_map') or {}):
            self.state['tvshow_map'] = stored
            self._save()

    def _refresh_tvshow_map(self, force=False):
        """Cache Kodi tvshowid -> ABC show id before native removals occur."""
        now = time.time()
        if not force and now < self._next_tvshow_map_refresh:
            return
        self._next_tvshow_map_refresh = now + 60

        try:
            response = _jsonrpc(
                'VideoLibrary.GetTVShows',
                {
                    'properties': ['file', 'uniqueid', 'title'],
                    'limits': {'start': 0, 'end': 10000},
                },
            )
        except Exception as exc:
            self._diag('tvshow_map_refresh_failed', error=repr(exc))
            return

        mapping = {}
        for details in response.get('tvshows') or []:
            show_id = self._show_id_from_tvshow_details(details)
            if not show_id:
                continue
            try:
                tvshow_id = int(details.get('tvshowid'))
            except (TypeError, ValueError):
                continue
            mapping[tvshow_id] = show_id

        self._tvshow_show_map = mapping
        self._store_tvshow_map()
        self._diag('tvshow_map_refreshed', count=len(mapping))

    def _mark_expected_tvshow_removal(self, tvshow_id, timeout=60):
        self._expected_tvshow_removals[int(tvshow_id)] = time.time() + timeout

    def _consume_expected_tvshow_removal(self, tvshow_id):
        now = time.time()
        for key in list(self._expected_tvshow_removals):
            if self._expected_tvshow_removals[key] <= now:
                del self._expected_tvshow_removals[key]
        expires = self._expected_tvshow_removals.pop(int(tvshow_id), 0)
        return bool(expires and expires > now)

    def _notification_item(self, data):
        """Normalise Kodi Monitor and JSON-RPC notification payload shapes."""
        try:
            payload = json.loads(data) if isinstance(data, str) and data else data
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return {}

        nested = payload.get('data')
        if isinstance(nested, dict):
            payload = nested

        item = payload.get('item')
        if isinstance(item, dict):
            return item
        if payload.get('id') is not None and payload.get('type'):
            return payload
        return {}

    def _handle_tvshow_removed(self, tvshow_id):
        """Mirror Kodi's native Remove from library into the manual list."""
        try:
            tvshow_id = int(tvshow_id)
        except (TypeError, ValueError):
            return

        show_id = str(self._tvshow_show_map.pop(tvshow_id, '') or '')
        self._store_tvshow_map()

        if self._consume_expected_tvshow_removal(tvshow_id):
            self._diag(
                'native_tvshow_remove_expected',
                tvshowid=tvshow_id,
                show_id=show_id,
            )
            return

        if not show_id:
            self._diag(
                'native_tvshow_remove_unresolved',
                tvshowid=tvshow_id,
            )
            return

        if not is_manual_show(show_id):
            self._diag(
                'native_tvshow_remove_not_manual',
                tvshowid=tvshow_id,
                show_id=show_id,
            )
            return

        changed = set_manual_show(show_id, False)
        if not changed:
            return

        # The native Kodi action has already removed the database row. Queue a
        # full manual reconciliation so generated files are deleted as well
        # and a later scan cannot silently add the show back.
        self.state['reconcile_reason'] = 'native_manual_remove'
        self.state['last_bootstrap'] = 0
        self._schedule_qualification_recheck(delay=0)
        self._next_tick = 0
        self._diag(
            'native_tvshow_remove_manual_cleared',
            tvshowid=tvshow_id,
            show_id=show_id,
        )
        xbmcgui.Dialog().notification(
            'ABC iView+',
            'Removed from manual Kodi library selection',
            xbmcgui.NOTIFICATION_INFO,
            4000,
        )

    def _remove_unqualified_files(self, qualified_show_ids):
        qualified_show_ids = {
            str(show_id)
            for show_id in qualified_show_ids
            if show_id
        }
        removed = []

        if not os.path.isdir(LIBRARY_ROOT):
            return removed

        for name in os.listdir(LIBRARY_ROOT):
            folder = os.path.join(LIBRARY_ROOT, name)
            if not os.path.isdir(folder):
                continue

            manifest_path = os.path.join(
                folder,
                '.abc_iview.json',
            )
            manifest = _read_json(manifest_path, {})
            show_id = str(manifest.get('show_id') or '')
            if not show_id:
                # Early generated folders did not always contain a manifest.
                # Recover the ABC show id from the folder suffix so a scope
                # downstep can still remove those legacy folders.
                match = re.search(r'\[ABC ([^\]]+)\]$', name, re.I)
                if match:
                    show_id = str(match.group(1) or '')
            if not show_id or show_id in qualified_show_ids:
                continue

            try:
                shutil.rmtree(folder)
                removed.append(show_id)
                self._diag(
                    'unqualified_show_files_removed',
                    show_id=show_id,
                    folder=folder,
                )
            except Exception as exc:
                log.warning(
                    'Unable to remove unqualified ABC iView show '
                    '{}: {}'.format(show_id, exc)
                )
                self._diag(
                    'unqualified_show_remove_failed',
                    show_id=show_id,
                    folder=folder,
                    error=repr(exc),
                )

        return removed

    def _remove_unqualified_library_rows(self, qualified_show_ids):
        """Remove stale ABC TV-show rows directly from Kodi's database.

        A directory-scoped VideoLibrary.Clean can remove missing episodes yet
        leave an empty TV-show row behind (displayed as 0 / 0).  Because this
        add-on owns a dedicated library root, identify TV shows whose database
        path is below that root and remove any that are not in the active
        qualification set.
        """
        qualified = {
            str(show_id)
            for show_id in qualified_show_ids
            if show_id
        }
        removed = []

        try:
            response = _jsonrpc(
                'VideoLibrary.GetTVShows',
                {
                    'properties': ['file', 'uniqueid', 'title'],
                    'limits': {'start': 0, 'end': 10000},
                },
            )
        except Exception as exc:
            log.warning(
                'Unable to inspect Kodi TV shows for ABC iView cleanup: {}'.format(
                    exc
                )
            )
            self._diag(
                'unqualified_library_rows_query_failed',
                error=repr(exc),
            )
            return removed

        for details in response.get('tvshows') or []:
            file_path = str(details.get('file') or '')
            show_id = self._show_id_from_tvshow_details(details)
            if not show_id:
                continue

            tvshow_id = details.get('tvshowid')
            try:
                tvshow_id = int(tvshow_id)
            except (TypeError, ValueError):
                self._diag(
                    'unqualified_library_row_missing_id',
                    show_id=show_id,
                    file=file_path,
                    title=details.get('title'),
                )
                continue

            self._tvshow_show_map[tvshow_id] = show_id
            if show_id in qualified:
                continue

            try:
                self._mark_expected_tvshow_removal(tvshow_id)
                _jsonrpc(
                    'VideoLibrary.RemoveTVShow',
                    {'tvshowid': tvshow_id},
                )
                token = show_id or 'tvshowid:{}'.format(tvshow_id)
                removed.append(token)
                self._diag(
                    'unqualified_library_row_removed',
                    show_id=show_id,
                    tvshowid=tvshow_id,
                    file=file_path,
                    title=details.get('title'),
                )
            except Exception as exc:
                self._expected_tvshow_removals.pop(tvshow_id, None)
                log.warning(
                    'Unable to remove stale ABC iView TV show {} (Kodi id {}): {}'.format(
                        show_id or details.get('title') or file_path,
                        tvshow_id,
                        exc,
                    )
                )
                self._diag(
                    'unqualified_library_row_remove_failed',
                    show_id=show_id,
                    tvshowid=tvshow_id,
                    file=file_path,
                    error=repr(exc),
                )

        self._store_tvshow_map()

        if removed:
            log.info(
                'ABC iView library directly removed {} stale Kodi TV-show rows'.format(
                    len(removed)
                )
            )
            self._mark_library_changed('stale_tvshow_rows_removed')

        return removed

    def _reconcile_followed(self, qualified_show_ids):
        qualified = {
            str(show_id)
            for show_id in qualified_show_ids
            if show_id
        }
        followed = self.state.get('followed', {})
        previous = set(str(show_id) for show_id in followed)
        removed_ids = previous - qualified

        if removed_ids:
            for show_id in removed_ids:
                followed.pop(show_id, None)

            self.state['queue'] = [
                row for row in self.state.get('queue', [])
                if str(row.get('show_id') or '') not in removed_ids
            ]

            self.state['episode_states'] = {
                house_number: state
                for house_number, state
                in self.state.get('episode_states', {}).items()
                if str(state.get('show_id') or '') not in removed_ids
            }

        removed_files = self._remove_unqualified_files(qualified)
        removed_library = self._remove_unqualified_library_rows(qualified)
        # A clean is required whenever followed entries, generated files or
        # direct Kodi rows are removed. The direct RemoveTVShow pass handles
        # the empty 0 / 0 rows that a scoped clean can leave behind.
        if removed_ids or removed_files or removed_library:
            self._clean_needed = True
            self._scan_needed = True
            self._episode_cache = ({}, 0)
            self._mark_library_changed('library_membership_removed')

        self.state['followed'] = followed
        self._save()
        self._diag(
            'qualification_reconciled',
            qualified_count=len(qualified),
            previously_followed_count=len(previous),
            removed_followed=sorted(removed_ids),
            removed_file_count=len(removed_files),
            removed_library_count=len(removed_library),
        )
        return {
            'removed_followed': sorted(removed_ids),
            'removed_files': sorted(removed_files),
            'removed_library': sorted(removed_library),
        }

    def _mark_library_changed(self, reason=''):
        """Record a real mutation for diagnostics only.

        Kodi updates library views from its clean/scan notifications; no
        additional GUI refresh or retry latch is required.
        """
        self._diag(
            'library_transaction_changed',
            reason=str(reason or 'library_change'),
        )

    def follow_show(
        self,
        show_id,
        source='',
        house_number='',
        priority=False,
    ):
        if not show_id:
            return
        show_id = str(show_id)
        now = time.time()
        followed = self.state['followed']
        entry = followed.get(show_id) or {}
        entry.update({
            'show_id': show_id,
            'last_seen': now,
            'source': str(source or entry.get('source') or ''),
            'house_number': str(house_number or entry.get('house_number') or ''),
        })
        followed[show_id] = entry

        queue = [
            row for row in self.state['queue']
            if str(row.get('show_id')) != show_id
        ]
        queued = {
            'show_id': show_id,
            'created': now,
            'not_before': 0,
            'attempts': 0,
            'source': str(source or ''),
        }
        if priority:
            queue.insert(0, queued)
        else:
            queue.append(queued)
        self.state['queue'] = queue[-500:]
        self._save()
        self._diag(
            'show_followed',
            show_id=show_id,
            source=source,
            priority=priority,
        )

    def queue_episode_state(
        self,
        show_id,
        house_number,
        playcount,
        progress=0,
        duration=0,
        source='',
    ):
        if not show_id or not house_number:
            return
        target = 1 if int(playcount or 0) > 0 else 0
        self.state['episode_states'][str(house_number)] = {
            'show_id': str(show_id),
            'house_number': str(house_number),
            'playcount': target,
            'progress': 0 if target else max(0, int(progress or 0)),
            'duration': max(0, int(duration or 0)),
            'source': str(source or ''),
            'created': time.time(),
            'not_before': 0,
            'missing_attempts': 0,
        }
        self._save()
        self._diag(
            'episode_state_queued',
            show_id=show_id,
            house_number=house_number,
            playcount=target,
            progress=progress,
            source=source,
        )

    def on_playback_started(self, show_id, house_number=''):
        # Merely starting an episode does not qualify a new show. A show enters
        # the library only when at least one episode has a real playcount.
        return

    def on_playback_progress(
        self,
        show_id,
        house_number,
        position,
        duration,
    ):
        if (
            not library_enabled()
            or not show_id
            or not house_number
            or str(show_id) not in self.state.get('followed', {})
        ):
            return

        # Resume progress is maintained only for a show already qualified by a
        # watched episode. Partial viewing alone must not populate the library.
        self.queue_episode_state(
            show_id,
            house_number,
            0,
            progress=position,
            duration=duration,
            source='playback_progress',
        )

    def on_playback_finished(
        self,
        show_id,
        house_number,
        watched,
        position,
        duration,
    ):
        if not library_enabled() or not show_id or not house_number:
            return

        if watched:
            if library_scope() == LIBRARY_SCOPE_WATCHED:
                self.follow_show(
                    show_id,
                    source='playback_finished_watched',
                    house_number=house_number,
                    priority=True,
                )
            else:
                self._schedule_qualification_recheck(delay=2)
            self.queue_episode_state(
                show_id,
                house_number,
                1,
                progress=0,
                duration=duration,
                source='playback_finished_watched',
            )
        elif str(show_id) in self.state.get('followed', {}):
            self.queue_episode_state(
                show_id,
                house_number,
                0,
                progress=position,
                duration=duration,
                source='playback_finished_partial',
            )

    def handle_notification(self, method, data):
        if method == 'VideoLibrary.OnRemove':
            item = self._notification_item(data)
            if item.get('type') == 'tvshow' and item.get('id') is not None:
                self._handle_tvshow_removed(item.get('id'))
            return
        if method == 'VideoLibrary.OnCleanStarted':
            self._clean_in_progress = True
            self._clean_started = time.time()
            self._diag('clean_started_notification')
            return
        if method == 'VideoLibrary.OnCleanFinished':
            # Kodi may emit CleanFinished automatically after any scan when
            # <cleanonupdate>true</cleanonupdate> is set. Track ownership
            # explicitly instead of inferring it from a pending flag, otherwise
            # an unrelated automatic clean can be mistaken for ours.
            owned_clean = self._owned_clean_active
            self._owned_clean_active = False
            self._clean_in_progress = False
            self._clean_started = 0
            self._clean_needed = False
            self._episode_cache = ({}, 0)
            if owned_clean:
                self._scan_needed = True
            self._next_tvshow_map_refresh = 0
            self._diag(
                'clean_finished_notification',
                owned=owned_clean,
                scan_needed=self._scan_needed,
            )
            return
        if method == 'VideoLibrary.OnScanStarted':
            self._scan_in_progress = True
            self._scan_started = time.time()
            self._diag(
                'scan_started_notification',
                owned=self._owned_scan_active,
            )
            return
        if method == 'VideoLibrary.OnScanFinished':
            self._scan_in_progress = False
            self._scan_started = 0
            self._episode_cache = ({}, 0)
            self._next_tvshow_map_refresh = 0
            owned = self._owned_scan_active
            self._owned_scan_active = False

            # A scan may have created rows that were previously missing. Make
            # delayed episode-state writes eligible for one immediate attempt.
            for state in self.state.get('episode_states', {}).values():
                state['not_before'] = 0

            if not owned and library_enabled():
                # Capture Kodi's built-in Update library command. The service
                # schedules one delayed pass; the qualification bootstrap then
                # refreshes every currently qualified ABC show.
                self.state['last_bootstrap'] = 0
                self._schedule_qualification_recheck(delay=0)

            self._save()
            self._diag(
                'scan_finished_notification',
                owned=owned,
                external_reconcile=not owned,
            )
            # Apply queued states once after an add-on-owned scan. External
            # scans are followed by a full reconciliation in the service.
            if owned and self.state.get('episode_states'):
                self._apply_episode_states(limit=None)
            return
        if method != 'VideoLibrary.OnUpdate':
            return
        payload = parse_notification_payload(data)
        episode_id = positive_episode_id(payload)
        if not episode_id:
            # Plugin ListItems may report no id, zero or a negative sentinel.
            # They are handled by ABCMonitor while browsing the add-on and must
            # never be queued as Kodi database episodes.
            return
        playcount = notification_playcount(payload)
        self._library_updates.append({
            'episodeid': episode_id,
            'playcount': playcount,
            'created': time.time(),
        })
        self._diag(
            'library_onupdate_queued',
            episodeid=episode_id,
            playcount=playcount,
        )

    def _consume_expected_update(self, episodeid, playcount):
        now = time.time()
        for key in list(self._expected_updates):
            if self._expected_updates[key].get('expires', 0) <= now:
                del self._expected_updates[key]
        expected = self._expected_updates.get(int(episodeid))
        if not expected:
            return False
        if playcount is None or int(expected['playcount']) == (1 if playcount > 0 else 0):
            del self._expected_updates[int(episodeid)]
            self._diag(
                'library_onupdate_expected',
                episodeid=episodeid,
                playcount=playcount,
            )
            return True
        return False

    def pop_watched_updates(self):
        updates = self._library_updates
        self._library_updates = []
        resolved = []
        for update in updates:
            if self._consume_expected_update(
                update['episodeid'],
                update.get('playcount'),
            ):
                continue
            item = self._resolve_library_episode(update['episodeid'])
            if not item:
                # Kodi can emit OnUpdate just before the changed episode row is
                # fully readable. Keep only young notifications for a bounded
                # event-driven retry; do not restore periodic polling.
                if time.time() - float(update.get('created') or 0) < 15:
                    self._library_updates.append(update)
                continue
            playcount = update.get('playcount')
            if playcount is None:
                playcount = item.pop('_library_playcount', 0)
            resolved.append((item, int(playcount or 0)))
        return resolved

    def _resolve_library_episode(self, episodeid):
        try:
            result = _jsonrpc(
                'VideoLibrary.GetEpisodeDetails',
                {
                    'episodeid': int(episodeid),
                    'properties': ['file', 'uniqueid', 'playcount', 'runtime'],
                },
            )
            details = result.get('episodedetails') or {}
        except Exception as exc:
            self._diag(
                'library_episode_resolve_failed',
                episodeid=episodeid,
                error=repr(exc),
            )
            return None
        item = self._item_from_library_details(details)
        if item:
            item['_library_playcount'] = int(details.get('playcount') or 0)
        return item

    def _item_from_library_details(self, details):
        file_path = str(details.get('file') or '')
        if not file_path or not _path_key(file_path).startswith(_path_key(LIBRARY_ROOT)):
            return None
        strm_path = xbmcvfs.translatePath(file_path)
        try:
            with open(strm_path, 'r', encoding='utf-8') as handle:
                plugin_url = handle.readline().strip()
        except Exception:
            return None
        try:
            query = parse_qs(urlparse(plugin_url).query)
            first = lambda name, default='': (query.get(name) or [default])[0]
            show_id = first('show_id')
            house_number = first('house_number')
            duration = int(float(first('duration', '0') or 0))
        except Exception:
            return None
        if not show_id or not house_number:
            return None
        return {
            'key': '{}:{}'.format(show_id, house_number),
            'show_id': show_id,
            'house_number': house_number,
            'duration': duration,
        }

    def needs_fast_tick(self):
        """Return True only while a real library transaction needs work.

        The background service sleeps for five seconds when idle. Active
        clean/scan/import/state work keeps a one-second cadence until that
        transaction settles. Merely being on a Kodi screen never counts as
        active work and never causes a refresh.
        """
        try:
            if not get_library_enabled():
                return False
        except Exception:
            return False

        now = time.time()
        if (
            self._clean_in_progress
            or self._scan_in_progress
            or self._clean_needed
            or self._scan_needed
            or bool(self.state.get('queue'))
            or bool(self.state.get('episode_states'))
            or not self.state.get('source_ready')
        ):
            return True

        qualification_recheck_at = float(
            self.state.get('qualification_recheck_at') or 0
        )
        if qualification_recheck_at and qualification_recheck_at <= now + 5:
            return True

        try:
            if any(
                name.endswith('.json')
                for name in os.listdir(
                    os.path.join(PROFILE, 'library_requests')
                )
            ):
                return True
        except (FileNotFoundError, OSError):
            pass

        return False

    def tick(self, playing=False):
        now = time.time()
        if now < self._next_tick:
            return
        self._next_tick = now + 1.0
        self._drain_requests()

        config = get_config()
        enabled = bool(config.get('library_integration'))
        try:
            scope_index = int(config.get('library_scope_mode') or 0)
        except (TypeError, ValueError):
            scope_index = 0
        scope = SCOPE_BY_INDEX.get(scope_index, LIBRARY_SCOPE_MANUAL)
        try:
            config_revision = int(config.get('revision') or 0)
        except (TypeError, ValueError):
            config_revision = 0

        stored_scope = str(self.state.get('library_scope') or '')
        stored_enabled = self.state.get('library_enabled')
        try:
            stored_revision = int(
                self.state.get('library_config_revision') or 0
            )
        except (TypeError, ValueError):
            stored_revision = -1

        settings_changed = (
            stored_scope != scope
            or stored_enabled is None
            or bool(stored_enabled) != enabled
        )
        if config_revision != stored_revision or settings_changed:
            self.state['library_config_revision'] = config_revision
            self.state['library_scope'] = scope
            self.state['library_enabled'] = enabled
            if settings_changed:
                self.state['reconcile_reason'] = 'durable_config_changed'
                self.state['last_bootstrap'] = 0
                self._schedule_qualification_recheck(delay=0)
                log.info(
                    'ABC iView library configuration changed: '
                    'enabled={}, scope={}, revision={}'.format(
                        enabled, scope, config_revision
                    )
                )
            else:
                self._save()

        if enabled != self._enabled_last:
            self._enabled_last = enabled
            self._diag('setting_changed', enabled=enabled)
            if enabled:
                self.state['source_ready'] = False
                self.state['setup_error_notified'] = False
                self.state['last_bootstrap'] = 0
                self._schedule_qualification_recheck(delay=0)
                self._save()

        if scope != self._scope_last:
            previous_scope = self._scope_last
            self._scope_last = scope
            self._diag(
                'library_scope_changed',
                previous=previous_scope,
                current=scope,
                persisted=stored_scope,
                revision=config_revision,
            )

        if not enabled:
            return

        if not self.state.get('source_ready'):
            try:
                self._ensure_library_source()
                self.state['source_ready'] = True
                self.state['setup_error_notified'] = False
                self._save()
                xbmcgui.Dialog().notification(
                    'ABC iView+',
                    'Library Integration enabled',
                    xbmcgui.NOTIFICATION_INFO,
                    4000,
                )
            except Exception as exc:
                self._diag('source_setup_failed', error=repr(exc))
                log.exception('ABC iView library source setup failed: {}'.format(exc))
                if not self.state.get('setup_error_notified'):
                    self.state['setup_error_notified'] = True
                    self._save()
                    xbmcgui.Dialog().notification(
                        'ABC iView+',
                        'Library setup failed. Check kodi.log',
                        xbmcgui.NOTIFICATION_ERROR,
                        7000,
                    )
                return

        if (
            now >= self._next_tvshow_map_refresh
            and not self._scan_in_progress
            and not self._clean_in_progress
        ):
            self._refresh_tvshow_map()

        qualification_recheck_at = float(
            self.state.get('qualification_recheck_at') or 0
        )
        if qualification_recheck_at:
            if now >= qualification_recheck_at:
                self._bootstrap_history()
        elif (
            now - float(self.state.get('last_bootstrap') or 0)
            >= BOOTSTRAP_INTERVAL
        ):
            self._bootstrap_history()

        if now - float(self.state.get('last_refresh') or 0) >= REFRESH_INTERVAL:
            self._queue_refresh_all()

        if self._scan_in_progress and now - self._scan_started > 180:
            owned = self._owned_scan_active
            self._scan_in_progress = False
            self._scan_started = 0
            self._owned_scan_active = False
            if owned:
                self._scan_needed = True
            self._diag('scan_timeout_released', owned=owned)

        if self._clean_in_progress and now - self._clean_started > 180:
            owned = self._owned_clean_active
            self._owned_clean_active = False
            self._clean_in_progress = False
            self._clean_started = 0
            self._clean_needed = False
            if owned:
                self._scan_needed = True
            self._diag('clean_timeout_released', owned=owned)

        if playing:
            return

        if (
            self._clean_needed
            and not self._clean_in_progress
            and not self._scan_in_progress
        ):
            self._start_clean()
            return

        queue_due = self._next_due_show(now)

        if (
            not self._scan_in_progress
            and not self._clean_in_progress
            and not self._scan_needed
            and not queue_due
            and self._episode_state_due(now)
        ):
            self._apply_episode_states(limit=None)

        should_scan = self._scan_needed and not queue_due
        if (
            should_scan
            and not self._scan_in_progress
            and not self._clean_in_progress
        ):
            self._start_scan()
            return

        if (
            queue_due
            and not self._scan_in_progress
            and not self._clean_in_progress
            and now >= self._next_show_sync
        ):
            self._sync_next_show(queue_due)
            self._next_show_sync = now + 1.0


    def next_work_at(self, playing=False):
        """Return the next real library-work deadline, or ``0`` when idle.

        The service uses this instead of calling :meth:`tick` every five
        seconds. Kodi notifications and durable-request wake tokens schedule
        immediate work; this method schedules only an active transaction,
        retry or the normal six-hour/twenty-four-hour maintenance deadlines.
        """
        now = time.time()

        try:
            config = get_config()
            enabled = bool(config.get('library_integration'))
            revision = int(config.get('revision') or 0)
        except Exception:
            enabled = bool(self.state.get('library_enabled'))
            revision = int(self.state.get('library_config_revision') or 0)

        try:
            stored_revision = int(
                self.state.get('library_config_revision') or 0
            )
        except (TypeError, ValueError):
            stored_revision = -1

        if revision != stored_revision:
            return max(now, float(self._next_tick or 0))
        if not enabled:
            return 0.0

        # Library mutation is deliberately deferred while video is playing.
        # The playback-finished callback schedules the next pass.
        if playing:
            return 0.0

        # During a Kodi scan/clean, notifications are the normal wake source.
        # Keep only a timeout deadline as a safety net; do not poll it.
        if self._clean_in_progress:
            started = float(self._clean_started or now)
            return max(now, started + 180)
        if self._scan_in_progress:
            started = float(self._scan_started or now)
            return max(now, started + 180)

        if (
            not self.state.get('source_ready')
            or self._clean_needed
            or self._scan_needed
        ):
            return max(now, float(self._next_tick or 0))

        deadlines = []

        qualification_at = float(
            self.state.get('qualification_recheck_at') or 0
        )
        if qualification_at:
            deadlines.append(max(
                qualification_at,
                float(self._next_tick or 0),
            ))

        queue = self.state.get('queue') or []
        if queue:
            show_due = min(
                float(row.get('not_before') or 0)
                for row in queue
            )
            deadlines.append(max(
                now,
                show_due,
                float(self._next_show_sync or 0),
                float(self._next_tick or 0),
            ))

        episode_state_due = self._next_episode_state_due_at()
        if episode_state_due:
            deadlines.append(max(
                now,
                episode_state_due,
                float(self._next_tick or 0),
            ))

        last_refresh = float(self.state.get('last_refresh') or 0)
        deadlines.append(
            max(now, float(self._next_tick or 0))
            if not last_refresh else max(
                last_refresh + REFRESH_INTERVAL,
                float(self._next_tick or 0),
            )
        )

        last_bootstrap = float(self.state.get('last_bootstrap') or 0)
        deadlines.append(
            max(now, float(self._next_tick or 0))
            if not last_bootstrap else max(
                last_bootstrap + BOOTSTRAP_INTERVAL,
                float(self._next_tick or 0),
            )
        )

        if not deadlines:
            return 0.0
        return min(deadlines)


    def _next_episode_state_due_at(self):
        states = self.state.get('episode_states') or {}
        if not states:
            return 0.0
        due = min(
            float(state.get('not_before') or 0)
            for state in states.values()
        )
        # A zero value means immediately due, not "no deadline".
        return due if due > 0 else time.time()

    def _episode_state_due(self, now=None):
        now = time.time() if now is None else float(now)
        due = self._next_episode_state_due_at()
        return bool(due <= now) if self.state.get('episode_states') else False


    def _next_due_show(self, now):
        for row in self.state['queue']:
            if float(row.get('not_before') or 0) <= now:
                return row
        return None

    def _bootstrap_history(self):
        reconcile_reason = str(self.state.get('reconcile_reason') or '')
        self.state['last_bootstrap'] = time.time()
        self.state['qualification_recheck_at'] = 0
        self._save()

        try:
            api = API()
            api.new_session()
            scope = library_scope()
            if not api.logged_in and scope != LIBRARY_SCOPE_MANUAL:
                self._diag(
                    'history_bootstrap_skipped_not_logged_in',
                    scope=scope,
                )
                self.state['qualification_recheck_at'] = time.time() + 60
                self._save()
                if reconcile_reason:
                    xbmcgui.Dialog().notification(
                        'ABC iView+',
                        'ABC Account login is required to apply this library mode',
                        xbmcgui.NOTIFICATION_WARNING,
                        6000,
                    )
                return

            # Manual mode works without an ABC Account. When an account is
            # linked, history is still loaded so watched/resume state can be
            # restored for manually selected shows.
            history = api.get_history(force=True) if api.logged_in else {}

            # History is loaded so watched and resume states can be restored
            # for whichever shows qualify under the selected mode.
            history_watched_episodes = {}
            for house_number, state in history.items():
                if not state.get('done'):
                    continue
                show_id = str(state.get('show_id') or '')
                if show_id and house_number:
                    history_watched_episodes.setdefault(show_id, set()).add(
                        str(house_number)
                    )

            local_watched_episodes = self._local_watched_episodes_by_show()
            pending_watched_episodes = self._pending_watched_episodes_by_show()
            watched_episodes = self._merge_watched_episodes(
                history_watched_episodes,
                local_watched_episodes,
                pending_watched_episodes,
            )

            previously_followed = set(
                str(show_id)
                for show_id in self.state.get('followed', {})
            )
            qualified_show_ids = set()
            unavailable_candidate_show_ids = set()
            availability_lookup_failed = set()

            if scope == LIBRARY_SCOPE_MANUAL:
                candidate_show_ids = manual_show_ids()
                qualification_source = 'manual_selection'
            elif scope == LIBRARY_SCOPE_WATCHLIST:
                candidate_show_ids = set(
                    str(show_id)
                    for show_id in api.get_watchlist_ids(force=True)
                    if show_id
                )
                qualification_source = 'watchlist_bootstrap'
            else:
                candidate_show_ids = set(watched_episodes)
                qualification_source = 'watched_history_bootstrap'

            # A candidate must still have at least one currently playable
            # episode. This prevents expired watchlist/history entries from
            # leaving empty shows in Kodi.
            for show_id in sorted(candidate_show_ids):
                try:
                    available = self._currently_available_house_numbers(
                        api,
                        show_id,
                    )
                except Exception as exc:
                    availability_lookup_failed.add(show_id)
                    # Do not destructively remove an existing show because of a
                    # temporary catalogue/network failure. It will be checked
                    # again on the next qualification pass.
                    if show_id in previously_followed:
                        qualified_show_ids.add(show_id)
                    log.warning(
                        'ABC iView library availability check failed for {}: {}'.format(
                            show_id,
                            exc,
                        )
                    )
                    self._diag(
                        'qualification_availability_failed',
                        show_id=show_id,
                        scope=scope,
                        error=repr(exc),
                    )
                    continue

                if scope in (
                    LIBRARY_SCOPE_MANUAL,
                    LIBRARY_SCOPE_WATCHLIST,
                ):
                    qualifies = bool(available)
                else:
                    qualifies = bool(
                        available.intersection(watched_episodes[show_id])
                    )

                if qualifies:
                    qualified_show_ids.add(show_id)
                else:
                    unavailable_candidate_show_ids.add(show_id)

            reconcile_result = self._reconcile_followed(qualified_show_ids)

            for show_id in sorted(qualified_show_ids):
                self.follow_show(
                    show_id,
                    source=qualification_source,
                    priority=False,
                )

            # Queue all known states for qualified shows. This restores watched
            # playcounts and genuine resumes after files are scanned. In
            # watchlist mode, viewing history never qualifies an unlisted show.
            for house_number, state in history.items():
                show_id = str(state.get('show_id') or '')
                if show_id not in qualified_show_ids:
                    continue
                self.queue_episode_state(
                    show_id,
                    house_number,
                    1 if state.get('done') else 0,
                    progress=state.get('progress') or 0,
                    source='history_bootstrap_state',
                )

            log.info(
                'ABC iView library qualification mode {} found {} current shows '
                'from {} candidates and {} history episodes'.format(
                    scope,
                    len(qualified_show_ids),
                    len(candidate_show_ids),
                    len(history),
                )
            )
            self._diag(
                'history_bootstrap_complete',
                scope=scope,
                history_count=len(history),
                history_watched_show_count=len(history_watched_episodes),
                local_watched_show_count=len(local_watched_episodes),
                pending_watched_show_count=len(pending_watched_episodes),
                candidate_show_count=len(candidate_show_ids),
                qualified_show_count=len(qualified_show_ids),
                qualified_show_ids=sorted(qualified_show_ids),
                unavailable_candidate_show_ids=sorted(
                    unavailable_candidate_show_ids
                ),
                availability_lookup_failed=sorted(
                    availability_lookup_failed
                ),
            )
            if reconcile_reason:
                removed_count = len(
                    set(reconcile_result.get('removed_followed') or [])
                    | set(reconcile_result.get('removed_files') or [])
                    | set(reconcile_result.get('removed_library') or [])
                )
                self.state['reconcile_reason'] = ''
                self._save()
                xbmcgui.Dialog().notification(
                    'ABC iView+',
                    'Library mode applied: {} shows; {} removed. '
                    'Clean/rescan queued.'.format(
                        len(qualified_show_ids),
                        removed_count,
                    ),
                    xbmcgui.NOTIFICATION_INFO,
                    7000,
                )
        except Exception as exc:
            # Do not silently postpone a failed mode change for 24 hours.
            # Keep the reason and retry shortly so transient ABC/Kodi failures
            # do not make the selector appear inert.
            self.state['qualification_recheck_at'] = time.time() + 60
            self._save()
            log.warning(
                'ABC iView library qualification failed: {}'.format(exc)
            )
            self._diag(
                'history_bootstrap_failed',
                scope=library_scope(),
                error=repr(exc),
            )
            if reconcile_reason:
                xbmcgui.Dialog().notification(
                    'ABC iView+',
                    'Library reconciliation failed; retrying shortly. '
                    'Check kodi.log.',
                    xbmcgui.NOTIFICATION_ERROR,
                    7000,
                )

    def _queue_refresh_all(self):
        now = time.time()
        self.state['last_refresh'] = now
        existing = {str(row.get('show_id')) for row in self.state['queue']}
        for show_id in sorted(self.state['followed']):
            if show_id in existing:
                continue
            self.state['queue'].append({
                'show_id': show_id,
                'created': now,
                'not_before': 0,
                'attempts': 0,
                'source': 'periodic_refresh',
            })
        self._save()
        self._diag(
            'refresh_all_queued',
            followed_count=len(self.state['followed']),
            queue_count=len(self.state['queue']),
        )

    def _sync_next_show(self, queued):
        show_id = str(queued.get('show_id') or '')
        self.state['queue'] = [
            row for row in self.state['queue']
            if row is not queued
        ]
        self._save()
        try:
            count, new_count, changed_count = self._sync_show(show_id)
            if changed_count:
                self._scan_needed = True
                self._unscanned_shows += 1
                self._last_files_written = time.time()
                self._mark_library_changed('library_files_changed')
            followed = self.state['followed'].get(show_id) or {}
            followed['last_sync'] = time.time()
            followed['episode_count'] = count
            followed['last_error'] = ''
            self.state['followed'][show_id] = followed
            self._save()
            log.info(
                'ABC iView library prepared {} episodes for show {} '
                '({} new, {} changed files)'.format(
                    count, show_id, new_count, changed_count
                )
            )
            self._diag(
                'show_sync_complete',
                show_id=show_id,
                episode_count=count,
                new_count=new_count,
                changed_file_count=changed_count,
            )
        except Exception as exc:
            attempts = int(queued.get('attempts') or 0) + 1
            followed = self.state['followed'].get(show_id) or {}
            followed['last_error'] = str(exc)
            followed['last_error_at'] = time.time()
            self.state['followed'][show_id] = followed
            if attempts < QUEUE_RETRY_LIMIT:
                queued['attempts'] = attempts
                queued['not_before'] = time.time() + min(900, 30 * (2 ** attempts))
                self.state['queue'].append(queued)
            self._save()
            log.warning(
                'ABC iView library show sync failed for {}: {}'.format(
                    show_id, exc
                )
            )
            self._diag(
                'show_sync_failed',
                show_id=show_id,
                attempts=attempts,
                error=repr(exc),
            )

    def _sync_show(self, show_id):
        api = API()
        api.new_session()
        show = api.get_show(show_id)
        if not show:
            raise RuntimeError('ABC show {} was not found'.format(show_id))
        show['id'] = str(show.get('id') or show_id)
        series_rows = self._load_all_series(api, show)
        records = build_episode_records(show, series_rows)
        if not records:
            raise RuntimeError('ABC show {} has no playable episodes'.format(show_id))
        for record in records:
            record['show_id'] = str(show_id)

        show_folder = self._show_folder(show, show_id)
        os.makedirs(show_folder, exist_ok=True)
        manifest_path = os.path.join(show_folder, '.abc_iview.json')
        old_manifest = _read_json(manifest_path, {})
        old_episodes = old_manifest.get('episodes', {}) if isinstance(old_manifest, dict) else {}
        new_count = 0
        changed_count = 0

        if _atomic_write(
            os.path.join(show_folder, 'tvshow.nfo'),
            tvshow_nfo(show, show_id, records),
            binary=True,
        ):
            changed_count += 1

        manifest_episodes = dict(old_episodes)
        new_house_numbers = []
        for record in records:
            season_folder = os.path.join(
                show_folder,
                'Season {:02d}'.format(int(record['season'])),
            )
            os.makedirs(season_folder, exist_ok=True)
            base = episode_basename(record)
            strm_path = os.path.join(season_folder, base + '.strm')
            nfo_path = os.path.join(season_folder, base + '.nfo')
            if record['house_number'] not in old_episodes:
                new_count += 1
                new_house_numbers.append(record['house_number'])
            if _atomic_write(strm_path, plugin_play_url(record) + '\n'):
                changed_count += 1
            if _atomic_write(nfo_path, episode_nfo(record), binary=True):
                changed_count += 1
            manifest_episodes[record['house_number']] = {
                'season': record['season'],
                'episode': record['episode'],
                'title': record['title'],
                'duration': record['duration'],
                'strm': strm_path,
                'last_available': time.time(),
            }

        manifest = {
            'show_id': str(show_id),
            'title': show_title(show),
            'updated': time.time(),
            # Previously imported episodes are deliberately retained when they
            # expire from iView. Their watched state keeps the show in Kodi's
            # In Progress row when a later season appears.
            'episodes': manifest_episodes,
            'currently_available': [row['house_number'] for row in records],
        }
        _atomic_write(
            manifest_path,
            json.dumps(manifest, sort_keys=True, indent=2),
        )

        if new_house_numbers:
            history = api.get_history(force=False) if api.logged_in else {}
            by_house = {row['house_number']: row for row in records}
            for house_number in new_house_numbers:
                record = by_house[house_number]
                state = history.get(house_number)
                self.queue_episode_state(
                    show_id,
                    house_number,
                    1 if state and state.get('done') else 0,
                    progress=(state or {}).get('progress') or 0,
                    duration=record.get('duration') or 0,
                    source='new_library_episode',
                )

        return len(records), new_count, changed_count

    def _load_all_series(self, api, show):
        href = link_href(show, 'deeplink', 'self')
        if not href:
            slug = show.get('slug')
            if slug:
                href = '/show/{}'.format(slug)
        if not href:
            raise RuntimeError('ABC show has no catalogue link')

        first_page = api.get_series(href)
        embedded = first_page.get('_embedded', {}) if isinstance(first_page, dict) else {}
        selected = embedded.get('selectedSeries')
        rows = []
        seen = set()

        def add_series(series):
            if not isinstance(series, dict):
                return
            key = str(series.get('id') or link_href(series, 'deeplink', 'self') or id(series))
            if key in seen:
                return
            seen.add(key)
            rows.append(series)

        add_series(selected)
        if not selected:
            highlight = embedded.get('highlightVideo')
            if isinstance(highlight, dict):
                add_series(highlight)
            elif first_page.get('houseNumber'):
                add_series(first_page)

        series_list = embedded.get('seriesList') or []
        for series in series_list[:100]:
            series_id = str(series.get('id') or '') if isinstance(series, dict) else ''
            if series_id and selected and series_id == str(selected.get('id') or ''):
                continue
            series_href = link_href(series, 'deeplink', 'self')
            if not series_href:
                add_series(series)
                continue
            try:
                page = api.get_series(series_href)
                page_embedded = page.get('_embedded', {}) if isinstance(page, dict) else {}
                add_series(page_embedded.get('selectedSeries') or series)
            except Exception as exc:
                self._diag(
                    'series_fetch_failed',
                    href=series_href,
                    error=repr(exc),
                )
                add_series(series)

        return rows

    def _show_folder(self, show, show_id):
        title = safe_component(show_title(show), 'ABC iView Show', 100)
        return os.path.join(
            LIBRARY_ROOT,
            '{} [ABC {}]'.format(title, safe_component(show_id, 'show', 30)),
        )

    def _start_clean(self):
        # Set transaction state before JSON-RPC so even an unusually fast Kodi
        # notification cannot arrive before ownership has been recorded.
        self._owned_clean_active = True
        self._clean_needed = False
        self._clean_in_progress = True
        self._clean_started = time.time()
        try:
            result = _jsonrpc(
                'VideoLibrary.Clean',
                {
                    'directory': _normalise_path(LIBRARY_ROOT),
                    'content': 'tvshows',
                    'showdialogs': False,
                },
            )
        except Exception:
            self._owned_clean_active = False
            self._clean_in_progress = False
            self._clean_started = 0
            self._clean_needed = True
            raise

        self._diag(
            'clean_requested',
            result=result,
            root=LIBRARY_ROOT,
        )

    def _start_scan(self):
        self._ensure_library_source()

        # Set transaction state before JSON-RPC for the same reason as clean:
        # ownership must already be visible if Kodi notifies immediately.
        self._owned_scan_active = True
        self._scan_needed = False
        self._unscanned_shows = 0
        self._scan_in_progress = True
        self._scan_started = time.time()
        try:
            result = _jsonrpc(
                'VideoLibrary.Scan',
                {
                    'directory': _normalise_path(LIBRARY_ROOT),
                    'showdialogs': False,
                },
            )
        except Exception:
            self._owned_scan_active = False
            self._scan_in_progress = False
            self._scan_started = 0
            self._scan_needed = True
            raise

        self._diag(
            'scan_requested',
            result=result,
            root=LIBRARY_ROOT,
        )

    def _library_episode_map(self, force=False):
        now = time.time()
        cached, expires = self._episode_cache
        if not force and cached and expires > now:
            return cached
        result = _jsonrpc(
            'VideoLibrary.GetEpisodes',
            {
                'properties': [
                    'file', 'uniqueid', 'playcount', 'resume', 'runtime',
                ],
                'limits': {'start': 0, 'end': 10000},
            },
        )
        mapping = {}
        root_key = _path_key(LIBRARY_ROOT)
        for details in result.get('episodes') or []:
            file_path = str(details.get('file') or '')
            if not file_path or not _path_key(file_path).startswith(root_key):
                continue
            house_number = ''
            uniqueid = details.get('uniqueid')
            if isinstance(uniqueid, dict):
                house_number = str(uniqueid.get('abciview') or '')
            if not house_number:
                match = re.search(r'\[ABC ([^\]]+)\]\.strm$', file_path, re.I)
                if match:
                    house_number = match.group(1)
            if not house_number:
                item = self._item_from_library_details(details)
                house_number = item.get('house_number') if item else ''
            if house_number:
                mapping[str(house_number)] = details
        self._episode_cache = (mapping, now + 30)
        return mapping

    def _apply_episode_states(self, limit=None):
        """Apply queued playcount/resume changes as one JSON-RPC batch."""
        try:
            mapping = self._library_episode_map(force=True)
        except Exception as exc:
            self._diag('episode_map_failed', error=repr(exc))
            return

        changed_state = False
        now = time.time()
        calls = []
        applied = []
        processed = 0
        maximum = None if limit is None else max(0, int(limit))

        for house_number in list(self.state['episode_states']):
            if maximum is not None and processed >= maximum:
                break
            state = self.state['episode_states'][house_number]
            not_before = float(state.get('not_before') or 0)
            if not_before > now:
                continue

            details = mapping.get(house_number)
            if not details:
                # A removed/expired episode can no longer be written in Kodi.
                # Retain the state for up to seven days, but back off from ten
                # minutes instead of waking the service every second. Any later
                # library scan clears this deadline for one immediate retry.
                if now - float(state.get('created') or now) > 7 * 86400:
                    del self.state['episode_states'][house_number]
                    changed_state = True
                else:
                    attempts = int(state.get('missing_attempts') or 0) + 1
                    delay = min(
                        EPISODE_STATE_MAX_RETRY,
                        EPISODE_STATE_MISSING_RETRY * (2 ** (attempts - 1)),
                    )
                    state['missing_attempts'] = attempts
                    state['not_before'] = now + delay
                    state['last_missing'] = now
                    changed_state = True
                    self._diag(
                        'episode_state_row_missing',
                        house_number=house_number,
                        attempts=attempts,
                        retry_in=delay,
                    )
                continue

            processed += 1
            target = 1 if int(state.get('playcount') or 0) > 0 else 0
            current = 1 if int(details.get('playcount') or 0) > 0 else 0
            progress = 0 if target else max(0, int(state.get('progress') or 0))
            duration = max(
                int(state.get('duration') or 0),
                int(details.get('runtime') or 0),
            )
            resume = details.get('resume') or {}
            current_position = int(float(resume.get('position') or 0))

            params = {'episodeid': int(details['episodeid'])}
            if current != target:
                params['playcount'] = target
            if target:
                if current_position:
                    params['resume'] = {
                        'position': 0,
                        'total': duration,
                    }
            elif progress != current_position and (
                progress > 0 or current_position > 0
            ):
                params['resume'] = {
                    'position': progress,
                    'total': max(duration, progress),
                }

            if len(params) > 1:
                episode_id = int(details['episodeid'])
                self._expected_updates[episode_id] = {
                    'playcount': target,
                    'expires': time.time() + 30,
                }
                calls.append(('VideoLibrary.SetEpisodeDetails', params))
                applied.append((
                    str(house_number),
                    episode_id,
                    target,
                    progress,
                    dict(state),
                ))

            del self.state['episode_states'][house_number]
            changed_state = True

        if calls:
            try:
                _jsonrpc_batch(calls)
                for house_number, episode_id, target, progress, _state in applied:
                    self._diag(
                        'episode_state_applied_batch',
                        house_number=house_number,
                        episodeid=episode_id,
                        playcount=target,
                        progress=progress,
                    )
                self._diag(
                    'episode_state_batch_complete',
                    count=len(calls),
                )
            except Exception as exc:
                # Requeue only the rows whose writes were attempted. They will
                # be retried as one later transaction rather than once a second.
                for house_number, episode_id, _target, _progress, state in applied:
                    self._expected_updates.pop(episode_id, None)
                    state['not_before'] = now + 30
                    self.state['episode_states'][house_number] = state
                # Retry later as another single batch, never once per item or
                # once per service tick.
                self._schedule_qualification_recheck(delay=30)
                self._diag(
                    'episode_state_batch_failed',
                    count=len(calls),
                    error=repr(exc),
                )
                log.warning(
                    'ABC iView episode-state batch failed: {}'.format(exc)
                )

        if changed_state:
            self._save()

    def _ensure_library_source(self):
        os.makedirs(LIBRARY_ROOT, exist_ok=True)
        self._ensure_sources_xml()
        self._ensure_source_content()

    def _ensure_sources_xml(self):
        sources_path = xbmcvfs.translatePath('special://profile/sources.xml')
        if os.path.exists(sources_path):
            tree = ET.parse(sources_path)
            root = tree.getroot()
        else:
            root = ET.Element('sources')
            tree = ET.ElementTree(root)
        video = root.find('video')
        if video is None:
            video = ET.SubElement(root, 'video')
        target_key = _path_key(LIBRARY_ROOT)
        for source in video.findall('source'):
            name = source.findtext('name') or ''
            paths = [node.text or '' for node in source.findall('path')]
            if name == SOURCE_NAME or any(_path_key(path) == target_key for path in paths):
                return
        if os.path.exists(sources_path):
            backup = sources_path + '.abc_iviewjc.bak'
            if not os.path.exists(backup):
                shutil.copy2(sources_path, backup)
        source = ET.SubElement(video, 'source')
        ET.SubElement(source, 'name').text = SOURCE_NAME
        path_node = ET.SubElement(source, 'path', {'pathversion': '1'})
        path_node.text = _normalise_path(LIBRARY_ROOT)
        ET.SubElement(source, 'allowsharing').text = 'true'
        directory = os.path.dirname(sources_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tree.write(sources_path, encoding='utf-8', xml_declaration=True)
        self._diag('sources_xml_added', path=sources_path)

    def _advanced_video_database_is_remote(self):
        path = xbmcvfs.translatePath('special://profile/advancedsettings.xml')
        if not os.path.exists(path):
            return False
        try:
            root = ET.parse(path).getroot()
            database = root.find('videodatabase')
            if database is None:
                return False
            db_type = (database.findtext('type') or '').strip().lower()
            host = (database.findtext('host') or '').strip()
            return bool(host or (db_type and db_type not in ('sqlite', 'sqlite3')))
        except Exception:
            return False

    def _video_database_path(self):
        if self._advanced_video_database_is_remote():
            raise RuntimeError(
                'Automatic library source setup does not support a remote Kodi video database'
            )
        database_dir = xbmcvfs.translatePath('special://database/')
        candidates = glob.glob(os.path.join(database_dir, 'MyVideos*.db'))
        if not candidates:
            raise RuntimeError('Kodi video database was not found')

        def version(path):
            match = re.search(r'MyVideos(\d+)\.db$', os.path.basename(path), re.I)
            return int(match.group(1)) if match else -1

        return max(candidates, key=version)

    def plugin_watch_signature(self, plugin_url):
        """Read Kodi's native playcount row for an ABC plugin episode.

        The standard ToggleWatched action writes this row even though the item
        is being browsed through a plugin rather than the scanned TV library.
        This is a read-only observation path and never edits Kodi's database.
        """
        if not plugin_url:
            return None

        try:
            db_path = self._plugin_watch_db_path
            if not db_path or not os.path.exists(db_path):
                db_path = self._video_database_path()
                self._plugin_watch_db_path = db_path
            signature = read_plugin_watch_signature(db_path, plugin_url)
            self._plugin_watch_db_error = ''
            return signature
        except Exception as exc:
            # Do not flood kodi.log while a remote/locked database is present.
            message = repr(exc)
            now = time.time()
            if (
                message != self._plugin_watch_db_error
                or now - self._plugin_watch_db_error_at >= 300
            ):
                self._plugin_watch_db_error = message
                self._plugin_watch_db_error_at = now
                self._diag(
                    'plugin_watch_database_unavailable',
                    error=message,
                )
                log.warning(
                    'Unable to observe native ToggleWatched for ABC plugin '
                    'items: {}'.format(exc)
                )
            return None

    def _ensure_source_content(self):
        db_path = self._video_database_path()
        source_path = _normalise_path(LIBRARY_ROOT)
        connection = sqlite3.connect(db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            columns = {
                row['name']
                for row in connection.execute('PRAGMA table_info(path)').fetchall()
            }
            if 'strPath' not in columns:
                raise RuntimeError('Kodi path table has an unsupported schema')
            rows = connection.execute('SELECT * FROM path').fetchall()
            selected = None
            target = _path_key(source_path)
            for row in rows:
                if _path_key(row['strPath']) == target:
                    selected = row
                    break

            desired = {
                'strPath': source_path,
                'strContent': 'tvshows',
                'strScraper': 'metadata.local',
                'strHash': '',
                'scanRecursive': 0,
                'useFolderNames': 0,
                'strSettings': '',
                'noUpdate': 0,
                'exclude': 0,
                'allAudio': 0,
                'dateAdded': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'idParentPath': None,
            }
            desired = {key: value for key, value in desired.items() if key in columns}

            if selected is None:
                names = list(desired)
                placeholders = ','.join('?' for _ in names)
                connection.execute(
                    'INSERT INTO path ({}) VALUES ({})'.format(
                        ','.join(names), placeholders
                    ),
                    [desired[name] for name in names],
                )
                self._diag('video_path_inserted', db=db_path, path=source_path)
            else:
                if 'source_db_previous' not in self.state:
                    self.state['source_db_previous'] = {
                        key: selected[key]
                        for key in selected.keys()
                        if key in desired
                    }
                update_names = [name for name in desired if name != 'strPath']
                connection.execute(
                    'UPDATE path SET {} WHERE idPath=?'.format(
                        ','.join('{}=?'.format(name) for name in update_names)
                    ),
                    [desired[name] for name in update_names] + [selected['idPath']],
                )
                self._diag(
                    'video_path_updated',
                    db=db_path,
                    path=source_path,
                    idPath=selected['idPath'],
                )
            connection.commit()
        finally:
            connection.close()
        self._save()
