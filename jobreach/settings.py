"""Settings bridge: Hermes plugin settings arrive as environment variables.

Why this module exists: the engine always runs as a child process of Hermes, so
``ctx.get_config()`` (which is how a Hermes plugin reads its
``plugins.entries.<id>.settings`` subtree) is unreachable from here. The plugin
process therefore mirrors the settings it reads into ``JOBREACH_SETTING_*``
environment variables and this module is the one place that reads them back.

To bridge settings to external processes like the cron monitor (which Hermes'
cron runner invokes independently without importing the plugin), the plugin
publishes a persistent JSON snapshot (``settings.json``) into the plugin data
directory on each tool call. Independent processes read this snapshot at run
time and merge it into their environment. Baking settings directly into the cron
monitor script would freeze them at install time, which this project
deliberately avoided so that later changes to ``config.yaml`` are automatically
picked up by the scheduler.

Nothing here validates against the schema in ``plugin.yaml`` — that schema is
declared for Hermes' own load-time validation; this module only applies
defaults and normalises types. A key that is absent or blank means "use the
plugin default", never "override with nothing".

The two enumerated settings (``default_validation``, ``default_profile``) are
the one place that goes further: the parser has to end up with a value it can
hand to the engine even when the operator typed something else, so an unknown
value falls back to the plugin default and is logged. A crash there would take
down an unattended monitor over a typo in ``config.yaml``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any

from . import config
from .logging_setup import get_logger

#: Every mirrored setting travels under this prefix, e.g.
#: ``JOBREACH_SETTING_DEFAULT_SOURCES``.
SETTING_PREFIX = "JOBREACH_SETTING_"

#: The keys ``plugin.yaml``'s ``config_schema`` advertises, in manifest order.
#: :func:`describe` reports exactly these, so an unrelated variable that happens
#: to share the prefix can never be mistaken for a setting the user configured.
CONFIG_SCHEMA_KEYS = (
    "default_keyword",
    "default_sources",
    "note_subfolder",
    "max_results",
    "default_validation",
    "default_profile",
)

#: Relevance-filter modes ``plugin.yaml`` advertises, in the order it lists them.
#: The first entry is the engine's own default, so an install that configures
#: nothing behaves exactly as it did before the setting existed.
VALIDATION_MODES = ("off", "local", "llm")

#: Mode used when neither a flag nor a setting names one.
DEFAULT_VALIDATION = VALIDATION_MODES[0]

#: Profiles ``plugin.yaml`` advertises, in the order it lists them. Duplicated
#: from ``filters.FILTER_PROFILES`` deliberately — the settings bridge must not
#: depend on the filter layer — and pinned equal by ``tests/test_settings.py``,
#: so adding a profile to the engine fails loudly here instead of quietly
#: refusing to be configured.
PROFILE_NAMES = ("designer", "frontend", "engineering", "product", "any")

#: Profile used when neither a flag nor a setting names one.
DEFAULT_PROFILE = "designer"

logger = get_logger("settings")


def env_name(key: str) -> str:
    """``"default_sources"`` → ``"JOBREACH_SETTING_DEFAULT_SOURCES"``.

    Hyphens become underscores: a configuration key may legitimately be
    hyphenated, but a POSIX shell cannot export a hyphenated variable name, so
    the bridge normalises the name instead of producing one the child process
    could never be handed.
    """
    return f"{SETTING_PREFIX}{key.strip().upper().replace('-', '_')}"


def _resolve_env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    """The mapping to read: an explicit one in tests, ``os.environ`` otherwise."""
    return os.environ if env is None else env


def _coerce(value: Any) -> str:
    """One setting value → the string the bridge carries, ``""`` to drop it.

    ``ctx.get_config`` returns whatever YAML held, so a list is the normal shape
    for ``default_sources``; joining with the same comma that
    :func:`get_setting_list` splits on keeps a configured list and a hand-typed
    one indistinguishable downstream. ``None`` is dropped rather than
    stringified — YAML writes an empty list entry as ``null``, and forwarding
    the literal word "None" as a board name would be an obscure failure.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        entries: list[str] = []
        for item in value:
            text = "" if item is None else str(item).strip()
            if text:
                entries.append(text)
        return ",".join(entries)
    return str(value).strip()


def get_setting(key: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    """Raw string value of one setting; ``default`` when unset or blank.

    Blank (empty *or* whitespace-only) counts as unset on purpose: a settings UI
    stores a cleared field as ``""``, and "cleared" has to mean "back to the
    plugin default", not "override the default with nothing".
    """
    value = str(_resolve_env(env).get(env_name(key), "") or "").strip()
    return value or default


def get_setting_list(
    key: str, default: Sequence[str] = (), env: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Comma-separated setting → tuple. Blank/unset → *default*. Trims entries, drops empties.

    A value that survives trimming to nothing (``","``, ``" , "``) falls back to
    *default* as well: a separator with no entries is a half-filled form, and the
    alternative is an empty selection that every caller would then have to
    special-case.
    """
    raw = get_setting(key, "", env)
    if not raw:
        return tuple(default)
    parts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return parts or tuple(default)


def get_setting_int(key: str, default: int = 0, env: Mapping[str, str] | None = None) -> int:
    """Integer setting; a non-numeric or non-positive value falls back to *default*.

    Non-positive counts as unset because every integer setting the plugin has is
    a count or a cap (``max_results``): ``0`` and negatives are not a meaningful
    choice there, they are what a half-filled form or a stringified float
    produces.

    ``max_results`` is read through here (for example, by ``list`` when no
    explicit limit is passed), falling back to *default* if non-positive or
    unparseable. The handler also applies it when building CLI flags, but the
    engine consults this accessor so direct invocations and fallbacks stay
    consistent.
    """
    raw = get_setting(key, "", env)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def default_sources(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """``Sources`` default when the caller passes no ``--source``.

    Falls back to :data:`jobreach.config.DEFAULT_SOURCES`, so the CLI's
    ``--source`` default stays the project's original design feed until the user
    configures something else.

    The value is deliberately *not* validated here. ``config.Sources.parse`` is
    the one place that knows the board names and can reject an unknown one with
    an actionable message; duplicating that list would let the two drift, and
    argparse would turn a bad default into a traceback instead of a tool result.
    """
    return get_setting_list("default_sources", config.DEFAULT_SOURCES, env)


def note_subfolder(env: Mapping[str, str] | None = None) -> str:
    """Subfolder for generated Obsidian notes (blank → ``config.NOTE_SUBFOLDER``)."""
    return get_setting("note_subfolder", config.NOTE_SUBFOLDER, env)


def _enum(
    key: str, allowed: tuple[str, ...], default: str, env: Mapping[str, str] | None
) -> str:
    """One of *allowed*, or *default* — never junk, never an exception.

    An unknown value is a typo in ``config.yaml``, and the engine is the wrong
    place to find out: a monitor that raised on it would stop reporting
    altogether. It is logged instead and the run continues on *default*, which
    is also what a blank field means — so "cleared" and "mistyped" end up in
    one place.

    Matching ignores case because the value is hand-typed, and the canonical
    (lower-case) spelling is returned, never what the user typed.
    """
    value = get_setting(key, "", env).lower()
    if not value:
        return default
    if value not in allowed:
        logger.warning("ignoring %s: %r is not one of %s", key, value, ", ".join(allowed))
        return default
    return value


def validation(
    env: Mapping[str, str] | None = None, *, default: str = DEFAULT_VALIDATION
) -> str:
    """Relevance-filter mode to run when no ``--validate`` is passed (``off`` by default).

    ``monitor`` passes ``default="local"``: an unattended digest keeps its own
    cheap filter unless the user configured something, which is the only place
    the engine's default differs from ``search``'s.
    """
    return _enum("default_validation", VALIDATION_MODES, default, env)


def profile(env: Mapping[str, str] | None = None, *, default: str = DEFAULT_PROFILE) -> str:
    """Filter profile to run when no ``--profile`` is passed (``designer`` by default).

    See :data:`PROFILE_NAMES` for why the list lives here as well as in
    ``filters``: an unknown profile must reach the parser as a *default*, not as
    a traceback.
    """
    return _enum("default_profile", PROFILE_NAMES, default, env)


def apply_default_settings(
    env: MutableMapping[str, str], settings: Mapping[str, Any]
) -> list[str]:
    """Write *settings* into *env* as ``JOBREACH_SETTING_*``.

    Returns the list of keys written (for logging/tests). ``None`` values and blank
    strings are dropped, and a value that is a list/tuple becomes a comma-joined
    string, so the caller can pass ``ctx.get_config`` results through untouched.

    *env* is mutated in place — the child environment has already been copied —
    and keys already present in it are left alone, so an operator who exported
    ``JOBREACH_SETTING_*`` by hand (the CLI path has no plugin process to mirror
    anything) keeps that override.
    """
    written: list[str] = []
    for key, value in settings.items():
        text = _coerce(value)
        if not text:
            continue
        env[env_name(key)] = text
        written.append(key)
    return written


def describe(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The active overrides as ``{key: value}`` for diagnostics (``jobreach doctor``).

    Only keys that are actually set appear; an empty dict means "all defaults".
    Values stay raw strings rather than parsed forms, so ``doctor`` answers
    "which settings did Hermes actually hand me?" with exactly what crossed the
    process boundary.
    """
    active: dict[str, str] = {}
    for key in CONFIG_SCHEMA_KEYS:
        value = get_setting(key, "", env)
        if value:
            active[key] = value
    return active


def snapshot_path(env: Mapping[str, str] | None = None) -> Path:
    """Location of the persistent settings snapshot ($JOBREACH_HOME/settings.json).

    *env* exists so a caller (and a test) can resolve the path from a mapping
    instead of the process environment. Note the fallback: a mapping that names
    neither ``JOBREACH_HOME`` nor ``HERMES_HOME`` ends up at
    :func:`config.jobreach_home`, which reads the **process** environment — so a
    caller that wants to stay hermetic must pass one of the two keys, and an
    ``env`` that is merely incomplete is not a sandbox.
    """
    if env is not None:
        raw = (env.get("JOBREACH_HOME", "") or "").strip()
        if raw:
            home = Path(raw).expanduser()
            home.mkdir(parents=True, exist_ok=True)
            return home / "settings.json"
        raw_hermes = (env.get("HERMES_HOME", "") or "").strip()
        if raw_hermes:
            home = Path(raw_hermes).expanduser() / "plugin-data" / config.PLUGIN_ID
            home.mkdir(parents=True, exist_ok=True)
            return home / "settings.json"
    return config.jobreach_home() / "settings.json"


def publish_snapshot(
    settings: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> Path | None:
    """Write the plugin's last-known configuration to disk as a JSON snapshot.

    Writes a JSON object mapping environment variable names to string values
    (e.g. ``{"JOBREACH_SETTING_DEFAULT_SOURCES": "wantedly,green"}``) to
    ``$JOBREACH_HOME/settings.json``. Reuses :func:`_coerce` and :func:`env_name`
    so the resulting file contains exactly what :func:`apply_default_settings`
    would inject into a child's environment. An empty or blank-free *settings*
    mapping writes ``{}``, legitimately signaling "the user has configured
    nothing".

    Why this snapshot exists: the cron monitor is an independent process started
    by Hermes' scheduler that never imports the plugin and cannot call
    ``ctx.get_config()``. Baking the settings into the monitor script at install
    time would freeze them; publishing this snapshot allows scheduled runs to
    resolve settings dynamically at runtime.

    Best-effort by contract: catches every exception, logs at debug level, and
    returns ``None`` on failure, otherwise the :class:`Path` written.
    """
    try:
        path = snapshot_path(env)
        payload: dict[str, str] = {}
        for key, value in settings.items():
            text = _coerce(value)
            if not text:
                continue
            payload[env_name(key)] = text
        content = json.dumps(payload, indent=2) + "\n"
        tmp_path = path.with_name(f".{path.name}.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)
        logger.debug(
            "published settings snapshot",
            extra={"path": str(path), "keys": list(payload.keys())},
        )
        return path
    except Exception as exc:
        logger.debug("could not publish settings snapshot", extra={"error": str(exc)})
        return None


def load_snapshot(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read the settings snapshot, returning non-empty ``JOBREACH_SETTING_*`` pairs.

    Returns only keys starting with :data:`SETTING_PREFIX` whose values are
    non-empty strings. Any failure (missing file, junk JSON, wrong shape, or
    unreadable file) returns an empty dict ``{}``. Never raises.
    """
    try:
        path = snapshot_path(env)
        if not path.is_file():
            return {}
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            return {}
        result: dict[str, str] = {}
        for key, val in data.items():
            if (
                isinstance(key, str)
                and key.startswith(SETTING_PREFIX)
                and isinstance(val, str)
                and val.strip()
            ):
                result[key] = val.strip()
        return result
    except Exception as exc:
        logger.debug("could not load settings snapshot", extra={"error": str(exc)})
        return {}
