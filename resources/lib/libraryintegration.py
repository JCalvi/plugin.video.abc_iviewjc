import datetime
import glob
import json
import os
import re
import shutil
import sqlite3
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
from .librarysettings import (
    ADDON_ID,
    SCOPE_BY_INDEX as LIBRARY_SCOPE_BY_INDEX,
    SCOPE_MANUAL as LIBRARY_SCOPE_MANUAL,
    SCOPE_WATCHED as LIBRARY_SCOPE_WATCHED,
    SCOPE_WATCHLIST as LIBRARY_SCOPE_WATCHLIST,
    get_scope as library_scope,
    get_scope_index as library_scope_index,
    set_scope_index as set_library_scope_index,
)
from .settings import settings


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
STATE_VERSION = 6

def library_enabled():
    try:
        return bool(settings.getBool('library_integration', True))
    except Exception:
        return False


def _property_json(name, default):
    raw = WINDOW.getProperty(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        WINDOW.clearProperty(name)
        return default


def _set_property_json(name, value):
    WINDOW.setProperty(
        name,
        json.dumps(value, sort_keys=True, separators=(',', ':')),
    )


def request_library_action(action, **fields):
    """Send a small cross-interpreter request to the background service."""
    if not library_enabled():
        return False
    requests = _property_json(REQUESTS_PROPERTY, [])
    if not isinstance(requests, list):
        requests = []
    request = {'action': action, 'created': time.time()}
    request.update(fields)
    requests.append(request)
    _set_property_json(REQUESTS_PROPERTY, requests[-MAX_REQUESTS:])
    return True


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


def _atomic_write(path, data, binary=False):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    temp = '{}.tmp'.format(path)
    mode = 'wb' if binary else 'w'
    kwargs = {} if binary else {'encoding': 'utf-8'}
    with open(temp, mode, **kwargs) as handle:
        handle.write(data)
    os.replace(temp, path)


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


def _extract_show_id(namespace):
    match = re.search(r'(?:^|:)show:([^:]+):video(?:$|:)', str(namespace or ''))
    return match.group(1) if match else ''


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
        self._library_updates = []
        self._expected_updates = {}
        self._episode_cache = ({}, 0)
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
        requests = _property_json(REQUESTS_PROPERTY, [])
        if requests:
            WINDOW.clearProperty(REQUESTS_PROPERTY)
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
                self._diag(
                    'qualification_recheck_requested',
                    show_id=request.get('show_id'),
                    source=request.get('source'),
                )
            elif action == 'manual_selection_changed':
                self._schedule_qualification_recheck(delay=0)
                self._diag(
                    'manual_selection_changed',
                    show_id=request.get('show_id'),
                    included=bool(request.get('included')),
                )
            elif action == 'library_scope_changed':
                self.state['last_bootstrap'] = 0
                self._schedule_qualification_recheck(delay=0)
                self._diag(
                    'library_scope_change_requested',
                    scope=request.get('scope'),
                    index=request.get('index'),
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
            show_id = str(
                manifest.get('show_id') or ''
            )
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
        if removed_files:
            self._clean_needed = True
            self._scan_needed = True
            self._episode_cache = ({}, 0)

        self.state['followed'] = followed
        self._save()
        self._diag(
            'qualification_reconciled',
            qualified_count=len(qualified),
            previously_followed_count=len(previous),
            removed_followed=sorted(removed_ids),
            removed_file_count=len(removed_files),
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
        if method == 'VideoLibrary.OnCleanStarted':
            self._clean_in_progress = True
            self._clean_started = time.time()
            self._diag('clean_started_notification')
            return
        if method == 'VideoLibrary.OnCleanFinished':
            self._clean_in_progress = False
            self._clean_started = 0
            self._clean_needed = False
            self._episode_cache = ({}, 0)
            self._scan_needed = True
            self._diag('clean_finished_notification')
            return
        if method == 'VideoLibrary.OnScanStarted':
            self._scan_in_progress = True
            self._scan_started = time.time()
            self._diag('scan_started_notification')
            return
        if method == 'VideoLibrary.OnScanFinished':
            self._scan_in_progress = False
            self._scan_started = 0
            self._episode_cache = ({}, 0)
            self._diag('scan_finished_notification')
            return
        if method != 'VideoLibrary.OnUpdate':
            return
        try:
            payload = json.loads(data) if data else {}
        except Exception:
            payload = {}
        item = payload.get('item') if isinstance(payload, dict) else None
        item = item if isinstance(item, dict) else {}
        if item.get('type') != 'episode' or item.get('id') is None:
            return
        playcount = payload.get('playcount')
        if playcount is None:
            playcount = item.get('playcount')
        try:
            playcount = int(playcount) if playcount is not None else None
        except Exception:
            playcount = None
        self._library_updates.append({
            'episodeid': int(item['id']),
            'playcount': playcount,
            'created': time.time(),
        })
        self._diag(
            'library_onupdate_queued',
            episodeid=item['id'],
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

    def tick(self, playing=False):
        now = time.time()
        if now < self._next_tick:
            return
        self._next_tick = now + 1.0
        self._drain_requests()

        enabled = library_enabled()
        if enabled != self._enabled_last:
            self._enabled_last = enabled
            self._diag('setting_changed', enabled=enabled)
            if enabled:
                self.state['source_ready'] = False
                self.state['setup_error_notified'] = False
                self.state['last_bootstrap'] = 0
                self._schedule_qualification_recheck(delay=1)
                self._save()

        scope = library_scope()
        stored_scope = str(self.state.get('library_scope') or '')
        if scope != self._scope_last:
            previous_scope = self._scope_last
            self._scope_last = scope
            self._diag(
                'library_scope_changed',
                previous=previous_scope,
                current=scope,
                persisted=stored_scope,
            )

        if stored_scope != scope:
            self.state['library_scope'] = scope
            self.state['last_bootstrap'] = 0
            self._schedule_qualification_recheck(delay=1)
            log.info(
                'ABC iView library setting active: {}'.format(scope)
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
                    'ABC iView',
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
                        'ABC iView',
                        'Library setup failed. Check kodi.log',
                        xbmcgui.NOTIFICATION_ERROR,
                        7000,
                    )
                return

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
            self._scan_in_progress = False
            self._scan_started = 0
            self._diag('scan_timeout_released')

        if self._clean_in_progress and now - self._clean_started > 180:
            self._clean_in_progress = False
            self._clean_started = 0
            self._clean_needed = False
            self._scan_needed = True
            self._diag('clean_timeout_released')

        if playing:
            return

        if (
            self._clean_needed
            and not self._clean_in_progress
            and not self._scan_in_progress
        ):
            self._start_clean()
            return

        # Apply states after scans and between catalogue updates.
        if (
            not self._scan_in_progress
            and not self._clean_in_progress
            and self.state['episode_states']
        ):
            self._apply_episode_states(limit=50)

        queue_due = self._next_due_show(now)
        should_scan = self._scan_needed and (
            self._unscanned_shows >= 3
            or not queue_due
            or now - self._last_files_written >= 10
        )
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

    def _next_due_show(self, now):
        for row in self.state['queue']:
            if float(row.get('not_before') or 0) <= now:
                return row
        return None

    def _bootstrap_history(self):
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

            self._reconcile_followed(qualified_show_ids)

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
        except Exception as exc:
            log.warning(
                'ABC iView library qualification failed: {}'.format(exc)
            )
            self._diag(
                'history_bootstrap_failed',
                scope=library_scope(),
                error=repr(exc),
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
            count, new_count = self._sync_show(show_id)
            self._scan_needed = True
            self._unscanned_shows += 1
            self._last_files_written = time.time()
            followed = self.state['followed'].get(show_id) or {}
            followed['last_sync'] = time.time()
            followed['episode_count'] = count
            followed['last_error'] = ''
            self.state['followed'][show_id] = followed
            self._save()
            log.info(
                'ABC iView library prepared {} episodes for show {} ({} new)'.format(
                    count, show_id, new_count
                )
            )
            self._diag(
                'show_sync_complete',
                show_id=show_id,
                episode_count=count,
                new_count=new_count,
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

        _atomic_write(
            os.path.join(show_folder, 'tvshow.nfo'),
            tvshow_nfo(show, show_id, records),
            binary=True,
        )

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
            _atomic_write(strm_path, plugin_play_url(record) + '\n')
            _atomic_write(nfo_path, episode_nfo(record), binary=True)
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

        return len(records), new_count

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
        result = _jsonrpc(
            'VideoLibrary.Clean',
            {
                'directory': _normalise_path(LIBRARY_ROOT),
                'showdialogs': False,
            },
        )
        self._clean_in_progress = True
        self._clean_started = time.time()
        self._diag(
            'clean_requested',
            result=result,
            root=LIBRARY_ROOT,
        )

    def _start_scan(self):
        self._ensure_library_source()
        result = _jsonrpc(
            'VideoLibrary.Scan',
            {
                'directory': _normalise_path(LIBRARY_ROOT),
                'showdialogs': False,
            },
        )
        self._scan_needed = False
        self._unscanned_shows = 0
        self._scan_in_progress = True
        self._scan_started = time.time()
        self._diag('scan_requested', result=result, root=LIBRARY_ROOT)

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

    def _apply_episode_states(self, limit=50):
        try:
            mapping = self._library_episode_map(force=True)
        except Exception as exc:
            self._diag('episode_map_failed', error=repr(exc))
            return
        changed_state = False
        count = 0
        now = time.time()
        for house_number in list(self.state['episode_states']):
            if count >= limit:
                break
            state = self.state['episode_states'][house_number]
            details = mapping.get(house_number)
            if not details:
                # Keep states while a scan is pending; expire abandoned entries
                # after seven days to stop unbounded state growth.
                if now - float(state.get('created') or now) > 7 * 86400:
                    del self.state['episode_states'][house_number]
                    changed_state = True
                continue

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
                self._expected_updates[int(details['episodeid'])] = {
                    'playcount': target,
                    'expires': time.time() + 15,
                }
                _jsonrpc('VideoLibrary.SetEpisodeDetails', params)
                count += 1
                self._diag(
                    'episode_state_applied',
                    house_number=house_number,
                    episodeid=details['episodeid'],
                    playcount=target,
                    progress=progress,
                )

            del self.state['episode_states'][house_number]
            changed_state = True

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
