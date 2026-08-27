"""
appconfig - small shared configuration library for the BG TTS toolset.

Several independent scripts (generate.py, generate_gui.py, the dialog
patcher, etc.) use config variables that share the same name and the
same intended value (Voicebox URL, retry settings, paths, ...). This
module is the single place those shared defaults live, and the single
JSON file on disk that persists user overrides, so changing a setting
in one script's UI is visible to every other script.

Design:
    - DEFAULTS below is the one and only place default values for
      shared keys are declared. Scripts do NOT bring their own
      defaults - they just ask appconfig for a value. Changing a
      default (e.g. FILENAME_PATTERN) here changes it for every
      script at once.
    - There is no "load into a module-level variable at import time"
      step anywhere. Reading `cfg.FILENAME_PATTERN` (or calling
      `get("FILENAME_PATTERN")`) always returns the *current* value -
      the saved override if one exists, else the default above. If
      something else in the same running process calls
      `cfg.FILENAME_PATTERN = "..."` a moment later, the very next
      read sees the new value. There is nothing to refresh or go
      stale, because nothing is cached in the caller.
    - set()/`cfg.KEY = value` records an override and persists it to
      appconfig.json immediately - but only if the value actually
      differs from the default (see "Sparse writes" below).

Usage:
    from appconfig import cfg, get, set

    url = cfg.BASE_URL              # attribute-style, live read
    url = get("BASE_URL")           # equivalent function-style read

    cfg.RETRY_COUNT = 5             # attribute-style, persists immediately
    set("RETRY_COUNT", 5)           # equivalent function-style write

Sparse writes:
    appconfig.json only ever contains keys whose value differs from
    DEFAULTS. Setting a key back to its default value removes it from
    the file rather than writing it out redundantly, so appconfig.json
    always shows you exactly what's actually been customized, and
    bumping a default in code takes effect for everyone who never
    overrode that key.

Type safety for non-JSON-native defaults:
    JSON only round-trips str/int/float/bool/list/dict/None natively.
    If a default's value is some other type (a pathlib.Path, a
    compiled re.Pattern, ...), register a codec for that type in
    _CODECS below - a (encode, decode) pair of functions converting
    to/from the JSON-native form. get()/set() then transparently
    encode on the way to disk and decode on the way back out, keyed
    off the *type of the default* for that key - callers never see
    the JSON-native form, only the real type. Add a new codec any
    time you introduce a default of a new non-JSON-native type.

Storage:
    Values live in appconfig.json next to this file, shared by every
    script that imports appconfig.

A script-specific setting that only one script cares about does not
belong in DEFAULTS below - just keep it as a normal local variable (or
constant) in that script instead of routing it through appconfig.
"""

import json
import re
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_CONFIG_PATH = Path(__file__).resolve().parent / "appconfig.json"
_MISSING = object()

# ============================================================================
# Shared defaults - the single source of truth for every script.
# ============================================================================
DEFAULTS: dict = {
    # Voicebox API Configuration
    "BASE_URL": "http://10.0.50.5:17600",  # http://localhost:17493 for local server, or remote URL for remote server
    "ENGINE": "qwen",
    "MODEL_SIZE": "1.7B",

    # Voicebox API Endpoints (relative to BASE_URL) - not user-editable,
    # but kept here too so every shared setting lives in one place.
    "PROFILES_ENDPOINT": "/profiles",
    "PROFILES_IMPORT_ENDPOINT": "/profiles/import",
    "GENERATE_ENDPOINT": "/generate",
    "GENERATE_STATUS_ENDPOINT": "/generate/{gen_id}/status",
    "GENERATE_CANCEL_ENDPOINT": "/generate/{gen_id}/cancel",
    "AUDIO_ENDPOINT": "/audio/{gen_id}",

    # Generation Timeout Safeguards
    "ENABLE_TIMEOUT_SAFEGUARD": True,
    "TIMEOUT_MAX_SECONDS": 600,
    "TIMEOUT_MULTIPLIER": 3.0,
    "TIMEOUT_MIN_ESTIMATES": 10,

    # Retry Configuration
    "RETRY_COUNT": 3,
    "RETRY_DELAY": 5.0,

    # Audio Conversion Configuration
    "CONVERT_TO_OGG": True,
    "OGG_QUALITY": 4,
    "MAX_DURATION": 30.0,

    # File Paths
    "CSV_PATH": r"dialog-report.csv",
    "PATCHER_CONFIG_PATH": r"patcher-config.json",
    "OUTPUT_DIR": r"output",

    # Generation Limits and filters
    "LIMIT": 0,
    "TARGET_VOICES": [
        # "Jaheira",
        # "Edwin",
        # "Neera",
        # "Bodhi",
        # "Gaelan Bayle"
    ],
    "VOICE_SUBSTITUTIONS_FILE": r"voice-substitutions.json",
    "FILENAME_PATTERN": r"^TS",

    # STRREF Filtering
    "USE_STRREF_FILTER": False,
    "STRREF_FILTER_FILE": r"strrefs.json",

    # Voice Fallback Configuration
    "USE_VOICE_FALLBACK": False,
    "FALLBACK_VOICE_MALE": "BG1 Narrator",
    "FALLBACK_VOICE_FEMALE": "BG3 Narrator",
    "FALLBACK_VOICE_NEUTRAL": "Description Narrator",

    # Filename Generation
    "FORCE_GENERATED_FILENAMES": False,
    "RESREF_PREFIX": "TS",

    # Generation memory
    "SKIP_ALREADY_GENERATED": True,
    "GENERATION_MEMORY_PATH": r"generation-memory.json",

    # Logging
    "LOG_DIR": r"logs",

    # Pre-generation Summary Options
    "COMPACT_SUMMARY": True,

    # Voice Profile Auto-Provisioning
    "AUTO_PROVISION_PROFILES": True,
    "VOICES_DIR": r"voices",
    "VOICES_PREP_DIR": r"voices_prep",
    "PROFILE_PACKAGES_DIR": r"profiles",
    "PROFILE_SYNC_MAX_ATTEMPTS": 10,
    "PROFILE_SYNC_RETRY_DELAY": 3.0,

    # Voice Profile Manager
    "REALNAME_NOT_FOUND": "RealNameMissing",
}

# ============================================================================
# Codecs for defaults whose type isn't natively JSON-representable.
# Keyed by the declared type (type of the value in DEFAULTS). Add an
# entry here whenever a new default is given a type JSON can't carry
# as-is; get()/set() apply it automatically based on that key's
# DEFAULTS type - no per-key wiring needed anywhere else.
# ============================================================================
# ============================================================================
# Codecs for defaults whose type isn't natively JSON-representable.
# Keyed by a base type checked via isinstance() (so pathlib.Path's
# platform-specific subclasses - PosixPath/WindowsPath - both match the
# Path entry, rather than needing an exact type() match). Add an entry
# here whenever a new default is given a type JSON can't carry as-is;
# get()/set() apply it automatically based on that key's DEFAULTS
# value - no per-key wiring needed anywhere else.
# ============================================================================
_CODECS = [
    (Path, lambda v: str(v), lambda v: Path(v)),
    (re.Pattern, lambda v: v.pattern, lambda v: re.compile(v)),
]

_overrides: dict = {}
_loaded = False


def _ensure_loaded() -> None:
    """Lazily load overrides from disk on first use (once per process)."""
    global _loaded
    if _loaded:
        return
    with _LOCK:
        if _loaded:
            return
        if _CONFIG_PATH.exists():
            try:
                with _CONFIG_PATH.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _overrides.update(data)
            except (json.JSONDecodeError, OSError):
                # Corrupt or unreadable file - fall back to defaults
                # rather than crashing every script that imports us.
                pass
        _loaded = True


def _codec_for(key: str):
    """The (base_type, encode, decode) codec that applies to key's default, if any."""
    if key not in DEFAULTS:
        return None
    default_value = DEFAULTS[key]
    for base_type, encode_fn, decode_fn in _CODECS:
        if isinstance(default_value, base_type):
            return base_type, encode_fn, decode_fn
    return None


def _decode(key: str, raw_value: Any) -> Any:
    """Convert a JSON-native stored value back into its real type, if a codec applies."""
    codec = _codec_for(key)
    if codec is not None:
        base_type, _, decode_fn = codec
        if not isinstance(raw_value, base_type):
            return decode_fn(raw_value)
    return raw_value


def _encode(key: str, value: Any) -> Any:
    """Convert a real value into its JSON-native form for storage, if a codec applies."""
    codec = _codec_for(key)
    if codec is not None:
        base_type, encode_fn, _ = codec
        if isinstance(value, base_type):
            return encode_fn(value)
    return value


def get(key: str, fallback: Any = None) -> Any:
    """Return the current effective value for key: override, else DEFAULTS, else fallback."""
    _ensure_loaded()
    with _LOCK:
        if key in _overrides:
            return _decode(key, _overrides[key])
        if key in DEFAULTS:
            return DEFAULTS[key]
        return fallback


def set(key: str, value: Any, persist: bool = True) -> None:
    """
    Record an override for key and persist to disk immediately (unless persist=False).

    If value equals the key's default (after encoding), any existing
    override is removed instead, so appconfig.json never carries a
    redundant "override" that just restates the default.
    """
    _ensure_loaded()
    with _LOCK:
        _set_locked(key, value)
        if persist:
            _save_locked()


def set_many(values: dict, persist: bool = True) -> None:
    """Set several overrides at once, writing to disk only once."""
    _ensure_loaded()
    with _LOCK:
        for key, value in values.items():
            _set_locked(key, value)
        if persist:
            _save_locked()


def _set_locked(key: str, value: Any) -> None:
    """Apply one override in memory. Caller must hold _LOCK."""
    encoded = _encode(key, value)
    if key in DEFAULTS and encoded == _encode(key, DEFAULTS[key]):
        _overrides.pop(key, None)
    else:
        _overrides[key] = encoded


def reset(key: str, persist: bool = True) -> None:
    """Remove an override for key, reverting it to its DEFAULTS value."""
    _ensure_loaded()
    with _LOCK:
        _overrides.pop(key, None)
        if persist:
            _save_locked()


def save() -> None:
    """Force a write of current overrides to disk (set()/set_many() already do this)."""
    _ensure_loaded()
    with _LOCK:
        _save_locked()


def all_values() -> dict:
    """Return a merged dict of DEFAULTS + overrides (overrides win, decoded) - handy for a config UI."""
    _ensure_loaded()
    with _LOCK:
        merged = dict(DEFAULTS)
        for key in _overrides:
            merged[key] = _decode(key, _overrides[key])
        return merged


def unknown_keys() -> list:
    """
    Keys present in appconfig.json's overrides that no longer match any
    DEFAULTS entry - typically left behind after a key was renamed or
    removed in code. Not used anywhere yet; intended for a future
    general settings/maintenance script to surface and let the user
    clean up.
    """
    _ensure_loaded()
    with _LOCK:
        return [key for key in _overrides if key not in DEFAULTS]


def _save_locked() -> None:
    """Write _overrides to disk atomically. Caller must hold _LOCK."""
    tmp_path = _CONFIG_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(_overrides, f, indent=2, sort_keys=True, ensure_ascii=False)
    tmp_path.replace(_CONFIG_PATH)


class _ConfigProxy:
    """
    Attribute-style live view onto get()/set(), e.g. ``cfg.BASE_URL``.

    Every attribute read calls get() fresh - nothing is cached on this
    object - so if the value changes anywhere (this process or, after a
    restart, another script), the next ``cfg.KEY`` read reflects it.
    Every attribute write calls set() (and therefore persists to disk,
    or clears the override if the new value matches the default).
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        set(name, value)


cfg = _ConfigProxy()
