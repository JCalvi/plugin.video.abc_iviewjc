import re
import time

import xbmcgui

from slyguy import plugin, gui, signals, monitor, inputstream, log
from slyguy.constants import ROUTE_LIVE_TAG

from .api import API
from .language import _
from .settings import settings
from .diagnostics import diagnostic_event
from .libraryintegration import (
    is_manual_show,
    request_episode_state,
    request_follow_show,
    request_reconcile_show,
    set_manual_show,
)
from .watchaction import (
    apply_pending_state,
    begin_folder_watch_render,
    current_folder_url,
    detect_folder_watch_changes,
    enqueue_action,
    get_folder_watch_state,
    record_folder_watch_item,
    set_pending_state,
)

api = API()
_RENDER_TOKEN = ''


@signals.on(signals.BEFORE_DISPATCH)
def before_dispatch():
    global _RENDER_TOKEN
    _RENDER_TOKEN = str(time.time_ns())

    diagnostic_event('IVIEW115LIST', 'before_dispatch_begin')

    # Kodi's native watched job commits the plugin URL's playcount, clears its
    # directory cache and reloads the same folder.  That new dispatch is the
    # event: compare the committed rows with the manifest from the previous
    # render before obtaining fresh ABC history.
    folder_url = current_folder_url()
    for action in detect_folder_watch_changes(
        folder_url,
        allow_commit_retry=True,
    ):
        item = action['item']
        playcount = 1 if int(action['playcount'] or 0) > 0 else 0

        set_pending_state(item, playcount, source='plugin_native_watched')
        enqueue_action(item, playcount, source='plugin_native_watched')

        if playcount:
            request_follow_show(
                item['show_id'],
                source='plugin_native_watched',
                house_number=item['house_number'],
            )
        else:
            request_reconcile_show(
                item['show_id'],
                source='plugin_native_unwatched',
            )
        request_episode_state(
            item['show_id'],
            item['house_number'],
            playcount,
            duration=item.get('duration') or 0,
            source='plugin_native_watched',
        )

        diagnostic_event(
            'IVIEW115FIX',
            'native_watched_change',
            house_number=item['house_number'],
            playcount=playcount,
            previous_signature=action.get('previous_signature'),
            current_signature=action.get('current_signature'),
        )

    # Start the replacement manifest only after the previous render has
    # been compared with Kodi's committed watched rows.
    begin_folder_watch_render(folder_url)

    # The service runs in a separate interpreter and may have changed ABC
    # history since the previous folder render. Refresh once per dispatch so a
    # newly watched episode cannot remain displayed as a stale resume item for
    # the API cache's five-minute lifetime.
    api.clear_history_cache()
    api.new_session()
    plugin.logged_in = api.logged_in
    diagnostic_event(
        'IVIEW115LIST',
        'before_dispatch_complete',
        logged_in=api.logged_in,
    )


@plugin.route('')
def home(**kwargs):
    folder = plugin.Folder(cacheToDisc=False)
    if api.logged_in:
        folder.add_item(label='[B]My Watchlist[/B]', path=plugin.url_for(watchlist))
        folder.add_item(label='[B]Continue Watching[/B]', path=plugin.url_for(continue_watching))
    else:
        folder.add_item(label='[B]Login[/B]', path=plugin.url_for(login), bookmark=False)

    folder.add_item(label='[B]Watch Live[/B]', path=plugin.url_for(live_streams))
    try:
        for cat in api.get_categories():
            path = cat.get('path')
            if not path:
                continue
            art = _art(cat)
            folder.add_item(label=cat.get('title', ''), art=art, path=plugin.url_for(category, path=path))
    except Exception as exc:
        log.exception('Failed to load ABC categories: {}'.format(exc))

    folder.add_item(label='[B]Search[/B]', path=plugin.url_for(search))
    folder.add_item(
        label='[B]Library Integration Settings[/B]',
        path=plugin.url_for(library_integration_settings),
        _kiosk=False,
        bookmark=False,
    )
    if settings.getBool('bookmarks', True):
        folder.add_item(label=_.BOOKMARKS, path=plugin.url_for(plugin.ROUTE_BOOKMARKS), bookmark=False)
    if api.logged_in:
        folder.add_item(label='Logout', path=plugin.url_for(logout), _kiosk=False, bookmark=False)
    folder.add_item(label=_.SETTINGS, path=plugin.url_for(plugin.ROUTE_SETTINGS), _kiosk=False, bookmark=False)
    return folder


@plugin.route()
def library_integration_settings(**kwargs):
    from .libraryconfigui import main
    main()



@plugin.route()
def login(**kwargs):
    data = api.device_code()

    if data.get('alreadyLinked'):
        gui.notification('ABC Account linked')
        gui.refresh()
        return

    verify_url = data['verifyURI']
    code = str(data['userCode']).upper()
    message = (
        '[B]Code: {code}[/B]\n\n'
        'Open {url} on your phone or computer and enter the code.'
    ).format(code=code, url=verify_url)

    with gui.progress_qr(
        data['qrCodeURI'],
        message,
        heading='Link ABC Account - {}'.format(code),
    ) as progress:
        expires = max(1, int(data.get('expiresIn') or 600))
        interval = max(1, int(data.get('interval') or 3))

        for elapsed in range(expires):
            if progress.iscanceled() or monitor.waitForAbort(1):
                return

            progress.update(int((elapsed / float(expires)) * 100))

            if elapsed % interval == 0 and api.device_login(data['deviceCode']):
                gui.notification('ABC Account linked')
                gui.refresh()
                return

    gui.notification('ABC linking code expired')


@plugin.route()
def logout(**kwargs):
    if gui.yes_no('Are you sure you want to log out?'):
        api.logout()
        gui.refresh()


@plugin.route()
def add_to_library(show_id, title='', thumb='', **kwargs):
    changed = set_manual_show(show_id, True)
    gui.notification(
        'Queued for Kodi library import' if changed
        else 'Already in manual Kodi library selection',
        heading=title or 'ABC iView+',
        icon=thumb or None,
    )
    gui.refresh()


@plugin.route()
def remove_from_library(show_id, title='', thumb='', **kwargs):
    changed = set_manual_show(show_id, False)
    gui.notification(
        'Queued for removal from Kodi library' if changed
        else 'Not in manual Kodi library selection',
        heading=title or 'ABC iView+',
        icon=thumb or None,
    )
    gui.refresh()


@plugin.route()
@plugin.login_required()
def add_watchlist(show_id, title='', thumb='', **kwargs):
    try:
        api.add_to_watchlist(show_id)
    except Exception as exc:
        log.exception('Unable to add show {} to ABC watchlist: {}'.format(show_id, exc))
        gui.notification(
            'Unable to add to My Watchlist: {}'.format(exc),
            heading=title or 'ABC iView+',
            icon=thumb or None,
        )
        return

    request_reconcile_show(show_id, source='watchlist_added')
    gui.notification(
        'Added to My Watchlist',
        heading=title or 'ABC iView+',
        icon=thumb or None,
    )
    gui.refresh()


@plugin.route()
@plugin.login_required()
def remove_watchlist(show_id, title='', thumb='', **kwargs):
    try:
        api.remove_from_watchlist(show_id)
    except Exception as exc:
        log.exception('Unable to remove show {} from ABC watchlist: {}'.format(show_id, exc))
        gui.notification(
            'Unable to remove from My Watchlist: {}'.format(exc),
            heading=title or 'ABC iView+',
            icon=thumb or None,
        )
        return

    request_reconcile_show(show_id, source='watchlist_removed')
    gui.notification(
        'Removed from My Watchlist',
        heading=title or 'ABC iView+',
        icon=thumb or None,
    )
    gui.refresh()


@plugin.route()
@plugin.login_required()
def mark_watched(show_id, house_number, duration=0, title='', thumb='', **kwargs):
    try:
        try:
            progress = int(float(duration or 0))
        except Exception:
            progress = 0
        api.mark_video_watched(show_id, house_number, progress=progress)
        request_follow_show(
            show_id,
            source='mark_watched_route',
            house_number=house_number,
        )
        request_episode_state(
            show_id,
            house_number,
            1,
            duration=progress,
            source='mark_watched_route',
        )
    except Exception as exc:
        log.exception('Unable to mark {} watched: {}'.format(house_number, exc))
        gui.notification('Unable to mark watched on ABC iview: {}'.format(exc), heading=title or 'ABC iView+')
        return

    gui.notification('Marked watched on ABC iview', heading=title or 'ABC iView+', icon=thumb or None)
    gui.refresh()


@plugin.route()
@plugin.login_required()
def mark_unwatched(show_id, house_number, title='', thumb='', **kwargs):
    try:
        api.mark_video_unwatched(show_id, house_number)
        request_episode_state(
            show_id,
            house_number,
            0,
            source='mark_unwatched_route',
        )
        request_reconcile_show(
            show_id,
            source='mark_unwatched_route',
        )
    except Exception as exc:
        log.exception('Unable to mark {} unwatched: {}'.format(house_number, exc))
        gui.notification('Unable to mark unwatched on ABC iview: {}'.format(exc), heading=title or 'ABC iView+')
        return

    gui.notification('Marked unwatched on ABC iview', heading=title or 'ABC iView+', icon=thumb or None)
    gui.refresh()


@plugin.route()
def watchlist(**kwargs):
    folder = plugin.Folder('My Watchlist')
    for item in api.get_watchlist():
        parsed = _parse_show(item)
        if parsed:
            folder.add_items([parsed])
    return folder


@plugin.route()
def continue_watching(**kwargs):
    folder = plugin.Folder('Continue Watching')
    for item in api.get_continue_watching():
        parsed = _parse_video(item)
        if parsed:
            resume = item.get('_resume') or 0
            if resume:
                parsed.resume_from = resume
            folder.add_items([parsed])
    return folder


@plugin.route()
def category(path, **kwargs):
    folder = plugin.Folder()
    for col in api.get_collections('/' + path.lstrip('/')):
        title = col.get('title', '')
        cid = col.get('id')
        if title and cid:
            folder.add_item(label=title, art=_art(col), path=plugin.url_for(collection, collection_id=cid))
    return folder


@plugin.route()
def collection(collection_id, **kwargs):
    folder = plugin.Folder()
    items = api.get_collection(collection_id)
    for item in items:
        parsed = _parse_item(item)
        if parsed:
            folder.add_items([parsed])
    return folder


@plugin.route()
def series(url, from_series_list=False, **kwargs):
    data = api.get_series(url)
    embedded = data.get('_embedded', {}) if isinstance(data, dict) else {}
    highlight = embedded.get('highlightVideo') if isinstance(embedded, dict) else None
    if isinstance(highlight, dict) and not embedded.get('selectedSeries'):
        item = _parse_video(highlight, fanart=_thumb(data))
        folder = plugin.Folder(data.get('showTitle') or data.get('title', ''), fanart=_thumb(data))
        if item:
            folder.add_items([item])
        return folder

    # A direct video URL can also be supplied by some collection records.
    if isinstance(data, dict) and (
        data.get('_entity') == 'video'
        or data.get('houseNumber')
        or data.get('type') in ('video', 'episode', 'program')
    ):
        folder = plugin.Folder(data.get('showTitle') or data.get('title', ''), fanart=_thumb(data))
        item = _parse_video(data, fanart=_thumb(data))
        if item:
            folder.add_items([item])
        return folder

    title = data.get('showTitle') or data.get('displayTitle') or data.get('title', '')
    folder = plugin.Folder(title, fanart=_thumb(data))
    series_list = embedded.get('seriesList', [])
    selected = embedded.get('selectedSeries', {})

    # The show endpoint always returns seriesList, even after a particular
    # season has been selected. Use an explicit route flag so selecting a
    # season displays its episodes instead of presenting the season list again.
    show_season_list = len(series_list) > 1 and str(from_series_list).lower() not in ('1', 'true', 'yes')

    if show_season_list:
        selected_id = selected.get('id')
        for row in series_list:
            href = row.get('_links', {}).get('deeplink', {}).get('href')
            if not href:
                continue

            label = row.get('title') or row.get('displayTitle') or 'Season'
            if row.get('id') == selected_id:
                label = '{} (Current)'.format(label)

            folder.add_item(
                label=label,
                art=_art(row),
                path=plugin.url_for(series, url=href, from_series_list=True),
            )
    else:
        selected_embedded = selected.get('_embedded', {}) if isinstance(selected, dict) else {}
        episodes = (
            selected_embedded.get('videoEpisodes')
            or selected_embedded.get('videoExtras')
            or []
        )
        for episode in episodes:
            parsed = _parse_video(episode, fanart=_thumb(data))
            if parsed:
                folder.add_items([parsed])

    return folder


@plugin.route()
@plugin.search()
def search(query, **kwargs):
    items = []
    for row in api.search(query):
        parsed = _parse_item(row)
        if parsed:
            items.append(parsed)
    return items, False


@plugin.route()
def live_streams(**kwargs):
    folder = plugin.Folder('Watch Live')
    for item in api.get_livestreams():
        if item.get('type') != 'livestream':
            continue
        href = item.get('_links', {}).get('self', {}).get('href', '')
        hn = item.get('houseNumber', '')
        folder.add_item(label=item.get('showTitle') or item.get('title', ''), art=_art(item),
            info={'plot': item.get('description', ''), 'mediatype': 'video'},
            path=plugin.url_for(play, url=href, house_number=hn, _is_live=True), playable=True)
    return folder


@plugin.route()
def play(url, house_number='', **kwargs):
    is_live = ROUTE_LIVE_TAG in kwargs
    data = api.get_program(url)
    if data.get('playable') is False:
        gui.notification(data.get('playableMessage', 'Content not available'))
        return
    hn = house_number or data.get('houseNumber', '')
    stream_url = _resolve_stream(data, hn)
    if not stream_url:
        gui.notification('Stream not found')
        return
    item = plugin.Item(path=stream_url, inputstream=inputstream.HLS(live=is_live))

    # Share the active ABC episode details with the background playback
    # service. Kodi window properties are process-safe and remain available
    # after this plugin route has returned the resolved stream.
    window = xbmcgui.Window(10000)
    if is_live or not api.logged_in:
        for key in ('show_id', 'house_number', 'duration', 'title'):
            window.clearProperty('abc_iview.{}'.format(key))
    else:
        show_id = _show_id(data)
        window.setProperty('abc_iview.show_id', str(show_id or ''))
        window.setProperty('abc_iview.house_number', str(hn or ''))
        window.setProperty('abc_iview.duration', str(data.get('duration') or ''))
        window.setProperty('abc_iview.title', str(data.get('episodeTitle') or data.get('title') or ''))
    for playlist in data.get('_embedded', {}).get('playlist', []):
        if playlist.get('type') in ('program', 'livestream'):
            captions = playlist.get('captions', {}).get('src-vtt')
            if captions:
                item.subtitles.append(captions)
            break
    return item


def _resolve_stream(data, hn):
    for playlist in data.get('_embedded', {}).get('playlist', []):
        if playlist.get('type') not in ('program', 'livestream'):
            continue
        hls = playlist.get('streams', {}).get('hls', {})
        base = hls.get('1080') or hls.get('720') or hls.get('sd') or hls.get('sd-low')
        if not base:
            continue
        if not hn:
            return base
        try:
            auth = api.get_auth(hn)
            return api._session.get(base, params={'hdnea': auth}).url
        except Exception as exc:
            log.warning('HLS authentication failed: {}'.format(exc))
            return base
    return None


def _parse_item(item):
    if not isinstance(item, dict):
        log.error('ABC COLLECTION ITEM IS NOT AN OBJECT: {}'.format(item))
        return None

    entity = str(item.get('_entity') or '').lower()
    item_type = str(item.get('type') or '').lower()

    # ABC catalogue records use several entity markers across feeds.
    if (
        entity == 'show'
        or item_type in ('show', 'series')
        or item.get('episodeCount') is not None
        or item.get('seriesCount') is not None
    ):
        return _parse_show(item)

    if (
        entity == 'video'
        or item_type in ('video', 'episode', 'program', 'movie', 'clip')
        or item.get('houseNumber')
    ):
        return _parse_video(item)

    links = item.get('_links') if isinstance(item.get('_links'), dict) else {}
    if isinstance(links.get('deeplink'), dict):
        return _parse_show(item)
    if isinstance(links.get('self'), dict):
        return _parse_video(item)

    log.error('UNSUPPORTED ABC COLLECTION ITEM: {}'.format(item))
    return None


def _parse_show(item):
    title = (
        item.get('displayTitle')
        or item.get('showTitle')
        or item.get('seriesTitle')
        or item.get('programTitle')
        or item.get('title')
        or item.get('name')
        or ''
    )
    count = item.get('episodeCount')
    label = '{} ({})'.format(title, count) if title and count else title

    links = item.get('_links') if isinstance(item.get('_links'), dict) else {}
    deeplink = links.get('deeplink') if isinstance(links.get('deeplink'), dict) else {}
    self_link = links.get('self') if isinstance(links.get('self'), dict) else {}
    href = deeplink.get('href') or self_link.get('href') or item.get('path') or item.get('url')

    if not label:
        label = str(item.get('id') or item.get('slug') or 'ABC iview program')
    if not href:
        log.error('ABC SHOW HAS NO LINK: {}'.format(item))
        return None

    parsed = plugin.Item(
        label=label,
        info={
            'title': title or label,
            'plot': item.get('description') or item.get('shortSynopsis', ''),
            'mediatype': 'tvshow',
        },
        art=_art(item),
        path=plugin.url_for(series, url=href),
    )

    show_id = _show_id(item)
    if show_id:
        thumb = _thumb(item) or ''
        if is_manual_show(show_id):
            parsed.context.append((
                'Remove from Kodi Library (manual)',
                'RunPlugin({})'.format(plugin.url_for(
                    remove_from_library,
                    show_id=show_id,
                    title=title or label,
                    thumb=thumb,
                )),
            ))
        else:
            parsed.context.append((
                'Add to Kodi Library (manual)',
                'RunPlugin({})'.format(plugin.url_for(
                    add_to_library,
                    show_id=show_id,
                    title=title or label,
                    thumb=thumb,
                )),
            ))

    if api.logged_in and show_id:
        try:
            in_watchlist = api.is_in_watchlist(show_id)
        except Exception as exc:
            # Catalogue browsing should continue even if Seesaw is temporarily
            # unavailable. In that case no potentially incorrect menu is shown.
            log.warning('Unable to determine watchlist state for show {}: {}'.format(show_id, exc))
        else:
            thumb = _thumb(item) or ''
            if in_watchlist:
                parsed.context.append((
                    'Remove from My Watchlist',
                    'RunPlugin({})'.format(plugin.url_for(
                        remove_watchlist,
                        show_id=show_id,
                        title=title or label,
                        thumb=thumb,
                    )),
                ))
            else:
                parsed.context.append((
                    'Add to My Watchlist',
                    'RunPlugin({})'.format(plugin.url_for(
                        add_watchlist,
                        show_id=show_id,
                        title=title or label,
                        thumb=thumb,
                    )),
                ))

    return parsed


def _parse_video(item, fanart=None):
    title = (
        item.get('showTitle')
        or item.get('seriesTitle')
        or item.get('programTitle')
        or item.get('displayTitle')
        or item.get('title')
        or ''
    )
    subtitle = (
        item.get('episodeTitle')
        or item.get('displaySubtitle')
        or item.get('subtitle')
        or item.get('title')
        or ''
    )
    href = item.get('_links', {}).get('self', {}).get('href', '')
    hn = str(item.get('houseNumber') or '')
    if not href and hn:
        href = '/video/{}'.format(hn)
    if not href:
        return None

    season, episode = _parse_season_episode(subtitle)
    info = {
        'plot': item.get('description', ''),
        'tvshowtitle': title,
        'season': season,
        'episode': episode,
        'duration': item.get('duration'),
        'mpaa': item.get('classification', ''),
        'mediatype': 'episode',
    }

    state = {}
    if api.logged_in and hn:
        try:
            state = api.get_history_state(hn)
        except Exception as exc:
            log.warning('Unable to obtain watched state for {}: {}'.format(hn, exc))

    server_state = dict(state or {})
    state = apply_pending_state(hn, state)

    duration = max(0, int(item.get('duration') or 0))
    progress = max(0, int(state.get('progress') or item.get('_resume') or 0))
    completed = bool(state.get('done'))
    if not completed and duration > 0:
        completed = progress >= duration * 0.90
    server_playcount = 1 if completed else 0

    # Use a versioned, per-dispatch playable URL. Kodi persists watched and
    # resume state against the final plugin URL; a fresh render token prevents
    # an old local row from overriding current ABC account history. The marker
    # and house number still provide stable identity for native watched actions.
    show_id = _show_id(item)
    stable_href = '/video/{}'.format(hn) if hn else href
    play_path = plugin.url_for(
        play,
        url=stable_href,
        house_number=hn,
        show_id=show_id or '',
        iview_watch='4',
        render_token=_RENDER_TOKEN,
    )

    local_playcount, has_kodi_row = get_folder_watch_state(
        hn,
        server_playcount,
    ) if hn else (server_playcount, False)

    # ABC history (plus any pending local action) is authoritative for every
    # fresh list render. A per-dispatch playable URL prevents Kodi from merging
    # an old plugin-row resume/playcount into the replacement ListItem.
    effective_playcount = server_playcount
    info['playcount'] = effective_playcount

    watch_item = {
        'key': '{}:{}'.format(show_id or '', hn),
        'show_id': show_id or '',
        'house_number': hn,
        'duration': int(item.get('duration') or 0),
    }
    if hn and show_id:
        record_folder_watch_item(watch_item, effective_playcount)

    diagnostic_event(
        'IVIEW115LIST',
        'episode_state',
        house_number=hn,
        title=_episode_label(title, subtitle),
        server_state=server_state,
        pending_state=state,
        effective_playcount=effective_playcount,
        kodi_row=has_kodi_row,
        kodi_row_playcount=local_playcount,
        assigned_playcount=info.get('playcount'),
        playable_url=play_path,
    )

    parsed = plugin.Item(
        label=_episode_label(title, subtitle) or hn or 'ABC iview episode',
        info=info,
        art={'thumb': _thumb(item), 'fanart': fanart or _thumb(item)},
        path=play_path,
        playable=True,
    )

    # A completed item has no resume point. Only genuine incomplete
    # playback receives a resume position.
    if progress and not effective_playcount:
        parsed.resume_from = progress

    return parsed


def _show_id(item):
    links = item.get('_links') if isinstance(item.get('_links'), dict) else {}
    show_link = links.get('show') if isinstance(links.get('show'), dict) else {}

    # Series/season records use a composite id such as 316110-2, while the
    # Seesaw watchlist requires the parent show's numeric id.
    value = (
        show_link.get('id')
        or item.get('showId')
        or item.get('showID')
        or item.get('show_id')
    )
    if value is None:
        value = item.get('id')

    if value is None or value == '':
        return None
    return str(value)


def _thumb(item):
    if item.get('thumbnail'):
        return item['thumbnail']
    if item.get('logoUrl'):
        return item['logoUrl']
    images = item.get('images') or []
    preferred = ('seriesThumbnail', 'titledThumbnail', 'thumb')
    for name in preferred:
        for image in images:
            if image.get('name') == name or image.get('id') == name or image.get('type') == name:
                return image.get('url')
    return images[0].get('url') if images else None


def _art(item):
    thumb = _thumb(item)
    return {'thumb': thumb, 'fanart': thumb}


def _episode_label(show, subtitle):
    if not subtitle or subtitle == show:
        return show
    return '{}: {}'.format(show, subtitle)


def _parse_season_episode(text):
    if not text:
        return None, None
    patterns = [
        r'^[Ss]eries\s?(?P<s>\w+):?\s[Ee]p(?:isode)?:?\s?(?P<e>\d+)',
        r'^[Ee]p(?:isode)?:?\s?(?P<e>\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            groups = match.groupdict()
            try: season = int(groups.get('s')) if groups.get('s') else None
            except Exception: season = None
            try: episode = int(groups.get('e')) if groups.get('e') else None
            except Exception: episode = None
            return season, episode
    return None, None
