import hashlib
import hmac
import time
import uuid

from slyguy import userdata, log
from slyguy.session import Session
from slyguy.exceptions import Error

from .constants import (
    API_BASE_URL, SEESAW_URL, AUTH_PATH, AUTH_PARAMS,
    SECRET, DRM_AUTH_CLIENT_ID, HEADERS,
)
from .language import _
from .diagnostics import diagnostic_event, redact


PROFILE_API = 'https://mylogin-api.abc.net.au/latest'
PRODUCT_ID = 'iview'
DEVICE_LINK_URL = 'https://account.abc.net.au/device-link?source=link-via-qr-code'
PROFILE_API_KEY = 'B3_q2UkUxt9jLgkPqIbS1r33MslNWwQRrnn-CCmZjpiftEKdgoyVa639spF8xbe1dVi'


class APIError(Error):
    pass


class API(object):
    def _diag(self, event, **fields):
        diagnostic_event(
            'IVIEW115API',
            event,
            **fields
        )

    def new_session(self):
        self._diag(
            'new_session_begin',
            has_uid=bool(userdata.get('uid')),
            has_access_token=bool(userdata.get('access_token')),
            token_expires=userdata.get('token_expires'),
        )
        self.logged_in = False
        self._session = Session(HEADERS)
        self._set_authentication()
        self._history = None
        self._history_expires = 0

        if self.logged_in:
            self._diag('new_session_existing_token', logged_in=True)
            return

        # A linked TV can be restored from its persistent deviceId. The
        # ctv/status response contains the account UID; Seesaw accepts that UID
        # directly for the initial token request.
        try:
            uid = userdata.get('uid')
            if not uid:
                status = self._get_link_status(self._device_id())
                uid = status.get('UID') or status.get('uid')
                if uid:
                    userdata.set('uid', uid)

            if uid:
                self._diag('new_session_refresh_token_begin', has_uid=True)
                self._get_seesaw_token(uid)
                self._diag(
                    'new_session_refresh_token_complete',
                    logged_in=self.logged_in,
                )
        except Exception as exc:
            log.warning('Unable to restore ABC account session: {}'.format(exc))

    def _set_authentication(self):
        token = userdata.get('access_token')
        uid = userdata.get('uid')
        if token and uid:
            self._session.headers.update({
                'Authorization': 'Bearer {}'.format(token),
            })
            self.logged_in = True

    def _device_id(self):
        device_id = userdata.get('device_id')
        if not device_id:
            device_id = str(uuid.uuid4())
            userdata.set('device_id', device_id)
        return device_id

    def _json(self, response, action):
        try:
            data = response.json()
        except Exception:
            raise APIError(
                '{} returned HTTP {} with no valid JSON response'.format(
                    action, getattr(response, 'status_code', '?')
                )
            )

        if getattr(response, 'status_code', 200) >= 400:
            message = (
                data.get('message')
                or data.get('errorMessage')
                or data.get('error')
                or data.get('detail')
                or '{} failed with HTTP {}'.format(action, response.status_code)
            )
            if isinstance(message, (list, tuple)):
                message = '; '.join(str(x) for x in message)
            elif not isinstance(message, str):
                message = str(message)
            raise APIError(message)

        return data

    def device_code(self):
        """Start the official ABC Connected-TV linking flow.

        The Android TV application uses the ABC Profile TV API endpoints:
          POST latest/ctv/link
          GET  latest/ctv/status
          POST latest/ctv/unlink

        The link response contains code, deviceId and qrCodeImageUrl.
        """
        device_id = self._device_id()
        payload = {
            'productId': PRODUCT_ID,
            'deviceId': device_id,
            'deviceName': 'Kodi',
        }

        response = self._session.post(
            PROFILE_API + '/ctv/link',
            json=payload,
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'Origin': 'https://iview.abc.net.au',
                'Referer': 'https://iview.abc.net.au/',
                'X-API-KEY': PROFILE_API_KEY,
            },
        )
        if response.status_code >= 400:
            try:
                error_data = response.json()
            except Exception:
                error_data = {}
            message = (
                error_data.get('message')
                or error_data.get('errorMessage')
                or error_data.get('error')
                or ''
            )
            if 'already connected' in str(message).lower():
                status = self._get_link_status(device_id)
                uid = status.get('UID') or status.get('uid')
                if uid:
                    userdata.set('uid', uid)
                    self._get_seesaw_token(uid)
                    return {
                        'alreadyLinked': True,
                        'userCode': '',
                        'deviceCode': device_id,
                        'requestId': status.get('requestId'),
                        'verifyURI': '',
                        'qrCodeURI': '',
                        'expiresIn': 1,
                        'interval': 1,
                    }

        data = self._json(response, 'ABC device linking')

        # The production API has used both a direct object and a data wrapper.
        info = data.get('data') if isinstance(data.get('data'), dict) else data
        code = info.get('code') or info.get('linkCode')
        returned_device_id = info.get('deviceId') or device_id
        request_id = info.get('requestId')
        verify_url = info.get('url') or DEVICE_LINK_URL
        if not verify_url.startswith(('http://', 'https://')):
            verify_url = 'https://' + verify_url.lstrip('/')
        qr_image = info.get('qrCodeImageUrl') or info.get('qrCodeUrl')

        if not code:
            log.error('ABC ctv/link response: {}'.format(redact(data)))
            raise APIError('ABC did not return a TV linking code')

        userdata.set('pending_device_id', returned_device_id)
        if request_id:
            userdata.set('pending_request_id', request_id)

        expires_in = info.get('expiresIn') or info.get('expires')
        if not expires_in and info.get('expiryAt'):
            try:
                from datetime import datetime, timezone
                expiry = datetime.fromisoformat(info['expiryAt'].replace('Z', '+00:00'))
                expires_in = max(1, int((expiry - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                expires_in = None

        return {
            'userCode': str(code).upper(),
            'deviceCode': returned_device_id,
            'requestId': request_id,
            'verifyURI': verify_url,
            'qrCodeURI': qr_image or verify_url,
            'expiresIn': int(expires_in or 600),
            'interval': int(info.get('interval') or 3),
        }

    def _get_link_status(self, device_id, request_id=None):
        params = {
            'productId': PRODUCT_ID,
            'deviceId': device_id,
        }
        if request_id:
            params['requestId'] = request_id

        response = self._session.get(
            PROFILE_API + '/ctv/status',
            params=params,
            headers={'Accept': 'application/json'},
        )

        if response.status_code in (204, 404):
            return {}

        data = self._json(response, 'ABC device-link status')
        return data.get('data') if isinstance(data.get('data'), dict) else data

    def device_login(self, device_code):
        """Poll the official TV-link status endpoint."""
        request_id = userdata.get('pending_request_id')
        status = self._get_link_status(device_code, request_id=request_id)

        if not status:
            return False

        state = str(
            status.get('status')
            or status.get('state')
            or status.get('linkingStatus')
            or ''
        ).lower()

        if state in ('pending', 'waiting', 'unlinked', 'not_linked'):
            return False

        uid = status.get('UID') or status.get('uid')
        if not uid:
            return False

        userdata.set('uid', uid)

        # Confirmed by the captured iview web flow: the first Seesaw token
        # request is GET /v2/token?UID=<uid>&source=iview, with no UID
        # signature or prior bearer token required.
        self._get_seesaw_token(uid)

        userdata.delete('pending_device_id')
        userdata.delete('pending_request_id')
        return True

    def _get_seesaw_token(self, uid):
        params = {
            'UID': uid,
            'source': 'iview',
        }

        # The token endpoint must be called without an existing bearer token.
        # Leaving an expired Authorization header attached causes ABC to return
        # HTTP 401 instead of issuing a fresh access token.
        authorization = self._session.headers.pop('Authorization', None)
        try:
            response = self._session.get(SEESAW_URL + '/v2/token', params=params)
        finally:
            if authorization:
                self._session.headers['Authorization'] = authorization

        token_data = self._json(response, 'ABC account token')
        auth = token_data.get('auth') or token_data

        access_token = auth.get('access_token') or auth.get('accessToken')
        if not access_token:
            log.error('ABC Seesaw token response: {}'.format(redact(token_data)))
            raise APIError('ABC did not return an account access token')

        expiry = auth.get('expiry') or auth.get('expires_at')
        if not expiry:
            expiry = time.time() + int(auth.get('expires_in') or 3500)

        userdata.set('access_token', access_token)
        userdata.set('refresh_token', auth.get('refresh_token') or auth.get('refreshToken'))
        userdata.set('token_expires', int(expiry) - 30)

        self._session.headers.update({
            'Authorization': 'Bearer {}'.format(access_token),
        })
        self.logged_in = True

    def _refresh_token(self, force=False):
        uid = userdata.get('uid')
        if not uid:
            return False

        if (
            not force
            and self.logged_in
            and userdata.get('access_token')
            and userdata.get('token_expires', 0) > time.time()
        ):
            return True

        self._get_seesaw_token(uid)
        return True

    def _seesaw_request(self, method, url, action, **kwargs):
        """Make an authenticated Seesaw request and retry once on HTTP 401."""
        self._refresh_token()
        response = getattr(self._session, method)(url, **kwargs)

        if getattr(response, 'status_code', 200) == 401:
            self._refresh_token(force=True)
            response = getattr(self._session, method)(url, **kwargs)

        return self._json(response, action)

    def get_categories(self):
        data = self._session.get(API_BASE_URL + '/v2/navigation/mobile').json()
        categories = []
        for section in data.get('items', []):
            if section.get('id') in ('channels', 'categories'):
                categories.extend(section.get('items', []))
        return categories

    def get_collections(self, category_path):
        data = self._session.get(API_BASE_URL + '/v2' + category_path).json()
        return data.get('_embedded', {}).get('collections', [])

    def get_collection(self, collection_id):
        data = self._session.get(API_BASE_URL + '/v2/collection/{}'.format(collection_id)).json()
        return data.get('items', [])

    def get_series(self, series_url):
        return self._session.get(API_BASE_URL + '/v2{}?embed=seriesList,selectedSeries'.format(series_url)).json()

    def get_program(self, program_url):
        return self._session.get(API_BASE_URL + '/v2{}'.format(program_url)).json()

    def search(self, query):
        data = self._session.get(API_BASE_URL + '/v2/search', params={'keyword': query}).json()
        return data.get('results', {}).get('items', [])

    def get_livestreams(self):
        data = self._session.get(API_BASE_URL + '/v2/home').json()
        for collection in data.get('_embedded', {}).get('collections', []):
            if 'watch abc channels live' in (collection.get('title') or '').lower():
                return self.get_collection(collection.get('id'))
        return []

    def _get_watchlist_entries(self):
        uid = userdata.get('uid')
        data = self._seesaw_request('get', SEESAW_URL + '/v1/saved/watchlist/show',
            'ABC watchlist', params={
                'source': 'iview', 'slug': 'watchlist', 'raw': 1, 'done': 0, 'UID': uid,
            })
        return data.get('data', []) if isinstance(data, dict) else []

    def _cache_watchlist_ids(self, ids):
        self._watchlist_ids = set(str(value) for value in ids if value is not None)
        self._watchlist_ids_expires = time.time() + 300

    def clear_watchlist_cache(self):
        self._watchlist_ids = None
        self._watchlist_ids_expires = 0

    def get_watchlist_ids(self, force=False):
        if not self.logged_in:
            return set()

        ids = getattr(self, '_watchlist_ids', None)
        expires = getattr(self, '_watchlist_ids_expires', 0)
        if not force and ids is not None and expires > time.time():
            return set(ids)

        entries = self._get_watchlist_entries()
        self._cache_watchlist_ids(row.get('key') for row in entries if row.get('key'))
        return set(self._watchlist_ids)

    def is_in_watchlist(self, show_id):
        return str(show_id) in self.get_watchlist_ids()

    def get_watchlist(self):
        entries = self._get_watchlist_entries()
        ids = [str(row.get('key')) for row in entries if row.get('key')]
        self._cache_watchlist_ids(ids)
        if not ids:
            return []

        response = self._session.get(API_BASE_URL + '/v3/shows/{}'.format(','.join(ids)))
        shows = self._json(response, 'ABC watchlist shows')
        order = {value: index for index, value in enumerate(ids)}
        return sorted(
            shows if isinstance(shows, list) else [],
            key=lambda item: order.get(str(item.get('id')), 99999),
        )

    def _change_watchlist(self, show_id, add):
        uid = userdata.get('uid')
        show_id = str(show_id)
        url = SEESAW_URL + '/v2/saved/watchlist/show/{}'.format(show_id)
        params = {'UID': uid, 'source': 'iview', 'raw': 1}

        if add:
            data = self._seesaw_request('post', url, 'Add to ABC watchlist',
                params=params, json={})
        else:
            data = self._seesaw_request('delete', url, 'Remove from ABC watchlist',
                params=params, json={})

        # Keep an existing in-memory cache accurate. If it has not yet been
        # loaded, leave it unset so the next listing obtains the complete set.
        ids = getattr(self, '_watchlist_ids', None)
        if ids is not None:
            if add:
                ids.add(show_id)
            else:
                ids.discard(show_id)
            self._watchlist_ids_expires = time.time() + 300

        return data

    def add_to_watchlist(self, show_id):
        return self._change_watchlist(show_id, True)

    def remove_from_watchlist(self, show_id):
        return self._change_watchlist(show_id, False)


    def clear_history_cache(self):
        self._history = None
        self._history_expires = 0

    def _load_history(self, force=False):
        self._diag(
            'history_load_enter',
            force=force,
            logged_in=self.logged_in,
            cache_present=self._history is not None,
            cache_expires_in=(
                self._history_expires - time.time()
                if self._history is not None
                else None
            ),
        )
        if not self.logged_in:
            self._diag('history_load_not_logged_in')
            return {}

        if not force and self._history is not None and self._history_expires > time.time():
            self._diag(
                'history_cache_hit',
                count=len(self._history),
                done_count=sum(
                    1 for value in self._history.values()
                    if value.get('done')
                ),
            )
            return dict(self._history)

        self._refresh_token()
        uid = userdata.get('uid')
        history = {}

        # Seesaw separates completed and incomplete history. Fetch both so
        # catalogue listings can display ABC's watched and resume state.
        for done in (0, 1):
            self._diag('history_fetch_begin', done=done)
            data = self._seesaw_request('get', SEESAW_URL + '/v1/history/video/recent',
                'ABC viewing history', params={
                    'source': 'iview', 'slug': 'watchlist', 'raw': 1,
                    'limit': 500, 'done': done, 'UID': uid,
                })
            rows = data.get('data', []) if isinstance(data, dict) else []
            self._diag(
                'history_fetch_result',
                done=done,
                row_count=len(rows),
                keys=[
                    str(row.get('key'))
                    for row in rows
                    if row.get('key')
                ],
            )
            for row in rows:
                key = row.get('key')
                if not key:
                    continue
                history[str(key)] = {
                    'done': bool(done or row.get('done')),
                    'progress': int(row.get('progress') or 0),
                }

        self._history = history
        self._history_expires = time.time() + 300
        self._diag(
            'history_load_complete',
            count=len(history),
            done_count=sum(
                1 for value in history.values()
                if value.get('done')
            ),
            done_keys=[
                key for key, value in history.items()
                if value.get('done')
            ],
        )
        return dict(history)

    def get_history_state(self, house_number):
        if not house_number:
            return {}
        state = self._load_history().get(str(house_number), {})
        self._diag(
            'history_state',
            house_number=house_number,
            state=state,
        )
        return state

    def _history_request(self, method, url, action, extra_params=None):
        # These are the same standard query values used by ABC's current TV
        # application for Seesaw history updates. History writes are PATCH
        # requests with no JSON body.
        params = {
            'UID': userdata.get('uid'),
            'source': 'iview',
            'slug': 'watchlist',
            'raw': 1,
        }
        if extra_params:
            params.update(extra_params)
        self._diag(
            'history_write_request',
            method=method,
            url=url,
            action=action,
            done=params.get('done'),
        )
        result = self._seesaw_request(
            method,
            url,
            action,
            params=params,
        )
        self._diag(
            'history_write_response',
            method=method,
            url=url,
            result=result,
        )
        return result

    def set_video_progress(self, show_id, house_number, progress, done=False):
        if not show_id or not house_number:
            return False

        progress = max(0, int(progress or 0))
        self._diag(
            'set_video_progress',
            show_id=show_id,
            house_number=house_number,
            progress=progress,
            done=done,
        )
        url = SEESAW_URL + '/v2/history/show/{}/video/{}/progress/{}'.format(
            show_id, house_number, progress
        )
        self._history_request(
            'patch',
            url,
            'Update ABC viewing progress',
            extra_params={'done': '1' if done else '0'},
        )

        if self._history is not None:
            self._history[str(house_number)] = {
                'done': bool(done),
                'progress': progress,
            }
            self._history_expires = time.time() + 300
        return True

    def mark_video_watched(self, show_id, house_number, progress=0):
        if not show_id or not house_number:
            return False

        # ABC's current TV application marks completion through the normal
        # progress endpoint with done=1, rather than a PUT to the /done path.
        if not progress and self._history is not None:
            progress = self._history.get(str(house_number), {}).get('progress') or 0
        return self.set_video_progress(
            show_id,
            house_number,
            max(1, int(progress or 0)),
            done=True,
        )

    def mark_video_unwatched(self, show_id, house_number):
        if not show_id or not house_number:
            return False

        # Resetting progress with done=0 changes only this episode and avoids
        # ABC's show-level history deletion endpoint, which would remove every
        # episode for the show.
        return self.set_video_progress(
            show_id,
            house_number,
            0,
            done=False,
        )

    def get_continue_watching(self):
        uid = userdata.get('uid')
        data = self._seesaw_request('get', SEESAW_URL + '/v1/history/video/recent',
            'ABC continue watching', params={
                'source': 'iview', 'slug': 'watchlist', 'raw': 1, 'limit': 20,
                'done': 0, 'UID': uid,
            })
        history = data.get('data', []) if isinstance(data, dict) else []
        items = []
        for row in history:
            key = row.get('key')
            if not key:
                continue
            try:
                video = self.get_program('/video/{}'.format(key))
                video['_resume'] = row.get('progress', 0)
                items.append(video)
            except Exception:
                continue
        return items

    def get_auth(self, hn):
        ts = str(int(time.time()))
        auth_path = AUTH_PATH + '?' + AUTH_PARAMS.format(ts=ts, hn=hn)
        digest = hmac.new(bytes(SECRET, 'utf-8'), msg=bytes(auth_path, 'utf-8'), digestmod=hashlib.sha256).hexdigest()
        response = self._session.get(API_BASE_URL + auth_path + '&sig=' + digest)
        response.raise_for_status()
        return response.text

    def get_drm_token(self, house_number):
        jwt_data = self._session.post(API_BASE_URL + '/v2/token/jwt', data={'clientId': DRM_AUTH_CLIENT_ID}).json()
        response = self._session.get(API_BASE_URL + '/v2/token/drm/{}'.format(house_number), headers={
            'Authorization': 'Bearer {}'.format(jwt_data.get('token')),
        }).json()
        return response.get('license')

    def logout(self):
        device_id = userdata.get('device_id')
        if device_id:
            try:
                self._session.post(
                    PROFILE_API + '/ctv/unlink',
                    json={
                        'productId': PRODUCT_ID,
                        'deviceId': device_id,
                    },
                    headers={'X-API-KEY': PROFILE_API_KEY},
                )
            except Exception:
                pass

        for key in (
            'uid', 'access_token', 'refresh_token', 'token_expires',
            'uid_signature', 'signature_timestamp', 'pending_device_id',
            'pending_request_id',
        ):
            userdata.delete(key)

        self.clear_watchlist_cache()
        self.clear_history_cache()
        self.logged_in = False
        self._session = Session(HEADERS)
