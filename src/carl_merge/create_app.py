"""`carl-merge create-app`: register the GitHub App from a manifest, end to end.

A one-shot server on the loopback address serves a form that posts the
manifest to GitHub. GitHub sends the browser back with a one-time code, the
code is exchanged for the App's client ID and private key, and both go
straight into the repository through `gh`. The key is never printed: it
travels to `gh secret set` on stdin, and only if that fails is it written to a
file that only the owner can read.

GitHub has no way to set an App's logo, so the command leaves the logo file in
the working directory and opens the App's settings page, where it is one
upload.
"""

from __future__ import annotations

import html
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path

from carl_merge.transport import API_URL, API_VERSION, TIMEOUT

CLIENT_ID_VARIABLE = "CARL_MERGE_CLIENT_ID"
PRIVATE_KEY_SECRET = "CARL_MERGE_PRIVATE_KEY"
NAME_LIMIT = 34
PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "actions": "write",
    "checks": "read",
    "statuses": "read",
    "workflows": "write",
    "metadata": "read",
}
NO_WEBHOOK = {"url": "https://example.com/carl-merge-uses-no-webhook", "active": False}
LOGO_FILE = "carl-merge-logo.png"
WAIT_FOR_GITHUB = 15 * 60
# A connection that sends nothing (a browser preconnect) is dropped after this,
# so it cannot hold the one-at-a-time server past its deadline.
CONNECTION_TIMEOUT = 5


class CreateAppError(Exception):
    pass


@dataclass(frozen=True)
class App:
    slug: str
    client_id: str
    html_url: str
    pem: str


def default_name(owner: str) -> str:
    return f"Carl Mergé {owner}"[:NAME_LIMIT].rstrip()


def manifest(repo: str, name: str, redirect_url: str) -> dict:
    return {
        "name": name,
        "url": f"https://github.com/{repo}",
        "description": f"Carl Mergé merge bot for {repo}.",
        "public": False,
        "hook_attributes": NO_WEBHOOK,
        "redirect_url": redirect_url,
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }


def registration_url(owner: str, is_org: bool, state: str) -> str:
    where = f"organizations/{owner}/settings" if is_org else "settings"
    return f"https://github.com/{where}/apps/new?state={state}"


def settings_url(owner: str, is_org: bool, slug: str) -> str:
    where = f"organizations/{owner}/settings" if is_org else "settings"
    return f"https://github.com/{where}/apps/{slug}"


def save_logo(directory: Path) -> Path:
    """Leave the logo next to the user; never overwrite a file already there."""
    path = directory / LOGO_FILE
    try:
        with path.open("xb") as handle:
            handle.write(files("carl_merge").joinpath(LOGO_FILE).read_bytes())
    except FileExistsError:
        pass
    return path


def form_page(action: str, manifest_json: str) -> bytes:
    return f"""<!doctype html>
<meta charset="utf-8">
<title>Create the Carl Mergé App</title>
<form id="f" action="{html.escape(action)}" method="post">
  <input type="hidden" name="manifest" value="{html.escape(manifest_json)}">
  <button type="submit">Create the GitHub App</button>
</form>
<script>document.getElementById("f").submit()</script>
""".encode()


DONE_PAGE = b"""<!doctype html>
<meta charset="utf-8">
<title>Carl Merg\xc3\xa9</title>
<p>The App is created. Go back to the terminal to finish.</p>
"""


def wait_for_code(
    action: str,
    manifest_for: Callable[[str], dict],
    state: str,
    open_page: Callable[[str], object] = webbrowser.open,
    log: Callable[[str], None] = print,
    wait: float = WAIT_FOR_GITHUB,
) -> str:
    """Serve the form, then return the code GitHub hands back, or give up
    after `wait` seconds."""
    found: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        timeout = CONNECTION_TIMEOUT

        def log_message(self, *args) -> None:
            pass

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parts = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parts.query)
            if parts.path == "/":
                body = json.dumps(manifest_for(callback))
                self._reply(200, form_page(action, body))
            elif parts.path == "/callback" and query.get("state") == [state]:
                found["code"] = query.get("code", [""])[0]
                self._reply(200, DONE_PAGE)
            else:
                self._reply(404, b"not found")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    port = server.server_address[1]
    start = urllib.parse.urlunsplit(("http", f"127.0.0.1:{port}", "/", "", ""))
    callback = urllib.parse.urljoin(start, "/callback")
    log(f"Opening {start} to create the App. Open it yourself if no browser starts.")
    threading.Thread(target=open_page, args=(start,), daemon=True).start()
    deadline = time.monotonic() + wait
    with server:
        while "code" not in found and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in found:
        minutes = max(1, round(wait / 60))
        raise CreateAppError(
            f"no answer from GitHub in {minutes} min; run create-app again"
        )
    if not found["code"]:
        raise CreateAppError("GitHub sent back no code")
    return found["code"]


def convert(code: str, base_url: str = API_URL) -> App:
    """Exchange the one-time code. It needs no token and works once."""
    quoted = urllib.parse.quote(code, safe="")
    request = urllib.request.Request(
        f"{base_url}/app-manifests/{quoted}/conversions", data=b"", method="POST"
    )
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", API_VERSION)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.load(response)
    except OSError as exc:
        raise CreateAppError(f"exchanging the code failed: {exc}") from None
    return App(data["slug"], data["client_id"], data["html_url"], data["pem"])


def _gh(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], input=stdin, capture_output=True, text=True, check=False
    )


def owner_is_org(owner: str, gh=_gh) -> bool:
    result = gh(["api", f"users/{owner}", "--jq", ".type"])
    if result.returncode != 0:
        raise CreateAppError(f"gh could not read the account {owner}")
    return result.stdout.strip() == "Organization"


def store(app: App, repo: str, gh=_gh, key_dir: Path | None = None) -> list[str]:
    """Put the client ID and key in the repository; return what was not stored."""
    left = []
    variable = gh(
        ["variable", "set", CLIENT_ID_VARIABLE, "--repo", repo, "--body", app.client_id]
    )
    if variable.returncode != 0:
        left.append(f"set the variable {CLIENT_ID_VARIABLE} to {app.client_id}")
    secret = gh(["secret", "set", PRIVATE_KEY_SECRET, "--repo", repo], stdin=app.pem)
    if secret.returncode != 0:
        path = _key_file(app, key_dir or Path.cwd())
        left.append(
            f"store {path} as the secret {PRIVATE_KEY_SECRET}, then delete the file"
        )
    return left


def _key_file(app: App, directory: Path) -> Path:
    """Write the key to a new owner-only file; never overwrite one already there,
    since it may hold the key of an earlier App."""
    for attempt in range(1, 100):
        suffix = "" if attempt == 1 else f"-{attempt}"
        path = directory / f"{app.slug}.private-key{suffix}.pem"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(app.pem)
        return path
    raise CreateAppError(f"no free file name for the key in {directory}")


def next_steps(app: App, left: list[str], logo: Path, settings: str) -> list[str]:
    return [
        *left,
        f"on the App settings page now open ({settings}), click Upload a logo "
        f"under Display information and pick {logo}",
        f"install the App on the repository: {app.html_url}/installations/new",
        "add the App to the ruleset's bypass list in pull_request mode (guide, "
        "section 4)",
    ]


def create_app(
    repo: str,
    name: str | None = None,
    *,
    gh=_gh,
    open_page: Callable[[str], object] = webbrowser.open,
    exchange: Callable[[str], App] = convert,
    log: Callable[[str], None] = print,
    logo_dir: Path | None = None,
) -> App:
    owner = repo.split("/", 1)[0]
    state = secrets.token_urlsafe(24)
    is_org = owner_is_org(owner, gh)
    action = registration_url(owner, is_org, state)
    title = name or default_name(owner)
    code = wait_for_code(
        action, lambda callback: manifest(repo, title, callback), state, open_page, log
    )
    app = exchange(code)
    left = store(app, repo, gh)
    logo = save_logo(logo_dir or Path.cwd())
    settings = settings_url(owner, is_org, app.slug)
    log(f"Created {app.html_url}. The private key was not printed.")
    open_page(settings)
    for number, step in enumerate(next_steps(app, left, logo, settings), 1):
        log(f"{number}. {step}")
    return app
