"""A small Matrix client (Client-Server API v3), only what the gateway needs.

Plain HTTPS with the standard library, run in a thread so the event loop is
never blocked. No dependency on a Matrix SDK: a bot account needs only a few
endpoints. An Application Service (one virtual user per Unseenet key) would
be the point to switch to mautrix-python.

What this client never does, on purpose (UN-RFC 0023 section 5.3): send
typing notifications, read receipts or read markers, or set presence. It
syncs with ``set_presence=offline`` so a sync does not mark the account as
online either.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)
API = "/_matrix/client/v3"


class MatrixError(Exception):
    def __init__(self, status: int, errcode: str, error: str = ""):
        super().__init__(f"{status} {errcode} {error}".strip())
        self.status, self.errcode, self.error = status, errcode, error


def q(value: str) -> str:
    """Percent-encode one path segment (room ids, aliases, event ids)."""
    return urllib.parse.quote(value, safe="")


class Matrix:
    def __init__(self, homeserver: str, token: str = "", *, proxy: str | None = None,
                 timeout: float = 30.0):
        self.base = homeserver.rstrip("/")
        self.token, self.timeout = token, timeout
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
        self.opener = urllib.request.build_opener(*handlers)
        self.user_id: str | None = None
        self._since: str | None = None                 # sync position
        self._invited: dict[str, list[dict]] = {}      # pending invitations

    # --- plumbing ---
    def _call(self, method: str, path: str, params: dict | None = None,
              body: dict | None = None) -> dict:
        url = self.base + API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode() or "{}")
            except ValueError:
                err = {}
            raise MatrixError(exc.code, err.get("errcode", "M_UNKNOWN"), err.get("error", "")) \
                from None
        return json.loads(raw.decode() or "{}")

    async def call(self, method: str, path: str, params: dict | None = None,
                   body: dict | None = None) -> dict:
        return await asyncio.to_thread(self._call, method, path, params, body)

    def _download(self, mxc: str, limit: int) -> bytes:
        """The octets of ``mxc://<server>/<id>``, through the authenticated media API
        (Matrix 1.11), at most ``limit`` octets."""
        server, _, media_id = mxc.removeprefix("mxc://").partition("/")
        if not server or not media_id or "/" in media_id:
            raise MatrixError(400, "M_INVALID_PARAM", "not an mxc:// URI")
        url = (f"{self.base}/_matrix/client/v1/media/download/{q(server)}/{q(media_id)}"
               "?allow_redirect=false")
        req = urllib.request.Request(url, method="GET")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                data = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            raise MatrixError(exc.code, "M_UNKNOWN", "media download refused") from None
        if len(data) > limit:
            raise MatrixError(413, "M_TOO_LARGE", f"more than {limit} octets")
        return data

    async def download(self, mxc: str, limit: int) -> bytes:
        return await asyncio.to_thread(self._download, mxc, limit)

    def _upload(self, data: bytes, content_type: str, filename: str = "") -> str:
        """Put ``data`` in the server's media store and return its ``mxc://`` URI."""
        url = f"{self.base}/_matrix/media/v3/upload"
        if filename:
            url += "?" + urllib.parse.urlencode({"filename": filename})
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", content_type or "application/octet-stream")
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode() or "{}")
            except ValueError:
                err = {}
            raise MatrixError(exc.code, err.get("errcode", "M_UNKNOWN"), err.get("error", "")) \
                from None
        mxc = json.loads(raw.decode() or "{}").get("content_uri", "")
        if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
            raise MatrixError(502, "M_UNKNOWN", "upload gave no content_uri")
        return mxc

    async def upload(self, data: bytes, content_type: str, filename: str = "") -> str:
        return await asyncio.to_thread(self._upload, data, content_type, filename)

    async def close(self) -> None:
        pass

    # --- account ---
    async def login(self, user: str, password: str, device_name: str) -> dict:
        return await self.call("POST", "/login", body={
            "type": "m.login.password", "identifier": {"type": "m.id.user", "user": user},
            "password": password, "initial_device_display_name": device_name})

    async def whoami(self) -> str:
        if self.user_id is None:
            self.user_id = (await self.call("GET", "/account/whoami"))["user_id"]
        return self.user_id

    # --- rooms ---
    async def resolve(self, room: str) -> str:
        """`#alias:server` -> `!room:server`; a room id is returned as it is."""
        if room.startswith("!"):
            return room
        return (await self.call("GET", f"/directory/room/{q(room)}"))["room_id"]

    async def state(self, room_id: str, event_type: str, state_key: str = "") -> dict | None:
        """One state event's content, or None if the room has none."""
        try:
            return await self.call("GET", f"/rooms/{q(room_id)}/state/{q(event_type)}/{q(state_key)}")
        except MatrixError as exc:
            if exc.status == 404:
                return None
            raise

    async def membership(self, room_id: str) -> str | None:
        """The gateway's own membership in a room: join, leave, ban, … or None."""
        try:
            m = await self.state(room_id, "m.room.member", await self.whoami())
        except MatrixError as exc:
            if exc.errcode == "M_FORBIDDEN":
                return None
            raise
        return m.get("membership") if m else None

    async def invites(self) -> dict[str, list[dict]]:
        """Rooms the gateway is invited to now: room id -> the room's invite
        state (the few state events a server shows before joining).

        The first call is a full sync, which lists every pending invitation;
        later calls are incremental, so a new invitation is seen at once
        (servers cache a full sync for a while).
        """
        # No state filter: Synapse applies it to the invite state too, and an
        # invite with no state left is dropped from the answer.
        flt = {"presence": {"types": []}, "account_data": {"types": []},
               "room": {"timeline": {"limit": 0}, "state": {"lazy_load_members": True},
                        "ephemeral": {"types": []}, "account_data": {"types": []}}}
        params = {"filter": json.dumps(flt), "set_presence": "offline", "timeout": "0"}
        if self._since:
            params["since"] = self._since
        r = await self.call("GET", "/sync", params=params)
        self._since = r.get("next_batch", self._since)
        rooms = r.get("rooms", {})
        for rid, room in rooms.get("invite", {}).items():
            self._invited[rid] = room.get("invite_state", {}).get("events", [])
        for gone in ("join", "leave"):
            for rid in rooms.get(gone, {}):
                self._invited.pop(rid, None)
        return dict(self._invited)

    async def join(self, room_id: str) -> None:
        await self.call("POST", f"/rooms/{q(room_id)}/join", body={})
        self._invited.pop(room_id, None)

    async def leave(self, room_id: str, reason: str = "") -> None:
        """Leave a room, or decline an invitation to it."""
        await self.call("POST", f"/rooms/{q(room_id)}/leave", body={"reason": reason} if reason else {})
        self._invited.pop(room_id, None)

    async def create_room(self, body: dict) -> str:
        return (await self.call("POST", "/createRoom", body=body))["room_id"]

    async def put_state(self, room_id: str, event_type: str, content: dict,
                        state_key: str = "") -> str:
        r = await self.call("PUT", f"/rooms/{q(room_id)}/state/{q(event_type)}/{q(state_key)}",
                            body=content)
        return r.get("event_id", "")

    async def messages(self, room_id: str, *, start: str | None = None, limit: int = 100,
                       types: list[str] | None = None) -> dict:
        """One page of the room's timeline, newest first."""
        params = {"dir": "b", "limit": str(limit)}
        if start:
            params["from"] = start
        if types:
            params["filter"] = json.dumps({"types": types})
        return await self.call("GET", f"/rooms/{q(room_id)}/messages", params=params)

    async def event(self, room_id: str, event_id: str) -> dict | None:
        try:
            return await self.call("GET", f"/rooms/{q(room_id)}/event/{q(event_id)}")
        except MatrixError as exc:
            if exc.status == 404:
                return None
            raise

    async def send(self, room_id: str, content: dict, txn_id: str,
                   event_type: str = "m.room.message") -> str:
        """Send an event; the same txn_id never sends twice (the server deduplicates)."""
        r = await self.call("PUT", f"/rooms/{q(room_id)}/send/{q(event_type)}/{q(txn_id)}",
                            body=content)
        return r["event_id"]

    async def redact(self, room_id: str, event_id: str, txn_id: str,
                     reason: str | None = None) -> str:
        """Redact an event (the bot's own); the same txn_id never redacts twice."""
        r = await self.call("PUT", f"/rooms/{q(room_id)}/redact/{q(event_id)}/{q(txn_id)}",
                            body={"reason": reason} if reason else {})
        return r.get("event_id", "")
