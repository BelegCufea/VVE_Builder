"""
Common utility functions for the VVE Builder project.

This module consolidates reusable functions used across multiple scripts
to avoid code duplication and ensure consistency.
"""

import json
import logging
import re
import struct
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
import jiwer
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple, Union, Set

from libs.tts_voicebox import transcribe_wav

from libs.appconfig import cfg


# ============================================================================
# Base36 Encoding/Decoding
# ============================================================================

BASE36_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def from_base36(s: str) -> int:
    """
    Convert a base36 string to an integer.

    Args:
        s: Base36 encoded string (case-insensitive)

    Returns:
        Integer value of the base36 string

    Raises:
        ValueError: If string contains invalid base36 characters

    Example:
        >>> from_base36("0009IX")
        12345
    """
    s = s.upper()
    value = 0
    for ch in s:
        idx = BASE36_ALPHABET.index(ch)  # raises ValueError on bad char
        value = value * 36 + idx
    return value


def to_base36(n: int, width: int = 6) -> str:
    """
    Convert an integer to a base36 string.

    Args:
        n: Integer to convert
        width: Minimum width of output string (zero-padded)

    Returns:
        Base36 encoded string, uppercase, zero-padded to width

    Example:
        >>> to_base36(12345, 6)
        '0009IX'
    """
    if n == 0:
        return "0" * width

    digits = []
    while n > 0:
        digits.append(BASE36_ALPHABET[n % 36])
        n //= 36

    result = "".join(reversed(digits))
    return result.zfill(width)


# ============================================================================
# Filename Pattern Matching
# ============================================================================

def filename_re() -> re.Pattern:
    """
    Build a regex pattern for matching TTS voice filenames.

    Matches filenames like TS000ABC.WAV where TS is cfg.FILENAME_PREFIX
    and 000ABC is the base36-encoded StrRef.

    Pattern is built fresh each call so config changes take effect immediately.

    Returns:
        Compiled regex pattern (case-insensitive) with one capture group
        for the 6-character base36 portion

    Example:
        >>> pattern = filename_re()
        >>> match = pattern.match("TS0009IX.WAV")
        >>> match.group(1)
        '0009IX'
    """
    return re.compile(
        re.escape(cfg.FILENAME_PREFIX) + r"([0-9A-Za-z]{6})\.WAV$",
        re.IGNORECASE,
    )


# ============================================================================
# Configuration Loading
# ============================================================================

def load_patcher_config(config_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Load the patcher configuration from a JSON file.

    The configuration file contains text transformation rules including
    identity tokens, gender tokens, and phonetic rules for TTS preparation.

    Args:
        config_path: Path to the JSON configuration file

    Returns:
        The loaded configuration dictionary

    Raises:
        FileNotFoundError: If the configuration file doesn't exist
        json.JSONDecodeError: If the file contains invalid JSON

    Example:
        >>> config = load_patcher_config("patcher-config.json")
        >>> pc_name = config.get("pcName", "CHARNAME")
    """
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def convert_replacement(replacement: str) -> str:
    """
    Convert .NET-style backreferences ($1, $2, etc.) to Python-style (\1, \2, etc.)
    """
    # Replace $1 with \1, $2 with \2, etc.
    # Using \g<1> ensures the backreference works correctly
    return re.sub(r'\$(\d+)', r'\\g<\1>', replacement)


def preprocess_text(text: str, patcher_config: Dict[str, Any]) -> str:
    """
    Apply comprehensive text transformations for TTS preparation.

    Processes the input text through several transformation stages:
        1. Identity tokens: Replace <CHARNAME>, <GABBER>, <RACE>, <PRO_RACE>
        2. Gender tokens: Replace <HE>, <SHE>, <HIS>, <HER>, <HIM>, etc.
        3. Phonetic rules: Apply regex-based substitutions
        4. Token cleanup: Remove any remaining <...> tokens

    Args:
        text: The raw input text from the CSV file
        patcher_config: Loaded patcher configuration dictionary

    Returns:
        The preprocessed text, ready for TTS generation

    Example:
        >>> config = {"pcName": "Gorion", "genderTokens": {"HE": {"male": "he"}}}
        >>> preprocess_text("Talk to <CHARNAME>", config)
        'Talk to Gorion'
    """
    # Stage 1: Identity tokens
    identity_tokens = patcher_config.get("identityTokens", {})

    token_map = {}
    for token, source_key in identity_tokens.items():
        if source_key in patcher_config:
            token_map[token] = patcher_config[source_key]

    for token, replacement in token_map.items():
        text = text.replace(f"<{token}>", replacement)

    # Stage 2: Gender tokens
    pc_gender = patcher_config.get("pcGender", "neutral")
    gender_tokens = patcher_config.get("genderTokens", {})

    for token, forms in gender_tokens.items():
        replacement = forms.get(pc_gender, "")
        if replacement:
            text = text.replace(f"<{token}>", replacement)

    # Stage 3: Phonetic rules
    phonetic_rules = patcher_config.get("phoneticRules", [])

    for rule in phonetic_rules:
        pattern = rule.get("pattern")
        replacement = rule.get("replacement", "")
        case_sensitive = rule.get("caseSensitive", False)

        # Convert .NET-style backreferences to Python-style
        replacement = convert_replacement(replacement)

        if not pattern:
            continue

        flags = 0 if case_sensitive else re.IGNORECASE

        try:
            text = re.sub(pattern, replacement, text, flags=flags)
        except re.error:
            continue

    # Stage 4: Remove any remaining tokens
    text = re.sub(r"<[^>]+>", "", text)

    return text


# ============================================================================
# dialog.tlk Parsing
# ============================================================================

@dataclass
class TlkEntry:
    """
    Represents a single entry from a dialog.tlk file.

    Attributes:
        strref: The string reference ID (index in the TLK file).
        flags: Bitmask of flags for this entry (gender, sound, etc.).
        sound_resref: The sound resource reference (8-character identifier).
        text: The actual text content of the entry.
    """
    strref: int
    flags: int
    sound_resref: str
    text: str


def parse_dialog_tlk(path: Path, encoding: Optional[str] = None) -> Dict[int, TlkEntry]:
    """
    Parse a dialog.tlk / dialogf.tlk file into strref -> TlkEntry.

    Reads the binary TLK file format used by Infinity Engine games and
    extracts each string entry with its associated metadata.

    Args:
        path: Path to the .tlk file.
        encoding: Text encoding to use for decoding strings.
            Defaults to cfg.TEXT_ENCODING or "utf-8".

    Returns:
        Dict mapping strref (int) to TlkEntry.

    Raises:
        ValueError: If the file signature isn't "TLK ".
        FileNotFoundError: If the file doesn't exist.

    Example:
        >>> tlk = parse_dialog_tlk(Path("lang/en_US/dialog.tlk"))
        >>> entry = tlk.get(12345)
        >>> print(entry.text)
    """
    encoding_str = encoding or cfg.TEXT_ENCODING or "utf-8"
    data = path.read_bytes()
    signature, version, lang_id, count, strings_offset = struct.unpack_from(
        "<4s4sHII", data, 0
    )
    if signature != b"TLK ":
        raise ValueError(f"Not a TLK file: {path} (signature={signature!r})")

    entries: Dict[int, TlkEntry] = {}
    pos = 18  # header size
    for strref in range(count):
        flags, sound_resref_raw, vol_var, pitch_var, text_off, text_len = (
            struct.unpack_from("<H8sIII I", data, pos)
        )
        sound_resref = sound_resref_raw.split(b"\x00", 1)[0].decode(encoding_str, errors="replace")
        text_start = strings_offset + text_off
        text = data[text_start:text_start + text_len].decode(encoding_str, errors="replace")
        entries[strref] = TlkEntry(strref, flags, sound_resref, text)
        pos += 26  # entry size

    return entries


def find_dialog_tlk(game_dir: Path) -> Path:
    """
    Locate dialog.tlk under <game_dir>/lang/*/, preferring en_us.

    Searches the game directory's lang subdirectories for dialog.tlk,
    preferring the en_us locale if available.

    Args:
        game_dir: Root game directory path.

    Returns:
        Path to the found dialog.tlk file.

    Raises:
        FileNotFoundError: If no dialog.tlk is found in any lang directory.

    Example:
        >>> tlk_path = find_dialog_tlk(Path("C:/BaldursGate"))
        >>> print(tlk_path)
        C:/BaldursGate/lang/en_US/dialog.tlk
    """
    candidates = list(game_dir.glob("lang/*/dialog.tlk"))
    if not candidates:
        raise FileNotFoundError(f"No dialog.tlk found under {game_dir}/lang/*/")
    for c in candidates:
        if c.parent.name.lower() == "en_us":
            return c
    return candidates[0]


def find_dialogf_tlk(dialog_tlk_path: Path) -> Optional[Path]:
    """
    Find the dialogf.tlk file adjacent to dialog.tlk.

    dialogf.tlk contains female-gendered versions of strings from dialog.tlk.

    Args:
        dialog_tlk_path: Path to the dialog.tlk file.

    Returns:
        Path to dialogf.tlk if it exists, None otherwise.

    Example:
        >>> tlk_path = Path("lang/en_US/dialog.tlk")
        >>> tlkf_path = find_dialogf_tlk(tlk_path)
        >>> if tlkf_path:
        ...     print("Female dialog found")
    """
    candidate = dialog_tlk_path.parent / "dialogf.tlk"
    return candidate if candidate.exists() else None


def load_valid_strrefs(game_dir: Path) -> Set[int]:
    """
    Load the set of strref numbers that actually exist in a game install's
    dialog.tlk (+ dialogf.tlk, if present).

    A strref is considered valid if it's present in either file, since
    WeiDU/the engine will resolve it from whichever TLK is active for the
    player's chosen game language/gender.

    Args:
        game_dir: Root game directory (containing lang/*/dialog.tlk).

    Returns:
        Set of valid strref integers.

    Example:
        >>> valid_strrefs = load_valid_strrefs(Path("C:/BaldursGate"))
        >>> print(f"Game has {len(valid_strrefs)} valid strrefs")
    """
    tlk_path = find_dialog_tlk(game_dir)
    valid = set(parse_dialog_tlk(tlk_path).keys())

    tlkf_path = find_dialogf_tlk(tlk_path)
    if tlkf_path is not None:
        valid |= set(parse_dialog_tlk(tlkf_path).keys())

    return valid


# ============================================================================
# File System Utilities
# ============================================================================

def iter_files_ci(directory: Path, extension: str) -> Iterator[Path]:
    """
    Iterate over files with a given extension (case-insensitive).

    Args:
        directory: Directory to search
        extension: File extension to match (without dot, e.g., "dlg")

    Yields:
        Path objects for matching files

    Example:
        >>> for dlg_file in iter_files_ci(Path("extracted"), "dlg"):
        ...     print(dlg_file.name)
    """
    ext_lower = extension.lower()
    for entry in directory.iterdir():
        if entry.is_file() and entry.suffix.lower() == f".{ext_lower}":
            yield entry


# ============================================================================
# Case-Insensitive Dictionary
# ============================================================================

class CaseInsensitiveDict(dict):
    """
    A dictionary with case-insensitive string keys while preserving original key casing.

    This class allows lookups with any case variation while maintaining the
    original casing of keys for display and iteration.

    Example:
        >>> d = CaseInsensitiveDict()
        >>> d["Hello"] = "World"
        >>> d["HELLO"]
        'World'
        >>> list(d.keys())
        ['Hello']
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize the case-insensitive dictionary.

        Args:
            *args: Positional arguments (dict, iterable of pairs, or mapping)
            **kwargs: Keyword arguments for initial key-value pairs
        """
        self._keys: Dict[str, str] = {}
        super().__init__()
        if args or kwargs:
            self.update(*args, **kwargs)

    def __setitem__(self, key: str, value: Any) -> None:
        """
        Set an item with case-insensitive key handling.

        If a key with the same lowercase version already exists, the original
        key is replaced with the new casing.

        Args:
            key: The key to set (case-insensitive)
            value: The value to associate with the key
        """
        if isinstance(key, str):
            lower = key.lower()
            old_canonical = self._keys.get(lower)
            if old_canonical is not None and old_canonical != key:
                super().pop(old_canonical, None)
            self._keys[lower] = key
            super().__setitem__(key, value)
        else:
            super().__setitem__(key, value)

    def __getitem__(self, key: str) -> Any:
        """
        Get an item with case-insensitive key lookup.

        Args:
            key: The key to look up (case-insensitive)

        Returns:
            The value associated with the key

        Raises:
            KeyError: If the key doesn't exist
        """
        if isinstance(key, str):
            canonical = self._keys.get(key.lower())
            if canonical is None:
                raise KeyError(key)
            return super().__getitem__(canonical)
        return super().__getitem__(key)

    def __delitem__(self, key: str) -> None:
        """
        Delete an item with case-insensitive key lookup.

        Args:
            key: The key to delete (case-insensitive)

        Raises:
            KeyError: If the key doesn't exist
        """
        if isinstance(key, str):
            canonical = self._keys.get(key.lower())
            if canonical is None:
                raise KeyError(key)
            self._keys.pop(key.lower())
            super().__delitem__(canonical)
        else:
            super().__delitem__(key)

    def __contains__(self, key: object) -> bool:
        """
        Check if a key exists (case-insensitive).

        Args:
            key: The key to check

        Returns:
            True if the key exists, False otherwise
        """
        if isinstance(key, str):
            return key.lower() in self._keys
        return super().__contains__(key)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get an item with a default value if not found (case-insensitive).

        Args:
            key: The key to look up
            default: Value to return if key is not found

        Returns:
            The value associated with the key, or default if not found
        """
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key: str, *args) -> Any:
        """
        Remove and return a value (case-insensitive).

        Args:
            key: The key to remove
            *args: Optional default value if key not found

        Returns:
            The popped value, or default if provided and key not found

        Raises:
            KeyError: If key not found and no default provided
        """
        if isinstance(key, str):
            canonical = self._keys.get(key.lower())
            if canonical is None:
                if args:
                    return args[0]
                raise KeyError(key)
            self._keys.pop(key.lower())
            return super().pop(canonical)
        return super().pop(key, *args)

    def update(self, *args, **kwargs) -> None:
        """
        Update the dictionary with key-value pairs (case-insensitive).

        Args:
            *args: Dictionary or iterable of pairs
            **kwargs: Keyword arguments
        """
        if args:
            other = args[0]
            if isinstance(other, dict):
                for key, value in other.items():
                    self[key] = value
            else:
                for key, value in other:
                    self[key] = value
        for key, value in kwargs.items():
            self[key] = value

    def copy(self) -> 'CaseInsensitiveDict':
        """
        Create a shallow copy of the dictionary.

        Returns:
            A new CaseInsensitiveDict with the same keys and values
        """
        new_dict = CaseInsensitiveDict()
        new_dict._keys = self._keys.copy()
        for key in self._keys.values():
            new_dict[key] = super(CaseInsensitiveDict, new_dict).__getitem__(key)
        return new_dict

    def __repr__(self) -> str:
        """Return a string representation of the dictionary."""
        items = {self._keys.get(k.lower(), k): v for k, v in super().items()}
        return f"{self.__class__.__name__}({items!r})"


def get_canonical_key(d: CaseInsensitiveDict, key: str) -> Optional[str]:
    """
    Get the canonical (original-casing) key from a CaseInsensitiveDict.

    Args:
        d: The CaseInsensitiveDict to query
        key: The key to get the canonical form of

    Returns:
        The original-cased key if it exists, None otherwise

    Example:
        >>> d = CaseInsensitiveDict({"Hello": "World"})
        >>> get_canonical_key(d, "HELLO")
        'Hello'
    """
    if not isinstance(key, str):
        return key if key in d else None
    return d._keys.get(key.lower())


# ============================================================================
# Audio Processing
# ============================================================================

def get_audio_duration(audio_path: Union[Path, str]) -> Optional[float]:
    """
    Get the duration of an audio file in seconds using ffprobe.

    Args:
        audio_path: Path to the audio file.

    Returns:
        Duration in seconds, or None if it could not be determined
        (missing file, missing ffprobe, unreadable format, etc.).

    Example:
        >>> duration = get_audio_duration("voice.wav")
        >>> if duration:
        ...     print(f"Audio is {duration:.2f}s long")
    """
    logger = logging.getLogger(__name__)
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(audio_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
        return None
    except Exception as e:
        logger.error(f"Failed to get duration for {audio_path}: {e}")
        return None


def convert_to_ogg(
    input_path: Union[Path, str],
    output_path: Union[Path, str],
    quality: Optional[int] = None
) -> bool:
    """
    Convert audio file to Ogg Vorbis format using ffmpeg.

    Args:
        input_path: Path to input audio file.
        output_path: Path to output Ogg file (will be overwritten if exists).
        quality: Vorbis quality (0-10, 4 is good quality/size balance).
            Defaults to cfg.OGG_QUALITY if not given.

    Returns:
        True if conversion succeeded, False otherwise.

    Example:
        >>> if convert_to_ogg("input.wav", "output.ogg", quality=4):
        ...     print("Conversion successful")
    """
    if quality is None:
        quality = cfg.OGG_QUALITY
    cmd = [
        'ffmpeg',
        '-y',                      # Overwrite output files
        '-i', str(input_path),     # Input file
        '-c:a', 'libvorbis',       # Use libvorbis codec
        '-qscale:a', str(quality), # Quality setting
        '-f', 'ogg',               # Force Ogg container format
        str(output_path)
    ]

    logger = logging.getLogger(__name__)
    try:
        logger.debug(f"Converting: {input_path} -> {output_path}")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.debug("Conversion successful")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg error: {e.stderr}")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found! Please install ffmpeg.")
        return False


# ============================================================================
# Time / Progress Formatting
# ============================================================================

def format_time(seconds: float) -> str:
    """
    Convert a duration in seconds to a human-readable string.

    Converts seconds to a compact format with appropriate units:
    - Under 60 seconds: "Xs" (e.g., "45.5s")
    - 1-59 minutes: "XmYs" (e.g., "5m30s")
    - 1-23 hours: "XhYm" (e.g., "2h15m")
    - 24+ hours: "XdYh" (e.g., "3d5h")

    Args:
        seconds: Duration in seconds.

    Returns:
        A formatted string representing the duration.

    Example:
        >>> format_time(3665)
        '1h1m'
        >>> format_time(125.5)
        '2m5s'
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m{secs}s"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    if hours < 24:
        return f"{hours}h{mins}m"
    days = int(hours // 24)
    hrs = int(hours % 24)
    return f"{days}d{hrs}h"


def format_finish_time(eta_seconds: float) -> str:
    """
    Return the expected finish time as a formatted time string.

    If the estimated time is today, returns time in "HH:MM:SS" format.
    Otherwise, includes the date in locale-appropriate format.

    Args:
        eta_seconds: Estimated seconds until completion.

    Returns:
        Formatted finish time string, or "..." if eta_seconds <= 0.

    Example:
        >>> format_finish_time(3600)  # 1 hour from now
        '15:30:00'
    """
    if eta_seconds > 0:
        finish = datetime.now() + timedelta(seconds=eta_seconds)
        if finish.date() == datetime.now().date():
            return finish.strftime("%H:%M:%S")
        return finish.strftime("%x %X")
    return "..."



# ============================================================================
# Voicebox Transcription & Similarity Scoring
# ============================================================================

# Display color for each similarity band, keyed by the label returned from
# score_status(). Shared so every GUI that shows a score colors it the same way.
SIMILARITY_STATUS_COLORS = {
    "Excellent": "#2ecc71",
    "Good": "#c8a900",
    "Poor": "#e67e22",
    "Bad": "#e74c3c",
}


def transcribe_via_voicebox(
    wav_path: Union[Path, str],
    timeout: Optional[float] = None,
    retry_count: Optional[int] = None,
    retry_delay: Optional[float] = None,
    language: Optional[str] = None,
) -> Tuple[str, bool, float]:
    """
    Send a WAV file to the Voicebox /transcribe endpoint, with retries.

    This function provides compatibility with the tts_voicebox module's
    transcribe_wav function, handling the actual API call with retry logic.

    Args:
        wav_path: Path to the .wav file to transcribe.
        timeout: Per-attempt request timeout in seconds.
            Defaults to cfg.SAMPLE_TIMEOUT_SECONDS.
        retry_count: Number of retries after the first attempt.
            Defaults to cfg.SAMPLE_RETRY_COUNT.
        retry_delay: Seconds to wait between retries.
            Defaults to cfg.SAMPLE_RETRY_DELAY.
        language: Language to transcribe to (english, german etc.).
            Defaults to cfg.TRANSCRIPTION_LANGUAGE.            

    Returns:
        Tuple of (text, success, duration). On failure, text is a "<ERROR: ...>"
        placeholder describing the last error, success is False, and duration is 0.0.

    Example:
        >>> text, success, duration = transcribe_via_voicebox("sample.wav")
        >>> if success:
        ...     print(f"Transcribed: {text}")
    """
    logger = logging.getLogger(__name__)
    wav_path = Path(wav_path)

    logger.debug(f"Transcribing {wav_path.name} via Voicebox API")
    try:
        result = transcribe_wav(
            wav_path,
            timeout=timeout,
            retry_count=retry_count,
            retry_delay=retry_delay,
            language=language
        )

        if result[1]:  # success
            logger.info(f"Successfully transcribed {wav_path.name}")
        else:
            logger.error(f"Transcription failed for {wav_path.name}: {result[0]}")

        return result
    except Exception as ex:
        logger.error(f"Unexpected error transcribing {wav_path.name}: {ex}")
        return f"<ERROR: {ex}>", False, 0.0


_jiwer_transform = jiwer.Compose([
    jiwer.ToLowerCase(),
    jiwer.RemovePunctuation(),
    jiwer.RemoveMultipleSpaces(),
    jiwer.Strip(),
    jiwer.ReduceToListOfListOfWords(),
])


def similarity_score(reference: str, hypothesis: str) -> float:
    """
    Compute a similarity score (0-100) between expected and actual text.

    Case-insensitive and whitespace-trimmed; combines difflib's SequenceMatcher
    ratio (character-level accuracy) with a jiwer-based completeness penalty,
    so a hypothesis that's an incomplete/truncated version of the reference
    scores meaningfully lower than raw character overlap alone would suggest.

    Args:
        reference: The expected/correct text.
        hypothesis: The text being evaluated against the reference.

    Returns:
        Similarity score from 0.0 to 100.0, rounded to 2 decimal places.

    Example:
        >>> similarity_score("Hello world", "Hello world")
        100.0
        >>> similarity_score("Hello world", "Hello")
        45.45  # Character overlap is high, but completeness penalty applies
    """
    a = reference.strip().lower()
    b = hypothesis.strip().lower()

    base = SequenceMatcher(None, a, b, autojunk=False).ratio() * 100

    if not a or not b:
        return round(base, 2)

    out = jiwer.process_words(
        reference, hypothesis,
        reference_transform=_jiwer_transform,
        hypothesis_transform=_jiwer_transform,
    )
    ref_word_count = out.hits + out.substitutions + out.deletions
    completeness = 1.0 if ref_word_count == 0 else 1 - (out.deletions / ref_word_count)

    return round(base * completeness, 2)


def transcribe_and_score(
    wav_path: Union[Path, str],
    expected_text: str,
    timeout: Optional[float] = None,
    retry_count: Optional[int] = None,
    retry_delay: Optional[float] = None,
    language: Optional[str] = None
) -> Dict[str, Any]:
    """
    Transcribe a WAV file via Voicebox and score it against expected text.

    Convenience wrapper combining transcribe_via_voicebox() and
    similarity_score() into the single call most callers need.

    Args:
        wav_path: Path to the .wav file to transcribe.
        expected_text: The text the audio is expected to say.
        timeout: Per-attempt request timeout in seconds.
            Defaults to cfg.SAMPLE_TIMEOUT_SECONDS.
        retry_count: Number of retries after the first attempt.
            Defaults to cfg.SAMPLE_RETRY_COUNT.
        retry_delay: Seconds to wait between retries.
            Defaults to cfg.SAMPLE_RETRY_DELAY.
        language: Language to transcribe to (english, german etc.).
            Defaults to cfg.TRANSCRIPTION_LANGUAGE.       

    Returns:
        Dict with keys "transcribed_text" (str), "success" (bool),
        "score" (float, 0-100), and "duration" (float, seconds).
        Note: a score is still computed on failure, comparing expected_text
        against the "<ERROR: ...>" placeholder, so callers can rely on
        "score" always being a number.

    Example:
        >>> result = transcribe_and_score("sample.wav", "Hello world")
        >>> if result["success"]:
        ...     print(f"Score: {result['score']:.1f}%")
    """
    transcribed_text, success, duration = transcribe_via_voicebox(
        wav_path, timeout=timeout, retry_count=retry_count, retry_delay=retry_delay, language=language
    )
    score = similarity_score(expected_text, transcribed_text)
    return {
        "transcribed_text": transcribed_text,
        "success": success,
        "score": score,
        "duration": duration,
    }


def score_status(score: Optional[float]) -> Tuple[str, str]:
    """
    Classify a similarity score into a label and display color.

    Bands are read from cfg.SIMILARITY_EXCELLENT / SIMILARITY_GOOD /
    SIMILARITY_POOR (anything below SIMILARITY_POOR is "Bad").

    Args:
        score: Similarity score (0-100), or None if not yet available.

    Returns:
        Tuple of (label, hex_color). Returns ("In progress", "#888888")
        when score is None or not numeric.

    Example:
        >>> label, color = score_status(85.5)
        >>> print(f"{label}: {color}")
        'Good: #c8a900'
    """
    if score is None or not isinstance(score, (int, float)):
        return "In progress", "#888888"
    if score >= cfg.SIMILARITY_EXCELLENT:
        label = "Excellent"
    elif score >= cfg.SIMILARITY_GOOD:
        label = "Good"
    elif score >= cfg.SIMILARITY_POOR:
        label = "Poor"
    else:
        label = "Bad"
    return label, SIMILARITY_STATUS_COLORS[label]


# ============================================================================
# Logging Setup
# ============================================================================

def setup_logging(
    script_name: str,
    level: int = logging.INFO,
    console_level: Optional[int] = None,
    file_level: Optional[int] = None
) -> logging.Logger:
    """
    Set up standardized logging for a script.

    Creates a logger that writes to both console and a log file in the
    configured log directory. The log file includes timestamps and log levels,
    while the console output is clean (message only) by default.

    Args:
        script_name: Name of the script (used for the log filename).
        level: Default log level for the logger.
        console_level: Log level for console output (defaults to level).
        file_level: Log level for file output (defaults to level).

    Returns:
        A configured logger instance.

    Example:
        >>> logger = setup_logging("my_script")
        >>> logger.info("This goes to both console and log file")
    """
    log_dir = Path(cfg.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"{script_name}.log"

    logger = logging.getLogger(script_name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(file_level if file_level is not None else level)
    file_handler.setFormatter(
        logging.Formatter(
            fmt='%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    )
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level if console_level is not None else level)
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)

    logger.propagate = False

    return logger