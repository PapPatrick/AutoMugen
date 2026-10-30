"""Twitch chat authorization. Credentials stay in server memory."""

import json
import os
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TwitchError(Exception):
    pass


class TwitchClient:
    scope = "chat:read chat:edit"
    def __init__(self):
        self.client_id = os.environ.get("AUTOMUGEN_TWITCH_CLIENT_ID", "")
        self.token = ""
        self.user_id = ""
        self.validated_until = 0
        self.login = ""
        self.oauth_pending = None

    def request(self, method, url, data=None, validation=False):
        headers = {
            "Authorization": ("OAuth " if validation else "Bearer ") + self.token,
            "Client-Id": self.client_id, "Content-Type": "application/json",
        }
        request = Request(url, data=json.dumps(data).encode() if data is not None else None,
                          headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                result = json.load(response)
                if not isinstance(result, dict):
                    raise TwitchError("Twitch returned a non-object authorization response.")
                return result
        except HTTPError as exc:
            # Do not echo arbitrary remote bodies or request headers containing credentials.
            if exc.code in (401, 403):
                self.validated_until = 0
            exc.close()
            raise TwitchError(f"Twitch HTTP {exc.code}. Check chat authorization and token scopes.") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise TwitchError("Twitch request failed or returned invalid JSON. Reconcile before retrying; no result assumed.") from None

    def validate(self):
        if not self.client_id or not self.token:
            raise TwitchError("Twitch setup required: set your client ID, then sign in with your separate bot account. See README.")
        if self.validated_until > time.time():
            return
        data = self.request("GET", "https://id.twitch.tv/oauth2/validate", validation=True)
        user_id = data.get("user_id")
        if (not isinstance(user_id, str) or not user_id or data.get("client_id") != self.client_id
                or (self.user_id and user_id != self.user_id)
                or not set(self.scope.split()).issubset(data.get("scopes", []))):
            raise TwitchError(f"Token must belong to this bot/application and include {self.scope}.")
        if data.get("expires_in", 0) <= 0:
            raise TwitchError("Twitch user authorization expired. Supply a renewed user token.")
        self.login = data.get("login", "")
        self.user_id = user_id
        self.validated_until = time.time() + min(300, data["expires_in"])

    def authorize_url(self, redirect_uri):
        if not self.client_id:
            raise TwitchError("Set AUTOMUGEN_TWITCH_CLIENT_ID, then restart the server and Sign in with Twitch.")
        state = secrets.token_urlsafe(32)
        self.oauth_pending = (state, time.time() + 600)
        return "https://id.twitch.tv/oauth2/authorize?" + urlencode({
            "client_id": self.client_id, "redirect_uri": redirect_uri,
            "response_type": "token", "scope": self.scope,
            "state": state, "force_verify": "true",
        })

    def authorize(self, token, state):
        pending = self.oauth_pending
        self.oauth_pending = None
        if (not pending or time.time() > pending[1] or not isinstance(state, str)
                or not secrets.compare_digest(pending[0], state)):
            raise TwitchError("OAuth state expired or mismatched. Restart Sign in from the dashboard.")
        if not isinstance(token, str) or not 1 <= len(token) <= 2048 or not token.isascii() or not token.isalnum():
            raise TwitchError("Invalid OAuth token response.")
        self.token, self.validated_until = token, 0
        self.user_id = ""
        try:
            self.validate()
        except TwitchError:
            self.token = ""
            raise
