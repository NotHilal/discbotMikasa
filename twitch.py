"""Twitch live detection using the official Helix API."""

from __future__ import annotations

import aiohttp

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"
MAX_LOGINS_PER_REQUEST = 100

# A dropped connection can bring the stream back under a new ID. A new ID this
# soon after the last one was seen counts as the same stream, not a new post.
RECONNECT_GRACE_SECONDS = 10 * 60


class TwitchClient:
    def __init__(self, session: aiohttp.ClientSession, client_id: str, client_secret: str):
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None

    async def _fetch_token(self) -> str:
        params = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials",
        }
        async with self._session.post(TOKEN_URL, params=params) as resp:
            resp.raise_for_status()
            return (await resp.json())["access_token"]

    async def _get_streams(self, logins: list[str], retry: bool = True) -> list[dict]:
        if self._token is None:
            self._token = await self._fetch_token()

        params = [("user_login", login) for login in logins] + [("first", str(MAX_LOGINS_PER_REQUEST))]
        headers = {"Client-Id": self._client_id, "Authorization": f"Bearer {self._token}"}
        async with self._session.get(STREAMS_URL, params=params, headers=headers) as resp:
            if resp.status != 401 or not retry:
                resp.raise_for_status()
                return (await resp.json())["data"]

        # App tokens expire, so get a new one once and try again.
        self._token = None
        return await self._get_streams(logins, retry=False)

    async def get_live_streams(self, usernames: set[str]) -> dict[str, str]:
        """Maps each username that is currently live to its stream ID."""
        logins = sorted(usernames)
        live: dict[str, str] = {}
        for start in range(0, len(logins), MAX_LOGINS_PER_REQUEST):
            for stream in await self._get_streams(logins[start : start + MAX_LOGINS_PER_REQUEST]):
                live[stream["user_login"].lower()] = stream["id"]
        return live


def should_announce(stream_id: str, settings: dict, now: float) -> bool:
    if stream_id == settings.get("stream_id"):
        return False
    return now - settings.get("last_seen", 0) >= RECONNECT_GRACE_SECONDS
