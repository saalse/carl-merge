"""Named sets of `shared_risk` rules an adopter lists in `presets`. Opt-in only.

Each rule carries example paths it must match and must not match; the tests
check them, so a change to `path_matches` cannot silently empty a preset.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PresetRule:
    pattern: str
    reason: str
    matches: tuple[str, ...]
    misses: tuple[str, ...] = ()


_MODELS = "Django model definitions"
_SETTINGS = "Django settings"
_DEPS = "dependencies"
_LOCKED = "locked dependencies"
_PACKAGING = "packaging and tool settings"

PRESETS: dict[str, tuple[PresetRule, ...]] = {
    "django": (
        PresetRule(
            "**/models.py",
            _MODELS,
            ("models.py", "billing/models.py"),
            ("shop/models_util.py",),
        ),
        PresetRule(
            "*/models/*.py",
            _MODELS,
            ("shop/models/order.py", "shop/models/sub/line.py"),
            ("docs/models/effort.md",),
        ),
        PresetRule(
            "*/migrations/*.py",
            "Django migrations: the schema tests run on",
            ("shop/migrations/0145_order_status_required.py",),
            ("shop/migrations/README.md",),
        ),
        PresetRule(
            "**/settings.py",
            _SETTINGS,
            ("site/settings.py",),
            ("shop/settings_view.py",),
        ),
        PresetRule(
            "*/settings/*.py",
            _SETTINGS,
            ("site/settings/base.py",),
            ("docs/settings/overview.md",),
        ),
    ),
    "python": (
        PresetRule(
            "**/pyproject.toml",
            "dependencies and tool settings",
            ("pyproject.toml", "services/api/pyproject.toml"),
        ),
        PresetRule("**/uv.lock", _LOCKED, ("uv.lock", "services/api/uv.lock")),
        PresetRule("**/poetry.lock", _LOCKED, ("poetry.lock",)),
        PresetRule("**/pdm.lock", _LOCKED, ("pdm.lock",)),
        PresetRule("**/Pipfile", _DEPS, ("Pipfile",), ("Pipfile.lock",)),
        PresetRule("**/Pipfile.lock", _LOCKED, ("Pipfile.lock",)),
        PresetRule(
            "**/requirements*.txt",
            _DEPS,
            (
                "requirements.txt",
                "requirements-dev.txt",
                "requirements/dev.txt",
                "api/requirements.txt",
            ),
            ("docs/notes_on_requirements.txt", "frontend/yarn.lock"),
        ),
        PresetRule(
            "**/requirements*.in",
            _DEPS,
            ("requirements-dev.in", "requirements/dev.in"),
            ("docs/notes_on_requirements.in",),
        ),
        PresetRule("**/setup.py", _PACKAGING, ("setup.py",)),
        PresetRule("**/setup.cfg", _PACKAGING, ("setup.cfg",)),
        PresetRule("**/tox.ini", "tool settings", ("tox.ini",)),
        PresetRule("**/ruff.toml", "lint settings", ("ruff.toml",)),
        PresetRule("**/.ruff.toml", "lint settings", (".ruff.toml",)),
        PresetRule("**/mypy.ini", "type-check settings", ("mypy.ini",)),
        PresetRule("**/pytest.ini", "test settings", ("pytest.ini",)),
        PresetRule("**/.python-version", "Python version", (".python-version",)),
        PresetRule(
            "**/conftest.py",
            "pytest fixtures shared across tests",
            ("conftest.py", "tests/billing/conftest.py"),
            ("tests/test_conftest_helpers.py",),
        ),
    ),
    "github-actions": (
        PresetRule(
            ".github/workflows/**",
            "CI workflows",
            (".github/workflows/ci.yml",),
            (".github/carl-merge.toml",),
        ),
        PresetRule(
            ".github/actions/**",
            "CI composite actions",
            (".github/actions/setup/action.yml",),
        ),
    ),
}
