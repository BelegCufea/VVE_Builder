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
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Union

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
# Logging Setup
# ============================================================================
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


