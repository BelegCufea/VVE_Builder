"""
TTS Voice Generation Tool for Infinity Engine Games (PySide6 desktop version)

Runs the same dialog-report.csv -> Voicebox TTS pipeline as generate.py, but
as a native desktop app instead of a console script. The original terminal
progress bars (job line + "Overall:" line, redrawn in place with ANSI escape
codes) are replaced with two real QProgressBar widgets; everything else the
console version logged (header, pre-generation summary, per-job results,
final summary) is routed into an on-screen log panel instead of stdout.

Usage:
    python generate_gui.py
"""

import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import shutil
import zipfile
import requests
import logging
from collections import defaultdict
from runstats import Regression
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtGui import QFont, QTextCursor, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QProgressBar, QTextEdit, QGroupBox, QStatusBar,
    QSplitter, QFrame, QSizePolicy,
)

# ============================================================================
# Configuration Constants
# ============================================================================
# Voicebox API Configuration
BASE_URL = "http://10.0.50.5:17600"    # VoiceBox API - http://localhost:17493 for local server, or remote URL for remote server
ENGINE = "qwen"
MODEL_SIZE = "1.7B"

# Voicebox API Endpoints (relative to BASE_URL)
PROFILES_ENDPOINT = "/profiles"
PROFILES_IMPORT_ENDPOINT = "/profiles/import"
GENERATE_ENDPOINT = "/generate"
GENERATE_STATUS_ENDPOINT = "/generate/{gen_id}/status"
GENERATE_CANCEL_ENDPOINT = "/generate/{gen_id}/cancel"
AUDIO_ENDPOINT = "/audio/{gen_id}"

# Generation Timeout Safeguards
ENABLE_TIMEOUT_SAFEGUARD = True
TIMEOUT_MAX_SECONDS = 600
TIMEOUT_MULTIPLIER = 3.0
TIMEOUT_MIN_ESTIMATES = 10

# Retry Configuration
RETRY_COUNT = 3
RETRY_DELAY = 5.0

# Audio Conversion Configuration
CONVERT_TO_OGG = True
OGG_QUALITY = 4

# File Paths
CSV_PATH = r"dialog-report.csv"
PATCHER_CONFIG_PATH = r"patcher-config.json"
OUTPUT_DIR = r"output"

# Generation Limits and filters
LIMIT = 0
TARGET_VOICES = [
    # "Jaheira",
    # "Edwin",
    # "Neera",
    # "Bodhi",
    # "Gaelan Bayle"
]
VOICE_SUBSTITUTIONS_FILE = r"voice-substitutions.json"
FILENAME_PATTERN = r"^TS"

# STRREF Filtering
USE_STRREF_FILTER = False
STRREF_FILTER_FILE = r"strrefs.json"

# Voice Fallback Configuration
USE_VOICE_FALLBACK = False
FALLBACK_VOICE_MALE = "BG1 Narrator"
FALLBACK_VOICE_FEMALE = "BG3 Narrator"
FALLBACK_VOICE_NEUTRAL = "Description Narrator"

# Filename Generation
FORCE_GENERATED_FILENAMES = False
RESREF_PREFIX = "TS"

# Generation memory
SKIP_ALREADY_GENERATED = True
GENERATION_MEMORY_PATH = r"generation-memory.json"

# Logging
LOG_FILE_PATH = r"logs/generate.log"

# Pre-generation Summary Options
COMPACT_SUMMARY = True

# Voice Profile Auto-Provisioning
AUTO_PROVISION_PROFILES = True
VOICES_DIR = r"voices"
PROFILE_PACKAGES_DIR = r"profiles"
PROFILE_SYNC_MAX_ATTEMPTS = 10
PROFILE_SYNC_RETRY_DELAY = 3.0


# ============================================================================
# Logging
# ============================================================================

class LogSignal(QObject):
    """Bridges Python logging records into Qt signals so a background
    QThread can safely append to a QTextEdit owned by the main thread."""
    message = Signal(str, int)


class QtLogHandler(logging.Handler):
    """Logging handler that forwards every record to LogSignal.message
    instead of (or in addition to) printing it, so the same logger.info()/
    warning()/error() calls used throughout the pipeline "just work" in
    the GUI without touching any call site."""

    def __init__(self, log_signal: LogSignal):
        super().__init__()
        self.log_signal = log_signal

    def emit(self, record):
        try:
            msg = self.format(record)
            self.log_signal.message.emit(msg, record.levelno)
        except Exception:
            pass


def log_initialize(log_signal: LogSignal):
    """
    Initialize logging with three sinks: a debug-level log file, a clean
    console mirror (when a console is actually attached), and the GUI log
    panel via QtLogHandler/LogSignal.
    """
    log_dir = Path(LOG_FILE_PATH).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(file_handler)

    # Only mirror to console if one is actually attached (pythonw has none).
    if sys.stdout is not None:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(console_handler)

    gui_handler = QtLogHandler(log_signal)
    gui_handler.setLevel(logging.INFO)
    gui_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(gui_handler)

    logger.propagate = False
    return logger


def log_header_start():
    """Log the run-start banner immediately, before any setup work happens."""
    lines = ["", "=" * 70, "Voice over Generation",
              f"# Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "=" * 70,
              f"TTS Engine: {ENGINE}" + (f" ({MODEL_SIZE})" if MODEL_SIZE and MODEL_SIZE.strip() else "")]
    logger.info("\n".join(lines))


def log_header_summary(total_jobs, total_chars_all):
    """Log the closing lines of the header block, once totals are known."""
    logger.info("\n".join([f"Total jobs: {total_jobs}, Total chars: {total_chars_all}", "=" * 70]))


def log_pregeneration_summary(npc_stats, profile_map):
    """
    Build and log the pre-generation summary table: voice profile status,
    lines per NPC, done/missing/to-generate counts, and character totals.
    See generate.py's original docstring for the full column semantics;
    behavior here is unchanged.
    """
    trunc = lambda text, width: (text[:width - 3] + "...") if len(text) > width else text
    fmt = lambda v, w: f"{v:{w},}" if v != 0 else ' ' * w

    class COL_WIDTH:
        NPC = 28
        PROFILE = 30
        TOTAL = 7
        DONE = 8
        SKIPPED = 9
        TO_GEN = 8
        CHARS = 12

    _widths = [v for k, v in COL_WIDTH.__dict__.items() if not k.startswith('__') and isinstance(v, int)]
    LINE_LENGTH = sum(_widths) + len(_widths) - 1

    header_lines, detail_lines, totals_lines = [], [], []

    header_lines.append("\n" + "=" * LINE_LENGTH)
    header_lines.append("📊 PRE-GENERATION VOICE SUMMARY")

    if TARGET_VOICES:
        header_lines.append(f"   🔍 Filter mode: TARGET_VOICES ({len(TARGET_VOICES)} NPCs)")
    else:
        header_lines.append("   📡 Scan mode: ALL lines (no TARGET_VOICES filter)")

    if USE_VOICE_FALLBACK:
        header_lines.append(f"   🔄 Voice fallback ENABLED: M->{FALLBACK_VOICE_MALE}, F->{FALLBACK_VOICE_FEMALE}, NEUTRAL->{FALLBACK_VOICE_NEUTRAL}")
    else:
        header_lines.append("   ⛔ Voice fallback DISABLED")

    if USE_STRREF_FILTER:
        try:
            with open(STRREF_FILTER_FILE, "r") as f:
                count = len(json.load(f))
            header_lines.append(f"   📋 STRREF filter ENABLED: {count} STRREFs from {STRREF_FILTER_FILE}")
        except Exception:
            header_lines.append(f"   📋 STRREF filter ENABLED (file: {STRREF_FILTER_FILE})")
    else:
        header_lines.append("   📋 STRREF filter DISABLED")

    if FORCE_GENERATED_FILENAMES:
        header_lines.append(f"   🔧 Filenames: FORCED generated (base36) with prefix: {RESREF_PREFIX}")
    else:
        header_lines.append(f"   🔧 Filenames: CSV with base36 fallback (prefix: {RESREF_PREFIX})")

    header_lines.append("=" * LINE_LENGTH)

    detail_lines.append("DETAILS")
    detail_lines.append("=" * LINE_LENGTH)
    table_header = (
        f"{'NPC Name':<{COL_WIDTH.NPC}} "
        f"{'Profile':<{COL_WIDTH.PROFILE}} "
        f"{'Total':>{COL_WIDTH.TOTAL}} "
        f"{'Done':>{COL_WIDTH.DONE}} "
        f"{'Missing':>{COL_WIDTH.SKIPPED}} "
        f"{'To Gen':>{COL_WIDTH.TO_GEN}} "
        f"{'Chars':>{COL_WIDTH.CHARS}}"
    )
    detail_lines.append(table_header)
    detail_lines.append("-" * LINE_LENGTH)

    grand_total = grand_done = grand_skipped = grand_to_gen = grand_chars = 0
    generate_total = generate_chars = 0
    missing_npcs, missing_chars_total, missing_done_total, missing_skipped_total = [], 0, 0, 0
    done_npcs, done_chars_total, done_done_total, done_skipped_total = [], 0, 0, 0

    for npc_name, stats in npc_stats.items():
        profile_name = stats.get("voice_name", npc_name)
        has_profile = profile_name in profile_map
        total, done, skipped, to_gen, chars = (
            stats["total"], stats["done"], stats["skipped"], stats["to_generate"], stats["chars"]
        )

        grand_total += total
        grand_done += done
        grand_skipped += skipped
        grand_to_gen += to_gen
        grand_chars += chars

        show_in_detail = False
        profile_str = ""

        if has_profile:
            if to_gen > 0:
                generate_total += to_gen
                generate_chars += chars
            done_npcs.append(npc_name)
            done_chars_total += chars
            done_done_total += done
            done_skipped_total += skipped
            show_in_detail = (not COMPACT_SUMMARY) or (to_gen > 0)
            profile_str = f"✅ {profile_name}"
        else:
            missing_npcs.append(npc_name)
            missing_chars_total += chars
            missing_done_total += done
            missing_skipped_total += skipped
            if not COMPACT_SUMMARY:
                show_in_detail = True
                profile_str = f"❌ Missing"

        if show_in_detail:
            detail_lines.append(
                f"{trunc(npc_name, COL_WIDTH.NPC):<{COL_WIDTH.NPC}} "
                f"{trunc(profile_str, COL_WIDTH.PROFILE):<{COL_WIDTH.PROFILE}} "
                f"{fmt(total, COL_WIDTH.TOTAL)} "
                f"{fmt(done, COL_WIDTH.DONE)} "
                f"{fmt(skipped, COL_WIDTH.SKIPPED)} "
                f"{fmt(to_gen, COL_WIDTH.TO_GEN)} "
                f"{fmt(chars, COL_WIDTH.CHARS)}"
            )

    detail_lines.append("=" * LINE_LENGTH)

    totals_table_header = (
        f"{'Summary':<{COL_WIDTH.NPC}} "
        f"{'':<{COL_WIDTH.PROFILE}} "
        f"{'Total':>{COL_WIDTH.TOTAL}} "
        f"{'Done':>{COL_WIDTH.DONE}} "
        f"{'Missing':>{COL_WIDTH.SKIPPED}} "
        f"{'To Gen':>{COL_WIDTH.TO_GEN}} "
        f"{'Chars':>{COL_WIDTH.CHARS}}"
    )
    totals_lines.append(totals_table_header)
    totals_lines.append("-" * LINE_LENGTH)

    if missing_npcs:
        missing_total = missing_done_total + missing_skipped_total
        totals_lines.append(
            f"{'❌ Missing voices':<{COL_WIDTH.NPC - 1}} "
            f"{'':<{COL_WIDTH.PROFILE}} "
            f"{fmt(missing_total, COL_WIDTH.TOTAL)} "
            f"{fmt(missing_done_total, COL_WIDTH.DONE)} "
            f"{fmt(missing_skipped_total, COL_WIDTH.SKIPPED)} "
            f"{fmt(0, COL_WIDTH.TO_GEN)} "
            f"{fmt(missing_chars_total, COL_WIDTH.CHARS)}"
        )

    if done_npcs:
        done_total = done_done_total + done_skipped_total
        totals_lines.append(
            f"{'✅ Already done':<{COL_WIDTH.NPC - 1}} "
            f"{'':<{COL_WIDTH.PROFILE}} "
            f"{fmt(done_total, COL_WIDTH.TOTAL)} "
            f"{fmt(done_done_total, COL_WIDTH.DONE)} "
            f"{fmt(done_skipped_total, COL_WIDTH.SKIPPED)} "
            f"{fmt(0, COL_WIDTH.TO_GEN)} "
            f"{fmt(done_chars_total, COL_WIDTH.CHARS)}"
        )

    totals_lines.append(
        f"{'🔄 Generate':<{COL_WIDTH.NPC - 1}} "
        f"{'':<{COL_WIDTH.PROFILE}} "
        f"{fmt(generate_total, COL_WIDTH.TOTAL)} "
        f"{fmt(0, COL_WIDTH.DONE)} "
        f"{fmt(0, COL_WIDTH.SKIPPED)} "
        f"{fmt(generate_total, COL_WIDTH.TO_GEN)} "
        f"{fmt(generate_chars, COL_WIDTH.CHARS)}"
    )
    totals_lines.append("-" * LINE_LENGTH)
    totals_lines.append(
        f"{'GRAND TOTAL':<{COL_WIDTH.NPC}} "
        f"{'':<{COL_WIDTH.PROFILE}} "
        f"{fmt(grand_total, COL_WIDTH.TOTAL)} "
        f"{fmt(grand_done, COL_WIDTH.DONE)} "
        f"{fmt(grand_skipped, COL_WIDTH.SKIPPED)} "
        f"{fmt(grand_to_gen, COL_WIDTH.TO_GEN)} "
        f"{fmt(grand_chars, COL_WIDTH.CHARS)}"
    )
    totals_lines.append("=" * LINE_LENGTH + "\n")

    logger.info("\n".join(header_lines + totals_lines + detail_lines))


def log_job_summary(idx, total_jobs, strref, filename, chars, elapsed, audio_duration,
                     npc_name, voice_name, success=True, error_msg=None):
    """Log one completed job's result line (unchanged from generate.py)."""
    realtime_speed = (audio_duration / elapsed * 100 if elapsed > 0 else 0)
    voice_part = f" (voice: {voice_name})" if voice_name != npc_name else ""
    status = "✅" if success else "❌"
    job_width = len(str(total_jobs))

    line = (
        f"[{idx:>{job_width}}/{total_jobs:>{job_width}}] "
        f"{status} "
        f"Strref: {strref:>6}  "
        f"File: {filename:<10}  "
        f"Chars: {chars:>4}  "
        f"Gen: {elapsed:>6.2f}s  "
        f"Audio: {audio_duration:>6.2f}s  "
        f"Speed: {realtime_speed:>5.1f}%  "
        f"Npc: {npc_name:<20}"
        f"{voice_part}"
    )
    if not success and error_msg:
        line += f"  Error: {error_msg}"

    logging.log(logging.INFO if success else logging.WARNING, line)


def log_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats, retry_stats=None):
    """Log the final summary after all generation jobs complete (unchanged)."""
    total_done = total_skipped = 0
    done_summary, skipped_summary = {}, {}

    for voice, stats in npc_stats.items():
        done = stats.get("done", 0)
        skipped = stats.get("skipped", 0)
        if done > 0:
            total_done += done
            done_summary[voice] = done
        if skipped > 0:
            total_skipped += skipped
            skipped_summary[voice] = skipped

    lines = ["", "=" * 70, "FINAL SUMMARY", "=" * 70,
              f"Processed: {total_jobs:,} files",
              f"Total characters: {total_chars_processed:,}"]

    if avg_time_per_char:
        lines.append(f"Average time per character: {avg_time_per_char:.4f}s")

    if total_done:
        if COMPACT_SUMMARY:
            lines.append(f"Skipped already generated: {total_done:,} files")
        else:
            done_details = ", ".join(f"{voice}: {count:,}" for voice, count in done_summary.items())
            lines.append(f"Skipped already generated: {total_done:,} ({done_details})")

    if total_skipped:
        if COMPACT_SUMMARY:
            lines.append(f"Skipped missing voices: {total_skipped:,} files")
        else:
            skipped_details = ", ".join(f"{voice}: {count:,}" for voice, count in skipped_summary.items())
            lines.append(f"Skipped missing voices: {total_skipped:,} ({skipped_details})")

    if retry_stats:
        lines.append("")
        lines.append("-" * 70)
        lines.append("RETRY STATISTICS")
        lines.append(f"Total retry attempts: {retry_stats.get('failed_attempts', 0)}")
        lines.append(f"Successful retries:   {retry_stats.get('successful_retries', 0)}")
        lines.append(f"Failed tasks:         {retry_stats.get('failed_tasks', 0)}")

        failed_tasks = retry_stats.get('failed_task_details', [])
        if failed_tasks:
            lines.append("")
            lines.append("Failed tasks:")
            for task in failed_tasks:
                lines.append(f"  [{task['idx']}] {task['strref']}/{task['filename']} - {task['npc_name']}")
        lines.append("")

    lines.append(f"# Finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append("")

    logging.info("\n".join(lines))


# Logger is created once the QApplication/LogSignal exist; see main().
logger: logging.Logger = logging.getLogger()


# ============================================================================
# Utility Functions
# ============================================================================

def format_time(seconds):
    """Convert a duration in seconds to a compact human-readable string."""
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


def format_finish_time(eta_seconds):
    """Format an ETA (seconds) as an absolute expected finish time."""
    if eta_seconds > 0:
        finish_time = datetime.now() + timedelta(seconds=eta_seconds)
        if finish_time.date() == datetime.now().date():
            return finish_time.strftime("%H:%M:%S")
        return finish_time.strftime("%x %X")
    return "..."


def sanitize_filename(name):
    r"""Clean a string to be safe for use as a Windows file/directory name."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.rstrip(' .')
    reserved_names = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    if name.upper() in reserved_names:
        name = f"_{name}_"
    if not name:
        name = "_unnamed_"
    return name


def to_base36(value):
    """Convert a non-negative integer to its base36 representation."""
    if value < 0:
        raise ValueError(f"Value must be non-negative, got {value}")
    if value == 0:
        return "0"
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = ""
    while value > 0:
        result = alphabet[value % 36] + result
        value //= 36
    return result


def generate_resref(strref, prefix="TS"):
    """Generate an 8-character resref: 2-char prefix + 6-char base36 number."""
    if len(prefix) != 2:
        raise ValueError(f"Prefix must be exactly 2 characters, got '{prefix}'")
    if isinstance(strref, str):
        try:
            strref_int = int(strref)
        except ValueError:
            raise ValueError(f"StrRef must be a valid integer, got '{strref}'")
    else:
        strref_int = strref
    if strref_int < 0:
        raise ValueError(f"StrRef must be non-negative, got {strref_int}")
    suffix = to_base36(strref_int).rjust(6, '0')
    return (prefix + suffix).upper()


# ============================================================================
# Voice Profile Management
# ============================================================================

def load_voice_substitutions_all():
    """Load NPC/gender/sysname voice substitution rules from a single JSON file."""
    substitutions, substitutions_gender, substitutions_sysname = {}, {}, {}

    path = Path(VOICE_SUBSTITUTIONS_FILE)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            substitutions = data.get("npc", {})
            substitutions_gender = data.get("gender", {})
            substitutions_sysname = data.get("sysname", {})
            logger.info(f"Loaded substitutions from {VOICE_SUBSTITUTIONS_FILE}:")
            logger.info(f"  NPC-level: {len(substitutions)} entries")
            logger.info(f"  Gender-level: {len(substitutions_gender)} entries")
            logger.info(f"  SysName-level: {len(substitutions_sysname)} entries")
        except Exception as e:
            logger.warning(f"⚠️ Could not load voice substitutions from {VOICE_SUBSTITUTIONS_FILE}: {e}")
    else:
        logger.info(f"No substitution file found, using defaults: {VOICE_SUBSTITUTIONS_FILE}")

    return substitutions, substitutions_gender, substitutions_sysname


def resolve_voice_substitution(npc_name, gender=None, sysname=None,
                                substitutions=None, substitutions_gender=None,
                                substitutions_sysname=None):
    """Shared substitution-lookup priority: sysname, then NPC+gender, then NPC name."""
    substitutions = substitutions or {}
    substitutions_gender = substitutions_gender or {}
    substitutions_sysname = substitutions_sysname or {}

    if sysname and sysname in substitutions_sysname:
        return substitutions_sysname[sysname]
    if npc_name and gender:
        gender_key = f"{npc_name}|{gender}"
        if gender_key in substitutions_gender:
            return substitutions_gender[gender_key]
    if npc_name and npc_name in substitutions:
        return substitutions[npc_name]
    return None


def get_voice_profile_name(npc_name, gender=None, profile_map=None, sysname=None,
                            substitutions=None, substitutions_gender=None,
                            substitutions_sysname=None):
    """Resolve an NPC name to a Voicebox profile name, applying substitutions
    and (if enabled) gender-based narrator fallback."""
    substituted = resolve_voice_substitution(
        npc_name, gender, sysname, substitutions, substitutions_gender, substitutions_sysname
    )
    if substituted:
        return substituted

    if npc_name and profile_map is not None and npc_name in profile_map:
        return npc_name

    if USE_VOICE_FALLBACK:
        if gender == "M":
            return FALLBACK_VOICE_MALE
        elif gender == "F":
            return FALLBACK_VOICE_FEMALE
        else:
            return FALLBACK_VOICE_NEUTRAL

    return None


def delete_profile(profile_id):
    """Delete a voice profile from the Voicebox server."""
    try:
        delete_url = f"{BASE_URL}{PROFILES_ENDPOINT}/{profile_id}"
        resp = requests.delete(delete_url)
        if resp.status_code == 200:
            return True, "Profile deleted successfully"
        return False, f"Deletion returned: {resp.status_code}"
    except Exception as e:
        return False, f"Deletion error: {e}"


def get_all_profiles():
    """Fetch all voice profiles from Voicebox, filtering out zero-sample ones."""
    resp = requests.get(f"{BASE_URL}{PROFILES_ENDPOINT}")
    resp.raise_for_status()

    profile_map, zero_sample_profiles = {}, {}
    total_profiles = 0

    for p in resp.json():
        total_profiles += 1
        profile_id = p.get("id")
        profile_name = p.get("name")
        sample_count = p.get("sample_count", 0)

        if not profile_name or not profile_id:
            continue
        if sample_count == 0:
            zero_sample_profiles[profile_name] = profile_id
            continue
        profile_map[profile_name] = profile_id

    if zero_sample_profiles:
        logger.warning(
            f"⚠️ Found {len(zero_sample_profiles)} zero-sample profile(s) "
            f"out of {total_profiles} total: {', '.join(list(zero_sample_profiles.keys())[:5])}"
            + (f" and {len(zero_sample_profiles) - 5} more" if len(zero_sample_profiles) > 5 else "")
        )
    else:
        logger.info(f"Loaded {len(profile_map)} voice profiles (all with samples).")

    return profile_map, zero_sample_profiles


# ============================================================================
# Voice Profile Auto-Provisioning
# ============================================================================

def get_candidate_voice_name(npc_name, gender=None, sysname=None,
                              substitutions=None, substitutions_gender=None,
                              substitutions_sysname=None):
    """Resolve the profile name a CSV row *would* use, without checking existence."""
    substituted = resolve_voice_substitution(
        npc_name, gender, sysname, substitutions, substitutions_gender, substitutions_sysname
    )
    return substituted or npc_name or None


def scan_csv_needed_voice_names(csv_path, filename_pattern, target_voices,
                                 use_strref_filter, strref_filter_file,
                                 substitutions=None, substitutions_gender=None,
                                 substitutions_sysname=None):
    """Scan the dialog CSV and collect the set of voice profile names it needs."""
    needed = set()
    strref_filter = set()

    if use_strref_filter:
        strref_filter = load_strref_filter(strref_filter_file)

    for strref, sysname, npc_name, gender, csv_filename, text in iter_filtered_csv_rows(
        csv_path, target_voices, filename_pattern, use_strref_filter, strref_filter
    ):
        candidate = get_candidate_voice_name(
            npc_name, gender, sysname, substitutions, substitutions_gender, substitutions_sysname
        )
        if candidate:
            needed.add(candidate)

    return needed


def scan_available_voice_dirs(voices_dir):
    """Scan a voices/ directory and group WAV+TXT sample pairs by NPC name."""
    voices_path = Path(voices_dir)
    voice_groups = defaultdict(list)

    if not voices_path.exists():
        return {}

    pattern = re.compile(r'^(.*?)(?:\s+(\d+))?$')

    for file_path in voices_path.iterdir():
        if not (file_path.is_file() and file_path.suffix in ('.WAV', '.wav')):
            continue

        base_name = file_path.stem
        txt_file = file_path.with_suffix('.txt')
        if not txt_file.exists():
            logger.warning(f"⚠️ No transcript found for {file_path.name}, skipping sample.")
            continue

        with open(txt_file, "r", encoding="utf-8") as f:
            transcript = f.read().strip()

        match = pattern.match(base_name)
        if match:
            name = match.group(1).strip()
            number = match.group(2)
            if number is None:
                if ' ' in name and name.split(' ')[-1].isdigit():
                    parts = name.rsplit(' ', 1)
                    name = parts[0]
                    number = parts[1]
                else:
                    number = "1"
        else:
            name = base_name
            number = "1"

        voice_groups[name].append({
            "number": int(number), "wav_path": file_path, "txt_path": txt_file, "transcript": transcript,
        })

    return dict(voice_groups)


def create_profile_package(voice_name, files, output_dir):
    """Build a .voicebox.zip package for a voice from its sample files."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    safe_name = voice_name.lower().replace(' ', '-')
    temp_dir = output_path / f"profile-{safe_name}.voicebox"
    temp_dir.mkdir(exist_ok=True)

    try:
        samples_dir = temp_dir / "samples"
        samples_dir.mkdir(exist_ok=True)

        manifest = {
            "version": "1.0",
            "profile": {"name": voice_name, "description": "", "language": "en"},
            "has_avatar": False,
        }
        with open(temp_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        samples_data = {}
        for file_info in sorted(files, key=lambda x: x["number"]):
            sample_uuid = str(uuid.uuid4())
            wav_filename = f"{sample_uuid}.wav"
            shutil.copy2(file_info["wav_path"], samples_dir / wav_filename)
            samples_data[wav_filename] = file_info["transcript"]

        with open(temp_dir / "samples.json", "w", encoding="utf-8") as f:
            json.dump(samples_data, f, indent=2)

        zip_path = output_path / f"profile-{safe_name}.voicebox.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files_in_dir in os.walk(temp_dir):
                for file in files_in_dir:
                    file_path = Path(root) / file
                    zipf.write(file_path, file_path.relative_to(temp_dir))

        return zip_path
    except Exception as e:
        logger.error(f"❌ Error creating profile package for {voice_name}: {e}")
        return None
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def import_profile_zip(zip_path):
    """Import a composed .voicebox.zip package into Voicebox."""
    try:
        with open(zip_path, "rb") as f:
            files = {"file": (zip_path.name, f, "application/zip")}
            resp = requests.post(f"{BASE_URL}{PROFILES_IMPORT_ENDPOINT}", files=files)
            resp.raise_for_status()
            return resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Error importing {zip_path.name}: {e}")
        return None


def sync_missing_profiles(csv_path, filename_pattern, target_voices,
                           use_strref_filter, strref_filter_file,
                           substitutions=None, substitutions_gender=None,
                           substitutions_sysname=None):
    """Reconcile Voicebox's profile list against what the CSV needs and what
    voices/ can provide, composing/importing (or rebuilding zero-sample
    profiles) before generation starts. See generate.py for full details."""
    profile_map, zero_sample_profiles = get_all_profiles()

    if not AUTO_PROVISION_PROFILES:
        return profile_map

    needed = scan_csv_needed_voice_names(
        csv_path, filename_pattern, target_voices, use_strref_filter, strref_filter_file,
        substitutions, substitutions_gender, substitutions_sysname
    )

    zero_sample_needed = set(zero_sample_profiles.keys()) & needed
    if zero_sample_needed:
        logger.info(f"🔧 Found {len(zero_sample_needed)} zero-sample profile(s) that are needed:")
        for name in sorted(zero_sample_needed):
            logger.info(f"  - {name}")

    missing = needed - profile_map.keys() - zero_sample_profiles.keys()
    available = scan_available_voice_dirs(VOICES_DIR)
    rebuildable = sorted(name for name in zero_sample_needed if available.get(name))
    composable = sorted(name for name in missing if available.get(name))

    if not rebuildable and not composable:
        if missing or zero_sample_needed:
            logger.warning(
                f"⚠️ {len(missing)} needed voice(s) missing from Voicebox and not found in {VOICES_DIR}/, "
                f"and {len(zero_sample_needed)} zero-sample profile(s) cannot be rebuilt."
            )
        return profile_map

    imported, reimported, failed = [], [], []

    if rebuildable:
        logger.info(f"♻️ Rebuilding {len(rebuildable)} zero-sample profile(s) from {VOICES_DIR}/...")
        for voice_name in rebuildable:
            profile_id = zero_sample_profiles[voice_name]
            logger.info(f"  Deleting zero-sample profile: {voice_name} (ID: {profile_id})...")
            success, message = delete_profile(profile_id)
            if not success:
                logger.warning(f"  ✗ Failed to delete {voice_name}: {message}")
                failed.append(voice_name)
                continue
            logger.info(f"  ✓ Deleted: {voice_name}")
            time.sleep(PROFILE_SYNC_RETRY_DELAY)

            logger.info(f"  Rebuilding profile: {voice_name}...")
            zip_path = create_profile_package(voice_name, available[voice_name], PROFILE_PACKAGES_DIR)
            if not zip_path:
                failed.append(voice_name)
                continue
            result = import_profile_zip(zip_path)
            if result:
                reimported.append(voice_name)
                logger.info(f"  ✓ Re-imported: {voice_name}")
            else:
                logger.warning(f"  ✗ Failed to re-import: {voice_name}")
                failed.append(voice_name)

    if composable:
        logger.info(f"🧩 Composing {len(composable)} missing voice profile(s) from {VOICES_DIR}/...")
        for voice_name in composable:
            zip_path = create_profile_package(voice_name, available[voice_name], PROFILE_PACKAGES_DIR)
            if not zip_path:
                failed.append(voice_name)
                continue
            result = import_profile_zip(zip_path)
            if result:
                imported.append(voice_name)
                logger.info(f"  ✓ Imported: {voice_name}")
            else:
                logger.warning(f"  ✗ Failed to import: {voice_name}")
                failed.append(voice_name)

    all_imported = imported + reimported
    if not all_imported:
        logger.warning("⚠️ Could not import any profiles.")
        return profile_map

    still_missing = set(all_imported)
    for attempt in range(1, PROFILE_SYNC_MAX_ATTEMPTS + 1):
        profile_map, _ = get_all_profiles()
        still_missing -= profile_map.keys()
        if not still_missing:
            break
        time.sleep(PROFILE_SYNC_RETRY_DELAY)

    if still_missing:
        logger.warning(
            f"⚠️ {len(still_missing)} imported/re-imported profile(s) not yet visible after "
            f"{PROFILE_SYNC_MAX_ATTEMPTS} attempts: {', '.join(sorted(still_missing))}"
        )

    if all_imported:
        logger.info(f"Auto-provisioned {len(all_imported) - len(still_missing)} voice profile(s) from {VOICES_DIR}/.")

    return profile_map


# ============================================================================
# Generation Memory
# ============================================================================

def load_generation_memory(memory_path):
    """Load the {npc: {strref: True}} generation history from JSON."""
    if not os.path.exists(memory_path):
        return {}
    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("⚠️ Generation memory is not a JSON object. Starting with empty memory.")
            return {}
        return data
    except Exception as e:
        logger.warning(f"⚠️ Could not load generation memory: {e}")
        logger.info("   Starting with empty memory.")
        return {}


def save_generation_memory(memory, memory_path):
    """Save the generation history to a JSON file."""
    with open(memory_path, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=4, ensure_ascii=False)


def is_already_generated(memory, npc_name, strref):
    """Check whether a specific NPC/STRREF line has already been generated."""
    return str(strref) in memory.get(npc_name, {})


def mark_as_generated(memory, npc_name, strref):
    """Record a successfully generated NPC/STRREF line in memory (in place)."""
    voice_memory = memory.setdefault(npc_name, {})
    voice_memory[str(strref)] = True


# ============================================================================
# TTS API Client
# ============================================================================

def submit_generation(profile_id, text, engine, model_size):
    """Submit a TTS generation request and return its generation ID."""
    payload = {
        "text": text, "profile_id": profile_id, "language": "en",
        "engine": engine, "model_size": model_size,
    }
    resp = requests.post(f"{BASE_URL}{GENERATE_ENDPOINT}", json=payload)
    resp.raise_for_status()
    data = resp.json()
    gen_id = data.get("id")
    if not gen_id:
        raise RuntimeError(f"Response missing 'id': {data}")
    return gen_id


def wait_for_completion(gen_id):
    """Block, streaming SSE, until a generation job completes or fails."""
    url = f"{BASE_URL}{GENERATE_STATUS_ENDPOINT.format(gen_id=gen_id)}"
    headers = {"Accept": "text/event-stream"}
    final_event = None

    with requests.get(url, headers=headers, stream=True) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            if not line or not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event.get("status") in ("completed", "failed"):
                final_event = event
                break

    return final_event


def cancel_generation(gen_id):
    """Cancel a queued or running generation on the Voicebox server."""
    try:
        cancel_url = f"{BASE_URL}{GENERATE_CANCEL_ENDPOINT.format(gen_id=gen_id)}"
        resp = requests.post(cancel_url)
        if resp.status_code == 200:
            return True, "Cancellation successful"
        return False, f"Cancellation returned: {resp.status_code}"
    except Exception as e:
        return False, f"Cancellation error: {e}"


def download_audio(gen_id, output_path):
    """Download the generated (raw) audio for a completed generation job."""
    url = f"{BASE_URL}{AUDIO_ENDPOINT.format(gen_id=gen_id)}"
    resp = requests.get(url)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(resp.content)


# ============================================================================
# Text Processing
# ============================================================================

def load_patcher_config(config_path):
    """Load the patcher configuration (identity/gender tokens, phonetic rules)."""
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def preprocess_text(text, patcher_config):
    """Apply identity-token, gender-token, phonetic-rule, and token-cleanup
    transformations to raw CSV text before sending it to the TTS engine."""
    pc_name = patcher_config.get("pcName", "CHARNAME")
    pc_race = patcher_config.get("pcRace", "RACE")
    identity_tokens = patcher_config.get("identityTokens", [])

    token_map = {}
    for token in identity_tokens:
        if token == "CHARNAME" or token == "GABBER":
            token_map[token] = pc_name
        elif token == "PRO_RACE" or token == "RACE":
            token_map[token] = pc_race
    for token, replacement in token_map.items():
        text = text.replace(f"<{token}>", replacement)

    pc_gender = patcher_config.get("pcGender", "neutral")
    gender_tokens = patcher_config.get("genderTokens", {})
    for token, forms in gender_tokens.items():
        replacement = forms.get(pc_gender, "")
        if replacement:
            text = text.replace(f"<{token}>", replacement)

    phonetic_rules = patcher_config.get("phoneticRules", [])
    for rule in phonetic_rules:
        pattern = rule.get("pattern")
        replacement_template = rule.get("replacement", "")
        try:
            compiled_pattern = re.compile(pattern, flags=re.IGNORECASE | re.MULTILINE)
            if replacement_template and '$' in replacement_template:
                def repl_func(match, _template=replacement_template):
                    result = _template
                    for i in range(1, match.lastindex + 1 if match.lastindex else 0):
                        group_value = match.group(i) or ''
                        result = result.replace(f'${i}', group_value)
                    return result
                text = compiled_pattern.sub(repl_func, text)
            else:
                text = compiled_pattern.sub(replacement_template, text)
        except re.error:
            continue

    text = re.sub(r'<[^>]+>', '', text)
    return text


# ============================================================================
# Audio Processing
# ============================================================================

def convert_to_ogg(input_path, output_path=None, quality=2):
    """Convert an audio file to Ogg Vorbis using ffmpeg (forces .ogg container)."""
    if output_path is None:
        output_path = input_path

    cmd = ['ffmpeg', '-y', '-i', input_path, '-c:a', 'libvorbis',
           '-qscale:a', str(quality), '-f', 'ogg', output_path]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"ffmpeg stderr:\n{e.stderr.decode('utf-8', errors='ignore') if isinstance(e.stderr, bytes) else e.stderr}")
        raise


# ============================================================================
# CSV Processing
# ============================================================================

def filter_and_sort_rows(selected_rows, profile_map):
    """Drop rows whose voice profile doesn't exist, then sort by voice name."""
    valid_rows = [row for row in selected_rows if row[2] in profile_map]
    valid_rows.sort(key=lambda row: row[2].lower())
    return valid_rows


def load_strref_filter(filter_file):
    """Load the STRREF filter list (a JSON array) as a set of strings."""
    if not os.path.exists(filter_file):
        logger.warning(f"⚠️ STRREF filter file not found: {filter_file}")
        logger.warning("   Processing all rows (no filter).")
        return set()
    try:
        with open(filter_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning(f"⚠️ STRREF filter file must contain a JSON array, got {type(data)}")
            return set()
        return {str(item) for item in data}
    except Exception as e:
        logger.warning(f"⚠️ Could not load STRREF filter: {e}")
        logger.warning("   Processing all rows (no filter).")
        return set()


def iter_filtered_csv_rows(csv_path, target_voices, filename_pattern, use_strref_filter, strref_filter):
    """Parse the dialog CSV and yield rows passing the shared row-level filters."""
    if not os.path.exists(csv_path):
        return

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 8:
                continue

            strref = row[0].strip()
            sysname = row[1].strip() if len(row) > 1 else ""
            npc_name = row[2].strip() if len(row) > 2 else ""
            gender = row[3].strip() if len(row) > 3 else ""
            csv_filename = row[5].strip() if len(row) > 5 else ""
            text = row[7].strip() if len(row) > 7 else ""

            if use_strref_filter and strref_filter:
                if strref not in strref_filter:
                    continue
            if not use_strref_filter and not npc_name:
                continue
            if not use_strref_filter and target_voices and npc_name not in target_voices:
                continue
            if filename_pattern and csv_filename and not re.match(filename_pattern, csv_filename):
                continue
            if not text:
                continue

            yield strref, sysname, npc_name, gender, csv_filename, text


def load_and_filter_csv(csv_path, target_voices, filename_pattern, patcher_config,
                         generation_memory, skip_generated, limit, profile_map=None,
                         use_strref_filter=False, strref_filter_file="strrefs.json",
                         force_generated_filenames=False,
                         substitutions=None, substitutions_gender=None,
                         substitutions_sysname=None):
    """
    Load CSV data, apply filters, and prepare rows for generation.

    Returns:
        tuple: (selected_rows, npc_stats) where selected_rows is a list of
        (strref, display_name, voice_name, filename, text) and npc_stats is
        a per-NPC statistics dict.

    Note: unlike the console version, CSV read errors are *raised* instead
    of calling sys.exit(1) - the GUI worker catches this and reports it
    through the failed signal instead of killing the whole application.
    """
    selected_rows = []
    npc_stats = {}
    strref_filter = set()

    if use_strref_filter:
        strref_filter = load_strref_filter(strref_filter_file)
        if strref_filter:
            logger.info(f"Loaded {len(strref_filter)} STRREFs from filter file.")
        else:
            logger.warning("⚠️ No STRREFs loaded from filter file. Processing all rows.")

    try:
        for strref, sysname, npc_name, gender, csv_filename, text in iter_filtered_csv_rows(
            csv_path, target_voices, filename_pattern, use_strref_filter, strref_filter
        ):
            if force_generated_filenames:
                filename = generate_resref(strref, RESREF_PREFIX)
            else:
                filename = csv_filename if csv_filename and csv_filename.strip() else generate_resref(strref, RESREF_PREFIX)

            voice_name = get_voice_profile_name(
                npc_name, gender, profile_map, sysname,
                substitutions, substitutions_gender, substitutions_sysname
            )

            display_name = npc_name if npc_name else "Descriptions"

            if display_name not in npc_stats:
                npc_stats[display_name] = {
                    "voice_name": voice_name if voice_name else "MISSING",
                    "total": 0, "done": 0, "skipped": 0, "to_generate": 0, "chars": 0,
                }

            npc_stats[display_name]["total"] += 1
            npc_stats[display_name]["chars"] += len(text)

            if voice_name is None:
                npc_stats[display_name]["skipped"] += 1
                continue

            if profile_map is not None and voice_name not in profile_map:
                npc_stats[display_name]["skipped"] += 1
                continue

            if skip_generated and is_already_generated(generation_memory, display_name, strref):
                npc_stats[display_name]["done"] += 1
                continue

            npc_stats[display_name]["to_generate"] += 1
            text = preprocess_text(text, patcher_config) if patcher_config else text
            selected_rows.append((strref, display_name, voice_name, filename, text))

            if limit and len(selected_rows) >= limit:
                break

    except Exception as e:
        logger.error(f"❌ Error reading CSV: {e}")
        raise

    return selected_rows, npc_stats


def estimate_generation_time(regressor, chars):
    """Estimate generation time from historical (chars, time) samples."""
    if len(regressor) > 1:
        estimated_sec = regressor.slope() * chars + regressor.intercept()
        return max(estimated_sec, 2.0)
    return 10.0


# ============================================================================
# Generation Worker (runs on a background QThread)
# ============================================================================

class GenerationWorker(QObject):
    """
    Runs the full generation pipeline (the equivalent of generate.py's
    main()) off the UI thread, reporting progress through Qt signals
    instead of the old terminal progress_worker()/format_overall_line().

    Signals:
        stage(str): a short status-bar message for the current pipeline step.
        job_progress(dict): live progress for the job currently generating.
        overall_progress(dict): live progress across the whole run.
        finished(dict): final run statistics (emitted on normal completion,
            including the "nothing to do" case).
        failed(str): a fatal error message (emitted instead of finished).
    """

    stage = Signal(str)
    job_progress = Signal(dict)
    overall_progress = Signal(dict)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._stop_requested = threading.Event()
        self._current_gen_id = None

    def request_stop(self):
        """Ask the worker to stop after the current job, and try to cancel
        whatever generation is in flight right now for a snappier response."""
        self._stop_requested.set()
        if self._current_gen_id:
            try:
                cancel_generation(self._current_gen_id)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Progress reporting (replaces progress_worker() / format_overall_line())
    # ------------------------------------------------------------------

    def _job_progress_ticker(self, stop_event, job_idx, total_jobs, filename, strref,
                              estimated_sec, timeout_sec, npc_name, voice_name, chars):
        """Background thread: emits job_progress every 0.5s while a single
        generation is in flight, driving the job QProgressBar in the UI."""
        start_time = time.time()
        while not stop_event.is_set():
            elapsed = time.time() - start_time
            percent = min(100.0, (elapsed / estimated_sec) * 100) if estimated_sec > 0 else 0.0
            self.job_progress.emit({
                "idx": job_idx, "total": total_jobs, "strref": strref, "filename": filename,
                "npc_name": npc_name, "voice_name": voice_name, "chars": chars,
                "percent": percent, "elapsed": elapsed, "estimated": estimated_sec,
                "timeout": timeout_sec,
            })
            time.sleep(0.5)

    def _compute_overall_progress(self, total_chars_processed, total_chars_all, total_jobs,
                                   idx, overall_regressor, avg_time_per_char, elapsed_total):
        """Compute the numbers the Overall progress bar/label need; the
        console version built a formatted string, the GUI just needs numbers."""
        if avg_time_per_char is None or total_chars_all <= 0:
            return {"ready": False}

        remaining_chars = total_chars_all - total_chars_processed
        if len(overall_regressor) > 1:
            eta_seconds = (overall_regressor.slope() * remaining_chars
                           + overall_regressor.intercept() * (total_jobs - idx))
        else:
            eta_seconds = remaining_chars * avg_time_per_char if remaining_chars > 0 else 0

        percent = (total_chars_processed / total_chars_all) * 100
        return {
            "ready": True, "percent": percent,
            "chars_processed": total_chars_processed, "chars_total": total_chars_all,
            "elapsed": elapsed_total, "eta_seconds": eta_seconds,
            "finish_str": format_finish_time(eta_seconds),
        }

    # ------------------------------------------------------------------
    # Job execution (same logic as generate.py's process_generation_job)
    # ------------------------------------------------------------------

    def _timeout_monitor(self, stop_event, gen_id, timeout_sec, start_time):
        """Cancel a generation if it runs longer than its timeout threshold."""
        while not stop_event.is_set():
            if time.time() - start_time > timeout_sec:
                cancel_generation(gen_id)
                break
            time.sleep(1.0)

    def process_generation_job(self, idx, total_jobs, strref, npc_name, voice_name, filename, text,
                                profile_id, regressor, generation_memory,
                                retry_count=0, retry_delay=0.0):
        """
        Execute a single TTS generation job with optional retry on failure.
        Returns (success, elapsed_time, audio_duration, chars_processed, retry_attempts).
        """
        chars = len(text)
        estimated_sec = estimate_generation_time(regressor, chars)

        max_attempts = retry_count + 1
        attempt = 0
        retry_attempts = 0
        last_elapsed = 0
        last_audio_duration = 0

        while attempt < max_attempts:
            if self._stop_requested.is_set() and attempt > 0:
                break
            attempt += 1

            if attempt > 1:
                logger.info(f"🔄 Retry {attempt - 1}/{retry_count} for {filename} (STRREF: {strref})")
                time.sleep(retry_delay)

            stop_event = threading.Event()
            cancel_event = threading.Event()

            timeout_sec = None
            if ENABLE_TIMEOUT_SAFEGUARD:
                timeout_sec = TIMEOUT_MAX_SECONDS
                if len(regressor) >= TIMEOUT_MIN_ESTIMATES:
                    timeout_sec = min(timeout_sec, estimated_sec * TIMEOUT_MULTIPLIER)

            ticker = threading.Thread(
                target=self._job_progress_ticker,
                args=(stop_event, idx, total_jobs, filename, strref,
                      estimated_sec, timeout_sec, npc_name, voice_name, chars),
                daemon=True,
            )
            ticker.start()

            start_time = time.time()
            elapsed = 0
            audio_duration = 0
            gen_id = None
            final_event = None
            monitor_thread = None

            try:
                gen_id = submit_generation(profile_id, text, ENGINE, MODEL_SIZE)
                self._current_gen_id = gen_id

                if timeout_sec is not None:
                    monitor_thread = threading.Thread(
                        target=self._timeout_monitor,
                        args=(cancel_event, gen_id, timeout_sec, start_time),
                        daemon=True,
                    )
                    monitor_thread.start()

                final_event = wait_for_completion(gen_id)

                cancel_event.set()
                if monitor_thread:
                    monitor_thread.join(timeout=0.5)

                elapsed = time.time() - start_time
                stop_event.set()
                ticker.join(timeout=1.0)
                self._current_gen_id = None

                if final_event and final_event.get("status") == "completed":
                    audio_duration = final_event.get("duration", 0.0)

                    safe_npc = sanitize_filename(npc_name)
                    npc_output_dir = os.path.join(OUTPUT_DIR, safe_npc)
                    os.makedirs(npc_output_dir, exist_ok=True)
                    output_path = os.path.join(npc_output_dir, f"{filename}.wav")

                    temp_path = output_path + ".tmp"
                    try:
                        download_audio(gen_id, temp_path)
                        if CONVERT_TO_OGG:
                            convert_to_ogg(temp_path, output_path, OGG_QUALITY)
                            os.remove(temp_path)
                        else:
                            os.rename(temp_path, output_path)
                    except Exception as e:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        raise e

                    mark_as_generated(generation_memory, npc_name, strref)
                    save_generation_memory(generation_memory, GENERATION_MEMORY_PATH)

                    return True, elapsed, audio_duration, chars, retry_attempts

            except Exception as e:
                if gen_id:
                    try:
                        cancel_generation(gen_id)
                    except Exception:
                        pass
                self._current_gen_id = None
                cancel_event.set()
                if monitor_thread:
                    monitor_thread.join(timeout=0.5)
                stop_event.set()
                ticker.join(timeout=1.0)
                logger.error(f"❌ Generation failed for {filename} (attempt {attempt}/{max_attempts}): {e}")

            last_elapsed = elapsed
            last_audio_duration = audio_duration
            retry_attempts += 1

            if self._stop_requested.is_set():
                break

        return False, last_elapsed, last_audio_duration, chars, retry_attempts

    def process_generation_jobs_all(self, profile_map, generation_memory, selected_rows, total_jobs, total_chars_all):
        """
        Process every selected row, emitting overall_progress before each job
        and stopping early (after the current job) if request_stop() was called.
        Returns (total_chars_processed, avg_time_per_char, retry_stats).
        """
        total_chars_processed = 0
        total_start_time = time.time()
        avg_time_per_char = None
        overall_regressor = Regression()
        regressor = Regression()

        retry_stats = {
            "failed_attempts": 0, "successful_retries": 0,
            "failed_tasks": 0, "failed_task_details": [],
        }

        for idx, (strref, display_name, voice_name, filename, text) in enumerate(selected_rows, start=1):
            if self._stop_requested.is_set():
                logger.warning("⏹ Stop requested - halting before the next job.")
                break

            profile_id = profile_map.get(voice_name)
            if not profile_id:
                logger.warning(f"Skipping {strref}/{filename}: Voice '{voice_name}' not found.")
                continue

            elapsed_total = time.time() - total_start_time
            overall_stats = self._compute_overall_progress(
                total_chars_processed, total_chars_all, total_jobs, idx - 1,
                overall_regressor, avg_time_per_char, elapsed_total
            )
            self.overall_progress.emit(overall_stats)

            success, elapsed, audio_duration, chars, retry_attempts = self.process_generation_job(
                idx, total_jobs, strref, display_name, voice_name, filename, text,
                profile_id, regressor, generation_memory, RETRY_COUNT, RETRY_DELAY
            )

            retry_stats["failed_attempts"] += retry_attempts

            if success:
                regressor.push(chars, elapsed)
                total_chars_processed += chars
                avg_time_per_char = (time.time() - total_start_time) / total_chars_processed
                overall_regressor.push(chars, elapsed)

                if retry_attempts > 0:
                    retry_stats["successful_retries"] += 1
                    log_job_summary(idx, total_jobs, strref, filename, chars, elapsed,
                                     audio_duration, display_name, voice_name, success=True,
                                     error_msg=f"(succeeded after {retry_attempts} retries)")
                else:
                    log_job_summary(idx, total_jobs, strref, filename, chars, elapsed,
                                     audio_duration, display_name, voice_name, success=True)
            else:
                retry_stats["failed_tasks"] += 1
                retry_stats["failed_task_details"].append({
                    "idx": idx, "strref": strref, "filename": filename, "npc_name": display_name,
                })
                error_msg = f"Failed after {retry_attempts} retries"
                log_job_summary(idx, total_jobs, strref, filename, chars, elapsed,
                                 audio_duration, display_name, voice_name, success=False, error_msg=error_msg)

        return total_chars_processed, avg_time_per_char, retry_stats

    # ------------------------------------------------------------------
    # Entry point (the equivalent of generate.py's main())
    # ------------------------------------------------------------------

    def run(self):
        """Run the full pipeline: substitutions -> profile sync -> patcher
        config -> generation memory -> CSV filtering -> generation -> summary."""
        try:
            log_header_start()

            self.stage.emit("Loading voice substitutions...")
            substitutions, substitutions_gender, substitutions_sysname = load_voice_substitutions_all()

            self.stage.emit("Syncing voice profiles...")
            try:
                profile_map = sync_missing_profiles(
                    CSV_PATH, FILENAME_PATTERN, TARGET_VOICES,
                    USE_STRREF_FILTER, STRREF_FILTER_FILE,
                    substitutions, substitutions_gender, substitutions_sysname
                )
                logger.info(f"Loaded {len(profile_map)} voice profiles.")
            except Exception as e:
                logger.error(f"❌ Failed to fetch/sync profiles: {e}")
                self.failed.emit(str(e))
                return

            self.stage.emit("Loading patcher configuration...")
            try:
                patcher_config = load_patcher_config(PATCHER_CONFIG_PATH)
                logger.info("Loaded patcher config.")
            except Exception as e:
                patcher_config = None
                logger.warning(f"⚠️ Could not load patcher config: {e}")

            generation_memory = load_generation_memory(GENERATION_MEMORY_PATH)
            if SKIP_ALREADY_GENERATED:
                logger.info("Already generated files will be skipped.")
            else:
                logger.info("Skipping already generated files is disabled.")

            self.stage.emit("Reading and filtering dialog CSV...")
            try:
                selected_rows, npc_stats = load_and_filter_csv(
                    CSV_PATH, TARGET_VOICES, FILENAME_PATTERN, patcher_config,
                    generation_memory, SKIP_ALREADY_GENERATED, LIMIT, profile_map,
                    USE_STRREF_FILTER, STRREF_FILTER_FILE, FORCE_GENERATED_FILENAMES,
                    substitutions, substitutions_gender, substitutions_sysname
                )
            except Exception as e:
                self.failed.emit(str(e))
                return

            selected_rows = filter_and_sort_rows(selected_rows, profile_map)
            total_jobs = len(selected_rows)
            total_chars_all = sum(len(text) for *_, text in selected_rows)

            filename_mode = "FORCED generated (base36)" if FORCE_GENERATED_FILENAMES else "CSV with base36 fallback"
            logger.info(f"Selected {total_jobs} rows. Total characters: {total_chars_all}")
            logger.info(f"Filename mode: {filename_mode}")
            log_header_summary(total_jobs, total_chars_all)

            if total_jobs == 0:
                logger.info("No jobs to process. Exiting.")
                self.finished.emit({
                    "total_jobs": 0, "total_chars_processed": 0,
                    "avg_time_per_char": None, "npc_stats": npc_stats, "retry_stats": None,
                })
                return

            log_pregeneration_summary(npc_stats, profile_map)

            self.stage.emit(f"Generating {total_jobs} lines...")
            total_chars_processed, avg_time_per_char, retry_stats = self.process_generation_jobs_all(
                profile_map, generation_memory, selected_rows, total_jobs, total_chars_all
            )

            log_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats, retry_stats)

            self.stage.emit("Stopped." if self._stop_requested.is_set() else "Done.")
            self.finished.emit({
                "total_jobs": total_jobs, "total_chars_processed": total_chars_processed,
                "avg_time_per_char": avg_time_per_char, "npc_stats": npc_stats, "retry_stats": retry_stats,
            })

        except Exception as e:
            logger.error(f"❌ Unexpected error: {e}")
            self.failed.emit(str(e))


# ============================================================================
# Main Application Window
# ============================================================================

class GenerateWindow(QMainWindow):
    """Main window: run controls, a live config summary, two real progress
    bars (job + overall), and a scrolling log panel fed by the logger."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎙️ TTS Voice Generator")
        self.resize(1100, 750)

        self.log_signal = LogSignal()
        self.log_signal.message.connect(self._append_log)

        global logger
        logger = log_initialize(self.log_signal)

        self.gen_thread: QThread | None = None
        self.worker: GenerationWorker | None = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # ---------- Configuration summary ----------
        config_group = QGroupBox("⚙️ Configuration")
        config_layout = QGridLayout(config_group)
        config_items = [
            ("Voicebox URL", BASE_URL), ("Engine", f"{ENGINE} ({MODEL_SIZE})"),
            ("CSV", CSV_PATH), ("Output dir", OUTPUT_DIR),
            ("Retry", f"{RETRY_COUNT}x, {RETRY_DELAY}s delay"),
            ("Convert to Ogg", "Yes" if CONVERT_TO_OGG else "No"),
        ]
        for i, (label, value) in enumerate(config_items):
            row, col = divmod(i, 2)
            config_layout.addWidget(QLabel(f"<b>{label}:</b>"), row, col * 2)
            config_layout.addWidget(QLabel(value), row, col * 2 + 1)
        layout.addWidget(config_group)

        # ---------- Run controls ----------
        controls_layout = QHBoxLayout()
        self.start_btn = QPushButton("▶️ Start Generation")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        controls_layout.addWidget(self.start_btn)
        controls_layout.addWidget(self.stop_btn)
        controls_layout.addStretch()
        layout.addLayout(controls_layout)

        # ---------- Progress ----------
        progress_group = QGroupBox("📊 Progress")
        progress_layout = QVBoxLayout(progress_group)

        self.job_label = QLabel("No job running.")
        self.job_bar = QProgressBar()
        self.job_bar.setRange(0, 100)
        progress_layout.addWidget(self.job_label)
        progress_layout.addWidget(self.job_bar)

        self.overall_label = QLabel("Overall: waiting to start...")
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 100)
        progress_layout.addWidget(self.overall_label)
        progress_layout.addWidget(self.overall_bar)

        layout.addWidget(progress_group)

        # ---------- Log panel ----------
        log_group = QGroupBox("📜 Log")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, stretch=1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready", 3000)

    # ------------------------------------------------------------------
    # Log panel
    # ------------------------------------------------------------------

    def _append_log(self, message: str, levelno: int):
        color = {
            logging.WARNING: QColor("#c98a1c"),
            logging.ERROR: QColor("#d64545"),
        }.get(levelno, self.log_view.palette().text().color())

        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        for line in message.split("\n"):
            self.log_view.setTextColor(color)
            self.log_view.append(line)
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    # ------------------------------------------------------------------
    # Run control
    # ------------------------------------------------------------------

    def _start(self):
        self.log_view.clear()
        self.job_bar.setValue(0)
        self.overall_bar.setValue(0)
        self.job_label.setText("Preparing...")
        self.overall_label.setText("Overall: preparing...")

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self.gen_thread = QThread()
        self.worker = GenerationWorker()
        self.worker.moveToThread(self.gen_thread)

        self.gen_thread.started.connect(self.worker.run)
        self.worker.stage.connect(self._on_stage)
        self.worker.job_progress.connect(self._on_job_progress)
        self.worker.overall_progress.connect(self._on_overall_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self.gen_thread.quit)
        self.worker.failed.connect(self.gen_thread.quit)
        self.gen_thread.finished.connect(self._on_thread_finished)

        self.gen_thread.start()

    def _stop(self):
        if self.worker:
            self.worker.request_stop()
            self.stop_btn.setEnabled(False)
            self.statusBar().showMessage("Stopping after current job...", 5000)

    def _on_thread_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------

    def _on_stage(self, text: str):
        self.statusBar().showMessage(text, 5000)

    def _on_job_progress(self, data: dict):
        self.job_bar.setValue(int(data["percent"]))
        job_width = len(str(data["total"]))
        voice_part = f" ({data['voice_name']})" if data["voice_name"] != data["npc_name"] else ""
        timeout_part = f" (max: {format_time(data['timeout'])})" if data["timeout"] else ""
        self.job_label.setText(
            f"[{data['idx']:>{job_width}}/{data['total']:>{job_width}}] "
            f"{data['strref']}/{data['filename']}  "
            f"{format_time(data['elapsed'])} / {format_time(data['estimated'])}{timeout_part}  "
            f"({data['chars']} chars)  {data['npc_name']}{voice_part}"
        )

    def _on_overall_progress(self, data: dict):
        if not data.get("ready"):
            self.overall_label.setText("Overall: processing...")
            return
        self.overall_bar.setValue(int(data["percent"]))
        self.overall_label.setText(
            f"Overall: {data['chars_processed']:,}/{data['chars_total']:,} chars  "
            f"Elapsed: {format_time(data['elapsed'])}  "
            f"ETA: {format_time(data['eta_seconds'])}  "
            f"@ {data['finish_str']}"
        )

    def _on_finished(self, stats: dict):
        total_jobs = stats["total_jobs"]
        if total_jobs == 0:
            self.job_label.setText("Nothing to generate.")
            self.overall_label.setText("Overall: nothing to generate.")
        else:
            self.job_bar.setValue(100)
            self.overall_bar.setValue(100)
            self.job_label.setText(f"Finished {total_jobs} job(s).")
        self.statusBar().showMessage("Finished.", 5000)

    def _on_failed(self, message: str):
        self.statusBar().showMessage(f"Failed: {message}", 8000)
        self.job_label.setText(f"❌ {message}")


# ============================================================================
# Application Entry Point
# ============================================================================

def main():
    app = QApplication(sys.argv)
    window = GenerateWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
