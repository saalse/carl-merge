"""The transport against real local servers: redirects, pages and secrets."""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from carl_merge.forge import ForgeError
from carl_merge.transport import Transport

TOKEN = "tok-secret-value"


def local(port: int) -> str:
    return urllib.parse.urlunsplit(("http", f"127.0.0.1:{port}", "", "", ""))


class Server:
    def __init__(self, handler):
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.seen.append((self.path, dict(self.headers)))
                status, headers, body = handler(self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            do_PUT = do_POST = do_GET

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = local(self.httpd.server_port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    servers = []

    def start(handler):
        servers.append(Server(handler))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def json_reply(data, status=200, **headers):
    return (
        status,
        {"Content-Type": "application/json", **headers},
        json.dumps(data).encode(),
    )


@pytest.mark.unit
def test_every_request_carries_the_token_and_the_api_version(serve):
    server = serve(lambda path: json_reply({"ok": True}))
    reply = Transport(TOKEN, base_url=server.url).request("GET", "/x", params={"a": 1})
    assert reply.data == {"ok": True}
    path, headers = server.seen[0]
    headers = {k.lower(): v for k, v in headers.items()}
    assert path == "/x?a=1"
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["x-github-api-version"] == "2026-03-10"
    assert headers["accept"] == "application/vnd.github+json"


@pytest.mark.unit
def test_a_redirect_to_another_host_is_refused_with_no_second_request(serve):
    elsewhere = serve(lambda path: json_reply({"leak": True}))
    server = serve(lambda path: (302, {"Location": elsewhere.url + "/stolen"}, b""))
    with pytest.raises(ForgeError, match="redirect refused"):
        Transport(TOKEN, base_url=server.url).request("GET", "/x")
    assert elsewhere.seen == []
    assert len(server.seen) == 1


@pytest.mark.unit
def test_pages_follow_the_link_header_to_the_same_origin(serve):
    holder = {}

    def handler(path):
        if "page=2" in path:
            return json_reply([{"n": 3}])
        link = f'<{holder["url"]}/items?page=2>; rel="next"'
        return json_reply([{"n": 1}, {"n": 2}], Link=link)

    server = serve(handler)
    holder["url"] = server.url
    items = Transport(TOKEN, base_url=server.url).pages("/items")
    assert [i["n"] for i in items] == [1, 2, 3]
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for _, h in server.seen)


@pytest.mark.unit
def test_pages_take_a_key(serve):
    server = serve(lambda path: json_reply({"check_runs": [{"n": 1}]}))
    assert Transport(TOKEN, base_url=server.url).pages("/c", key="check_runs") == [
        {"n": 1}
    ]


@pytest.mark.unit
def test_a_next_page_on_another_host_is_refused(serve):
    elsewhere = serve(lambda path: json_reply([]))
    link = f'<{elsewhere.url}/items?page=2>; rel="next"'
    server = serve(lambda path: json_reply([{"n": 1}], Link=link))
    with pytest.raises(ForgeError, match="another host"):
        Transport(TOKEN, base_url=server.url).pages("/items")
    assert elsewhere.seen == []


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
def test_fatal_statuses_raise_without_the_token_or_the_body(serve, status):
    server = serve(lambda path: (status, {"X-Echo": TOKEN}, f"echo {TOKEN}".encode()))
    with pytest.raises(ForgeError) as caught:
        Transport(TOKEN, base_url=server.url).request("GET", "/x", expect=(200, status))
    assert TOKEN not in str(caught.value)
    assert caught.value.status == status
    assert "/x" in str(caught.value)


@pytest.mark.unit
def test_a_status_the_caller_did_not_expect_raises_and_one_it_did_returns(serve):
    server = serve(lambda path: json_reply({}, status=404))
    transport = Transport(TOKEN, base_url=server.url)
    with pytest.raises(ForgeError, match="HTTP 404"):
        transport.request("GET", "/x")
    assert transport.request("GET", "/x", expect=(404,)).status == 404


@pytest.mark.unit
def test_a_dead_server_raises_without_the_token():
    transport = Transport(TOKEN, base_url=local(1))
    with pytest.raises(ForgeError) as caught:
        transport.request("GET", "/x")
    assert TOKEN not in str(caught.value)


@pytest.mark.unit
def test_a_body_that_is_not_json_raises(serve):
    server = serve(lambda path: (200, {}, b"<html>"))
    with pytest.raises(ForgeError, match="not JSON"):
        Transport(TOKEN, base_url=server.url).request("GET", "/x")


@pytest.mark.unit
def test_a_json_body_is_sent_with_its_content_type(serve):
    server = serve(lambda path: json_reply({}, status=201))
    Transport(TOKEN, base_url=server.url).request("POST", "/x", body={"a": 1})
    assert server.seen[0][1]["Content-Type"] == "application/json"


@pytest.mark.unit
def test_iter_pages_fetches_a_page_only_when_asked(serve):
    holder = {}

    def handler(path):
        if "page=2" in path:
            return json_reply([{"n": 3}])
        link = f'<{holder["url"]}/items?page=2>; rel="next"'
        return json_reply([{"n": 1}, {"n": 2}], Link=link)

    server = serve(handler)
    holder["url"] = server.url
    pages = Transport(TOKEN, base_url=server.url).iter_pages("/items")
    assert next(pages) == [{"n": 1}, {"n": 2}]
    assert len(server.seen) == 1
    assert next(pages) == [{"n": 3}]
    assert len(server.seen) == 2
