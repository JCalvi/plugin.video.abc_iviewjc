import json
import time
from urllib.parse import parse_qs, urlparse

import xbmc
import xbmcgui


from slyguy import log

from resources.lib.api import API
from resources.lib.diagnostics import (
    diagnostic_event,
    diagnostics_enabled,
)
from resources.lib.libraryintegration import (
    LibraryIntegration,
    request_episode_state,
    request_follow_show,
    request_reconcile_show,
)
from resources.lib.watchaction import (
    enqueue_action,
    mark_context_closed,
    pop_due_action,
    set_context_candidate,
    set_pending_state,
)


WINDOW = xbmcgui.Window(10000)
SYNC_INTERVAL = 30
WATCHED_PERCENT = 0.90
CONTEXT_POLL_INTERVAL = 0.10
ADDON_URL_PREFIX = 'plugin://plugin.video.abc_iviewjc/'


_DIAG_SEQUENCE = 0


def diag(event, **fields):
    """Emit unique, targeted diagnostics only when enabled."""
    global _DIAG_SEQUENCE

    if not diagnostics_enabled():
        return

    _DIAG_SEQUENCE += 1
    diagnostic_event(
        'IVIEW115DIAG',
        '#{:05d} {}'.format(_DIAG_SEQUENCE, event),
        **fields
    )


def raw_gui_state(full=True):
    labels = {}
    names = (
        (
            'ListItem.FileNameAndPath',
            'ListItem.Path',
            'ListItem.FolderPath',
            'ListItem.PlayCount',
            'ListItem.Overlay',
            'ListItem.Label',
            'ListItem.Title',
            'ListItem.DBID',
            'ListItem.MediaType',
            'Container.FolderPath',
            'Container.Position',
            'Container.CurrentItem',
            'Container.NumItems',
            'Container.NumAllItems',
        )
        if full
        else ('Container.FolderPath',)
    )
    for name in names:
        try:
            labels[name] = xbmc.getInfoLabel(name)
        except Exception as exc:
            labels[name] = '<ERROR {}>'.format(repr(exc))

    labels['window_id'] = xbmcgui.getCurrentWindowId()
    labels['dialog_id'] = xbmcgui.getCurrentWindowDialogId()

    conditions = {}
    for condition in (
        'Window.IsActive(contextmenu)',
        'Window.IsVisible(contextmenu)',
        'Window.IsActive(10106)',
        'Window.IsVisible(10106)',
    ):
        try:
            conditions[condition] = bool(
                xbmc.getCondVisibility(condition)
            )
        except Exception as exc:
            conditions[condition] = '<ERROR {}>'.format(repr(exc))
    labels['conditions'] = conditions

    return labels


diag('service_imports_complete')


class ABCPlayer(xbmc.Player):
    def __init__(self):
        super(ABCPlayer, self).__init__()
        self.reset()

    def reset(self):
        self.show_id = ''
        self.house_number = ''
        self.title = ''
        self.position = 0
        self.duration = 0
        self.last_synced = -1

    def onAVStarted(self):
        diag('player_av_started', gui=raw_gui_state())
        self.show_id = WINDOW.getProperty('abc_iview.show_id')
        self.house_number = WINDOW.getProperty('abc_iview.house_number')
        self.title = WINDOW.getProperty('abc_iview.title')
        try:
            self.duration = int(float(WINDOW.getProperty('abc_iview.duration') or 0))
        except Exception:
            self.duration = 0
        self.position = 0
        self.last_synced = -1
        library_service.on_playback_started(
            self.show_id,
            self.house_number,
        )

    def onPlayBackEnded(self):
        diag(
            'player_ended',
            show_id=self.show_id,
            house_number=self.house_number,
            position=self.position,
            duration=self.duration,
        )
        self.finish(True)

    def onPlayBackStopped(self):
        diag(
            'player_stopped',
            show_id=self.show_id,
            house_number=self.house_number,
            position=self.position,
            duration=self.duration,
        )
        self.finish(False)

    def update_position(self):
        if not self.house_number:
            return
        try:
            self.position = max(self.position, int(self.getTime()))
            self.duration = max(self.duration, int(self.getTotalTime()))
        except Exception:
            pass

    def sync_progress(self, force=False):
        if not self.show_id or not self.house_number or self.position < 1:
            return
        if not force and self.last_synced >= 0 and self.position - self.last_synced < SYNC_INTERVAL:
            return

        try:
            api = API()
            api.new_session()
            if api.logged_in:
                api.set_video_progress(self.show_id, self.house_number, self.position, done=False)
                self.last_synced = self.position
                library_service.on_playback_progress(
                    self.show_id,
                    self.house_number,
                    self.position,
                    self.duration,
                )
        except Exception as exc:
            log.warning('ABC watched sync progress failed for {}: {}'.format(self.house_number, exc))

    def finish(self, ended):
        self.update_position()
        watched = ended or (self.duration > 0 and self.position >= self.duration * WATCHED_PERCENT)
        try:
            api = API()
            api.new_session()
            if api.logged_in and self.show_id and self.house_number:
                if watched:
                    api.mark_video_watched(
                        self.show_id,
                        self.house_number,
                        progress=max(self.position, self.duration if ended else 0),
                    )
                elif self.position > 0:
                    api.set_video_progress(self.show_id, self.house_number, self.position, done=False)
        except Exception as exc:
            log.warning('ABC watched sync completion failed for {}: {}'.format(self.house_number, exc))
        finally:
            library_service.on_playback_finished(
                self.show_id,
                self.house_number,
                watched,
                self.position,
                self.duration,
            )
            self.reset()


class ABCMonitor(xbmc.Monitor):
    """Mirror Kodi's built-in watched toggle to the linked ABC account.

    ABC episodes are plugin list items rather than Kodi database records. Kodi
    can briefly clear the selected ListItem while the context menu closes,
    especially when changing a watched item back to unwatched. Keep the last
    valid ABC item for a short grace period and use VideoLibrary.OnUpdate's
    playcount value when Kodi supplies it.
    """

    SELECTION_GRACE = 4.0
    NOTIFICATION_GRACE = 4.0

    def __init__(self, library):
        super(ABCMonitor, self).__init__()
        self.library = library
        self._last_item = None
        self._last_seen = 0.0
        self._last_playcount = None
        self._pending_playcount = None
        self._pending_at = 0.0
        self._last_write = ('', None, 0.0)
        self._last_diag_signature = None
        self._last_heartbeat = 0.0
        self._context_active = False
        diag(
            'monitor_created',
            selection_grace=self.SELECTION_GRACE,
            notification_grace=self.NOTIFICATION_GRACE,
            poll_interval=CONTEXT_POLL_INTERVAL,
            gui=raw_gui_state(),
        )

    def onNotification(self, sender, method, data):
        self.library.handle_notification(method, data)
        if method != 'VideoLibrary.OnUpdate':
            return

        # Kodi normally includes playcount in this notification. For plugin
        # items the item path may disappear for a moment, so defer the write to
        # the main service loop where the most recently selected ABC episode is
        # still available.
        playcount = None
        try:
            payload = json.loads(data) if data else {}
            if isinstance(payload, dict):
                notification_item = payload.get('item')
                if (
                    isinstance(notification_item, dict)
                    and notification_item.get('type') == 'episode'
                    and notification_item.get('id') is not None
                ):
                    # LibraryIntegration resolves this database episode and
                    # feeds it back through _sync_state without relying on a
                    # selected plugin ListItem.
                    return
                value = payload.get('playcount')
                if value is None and isinstance(payload.get('item'), dict):
                    value = payload['item'].get('playcount')
                if value is not None:
                    playcount = int(value)
        except Exception:
            pass

        self._pending_playcount = playcount
        self._pending_at = time.time()
        diag(
            'onupdate_queued',
            parsed_playcount=playcount,
            pending_at=self._pending_at,
            cached_item=self._last_item,
            cached_playcount=self._last_playcount,
        )

    @staticmethod
    def _selected_path():
        for label in (
            'ListItem.FileNameAndPath',
            'ListItem.Path',
            'ListItem.FolderPath',
        ):
            value = xbmc.getInfoLabel(label)
            if value and value.startswith(ADDON_URL_PREFIX):
                return value
        return ''

    @staticmethod
    def _parse_item(path):
        try:
            query = parse_qs(urlparse(path).query)
        except Exception:
            return None

        def first(name, default=''):
            values = query.get(name)
            return values[0] if values else default

        house_number = first('house_number')
        show_id = first('show_id')
        source_url = first('url')
        if not house_number and source_url.startswith('/video/'):
            house_number = source_url.rsplit('/', 1)[-1]

        try:
            duration = max(0, int(float(first('duration', '0') or 0)))
        except Exception:
            duration = 0

        if not show_id or not house_number:
            return None

        return {
            'key': '{}:{}'.format(show_id, house_number),
            'show_id': show_id,
            'house_number': house_number,
            'duration': duration,
        }

    @staticmethod
    def _current_playcount():
        try:
            return int(xbmc.getInfoLabel('ListItem.PlayCount') or 0)
        except Exception:
            return 0

    def _sync_state(self, item, playcount):
        watched = int(playcount or 0) > 0
        now = time.time()

        diag(
            'sync_state_enter',
            item=item,
            playcount=playcount,
            watched=watched,
            last_write=self._last_write,
            gui=raw_gui_state(),
        )

        last_key, last_watched, last_time = self._last_write
        if (
            last_key == item['key']
            and last_watched == watched
            and now - last_time < 3
        ):
            diag(
                'sync_state_duplicate_suppressed',
                item=item,
                watched=watched,
                age=now - last_time,
            )
            return

        self._last_write = (item['key'], watched, now)

        # Kodi is authoritative immediately. Protect the new local playcount
        # before any automatic folder rebuild or network request.
        set_pending_state(
            item,
            1 if watched else 0,
            source='playcount_transition',
        )
        enqueue_action(
            item,
            1 if watched else 0,
            source='playcount_transition',
        )
        if watched:
            request_follow_show(
                item['show_id'],
                source='watched_toggle',
                house_number=item['house_number'],
            )
        else:
            request_reconcile_show(
                item['show_id'],
                source='unwatched_toggle',
            )
        request_episode_state(
            item['show_id'],
            item['house_number'],
            1 if watched else 0,
            duration=item.get('duration') or 0,
            source='watched_toggle',
        )
        diag(
            'sync_state_queued',
            item=item,
            watched=watched,
        )

    def process_action_queue(self):
        action = pop_due_action()
        if not action:
            return

        watched = int(action.get('playcount') or 0) > 0
        item = {
            'key': action.get('key'),
            'show_id': action.get('show_id'),
            'house_number': action.get('house_number'),
            'duration': int(action.get('duration') or 0),
        }
        attempts = int(action.get('attempts') or 0)

        diag(
            'queue_action_begin',
            item=item,
            watched=watched,
            attempts=attempts,
            source=action.get('source'),
        )

        try:
            api = API()
            api.new_session()
            if not api.logged_in:
                raise RuntimeError('ABC account is not signed in')

            if watched:
                api.mark_video_watched(
                    item['show_id'],
                    item['house_number'],
                    progress=max(1, item['duration']),
                )
            else:
                api.mark_video_unwatched(
                    item['show_id'],
                    item['house_number'],
                )

            diag(
                'queue_action_success',
                item=item,
                watched=watched,
                attempts=attempts,
            )
            log.info(
                'Kodi built-in watched state synced to ABC for {}: {}'.format(
                    item['house_number'],
                    'watched' if watched else 'unwatched',
                )
            )
        except Exception as exc:
            attempts += 1
            diag(
                'queue_action_failure',
                item=item,
                watched=watched,
                attempts=attempts,
                error=repr(exc),
            )

            if attempts < 4:
                enqueue_action(
                    item,
                    1 if watched else 0,
                    source='retry',
                    attempts=attempts,
                    not_before=time.time() + min(30, 2 ** attempts),
                )
            else:
                log.exception(
                    'Unable to sync Kodi watched state for {}: {}'.format(
                        item['house_number'],
                        exc,
                    )
                )
                xbmcgui.Dialog().notification(
                    'ABC iView',
                    'Unable to sync watched status: {}'.format(exc),
                    xbmcgui.NOTIFICATION_ERROR,
                    5000,
                )

            # Deliberately no Container.Refresh. A network problem must never
            # undo the playcount Kodi has just set.

    def poll_watched_toggle(self):
        for library_item, library_playcount in self.library.pop_watched_updates():
            diag(
                'library_playcount_transition',
                item=library_item,
                playcount=library_playcount,
            )
            self._sync_state(library_item, library_playcount)

        now = time.time()
        diagnostic = diagnostics_enabled()
        gui_state = raw_gui_state(full=diagnostic)
        path = self._selected_path()
        item = self._parse_item(path) if path else None
        playcount = None

        if diagnostic:
            signature = json.dumps(
                {
                    'path': path,
                    'item': item,
                    'raw_playcount': gui_state.get('ListItem.PlayCount'),
                    'overlay': gui_state.get('ListItem.Overlay'),
                    'label': gui_state.get('ListItem.Label'),
                    'folder': gui_state.get('Container.FolderPath'),
                    'position': gui_state.get('Container.Position'),
                    'current_item': gui_state.get('Container.CurrentItem'),
                    'window_id': gui_state.get('window_id'),
                    'dialog_id': gui_state.get('dialog_id'),
                    'conditions': gui_state.get('conditions'),
                    'last_item': self._last_item,
                    'last_playcount': self._last_playcount,
                    'pending_playcount': self._pending_playcount,
                    'pending_at': bool(self._pending_at),
                },
                sort_keys=True,
                separators=(',', ':'),
                default=str,
            )
            if signature != self._last_diag_signature:
                self._last_diag_signature = signature
                diag(
                    'poll_state_changed',
                    path=path,
                    parsed_item=item,
                    cached_item=self._last_item,
                    cached_playcount=self._last_playcount,
                    pending_playcount=self._pending_playcount,
                    pending_age=(
                        now - self._pending_at
                        if self._pending_at
                        else None
                    ),
                    gui=gui_state,
                )
            elif now - self._last_heartbeat >= 5.0:
                self._last_heartbeat = now
                diag(
                    'poll_heartbeat',
                    path=path,
                    parsed_item=item,
                    cached_item=self._last_item,
                    cached_playcount=self._last_playcount,
                    pending_playcount=self._pending_playcount,
                    pending_age=(
                        now - self._pending_at
                        if self._pending_at
                        else None
                    ),
                    gui=gui_state,
                )

        context_active = bool(
            gui_state.get('conditions', {}).get(
                'Window.IsActive(contextmenu)'
            )
            or gui_state.get('conditions', {}).get(
                'Window.IsActive(10106)'
            )
        )

        if item:
            playcount = self._current_playcount()

            if context_active and not self._context_active:
                set_context_candidate(
                    item,
                    playcount,
                    gui_state.get('Container.FolderPath'),
                )
                diag(
                    'context_candidate_set',
                    item=item,
                    playcount=playcount,
                    folder=gui_state.get('Container.FolderPath'),
                )

            if not context_active and self._context_active:
                mark_context_closed()
                diag(
                    'context_candidate_closed',
                    item=item,
                    playcount=playcount,
                )

            if not self._last_item or item['key'] != self._last_item['key']:
                # Establish the baseline for a newly selected episode.
                diag(
                    'selection_baseline',
                    item=item,
                    playcount=playcount,
                    previous_item=self._last_item,
                    previous_playcount=self._last_playcount,
                )
                self._last_item = item
                self._last_seen = now
                self._last_playcount = playcount
            else:
                self._last_item = item
                self._last_seen = now
                if playcount != self._last_playcount:
                    diag(
                        'poll_playcount_transition',
                        item=item,
                        previous=self._last_playcount,
                        current=playcount,
                        gui=gui_state,
                    )
                    self._last_playcount = playcount
                    self._sync_state(item, playcount)

        # VideoLibrary.OnUpdate is the reliable signal for the 1 -> 0 change.
        # When Kodi closes the context menu, the selected plugin ListItem may be
        # blank for a few polling cycles. Use the recently cached ABC item so a
        # Mark unwatched action is not lost.
        if self._pending_at:
            age = now - self._pending_at
            target = item
            diag(
                'pending_processing',
                age=age,
                item=item,
                cached_item=self._last_item,
                cached_age=(
                    now - self._last_seen
                    if self._last_seen
                    else None
                ),
                pending_playcount=self._pending_playcount,
                cached_playcount=self._last_playcount,
                gui=gui_state,
            )
            if not target and self._last_item and now - self._last_seen <= self.SELECTION_GRACE:
                target = self._last_item
                diag(
                    'pending_uses_cached_item',
                    target=target,
                    cached_age=now - self._last_seen,
                )

            if target and age <= self.NOTIFICATION_GRACE:
                notified_playcount = self._pending_playcount
                if notified_playcount is None:
                    # Some Kodi builds omit playcount from the callback. Once
                    # the ListItem reappears, use its resulting value.
                    if item:
                        notified_playcount = playcount
                        diag(
                            'pending_uses_reappeared_item_playcount',
                            playcount=notified_playcount,
                            item=item,
                        )
                    elif self._last_playcount is not None:
                        # The notification followed a built-in toggle while the
                        # item vanished. The only possible new state is the
                        # inverse of the cached baseline.
                        notified_playcount = 0 if self._last_playcount > 0 else 1
                        diag(
                            'pending_inverts_cached_playcount',
                            cached_playcount=self._last_playcount,
                            inferred_playcount=notified_playcount,
                            target=target,
                        )

                if notified_playcount is not None:
                    diag(
                        'pending_sync_decision',
                        target=target,
                        notified_playcount=notified_playcount,
                        original_pending_playcount=self._pending_playcount,
                    )
                    self._last_item = target
                    self._last_seen = now
                    self._last_playcount = int(notified_playcount)
                    self._sync_state(target, notified_playcount)
                    self._pending_at = 0.0
                    self._pending_playcount = None
            elif age > self.NOTIFICATION_GRACE:
                diag(
                    'pending_expired',
                    age=age,
                    item=item,
                    cached_item=self._last_item,
                    pending_playcount=self._pending_playcount,
                )
                self._pending_at = 0.0
                self._pending_playcount = None

        if not item and not context_active and self._context_active:
            mark_context_closed()
            diag(
                'context_candidate_closed_without_item',
                cached_item=self._last_item,
            )

        self._context_active = context_active

        # Do not immediately discard the baseline when Kodi temporarily clears
        # the selected ListItem. Expire it only after the context-menu grace
        # period has elapsed.
        if not item and self._last_item and now - self._last_seen > self.SELECTION_GRACE:
            diag(
                'selection_cache_expired',
                cached_item=self._last_item,
                cached_playcount=self._last_playcount,
                age=now - self._last_seen,
                gui=gui_state,
            )
            self._last_item = None
            self._last_playcount = None


diag('service_objects_begin')
library_service = LibraryIntegration()
player = ABCPlayer()
monitor = ABCMonitor(library_service)
diag('service_loop_started', gui=raw_gui_state())
while not monitor.abortRequested():
    if player.isPlayingVideo():
        player.update_position()
        player.sync_progress()

    monitor.poll_watched_toggle()
    monitor.process_action_queue()
    library_service.tick(playing=player.isPlayingVideo())

    if monitor.waitForAbort(CONTEXT_POLL_INTERVAL):
        break

diag('service_loop_ended')
