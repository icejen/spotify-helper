import re
import shelve
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import requests
from flask import Flask, redirect, request

from .util import ElementIterator, chunk_gen, id_from_uri, is_uri
from .config import config


URLS = {
    'auth': 'https://accounts.spotify.com/authorize',
    'token': 'https://accounts.spotify.com/api/token',
    'playlists': 'https://api.spotify.com/v1/me/playlists',
    'playlist': 'https://api.spotify.com/v1/playlists/{playlist_id}',
    'tracks': 'https://api.spotify.com/v1/playlists/{playlist_id}/tracks',
}


def get_url(endpoint, **kwargs):
    """ Get endpoint url, and format it with ids.
    """
    return URLS[endpoint].format(**kwargs)


@dataclass
class Track:
    id: str
    name: str
    submitted_by: str
    spotify_uri: str
    raw_data: dict


@dataclass
class Playlist:
    id: str
    name: str
    _tracks: list
    raw_data: dict

    @property
    def tracks(self):
        if isinstance(self._tracks, list):
            return self._tracks
        tracks = self._tracks['parser'](self._tracks['track_url'])
        return list(map(self.parse_track, tracks))

    def parse_track(self, track):
        return Track(id=track["track"]["id"],
                     name=track["track"]["name"],
                     submitted_by=track["added_by"]["id"],
                     spotify_uri=track["track"]["uri"],
                     raw_data=track)

    def __str__(self):
        return self.name


class SpotifyNerdPlaylistIterator(ElementIterator):
    def __init__(self):
        matcher = re.compile(r"(\d{1,2}/\d{1,2} ?肥宅聽歌團)")
        excluder = re.compile(r"[Rr]ound ?\d+")
        self.spotify_client = SpotifyClient()
        all_playlists = self.spotify_client.all_playlists()
        matched = filter(lambda p: matcher.search(p['name']), all_playlists)
        matched = filter(
            lambda p: not bool(excluder.search(p['name'])), matched)
        self.elements = list(map(self.parse_playlist, matched))
        self.elements.reverse()

    def parse_playlist(self, p):
        return Playlist(
            id=p['id'],
            name=p['name'],
            _tracks={'parser': self.spotify_client.all_tracks_in_playlist,
                     'track_url': p['id']},
            raw_data=p)


class SpotifyAuthClient:
    """ AuthClient facilitates oauth access_token retrieving and refreshing.
        Use AuthClient().get_auth_header() to retrieve header for Http header.
    """

    @property
    def _get_authorization_header(self):
        login = "{}:{}".format(config['APP_CLIENT_ID'],
                               config['APP_CLIENT_SECRET']).encode('ascii')
        return {
            "Authorization": "Basic %s" % b64encode(login).decode('utf-8')
        }

    def _handle_token_response(self, token_response):
        """ Handle access_token and refresh access_token response and save
            the attributes to shelve db.
        """
        if not token_response.ok:
            raise ValueError(str(token_response.json()))

        token_response = token_response.json()
        self.token['access_token'] = token_response['access_token']
        self.token['scope'] = set(token_response['scope'].split(' '))
        if 'refresh_token' in token_response:
            self.token['refresh_token'] = token_response['refresh_token']
        expires_in = token_response['expires_in']
        self.token['expiry'] = datetime.now() + timedelta(seconds=expires_in)

    def _run_oauth_client(self, session):
        """ Run a Flask App to handle Spotify OAuth Callback.
        """
        app = Flask('OAuth Client')
        host = config['OAUTH_CLIENT_HOST']
        port = config['OAUTH_CLIENT_PORT']
        redirect_uri = f"http://{host}:{port}/callback"

        @app.route('/auth')
        def authorization_code():
            """ Redirects /auth to the authorization code webpage.
            """
            params = {
                'client_id': config['APP_CLIENT_ID'],
                'response_type': "code",
                'redirect_uri': redirect_uri,
                'state': session.session_id,
                'scope': ' '.join(config['SCOPE'])
            }
            return redirect('{}?{}'.format(get_url('auth'), urlencode(params)))

        @app.route('/callback')
        def callback():
            """ Handles authorization code callback and later calls
                access_token get.
            """
            error = request.args.get('error')
            if error:
                raise ValueError(error)

            auth_code = request.args.get('code')

            token_request_body = {
                'grant_type': "authorization_code",
                'code': auth_code,
                'redirect_uri': redirect_uri
            }
            token_response = session.post(
                get_url('token'), data=token_request_body,
                headers=self._get_authorization_header)
            self._handle_token_response(token_response)
            shutdown = request.environ.get('werkzeug.server.shutdown')
            if not shutdown:
                raise RuntimeError("Access code succesfully retrieved.")
            shutdown()
            return 'Well, hello there!'

        app.run(host=host, port=port)

    def _refresh_access_token(self, session):
        """ Refreshes access_token and saves the new access_token,
            refresh_token, expiry to shelve db.
        """
        token_request_body = {
            'grant_type': "refresh_token",
            'refresh_token': self.token['refresh_token'],
        }
        token_response = session.post(
            get_url('token'), data=token_request_body,
            headers=self._get_authorization_header)
        self._handle_token_response(token_response)

    def _auth_flow(self, session):
        """ Helper function to start the auth flow for the user. Consider
            popping a window in web server.
        """
        host = config['OAUTH_CLIENT_HOST']
        port = config['OAUTH_CLIENT_PORT']
        auth_uri = f"http://{host}:{port}/auth"
        print(f"Visit {auth_uri} and complete the authorization flow")
        self._run_oauth_client(session)

    def get_auth_header(self):
        """ Get authentication header dict for spotify request session.
        """
        return {'Authorization': "Bearer %s" % self.access_token}

    def __init__(self):
        try:
            self.token = shelve.open('oauth2_token.db', writeback=True)
            auth_session = requests.Session()
            auth_session.session_id = uuid4().hex
            if not self.token.get('access_token'):
                self._auth_flow(auth_session)
            elif self.token.get('expiry') <= datetime.now():
                self._refresh_access_token(auth_session)
            self.access_token = self.token['access_token']
        finally:
            self.token.sync()
            self.token.close()
            delattr(self, 'token')


class SpotifyClient:
    def handle_request(self, method, *args, **kwargs):
        """ Handles HTTP response erros.
            TODO: Better handler
        """
        resp = method(*args, **kwargs)
        result = resp.json()
        if not resp.ok:
            raise ValueError(str(result))
        return result

    def paginate_through(self, url, params=None):
        """ Paginates through a paginated object listing with the starter url.
        """
        results = []
        if params is None:
            params = {'limit': 50}
        while url:
            resp = self.handle_request(
                self.spotify_session.get, url, params=params)
            results.extend(resp['items'])
            url = resp.get('next')
        return results

    def get_playlist_id(self, playlist):
        """ Retrieve a playlist by its URI or its name
        """
        # If in URI format
        if is_uri(playlist):
            playlist_id = id_from_uri(playlist)
        # If in name format
        else:
            all_playlists = self.all_playlists()
            matched = next(
                filter(lambda p: playlist in p['name'], all_playlists), {})
            playlist_id = matched.get('id')
        if not playlist_id:
            raise ValueError("No matching playlist found.")
        return playlist_id

    def all_playlists(self):
        return self.paginate_through(get_url('playlists'))

    def all_tracks_in_playlist(self, playlist_id):
        return self.paginate_through(
            get_url('tracks', playlist_id=playlist_id),
            params={'offset': 0, 'limit': 100})

    def add_tracks_to_playlist(self, tracks, playlist_id):
        for chunk in chunk_gen(tracks):
            self.handle_request(
                self.spotify_session.post,
                get_url('tracks', playlist_id=playlist_id),
                json={'uris': chunk})

    def update_playlist_tracks(self, playlist_id, **data):
        return self.handle_request(
            self.spotify_session.put,
            get_url('tracks', playlist_id=playlist_id), json=data)

    def __init__(self):
        self.spotify_session = requests.Session()
        self.spotify_session.headers = SpotifyAuthClient().get_auth_header()
