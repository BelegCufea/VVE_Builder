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
      defaults (e.g. FILENAME_PREFIX) here changes it for every
      script at once.
    - There is no "load into a module-level variable at import time"
      step anywhere. Reading `cfg.FILENAME_PREFIX` (or calling
      `get("FILENAME_PREFIX")`) always returns the *current* value -
      the saved override if one exists, else the default above. If
      something else in the same running process calls
      `cfg.FILENAME_PREFIX = "..."` a moment later, the very next
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
from typing import Any, Dict, List, Optional, Tuple, Callable, TypeVar, Union

_LOCK = threading.RLock()
_CONFIG_PATH = Path(__file__).resolve().parent / "appconfig.json"
_MISSING = object()

Codec = Tuple[type, Callable[[Any], Any], Callable[[Any], Any]]


# ============================================================================
# Shared defaults - the single source of truth for every script.
# ============================================================================
DEFAULTS: Dict[str, Any] = {
    # =======================================================================
    # Game location, language and encoding
    # =======================================================================

    "GAME_DIRECTORY": r"C:/Relax/BGEET",
    "LANGUAGE": "en_US",
    "TEXT_ENCODING": "utf-8",

    # =======================================================================
    # Voice generation
    # =======================================================================

    # OGG conversion settings
    "CONVERT_TO_OGG": True,
    "OGG_QUALITY": 4,

    # Generation filtering
    "LIMIT": 0,
    "TARGET_VOICES": [],

    # STRREF Filtering
    "USE_STRREF_FILTER": False,
    "STRREF_FILTER_FILE": r"strrefs.json",

    # Audio file prefix (plain text, not a regex - used for both matching
    # existing generated files and building new generated filenames).
    "FILENAME_PREFIX": "TS",

    # =======================================================================
    # VoiceBox configuration
    # =======================================================================    

    # Voicebox API Configuration
    "BASE_URL": "http://localhost:17493",  # http://localhost:17493 for local server, or remote URL for remote server
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
    "TRANSCRIBE_ENDPOINT": "/transcribe",

    # =======================================================================
    # Other variables needed by or shared between scripts
    # =======================================================================     

    # Generation Timeout Safeguards
    "ENABLE_TIMEOUT_SAFEGUARD": True,
    "TIMEOUT_MAX_SECONDS": 600,
    "TIMEOUT_MULTIPLIER": 3.0,
    "TIMEOUT_MIN_ESTIMATES": 10,

    # Retry Configuration
    "RETRY_COUNT": 3,
    "RETRY_DELAY": 5.0,

    # Audio Prepare and Conversion Configuration
    "MAX_DURATION": 30.0,
    "MIN_DURATION": 10.0,

    # File Paths
    "CSV_PATH": r"dialog-report.csv",
    "PATCHER_CONFIG_PATH": r"patcher-config.json",
    "OUTPUT_DIR": r"output",

    # Generation Limits and filters
    "VOICE_SUBSTITUTIONS_FILE": r"voice-substitutions.json",

    # Voice Fallback Configuration
    "USE_VOICE_FALLBACK": False,
    "FALLBACK_VOICE_MALE": "Default Male",
    "FALLBACK_VOICE_FEMALE": "Default Female",
    "FALLBACK_VOICE_NEUTRAL": "Default Neutral",

    # Filename Generation
    "FORCE_GENERATED_FILENAMES": False,

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
    "PROFILE_SYNC_RENEW": False,

    # Voice Profile Manager
    "REALNAME_NOT_FOUND": "RealNameMissing",

    # Mod Build
    "MOD_ROOT": r"mod",
    "MOD_NAME": "ievo",
    "MOD_TP2": r"setup.tp2",
    "MOD_TRA": r"setup.tra",

    # Voice Sample Preparation / Extraction
    "WEIDU_PATH": r"weidu/weidu.exe",
    "BLACKLIST_FILE": r"blacklist.txt",

    # RealNames to skip during voice sample preparation (also loaded from
    # BLACKLIST_FILE, one name per line).
    "BLACKLIST": [],

    # Dialog Report Preparation
    "EXTRACT_DIR": r"extracted",
    "GENDER_MAP": {1: "M", 2: "F", 3: "O", 4: "N"},

    # ============================================================================
    # Transcription check settings
    # ============================================================================

    # Transcription language
    "TRANSCRIPTION_LANGUAGE": "english",

    # How many wav files to sample per NPC directory for transcription checking
    "SAMPLES_PER_NPC": 5,

    # Per-call timeout for the /transcribe endpoint (seconds)
    "SAMPLE_TIMEOUT_SECONDS": 300,

    # Retry configuration for transcription failures
    "SAMPLE_RETRY_COUNT": 2,
    "SAMPLE_RETRY_DELAY": 2.0,

    # Similarity score thresholds (0-100) for color-coding results.
    # Used to classify the WORST (minimum) score per NPC in the NPC table.
    "SIMILARITY_EXCELLENT": 90.0,
    "SIMILARITY_GOOD": 65.0,
    "SIMILARITY_POOR": 20.0,
}

# ============================================================================
# Codecs for defaults whose type isn't natively JSON-representable.
# Keyed by a base type checked via isinstance() (so pathlib.Path's
# platform-specific subclasses - PosixPath/WindowsPath - both match the
# Path entry, rather than needing an exact type() match). Add an entry
# here whenever a new default is given a type JSON can't carry as-is;
# get()/set() apply it automatically based on that key's DEFAULTS
# value - no per-key wiring needed anywhere else.
# ============================================================================
_CODECS: List[Codec] = [
    (Path, lambda v: str(v), lambda v: Path(v)),
    (re.Pattern, lambda v: v.pattern, lambda v: re.compile(v)),
]

_overrides: Dict[str, Any] = {}
_loaded: bool = False


def _ensure_loaded() -> None:
    """
    Lazily load overrides from disk on first use (once per process).

    This function is idempotent and thread-safe. It loads the JSON
    configuration file only once, merging any stored overrides into
    the _overrides dictionary. If the file is missing or corrupted,
    falls back to defaults without raising an exception.
    """
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


def _codec_for(key: str) -> Optional[Codec]:
    """
    Retrieve the codec that applies to a given key's default value.

    Args:
        key: Configuration key to look up.

    Returns:
        The (base_type, encode_fn, decode_fn) codec tuple if one applies,
        None if the key doesn't exist in DEFAULTS or has a JSON-native type.
    """
    if key not in DEFAULTS:
        return None
    default_value = DEFAULTS[key]
    for base_type, encode_fn, decode_fn in _CODECS:
        if isinstance(default_value, base_type):
            return base_type, encode_fn, decode_fn
    return None


def _decode(key: str, raw_value: Any) -> Any:
    """
    Convert a JSON-native stored value back into its real type.

    Args:
        key: Configuration key to decode for.
        raw_value: JSON-native value from storage.

    Returns:
        Decoded value in its native type, or the raw value if no codec applies.
    """
    codec = _codec_for(key)
    if codec is not None:
        base_type, _, decode_fn = codec
        if not isinstance(raw_value, base_type):
            return decode_fn(raw_value)
    return raw_value


def _encode(key: str, value: Any) -> Any:
    """
    Convert a real value into its JSON-native form for storage.

    Args:
        key: Configuration key to encode for.
        value: Native type value to encode.

    Returns:
        JSON-native representation of the value, or the value unchanged
        if no codec applies.
    """
    codec = _codec_for(key)
    if codec is not None:
        base_type, encode_fn, _ = codec
        if isinstance(value, base_type):
            return encode_fn(value)
    return value


def get(key: str, fallback: Any = None) -> Any:
    """
    Return the current effective value for a configuration key.

    Checks overrides first, then DEFAULTS, then the provided fallback.
    Values are automatically decoded from JSON-native form if a codec applies.

    Args:
        key: Configuration key to look up.
        fallback: Value to return if the key is not found in DEFAULTS.

    Returns:
        The effective configuration value, or fallback if not found.
    """
    _ensure_loaded()
    with _LOCK:
        if key in _overrides:
            return _decode(key, _overrides[key])
        if key in DEFAULTS:
            return DEFAULTS[key]
        return fallback


def set(key: str, value: Any, persist: bool = True) -> None:
    """
    Record an override for a configuration key and persist to disk.

    If the new value equals the key's default (after encoding), any existing
    override is removed instead, so appconfig.json never carries a redundant
    "override" that just restates the default.

    Args:
        key: Configuration key to set.
        value: New value for the key.
        persist: If True (default), immediately writes to disk.
    """
    _ensure_loaded()
    with _LOCK:
        _set_locked(key, value)
        if persist:
            _save_locked()


def set_many(values: Dict[str, Any], persist: bool = True) -> None:
    """
    Set several overrides at once, writing to disk only once.

    This is more efficient than calling set() multiple times when updating
    multiple configuration values.

    Args:
        values: Dictionary of key-value pairs to set.
        persist: If True (default), writes to disk after all updates.
    """
    _ensure_loaded()
    with _LOCK:
        for key, value in values.items():
            _set_locked(key, value)
        if persist:
            _save_locked()


def _set_locked(key: str, value: Any) -> None:
    """
    Apply one override in memory.

    Caller must hold _LOCK. If the value equals the default, removes
    any existing override rather than storing it.

    Args:
        key: Configuration key to set.
        value: New value for the key.
    """
    encoded = _encode(key, value)
    if key in DEFAULTS and encoded == _encode(key, DEFAULTS[key]):
        _overrides.pop(key, None)
    else:
        _overrides[key] = encoded


def reset(key: str, persist: bool = True) -> None:
    """
    Remove an override for a key, reverting it to its DEFAULTS value.

    Args:
        key: Configuration key to reset.
        persist: If True (default), immediately writes to disk.
    """
    _ensure_loaded()
    with _LOCK:
        _overrides.pop(key, None)
        if persist:
            _save_locked()


def save() -> None:
    """
    Force a write of current overrides to disk.

    Note: set() and set_many() already persist automatically, so this
    is typically only needed when manually modifying _overrides.
    """
    _ensure_loaded()
    with _LOCK:
        _save_locked()


def all_values() -> Dict[str, Any]:
    """
    Return a merged dictionary of DEFAULTS + overrides.

    Overrides take precedence, and all values are decoded to their
    native types. This is useful for configuration UIs that need to
    display all current settings.

    Returns:
        Dictionary containing all configuration keys with their
        current effective values.
    """
    _ensure_loaded()
    with _LOCK:
        merged = dict(DEFAULTS)
        for key in _overrides:
            merged[key] = _decode(key, _overrides[key])
        return merged


def unknown_keys() -> List[str]:
    """
    Return keys in overrides that no longer exist in DEFAULTS.

    These are typically left behind after a key was renamed or removed
    from the code. Not used anywhere currently; intended for a future
    maintenance script to surface and allow cleanup.

    Returns:
        List of unknown key names in the overrides.
    """
    _ensure_loaded()
    with _LOCK:
        return [key for key in _overrides if key not in DEFAULTS]


def _save_locked() -> None:
    """
    Write _overrides to disk atomically.

    Writes to a temporary file first, then replaces the target atomically.
    Caller must hold _LOCK.
    """
    tmp_path = _CONFIG_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(_overrides, f, indent=2, sort_keys=True, ensure_ascii=False)
    tmp_path.replace(_CONFIG_PATH)


class _ConfigProxy:
    """
    Attribute-style live view onto get()/set().

    Every attribute read calls get() fresh - nothing is cached on this
    object - so if the value changes anywhere (this process or, after a
    restart, another script), the next ``cfg.KEY`` read reflects it.
    Every attribute write calls set() (and therefore persists to disk,
    or clears the override if the new value matches the default).

    Example:
        >>> from appconfig import cfg
        >>> url = cfg.BASE_URL          # Reads current value
        >>> cfg.RETRY_COUNT = 5         # Sets and persists immediately
    """

    def __getattr__(self, name: str) -> Any:
        """
        Get a configuration value using attribute syntax.

        Args:
            name: Configuration key name.

        Returns:
            The current effective value for the key.

        Raises:
            AttributeError: If the key name starts with an underscore.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        """
        Set a configuration value using attribute syntax.

        Args:
            name: Configuration key name.
            value: New value to set.
        """
        set(name, value)


cfg = _ConfigProxy()