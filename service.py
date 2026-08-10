import time

import xbmc
import xbmcgui

from slyguy import log

from resources.lib.api import API
from resources.lib.diagnostics import diagnostic_event, diagnostics_enabled
from resources.lib.libraryintegration import (
    LibraryIntegration,
    library_enabled,
    library_scope,
    request_episode_state,
    request_follow_show,
    request_reconcile_show,
)
from resources.lib.librarysettings import (
    LIBRARY_WAKE_PROPERTY,
    has_library_requests,
)
from resources.lib.watchaction import (
    enqueue_action,
    next_action_due_at,
    pop_due_action,
    set_pending_state,
)


WINDOW = xbmcgui.Window(10000)
SYNC_INTERVAL = 3 * 60
WATCHED_PERCENT = 0.90
SERVICE_LOOP_INTERVAL = 1.0
STARTUP_DELAY = 3.0
LIBRARY_REQUEST_DELAY = 2.0
KODI_UPDATE_DELAY = 3.0
LIBRARY_FOLLOWUP_DELAY = 1.0
LIBRARY_TICK_RETRY_DELAY = 30.0
LIBRARY_REQUEST_SAFETY_INTERVAL = 60.0
MAX_ACTION_BURST = 50

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


class ABCPlayer(xbmc.Player):
    def __init__(self, service_monitor):
        super(ABCPlayer, self).__init__()
        self.service_monitor = service_monitor
        self.reset()

    def reset(self):
        self.show_id = ''
        self.house_number = ''
        self.title = ''
        self.position = 0
        self.duration = 0
        self.last_synced = -1

    def onAVStarted(self):
        self.show_id = WINDOW.getProperty('abc_iview.show_id')
        self.house_number = WINDOW.getProperty('abc_iview.house_number')
        self.title = WINDOW.getProperty('abc_iview.title')
        try:
            self.duration = int(float(
                WINDOW.getProperty('abc_iview.duration') or 0
            ))
        except Exception:
            self.duration = 0
        self.position = 0
        self.last_synced = -1
        library_service.on_playback_started(
            self.show_id,
            self.house_number,
        )

    def onPlayBackEnded(self):
        self.finish(True)

    def onPlayBackStopped(self):
        self.finish(False)

    def onPlayBackPaused(self):
        self.update_position()
        self.sync_progress(force=True)

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
        if (
            not force
            and self.last_synced >= 0
            and self.position - self.last_synced < SYNC_INTERVAL
        ):
            return

        try:
            api = API()
            api.new_session()
            if api.logged_in:
                api.set_video_progress(
                    self.show_id,
                    self.house_number,
                    self.position,
                    done=False,
                )
                self.last_synced = self.position
                library_service.on_playback_progress(
                    self.show_id,
                    self.house_number,
                    self.position,
                    self.duration,
                )
        except Exception as exc:
            log.warning(
                'ABC watched sync progress failed for {}: {}'.format(
                    self.house_number,
                    exc,
                )
            )

    def finish(self, ended):
        self.update_position()
        watched = ended or (
            self.duration > 0
            and self.position >= self.duration * WATCHED_PERCENT
        )
        try:
            api = API()
            api.new_session()
            if api.logged_in and self.show_id and self.house_number:
                if watched:
                    api.mark_video_watched(
                        self.show_id,
                        self.house_number,
                        progress=max(
                            self.position,
                            self.duration if ended else 0,
                        ),
                    )
                elif self.position > 0:
                    api.set_video_progress(
                        self.show_id,
                        self.house_number,
                        self.position,
                        done=False,
                    )
        except Exception as exc:
            log.warning(
                'ABC watched sync completion failed for {}: {}'.format(
                    self.house_number,
                    exc,
                )
            )
        finally:
            library_service.on_playback_finished(
                self.show_id,
                self.house_number,
                watched,
                self.position,
                self.duration,
            )
            # Apply the completed/resume state after playback has settled.
            self.service_monitor.schedule_library_work(
                2.0,
                'playback_finished',
                debounce=True,
            )
            self.reset()


class ABCMonitor(xbmc.Monitor):
    """Event-driven ABC/Kodi watched and library synchronisation."""

    def __init__(self, library):
        super(ABCMonitor, self).__init__()
        self.library = library
        self._last_write = ('', None, 0.0)
        self._next_library_work = 0.0
        self._library_reason = ''
        self._next_kodi_watch_work = 0.0
        self._last_wake_token = WINDOW.getProperty(LIBRARY_WAKE_PROPERTY)

    def schedule_library_work(self, delay=0, reason='', debounce=False):
        target = time.time() + max(0.0, float(delay or 0))
        reason = str(reason or '')

        if not self._next_library_work:
            self._next_library_work = target
            self._library_reason = reason
            return

        if debounce:
            # A user/request event must pre-empt a far-future maintenance
            # deadline. Only two already-pending event requests debounce each
            # other by extending to the latest target.
            if self._library_reason in ('library_followup', 'library_tick_retry'):
                self._next_library_work = target
            else:
                self._next_library_work = max(self._next_library_work, target)
            if reason:
                self._library_reason = reason
            return

        # Maintenance/follow-up work must never overwrite the reason or time of
        # an earlier event that is already scheduled.
        if target < self._next_library_work:
            self._next_library_work = target
            if reason:
                self._library_reason = reason

    def schedule_kodi_watch_work(self, delay=KODI_UPDATE_DELAY):
        target = time.time() + max(0.0, float(delay or 0))
        # Debounce a burst of VideoLibrary.OnUpdate notifications after the
        # final MyVideos commit. This deadline is intentionally independent of
        # catalogue maintenance so a six-hour refresh can never swallow it.
        self._next_kodi_watch_work = max(
            float(self._next_kodi_watch_work or 0),
            target,
        )

    def kodi_watch_work_due(self, now=None):
        now = time.time() if now is None else float(now)
        return bool(
            self._next_kodi_watch_work
            and now >= self._next_kodi_watch_work
        )

    def consume_kodi_watch_work(self):
        self._next_kodi_watch_work = 0.0

    def schedule_library_at(self, when, reason='followup'):
        try:
            when = float(when or 0)
        except (TypeError, ValueError):
            return
        if when <= 0:
            return
        delay = max(0.0, when - time.time())
        self.schedule_library_work(delay, reason)

    def library_work_due(self, now=None):
        now = time.time() if now is None else float(now)
        return bool(self._next_library_work and now >= self._next_library_work)

    def consume_library_work(self):
        reason = self._library_reason or 'scheduled'
        self._next_library_work = 0.0
        self._library_reason = ''
        # Kodi watched updates have an independent debounce deadline. A
        # catalogue-maintenance pass must never cancel that pending work.
        return reason

    def check_library_wake_signal(self):
        token = WINDOW.getProperty(LIBRARY_WAKE_PROPERTY)
        if token and token != self._last_wake_token:
            self._last_wake_token = token
            self.schedule_library_work(
                LIBRARY_REQUEST_DELAY,
                'library_request',
                debounce=True,
            )
            return True
        return False

    def onSettingsChanged(self):
        try:
            xbmc.log(
                'plugin.video.abc_iviewjc - Settings saved: '
                'library_integration={} library_scope={}'.format(
                    library_enabled(),
                    library_scope(),
                ),
                xbmc.LOGINFO,
            )
            self.library._next_tick = 0
            self.schedule_library_work(
                LIBRARY_REQUEST_DELAY,
                'settings_changed',
                debounce=True,
            )
        except Exception as exc:
            xbmc.log(
                'plugin.video.abc_iviewjc - Settings change handling failed: '
                '{}'.format(exc),
                xbmc.LOGERROR,
            )

    def onNotification(self, sender, method, data):
        before_updates = len(self.library._library_updates)
        owned_scan_finished = bool(
            method == 'VideoLibrary.OnScanFinished'
            and self.library._owned_scan_active
        )
        self.library.handle_notification(method, data)
        after_updates = len(self.library._library_updates)

        if after_updates > before_updates:
            self.schedule_kodi_watch_work(KODI_UPDATE_DELAY)
            log.info(
                'ABC iView Kodi library watched update queued; '
                'processing after {:.1f} seconds'.format(KODI_UPDATE_DELAY)
            )
            return

        # Start notifications are state markers only. Scheduling a tick for
        # every ScanStarted/ScanFinished pair can create a feedback loop when
        # Kodi's cleanonupdate option performs an automatic clean after scans.
        if method == 'VideoLibrary.OnRemove':
            self.schedule_library_work(
                LIBRARY_FOLLOWUP_DELAY,
                method,
                debounce=True,
            )
        elif (
            method == 'VideoLibrary.OnCleanFinished'
            and self.library._scan_needed
        ):
            # Only an add-on-requested clean needs the generated source scanned
            # again. External/automatic cleans do not schedule another scan.
            self.schedule_library_work(
                LIBRARY_FOLLOWUP_DELAY,
                method,
                debounce=True,
            )
        elif (
            method == 'VideoLibrary.OnScanFinished'
            and not owned_scan_finished
            and library_enabled()
        ):
            # Kodi's normal Update library command is an external scan. Once it
            # finishes, run one ABC reconciliation. If that pass changes the
            # generated files it may request one add-on-owned scan; completion
            # of that owned scan is ignored, so no scan/clean feedback loop can
            # form.
            self.schedule_library_work(
                LIBRARY_FOLLOWUP_DELAY,
                'external_scan_finished',
                debounce=True,
            )

    def _sync_state(self, item, playcount):
        if not item or not item.get('show_id') or not item.get('house_number'):
            return False

        watched = int(playcount or 0) > 0
        now = time.time()
        key = item.get('key') or '{}:{}'.format(
            item['show_id'],
            item['house_number'],
        )

        last_key, last_watched, last_time = self._last_write
        if (
            last_key == key
            and last_watched == watched
            and now - last_time < 3
        ):
            return False

        self._last_write = (key, watched, now)
        normalised = {
            'key': key,
            'show_id': str(item['show_id']),
            'house_number': str(item['house_number']),
            'duration': int(item.get('duration') or 0),
        }

        set_pending_state(
            normalised,
            1 if watched else 0,
            source='playcount_transition',
        )
        queued = enqueue_action(
            normalised,
            1 if watched else 0,
            source='playcount_transition',
        )

        if watched:
            request_follow_show(
                normalised['show_id'],
                source='watched_toggle',
                house_number=normalised['house_number'],
            )
        else:
            request_reconcile_show(
                normalised['show_id'],
                source='unwatched_toggle',
            )

        request_episode_state(
            normalised['show_id'],
            normalised['house_number'],
            1 if watched else 0,
            duration=normalised['duration'],
            source='watched_toggle',
        )
        diag(
            'library_sync_state_queued',
            item=normalised,
            watched=watched,
            action_queued=queued,
        )
        return True

    def process_library_watched_updates(self):
        changed = False
        for item, playcount in self.library.pop_watched_updates():
            diag(
                'library_playcount_transition',
                item=item,
                playcount=playcount,
            )
            changed = self._sync_state(item, playcount) or changed
        return changed

    def process_action_queue(self, max_actions=MAX_ACTION_BURST):
        processed = False
        for _unused in range(max(1, int(max_actions or 1))):
            action = pop_due_action()
            if not action:
                break

            processed = True
            watched = int(action.get('playcount') or 0) > 0
            item = {
                'key': action.get('key'),
                'show_id': action.get('show_id'),
                'house_number': action.get('house_number'),
                'duration': int(action.get('duration') or 0),
            }
            attempts = int(action.get('attempts') or 0)

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

                log.info(
                    'Kodi built-in watched state synced to ABC for {}: {}'.format(
                        item['house_number'],
                        'watched' if watched else 'unwatched',
                    )
                )
            except Exception as exc:
                attempts += 1
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
                        'ABC iView+',
                        'Unable to sync watched status: {}'.format(exc),
                        xbmcgui.NOTIFICATION_ERROR,
                        5000,
                    )

        return processed


library_service = LibraryIntegration()
monitor = ABCMonitor(library_service)
player = ABCPlayer(monitor)

monitor.schedule_library_work(STARTUP_DELAY, 'startup')
next_action_work = time.time() + STARTUP_DELAY
next_request_safety_check = time.time() + LIBRARY_REQUEST_SAFETY_INTERVAL

while not monitor.abortRequested():
    now = time.time()
    playing = player.isPlayingVideo()

    if playing:
        player.update_position()
        player.sync_progress()

    monitor.check_library_wake_signal()

    # The window-property token is the normal immediate wake mechanism. This
    # low-frequency disk check is only a safety net for requests created in a
    # separate interpreter where the token was missed. It never calls the
    # library service unless an actual request file exists.
    if now >= next_request_safety_check:
        next_request_safety_check = (
            now + LIBRARY_REQUEST_SAFETY_INTERVAL
        )
        if has_library_requests():
            log.info(
                'ABC iView durable library request found by safety check'
            )
            monitor.schedule_library_work(
                0,
                'library_request_safety',
                debounce=True,
            )

    if next_action_work and now >= next_action_work:
        monitor.process_action_queue(max_actions=MAX_ACTION_BURST)
        next_action_work = next_action_due_at()

    # Kodi-library watched changes use their own event deadline. They are
    # resolved and written to ABC without waiting for, or invoking, a catalogue
    # maintenance tick.
    if monitor.kodi_watch_work_due(now):
        monitor.consume_kodi_watch_work()
        changed = monitor.process_library_watched_updates()
        if changed:
            monitor.process_action_queue(max_actions=MAX_ACTION_BURST)
            next_action_work = next_action_due_at()

        # If Kodi's row was not resolvable yet, LibraryIntegration retains the
        # young notification and this bounded retry runs two seconds later.
        if library_service._library_updates:
            monitor.schedule_kodi_watch_work(2.0)

    if monitor.library_work_due(now):
        reason = monitor.consume_library_work()
        log.info(
            'ABC iView running scheduled library work: {}'.format(reason)
        )

        try:
            library_service.tick(playing=playing)
            # Requests created by this same pass have already been drained;
            # consume their wake token so they do not schedule a redundant
            # empty pass.
            monitor._last_wake_token = WINDOW.getProperty(
                LIBRARY_WAKE_PROPERTY
            )

            # Schedule exactly the next real transaction or maintenance
            # deadline. There is no idle LibraryIntegration.tick() poll.
            next_library_work = library_service.next_work_at(
                playing=playing,
            )
            if next_library_work:
                monitor.schedule_library_at(
                    next_library_work,
                    'library_followup',
                )
        except Exception as exc:
            # A transient Kodi JSON-RPC or filesystem error must not terminate
            # the long-running service. Retry once as a normal delayed event.
            log.exception(
                'ABC iView scheduled library work failed ({}): {}'.format(
                    reason,
                    exc,
                )
            )
            library_service._next_tick = 0
            monitor.schedule_library_work(
                LIBRARY_TICK_RETRY_DELAY,
                'library_tick_retry',
            )

    # A plugin-browser native watched action creates both a durable library
    # request and an ABC action. The wake token schedules the request path; use
    # the same event to drain the action immediately rather than polling it.
    if monitor._next_library_work and not next_action_work:
        due = next_action_due_at()
        if due:
            next_action_work = due

    if monitor.waitForAbort(SERVICE_LOOP_INTERVAL):
        break
