"""The manifest flow against a real loopback server, with GitHub and gh faked."""

import json
import re
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from importlib.resources import files

import pytest

from carl_merge import cli
from carl_merge.create_app import (
    LOGO_FILE,
    PERMISSIONS,
    App,
    CreateAppError,
    create_app,
    default_name,
    manifest,
    registration_url,
    save_logo,
    settings_url,
    store,
)

PEM = "-----BEGIN RSA PRIVATE KEY-----\nkey-secret-value\n-----END-----\n"
APP = App(
    "carl-merge-acme", "Iv1.client", "https://github.com/apps/carl-merge-acme", PEM
)


class FakeGh:
    def __init__(self, kind="User", fail=()):
        self.kind = kind
        self.fail = set(fail)
        self.calls = []

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        code = 1 if args[0] in self.fail else 0
        out = f"{self.kind}\n" if args[0] == "api" else ""
        return subprocess.CompletedProcess(args, code, out, "")


def github(state_override=None, code="one-time-code"):
    """A browser that loads the form, then follows GitHub's redirect back."""
    seen = {"opened": []}

    def open_page(start):
        if start.startswith("https://github.com/"):
            seen["opened"].append(start)
            return
        with urllib.request.urlopen(start, timeout=5) as response:
            page = response.read().decode()
        action = re.search(r'action="([^"]+)"', page).group(1).replace("&amp;", "&")
        value = re.search(r'name="manifest" value="([^"]+)"', page).group(1)
        seen["action"] = action
        seen["manifest"] = json.loads(value.replace("&quot;", '"'))
        state = (
            state_override
            or urllib.parse.parse_qs(urllib.parse.urlsplit(action).query)["state"][0]
        )
        query = urllib.parse.urlencode({"code": code, "state": state})
        back = f"{seen['manifest']['redirect_url']}?{query}"
        if state_override:
            try:
                urllib.request.urlopen(back, timeout=5)
            except urllib.error.HTTPError as exc:
                seen["forged"] = exc.code
            good = urllib.parse.parse_qs(urllib.parse.urlsplit(action).query)
            query = urllib.parse.urlencode({"code": code, "state": good["state"][0]})
            back = f"{seen['manifest']['redirect_url']}?{query}"
        with urllib.request.urlopen(back, timeout=5) as response:
            seen["done"] = response.read().decode()

    return open_page, seen


@pytest.mark.unit
def test_manifest_asks_for_the_guides_permissions_and_no_webhook():
    body = manifest("acme/app", "Carl Mergé acme", "cb")
    assert body["default_permissions"] == PERMISSIONS
    assert body["default_permissions"]["workflows"] == "write"
    assert body["hook_attributes"]["active"] is False
    assert body["public"] is False
    assert body["url"] == "https://github.com/acme/app"
    assert body["redirect_url"] == "cb"


@pytest.mark.unit
def test_registration_url_picks_personal_or_organisation():
    assert registration_url("al", False, "s") == (
        "https://github.com/settings/apps/new?state=s"
    )
    assert registration_url("acme", True, "s") == (
        "https://github.com/organizations/acme/settings/apps/new?state=s"
    )


@pytest.mark.unit
def test_default_name_fits_githubs_limit():
    assert default_name("acme") == "Carl Mergé acme"
    assert len(default_name("a" * 60)) == 34


@pytest.mark.unit
def test_whole_flow_stores_both_values_and_never_prints_the_key(tmp_path):
    gh, logs, codes = FakeGh(kind="Organization"), [], []
    open_page, seen = github()

    def exchange(code):
        codes.append(code)
        return APP

    app = create_app(
        "acme/app",
        gh=gh,
        open_page=open_page,
        exchange=exchange,
        log=logs.append,
        logo_dir=tmp_path,
    )
    assert app == APP
    assert codes == ["one-time-code"]
    assert seen["action"].startswith(
        "https://github.com/organizations/acme/settings/apps/new?state="
    )
    assert seen["manifest"]["name"] == "Carl Mergé acme"
    assert seen["manifest"]["redirect_url"].endswith("/callback")
    assert "Go back to the terminal" in seen["done"]
    sets = [call for call in gh.calls if call[0][0] in ("variable", "secret")]
    assert sets == [
        (
            [
                "variable",
                "set",
                "CARL_MERGE_CLIENT_ID",
                "--repo",
                "acme/app",
                "--body",
                "Iv1.client",
            ],
            None,
        ),
        (["secret", "set", "CARL_MERGE_PRIVATE_KEY", "--repo", "acme/app"], PEM),
    ]
    printed = "\n".join(logs)
    assert "key-secret-value" not in printed
    assert "installations/new" in printed
    assert seen["opened"] == [
        "https://github.com/organizations/acme/settings/apps/carl-merge-acme"
    ]
    assert f"pick {tmp_path / LOGO_FILE}" in printed
    assert (tmp_path / LOGO_FILE).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.unit
def test_a_callback_with_the_wrong_state_is_refused_and_the_flow_waits(tmp_path):
    open_page, seen = github(state_override="forged")
    create_app(
        "al/app",
        gh=FakeGh(),
        open_page=open_page,
        exchange=lambda c: APP,
        log=lambda line: None,
        logo_dir=tmp_path,
    )
    assert seen["forged"] == 404
    assert "Go back to the terminal" in seen["done"]


@pytest.mark.unit
def test_failed_secret_set_leaves_the_key_in_an_owner_only_file(tmp_path):
    left = store(APP, "al/app", FakeGh(fail={"secret"}), key_dir=tmp_path)
    path = tmp_path / "carl-merge-acme.private-key.pem"
    assert path.read_text() == PEM
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(left) == 1 and str(path) in left[0]
    assert "key-secret-value" not in left[0]


@pytest.mark.unit
def test_cli_reports_a_failure_without_a_traceback(monkeypatch, capsys):
    def boom(repo, name):
        raise CreateAppError("gh could not read the account al")

    monkeypatch.setattr(cli, "create_app", boom)
    assert cli.main(["create-app", "--repo", "al/app"]) == 3
    assert "gh could not read" in capsys.readouterr().err


@pytest.mark.unit
def test_settings_url_picks_personal_or_organisation():
    assert settings_url("al", False, "s") == "https://github.com/settings/apps/s"
    assert settings_url("acme", True, "s") == (
        "https://github.com/organizations/acme/settings/apps/s"
    )


@pytest.mark.unit
def test_logo_is_the_packaged_image_and_an_existing_file_is_kept(tmp_path):
    packaged = files("carl_merge").joinpath(LOGO_FILE).read_bytes()
    assert save_logo(tmp_path).read_bytes() == packaged
    (tmp_path / LOGO_FILE).write_bytes(b"mine")
    assert save_logo(tmp_path).read_bytes() == b"mine"
