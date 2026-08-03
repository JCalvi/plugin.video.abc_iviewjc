import datetime
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode


ADDON_URL = 'plugin://plugin.video.abc_iviewjc/'


def text_value(item, *names):
    if not isinstance(item, dict):
        return ''
    for name in names:
        value = item.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ''


def safe_component(value, fallback='Untitled', max_length=120):
    value = str(value or '').strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    if not value:
        value = fallback
    return value[:max_length].rstrip(' .') or fallback


def show_title(item):
    return text_value(
        item,
        'displayTitle', 'showTitle', 'seriesTitle', 'programTitle',
        'title', 'name',
    )


def episode_title(item):
    return text_value(
        item,
        'episodeTitle', 'displaySubtitle', 'subtitle', 'title',
    )


def image_url(item):
    if not isinstance(item, dict):
        return ''
    direct = text_value(item, 'thumbnail', 'logoUrl')
    if direct:
        return direct
    images = item.get('images') or []
    preferred = (
        'seriesThumbnail', 'titledThumbnail', 'poster',
        'landscape', 'thumb',
    )
    for preferred_name in preferred:
        for image in images:
            if not isinstance(image, dict):
                continue
            if (
                image.get('name') == preferred_name
                or image.get('id') == preferred_name
                or image.get('type') == preferred_name
            ):
                return str(image.get('url') or '')
    for image in images:
        if isinstance(image, dict) and image.get('url'):
            return str(image['url'])
    return ''


def link_href(item, *names):
    links = item.get('_links') if isinstance(item, dict) else None
    links = links if isinstance(links, dict) else {}
    for name in names:
        value = links.get(name)
        if isinstance(value, dict) and value.get('href'):
            return str(value['href'])
    return text_value(item, 'path', 'url')


def int_value(item, *names):
    if not isinstance(item, dict):
        return None
    for name in names:
        value = item.get(name)
        if value is None or value == '':
            continue
        try:
            return int(float(value))
        except Exception:
            match = re.search(r'\d+', str(value))
            if match:
                return int(match.group(0))
    return None


def parse_season_episode(text):
    text = str(text or '')
    patterns = (
        r'\b[Ss](?P<s>\d{1,3})\s*[Ee](?P<e>\d{1,4})\b',
        r'\b(?:Series|Season)\s*(?P<s>\d{1,3})\s*[:\-]?\s*'
        r'(?:Episode|Ep)\s*(?P<e>\d{1,4})\b',
        r'\b(?:Episode|Ep)\s*(?P<e>\d{1,4})\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            groups = match.groupdict()
            season = int(groups['s']) if groups.get('s') else None
            episode = int(groups['e']) if groups.get('e') else None
            return season, episode
    return None, None


def series_number(series, fallback=None):
    value = int_value(
        series,
        'seriesNumber', 'seasonNumber', 'season', 'series', 'number',
    )
    if value is not None:
        return value
    title = text_value(series, 'title', 'displayTitle', 'name')
    match = re.search(r'\b(?:Series|Season)\s*(\d{1,3})\b', title, re.I)
    if match:
        return int(match.group(1))
    series_id = str(series.get('id') or '') if isinstance(series, dict) else ''
    match = re.search(r'-(\d{1,3})$', series_id)
    if match:
        return int(match.group(1))
    return fallback


def episode_number(item):
    value = int_value(
        item,
        'episodeNumber', 'episode', 'episodeNo', 'episode_number', 'number',
    )
    if value is not None:
        return value
    _season, episode = parse_season_episode(episode_title(item))
    return episode


def season_number(item):
    value = int_value(
        item,
        'seriesNumber', 'seasonNumber', 'season', 'series', 'series_number',
    )
    if value is not None:
        return value
    season, _episode = parse_season_episode(episode_title(item))
    return season


def aired_date(item):
    value = text_value(
        item,
        'firstAired', 'firstaired', 'airDate', 'aired', 'broadcastDate',
        'releaseDate', 'published', 'pubDate', 'date',
    )
    if not value:
        return ''
    match = re.match(r'(\d{4}-\d{2}-\d{2})', value)
    if match:
        return match.group(1)
    for fmt in ('%d/%m/%Y', '%Y/%m/%d'):
        try:
            return datetime.datetime.strptime(value[:10], fmt).strftime('%Y-%m-%d')
        except Exception:
            pass
    return ''


def duration_seconds(item):
    return max(0, int_value(item, 'duration', 'runtime', 'length') or 0)


def house_number(item):
    return text_value(item, 'houseNumber', 'house_number', 'productionCode')


def episode_href(item):
    href = link_href(item, 'self', 'deeplink')
    hn = house_number(item)
    if not href and hn:
        return '/video/{}'.format(hn)
    return href


def _date_sort_key(item, index):
    value = aired_date(item)
    return (value or '9999-99-99', index)


def build_episode_records(show, series_rows):
    """Normalise ABC series payloads into Kodi episode records.

    series_rows contains selected-series objects. Each may hold
    _embedded.videoEpisodes. Missing episode numbers are allocated in
    chronological order within the series, which keeps the next available
    episode stable for date-based ABC programmes.
    """
    records = []
    used_house_numbers = set()
    show_name = show_title(show) or 'ABC iView'
    show_art = image_url(show)

    prepared = []
    for index, series in enumerate(series_rows or []):
        embedded = series.get('_embedded', {}) if isinstance(series, dict) else {}
        episodes = embedded.get('videoEpisodes') or []
        if not episodes:
            episodes = embedded.get('videoExtras') or []
        if not episodes and (
            house_number(series) or str(series.get('_entity') or '').lower() == 'video'
        ):
            episodes = [series]
        prepared.append((series_number(series), index, series, list(episodes)))

    # Allocate missing season numbers in API order while retaining explicit
    # season numbering (including season 0 specials).
    explicit = {row[0] for row in prepared if row[0] is not None}
    next_season = 1
    normalised_series = []
    for season, index, series, episodes in prepared:
        if season is None:
            while next_season in explicit:
                next_season += 1
            season = next_season
            explicit.add(season)
            next_season += 1
        normalised_series.append((season, index, series, episodes))

    normalised_series.sort(key=lambda row: (row[0], row[1]))

    for season, _series_index, series, episodes in normalised_series:
        explicit_numbers = set()
        for item in episodes:
            number = episode_number(item)
            if number is not None:
                explicit_numbers.add(number)

        indexed = list(enumerate(episodes))
        # Only reorder when numbering is absent. Explicitly numbered feeds keep
        # the API order and their actual episode numbers.
        if not explicit_numbers:
            indexed.sort(key=lambda pair: _date_sort_key(pair[1], pair[0]))

        next_episode = 1
        for _index, item in indexed:
            hn = house_number(item)
            href = episode_href(item)
            if not hn or not href or hn in used_house_numbers:
                continue

            item_season = season_number(item)
            if item_season is None:
                item_season = season

            number = episode_number(item)
            if number is None:
                while next_episode in explicit_numbers:
                    next_episode += 1
                number = next_episode
                explicit_numbers.add(number)
                next_episode += 1

            title = episode_title(item) or 'Episode {}'.format(number)
            records.append({
                'show_id': str(show.get('id') or show.get('showId') or ''),
                'show_title': show_name,
                'show_plot': text_value(show, 'description', 'shortSynopsis'),
                'show_art': show_art,
                'house_number': hn,
                'href': href,
                'season': int(item_season),
                'episode': int(number),
                'title': title,
                'plot': text_value(item, 'description', 'shortSynopsis'),
                'duration': duration_seconds(item),
                'aired': aired_date(item),
                'classification': text_value(item, 'classification', 'mpaa'),
                'thumb': image_url(item) or show_art,
            })
            used_house_numbers.add(hn)

    records.sort(key=lambda row: (row['season'], row['episode'], row['house_number']))
    return records


def plugin_play_url(record):
    query = urlencode({
        '_': 'play',
        'url': record['href'],
        'house_number': record['house_number'],
        'show_id': record['show_id'],
        'duration': record.get('duration') or 0,
        'library': 1,
    })
    return '{}?{}'.format(ADDON_URL, query)


def episode_basename(record):
    title = safe_component(record.get('title'), 'Episode')
    return safe_component(
        '{show} - S{season:02d}E{episode:02d} - {title} [ABC {hn}]'.format(
            show=safe_component(record.get('show_title'), 'ABC iView', 70),
            season=int(record['season']),
            episode=int(record['episode']),
            title=title,
            hn=safe_component(record['house_number'], 'episode', 40),
        ),
        max_length=190,
    )


def _add_text(parent, name, value, **attributes):
    if value is None or value == '':
        return None
    child = ET.SubElement(parent, name, attributes)
    child.text = str(value)
    return child


def tvshow_nfo(show, show_id, records):
    root = ET.Element('tvshow')
    title = show_title(show) or (records[0]['show_title'] if records else 'ABC iView')
    _add_text(root, 'title', title)
    _add_text(root, 'originaltitle', title)
    _add_text(root, 'plot', text_value(show, 'description', 'shortSynopsis'))
    _add_text(root, 'studio', 'ABC')
    _add_text(root, 'status', 'Continuing')
    _add_text(root, 'uniqueid', show_id, type='abciview', default='true')
    art = image_url(show) or (records[0].get('show_art') if records else '')
    if art:
        _add_text(root, 'thumb', art, aspect='poster')
        fanart = ET.SubElement(root, 'fanart')
        _add_text(fanart, 'thumb', art)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def episode_nfo(record):
    root = ET.Element('episodedetails')
    _add_text(root, 'title', record['title'])
    _add_text(root, 'showtitle', record['show_title'])
    _add_text(root, 'season', int(record['season']))
    _add_text(root, 'episode', int(record['episode']))
    _add_text(root, 'plot', record.get('plot'))
    if record.get('duration'):
        _add_text(root, 'runtime', max(1, int(round(record['duration'] / 60.0))))
    _add_text(root, 'aired', record.get('aired'))
    _add_text(root, 'dateadded', record.get('aired'))
    _add_text(root, 'mpaa', record.get('classification'))
    _add_text(
        root,
        'uniqueid',
        record['house_number'],
        type='abciview',
        default='true',
    )
    _add_text(root, 'productioncode', record['house_number'])
    if record.get('thumb'):
        _add_text(root, 'thumb', record['thumb'])
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)
