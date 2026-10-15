"""The only code that holds the token: stdlib HTTP, JSON and `Link` pagination.

It refuses every redirect, so the token is never sent anywhere it was not
aimed, and it never puts a header, a body or the token in an error message.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

from carl_merge.forge import ForgeError

API_URL = "https://api.github.com"
API_VERSION = "2026-03-10"
TIMEOUT = 30
ALWAYS_FATAL = (401, 403, 429)
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Response:
    status: int
    data: object
    link: str = ""


class Transport:
    def __init__(
        self, token: str, *, base_url: str = API_URL, version: str = API_VERSION
    ) -> None:
        self._token = token
        self.base_url = base_url.rstrip("/")
        self._version = version
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _url(self, path: str, params: dict | None) -> str:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return f"{self.base_url}{path}{query}"

    def _send(self, method: str, url: str, body: object | None) -> Response:
        path = urllib.parse.urlsplit(url).path
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", self._version)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=TIMEOUT) as reply:
                status, raw, link = reply.status, reply.read(), reply.headers
        except urllib.error.HTTPError as exc:
            status, raw, link = exc.code, exc.read(), exc.headers
            if 300 <= status < 400:
                raise ForgeError(f"{method} {path}: redirect refused", status) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ForgeError(f"{method} {path}: {type(reason).__name__}") from None
        if status in ALWAYS_FATAL or status >= 500:
            raise ForgeError(f"{method} {path}: HTTP {status}", status)
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            raise ForgeError(f"{method} {path}: reply is not JSON", status) from None
        return Response(status, parsed, link.get("Link", "") or "")

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: object | None = None,
        expect: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Response:
        reply = self._send(method, self._url(path, params), body)
        if reply.status not in expect:
            raise ForgeError(f"{method} {path}: HTTP {reply.status}", reply.status)
        return reply

    def iter_pages(
        self, path: str, *, params: dict | None = None, key: str | None = None
    ) -> Iterator[list]:
        """Each page of a paginated GET, fetched only as it is asked for.

        It follows `Link` to the same origin and no further.
        """
        url: str | None = self._url(path, params)
        while url:
            reply = self._send("GET", url, None)
            if reply.status != 200:
                raise ForgeError(f"GET {path}: HTTP {reply.status}", reply.status)
            yield reply.data[key] if key else reply.data
            found = _NEXT.search(reply.link)
            url = found.group(1) if found else None
            if url and not url.startswith(self.base_url + "/"):
                raise ForgeError(f"GET {path}: next page is on another host")

    def pages(
        self, path: str, *, params: dict | None = None, key: str | None = None
    ) -> list:
        """Every item of a paginated GET."""
        return [
            item
            for page in self.iter_pages(path, params=params, key=key)
            for item in page
        ]
