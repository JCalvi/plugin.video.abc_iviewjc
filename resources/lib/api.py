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


PROFILE_API = 'https://mylogin-api.abc.net.au/latest'
PRODUCT_ID = 'iview'
DEVICE_LINK_URL = 'https://account.abc.net.au/device-link?source=link-via-qr-code'
PROFILE_API_KEY = 'B3_q2UkUxt9jLgkPqIbS1r33MslNWwQRrnn-CCmZjpiftEKdgoyVa639spF8xbe1dVi'


class APIError(Error):
    pass


class API(object):
    def new_session(self):
        self.logged_in = False
        self._session = Session(HEADERS)
        self._set_authentication()

        if self.logged_in:
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
                self._get_seesaw_token(uid)
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
            log.error('ABC ctv/link response: {}'.format(data))
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

        response = self._session.get(SEESAW_URL + '/v2/token', params=params)
        token_data = self._json(response, 'ABC account token')
        auth = token_data.get('auth') or token_data

        access_token = auth.get('access_token') or auth.get('accessToken')
        if not access_token:
            log.error('ABC Seesaw token response: {}'.format(token_data))
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

    def get_watchlist(self):
        self._refresh_token()
        uid = userdata.get('uid')
        data = self._session.get(SEESAW_URL + '/v1/saved/watchlist/show', params={
            'source': 'iview', 'slug': 'watchlist', 'raw': 1, 'done': 0, 'UID': uid,
        }).json()
        ids = [str(x.get('key')) for x in data.get('data', []) if x.get('key')]
        if not ids:
            return []
        shows = self._session.get(API_BASE_URL + '/v3/shows/{}'.format(','.join(ids))).json()
        order = {value: index for index, value in enumerate(ids)}
        return sorted(shows if isinstance(shows, list) else [], key=lambda x: order.get(str(x.get('id')), 99999))

    def get_continue_watching(self):
        self._refresh_token()
        uid = userdata.get('uid')
        history = self._session.get(SEESAW_URL + '/v1/history/video/recent', params={
            'source': 'iview', 'slug': 'watchlist', 'raw': 1, 'limit': 20,
            'done': 0, 'UID': uid,
        }).json().get('data', [])
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

        self.logged_in = False
        self._session = Session(HEADERS)
