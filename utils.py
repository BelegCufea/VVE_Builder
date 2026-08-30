"""
Common utility functions for the VVE Builder project.

This module consolidates reusable functions used across multiple scripts
to avoid code duplication and ensure consistency.
"""

import json
import logging
import re
import subprocess
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple, Union

import requests

from appconfig import cfg


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
    """
    return re.compile(
        re.escape(cfg.FILENAME_PREFIX) + r"([0-9A-Za-z]{6})\.WAV$",
        re.IGNORECASE,
    )


# ============================================================================
# Configuration Loading
# ============================================================================

def load_patcher_config(config_path: str | Path) -> Dict[str, Any]:
    """
    Load the patcher configuration from a JSON file.
    
    Args:
        config_path: Path to the JSON configuration file
    
    Returns:
        The loaded configuration dictionary
    
    Raises:
        FileNotFoundError: If the configuration file doesn't exist
        json.JSONDecodeError: If the file contains invalid JSON
    """
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


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
    """
    # Stage 1: Identity tokens
    pc_name = patcher_config.get("pcName", "CHARNAME")
    pc_race = patcher_config.get("pcRace", "RACE")
    identity_tokens = patcher_config.get("identityTokens", [])
    
    token_map = {}
    for token in identity_tokens:
        if token in ("CHARNAME", "GABBER"):
            token_map[token] = pc_name
        elif token in ("PRO_RACE", "RACE"):
            token_map[token] = pc_race
    
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
    """

    def __init__(self, *args, **kwargs):
        self._keys: Dict[str, str] = {}
        super().__init__()
        if args or kwargs:
            self.update(*args, **kwargs)

    def __setitem__(self, key, value):
        if isinstance(key, str):
            lower = key.lower()
            old_canonical = self._keys.get(lower)
            if old_canonical is not None and old_canonical != key:
                super().pop(old_canonical, None)
            self._keys[lower] = key
            super().__setitem__(key, value)
        else:
            super().__setitem__(key, value)

    def __getitem__(self, key):
        if isinstance(key, str):
            canonical = self._keys.get(key.lower())
            if canonical is None:
                raise KeyError(key)
            return super().__getitem__(canonical)
        return super().__getitem__(key)

    def __delitem__(self, key):
        if isinstance(key, str):
            canonical = self._keys.get(key.lower())
            if canonical is None:
                raise KeyError(key)
            self._keys.pop(key.lower())
            super().__delitem__(canonical)
        else:
            super().__delitem__(key)

    def __contains__(self, key):
        if isinstance(key, str):
            return key.lower() in self._keys
        return super().__contains__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key, *args):
        if isinstance(key, str):
            canonical = self._keys.get(key.lower())
            if canonical is None:
                if args:
                    return args[0]
                raise KeyError(key)
            self._keys.pop(key.lower())
            return super().pop(canonical)
        return super().pop(key, *args)

    def update(self, *args, **kwargs):
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

    def copy(self):
        new_dict = CaseInsensitiveDict()
        new_dict._keys = self._keys.copy()
        for key in self._keys.values():
            new_dict[key] = super(CaseInsensitiveDict, new_dict).__getitem__(key)
        return new_dict

    def __repr__(self):
        items = {self._keys.get(k.lower(), k): v for k, v in super().items()}
        return f"{self.__class__.__name__}({items!r})"


def get_canonical_key(d: CaseInsensitiveDict, key: str) -> str | None:
    """Get the canonical (original-casing) key from a CaseInsensitiveDict."""
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
) -> Tuple[str, bool]:
    """
    Send a WAV file to the Voicebox /transcribe endpoint, with retries.

    Args:
        wav_path: Path to the .wav file to transcribe.
        timeout: Per-attempt request timeout in seconds.
            Defaults to cfg.SAMPLE_TIMEOUT_SECONDS.
        retry_count: Number of retries after the first attempt.
            Defaults to cfg.SAMPLE_RETRY_COUNT.
        retry_delay: Seconds to wait between retries.
            Defaults to cfg.SAMPLE_RETRY_DELAY.

    Returns:
        Tuple of (text, success). On failure, text is a "<ERROR: ...>"
        placeholder describing the last error and success is False.
    """
    logger = logging.getLogger(__name__)
    wav_path = Path(wav_path)
    timeout = cfg.SAMPLE_TIMEOUT_SECONDS if timeout is None else timeout
    retry_count = cfg.SAMPLE_RETRY_COUNT if retry_count is None else retry_count
    retry_delay = cfg.SAMPLE_RETRY_DELAY if retry_delay is None else retry_delay
    
    if retry_count is None:
        retry_count = 0
    if retry_delay is None:
        retry_delay = 0

    url = cfg.BASE_URL.rstrip("/") + "/" + cfg.TRANSCRIBE_ENDPOINT.lstrip("/")

    last_error = ""
    for attempt in range(retry_count + 1):
        try:
            with open(wav_path, "rb") as f:
                resp = requests.post(
                    url,
                    files={"file": (wav_path.name, f, "audio/wav")},
                    timeout=timeout,
                )
            resp.raise_for_status()
            return resp.json().get("text", ""), True
        except Exception as ex:
            last_error = str(ex)
            logger.debug(f"Transcribe attempt {attempt + 1} failed for {wav_path.name}: {ex}")
            if attempt < retry_count:
                time.sleep(retry_delay)

    return f"<ERROR: {last_error}>", False


def similarity_score(text_a: str, text_b: str) -> float:
    """
    Compute a similarity score (0-100) between two texts.

    Case-insensitive and whitespace-trimmed; uses difflib's SequenceMatcher
    ratio, matching the scoring approach used across the project's checking
    tools.

    Args:
        text_a: First text (order doesn't matter).
        text_b: Second text.

    Returns:
        Similarity score from 0.0 to 100.0, rounded to 2 decimal places.
    """
    return round(
        SequenceMatcher(None, text_a.strip().lower(), text_b.strip().lower()).ratio() * 100,
        2,
    )


def transcribe_and_score(
    wav_path: Union[Path, str],
    expected_text: str,
    timeout: Optional[float] = None,
    retry_count: Optional[int] = None,
    retry_delay: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Transcribe a WAV file via Voicebox and score it against expected text.

    Convenience wrapper combining transcribe_via_voicebox() and
    similarity_score() into the single call most callers need.

    Args:
        wav_path: Path to the .wav file to transcribe.
        expected_text: The text the audio is expected to say.
        timeout, retry_count, retry_delay: Passed through to
            transcribe_via_voicebox().

    Returns:
        Dict with keys "transcribed_text" (str), "success" (bool), and
        "score" (float, 0-100). Note: a score is still computed on failure,
        comparing expected_text against the "<ERROR: ...>" placeholder,
        so callers can rely on "score" always being a number.
    """
    transcribed_text, success = transcribe_via_voicebox(
        wav_path, timeout=timeout, retry_count=retry_count, retry_delay=retry_delay
    )
    score = similarity_score(expected_text, transcribed_text)
    return {
        "transcribed_text": transcribed_text,
        "success": success,
        "score": score,
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
    console_level: int | None = None,
    file_level: int | None = None
) -> logging.Logger:
    """
    Set up standardized logging for a script.
    
    Creates a logger that writes to both console and a log file.
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


