#!/usr/bin/env python3
"""
TTS Voice Generation Tool for Infinity Engine Games
Processes dialog-report.csv and generates voice lines using Voicebox TTS API
"""

import csv
import json
import os
import re
import subprocess
import sys
import io
import threading
import time
import requests
import logging
from runstats import Regression
from datetime import datetime, timedelta
from pathlib import Path

#region Configuration
# Voicebox API Configuration
BASE_URL = "http://10.0.50.5:17600"    # VoiceBox API - http://localhost:17493 for local server, or remote URL for remote server
ENGINE = "qwen"
MODEL_SIZE = "1.7B"

# Generation Timeout Safeguards
ENABLE_TIMEOUT_SAFEGUARD = True        # Enable/disable timeout protection
TIMEOUT_MAX_SECONDS = 600              # Hard maximum: 10 minutes (600 seconds)
TIMEOUT_MULTIPLIER = 3.0               # Cancel if actual time > estimated * multiplier
TIMEOUT_MIN_ESTIMATES = 10             # Minimum jobs before using estimated time

# Audio Conversion Configuration
CONVERT_TO_OGG = True                  # convert WAV to Ogg Vorbis after download
OGG_QUALITY = 4                        # libvorbis quality

# File Paths
CSV_PATH = r"dialog-report.csv"
PATCHER_CONFIG_PATH = r"patcher-config.json"
OUTPUT_DIR = r"output"

# Generation Limits and filters
LIMIT = 0                              # set to 0 to process all
# Process only these voices, leaving empty will process all voices.
TARGET_VOICES = [                   
    # "Jaheira",
    # "Edwin",
    # "Neera",
    # "Bodhi",
    # "Gaelan Bayle"
]   
# Voice Substitution Files
VOICE_SUBSTITUTIONS_FILE = r"voice-substitutions.json"
VOICE_SUBSTITUTIONS_GENDER_FILE = r"voice-substitutions-gender.json"
VOICE_SUBSTITUTIONS_SYSNAME_FILE = r"voice-substitutions-sysname.json"

# Filter for which lines to process based on the CSV sound filename (column 6).
FILENAME_PATTERN = r"^TS"              # regex pattern for filename (column 6)

# STRREF Filtering
USE_STRREF_FILTER = False             # If False, falls back to TARGET_VOICES/FILENAME_PATTERN
STRREF_FILTER_FILE = r"strrefs.json"   # JSON file with list of strrefs to process

# Voice Fallback Configuration
USE_VOICE_FALLBACK = False
FALLBACK_VOICE_MALE = "BG1 Narrator"
FALLBACK_VOICE_FEMALE = "BG3 Narrator"
FALLBACK_VOICE_NEUTRAL = "Description Narrator"

# Filename Generation
FORCE_GENERATED_FILENAMES = False      # If True, always use generated; if False, use CSV fallback
RESREF_PREFIX = "TS"                   # 2-character prefix for generated resrefs

# Generation memory
SKIP_ALREADY_GENERATED = True          # If True, skip lines already generated (based on generation-memory.json)
GENERATION_MEMORY_PATH = r"generation-memory.json"

# Logging
LOG_FILE_PATH = r"logs/generate.log"

# Pre-generation Summary Options
COMPACT_SUMMARY = True                 # If True, only show NPCs with valid voices; if False, show all
#endregion Configuration

#region Logging
def log_initialize():
    """
    Initialize the logging system with dual output to file and console.
    
    Sets up a logger that writes to two destinations:
        - File: Full debug-level logs with timestamps (YYYY-MM-DD HH:MM:SS)
        - Console: Clean info-level messages without timestamps (just the message)
    
    This dual-sink approach provides detailed logs for troubleshooting while
    keeping the console output clean and readable during interactive use.
    
    The function also configures UTF-8 encoding for Windows console output
    with line buffering to ensure proper display of Unicode characters
    (like emojis and special symbols).
    
    Returns:
        logging.Logger: Configured logger instance ready for use.
    
    Note:
        The logger is initialized at module load time and stored in the
        global variable `logger` for use throughout the application.
    """
    # Ensure UTF-8 output on Windows
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

    log_dir = Path(LOG_FILE_PATH).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.setLevel(logging.INFO)

    # File handler - full format with timestamp
    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)  # Log everything to file
    file_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Console handler - INFO and above
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)  # Only INFO and above to console
    console_formatter = logging.Formatter('%(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    logger.propagate = False

    return logger

# Initialize logger at module level
logger = log_initialize()


def log_header(total_jobs, total_chars_all, header_messages):
    """
    Log a formatted header block containing initialization information.
    
    Creates a visually distinct header section in the log that summarizes
    the configuration and setup state before generation begins. This helps
    contextualize the log entries that follow and provides a quick reference
    for what was configured.
    
    The header includes:
        - Script name and start timestamp
        - All collected initialization messages (profile count, config status)
        - Total number of jobs and characters to process
    
    Args:
        total_jobs (int): Total number of generation jobs to process.
        total_chars_all (int): Total character count across all jobs.
        header_messages (list): List of message strings collected during
            initialization (e.g., "Loaded 88 voice profiles").
    
    Note:
        The header is logged at INFO level and appears in both the console
        and log file with a consistent format.
    """    
    lines =  []

    lines.append("")
    lines.append("=" * 70)
    lines.append("Voice over Generation")
    lines.append(f"# Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append(f"TTS Engine: {ENGINE}" + (f" ({MODEL_SIZE})" if MODEL_SIZE and MODEL_SIZE.strip() else ""))
    lines.extend(header_messages)
    lines.append(f"Total jobs: {total_jobs}, Total chars: {total_chars_all}")
    lines.append("=" * 70)

    summary = "\n".join(lines)
    logger.info(summary)


def log_pregeneration_summary(npc_stats, profile_map):
    """
    Format the pre-generation summary as a string for both console and log output.

    Builds a complete summary table showing:
    - Voice profile status (valid or missing)
    - Total lines per NPC
    - Already generated (Done)
    - Missing voices (Missing)
    - Remaining to generate (To Gen)
    - Total character count

    The summary helps users verify that all configured voices exist before
    starting the potentially long generation process.

    If COMPACT_SUMMARY is True:
        - Only NPCs with valid voices AND pending work are shown in detail
        - NPCs with missing voices are summarized in a single line
        - NPCs with valid voices but nothing to generate are summarized in a single line
    If COMPACT_SUMMARY is False:
        - All NPCs are shown including those with missing voices (marked with "❌ Missing")

    Column definitions:
        - Total: Total number of rows for this NPC
        - Done: Rows already generated (from generation-memory.json)
        - Missing: Rows skipped because no valid voice profile exists
        - To Gen: Rows that will be generated now
        - Characters: Total characters across all rows for this NPC

    Args:
        npc_stats (dict): Statistics dictionary for each NPC, structured as:
            {
                "NPC Name": {
                    "voice_name": str,
                    "total": int,
                    "done": int,
                    "missing": int,
                    "to_generate": int,
                    "chars": int
                }
            }
        profile_map (dict): Voice profile map from get_all_profiles(),
            mapping profile names to their IDs.

    Note:
        The table includes a "VALID TOTAL" row showing only NPCs with
        existing voice profiles, and a "TOTAL" row showing all NPCs
        (including those with missing profiles that will be skipped).
        Missing profiles are marked with "❌ Missing" in the table when
        COMPACT_SUMMARY is False, or summarized in a single line when True.
    """
    # formatting helpers
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

    header_lines = []
    detail_lines = []
    totals_lines = []
    
    header_lines.append("\n" + "=" * LINE_LENGTH)
    header_lines.append("📊 PRE-GENERATION VOICE SUMMARY")

    if TARGET_VOICES:
        header_lines.append(f"   🔍 Filter mode: TARGET_VOICES ({len(TARGET_VOICES)} NPCs)")
    else:
        header_lines.append("   📡 Scan mode: ALL lines (no TARGET_VOICES filter)")
    
    # Show fallback status
    if USE_VOICE_FALLBACK:
        header_lines.append(f"   🔄 Voice fallback ENABLED: M->{FALLBACK_VOICE_MALE}, F->{FALLBACK_VOICE_FEMALE}, NEUTRAL->{FALLBACK_VOICE_NEUTRAL}")
    else:
        header_lines.append("   ⛔ Voice fallback DISABLED")
    
    # Show strref filter status
    if USE_STRREF_FILTER:
        try:
            with open(STRREF_FILTER_FILE, "r") as f:
                count = len(json.load(f))
            header_lines.append(f"   📋 STRREF filter ENABLED: {count} STRREFs from {STRREF_FILTER_FILE}")
        except:
            header_lines.append(f"   📋 STRREF filter ENABLED (file: {STRREF_FILTER_FILE})")
    else:
        header_lines.append("   📋 STRREF filter DISABLED")
    
    # Show filename generation status
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

    grand_total = 0
    grand_done = 0
    grand_skipped = 0
    grand_to_gen = 0
    grand_chars = 0

    # Track counts for NPCs with work to generate
    generate_total = 0
    generate_chars = 0
    
    missing_npcs = []
    missing_chars_total = 0
    missing_done_total = 0
    missing_skipped_total = 0
    
    # Track NPCs with valid voices but nothing to generate
    done_npcs = []
    done_chars_total = 0
    done_done_total = 0
    done_skipped_total = 0

    for npc_name, stats in npc_stats.items():
        profile_name = stats.get("voice_name", npc_name)
        has_profile = profile_name in profile_map
        total = stats["total"]
        done = stats["done"]
        skipped = stats["skipped"]
        to_gen = stats["to_generate"]
        chars = stats["chars"]

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

            # Track NPCs with valid voices but nothing to generate
            done_npcs.append(npc_name)
            done_chars_total += chars
            done_done_total += done
            done_skipped_total += skipped
            
            # Check if we should show this NPC in detail
            # Show if: (COMPACT_SUMMARY is False) OR (we have work to generate)
            show_in_detail = (not COMPACT_SUMMARY) or (to_gen > 0)
            profile_str = f"✅ {profile_name}"
        else:
            # Track missing NPCs for summary
            missing_npcs.append(npc_name)
            missing_chars_total += chars
            missing_done_total += done
            missing_skipped_total += skipped
            
            # Only print missing NPCs if COMPACT_SUMMARY is False
            show_in_detail = (not COMPACT_SUMMARY)
            profile_str = "❌ Missing"

        if show_in_detail:
            # Format valid NPCs
            detail_lines.append(
                f"{trunc(npc_name, COL_WIDTH.NPC):<{COL_WIDTH.NPC}} "
                f"{trunc(profile_str, COL_WIDTH.PROFILE - 1):<{COL_WIDTH.PROFILE - 1}} "
                f"{fmt(total, COL_WIDTH.TOTAL)} "
                f"{fmt(done, COL_WIDTH.DONE)} "
                f"{fmt(skipped, COL_WIDTH.SKIPPED)} "
                f"{fmt(to_gen, COL_WIDTH.TO_GEN)} "
                f"{fmt(chars, COL_WIDTH.CHARS)}"
            )

    detail_lines.append("-" * LINE_LENGTH)

    # Build totals section
    totals_table_header = (
        f"{'TOTALS':<{COL_WIDTH.NPC}} "
        f"{'':<{COL_WIDTH.PROFILE}} "
        f"{'Total':>{COL_WIDTH.TOTAL}} "
        f"{'Done':>{COL_WIDTH.DONE}} "
        f"{'Missing':>{COL_WIDTH.SKIPPED}} "
        f"{'To Gen':>{COL_WIDTH.TO_GEN}} "
        f"{'Chars':>{COL_WIDTH.CHARS}}"
    )
    totals_lines.append(totals_table_header)
    totals_lines.append("-" * LINE_LENGTH)
    
    # Print summary for missing voices if there are any
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
    
    # Print summary for NPCs with nothing to generate
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

    # Compose summary: header -> totals -> details
    summary = "\n".join(header_lines + totals_lines + detail_lines)
    
    # Single log call - logger handles both console and file
    logger.info(summary)


def log_job_summary(idx, total_jobs, strref, filename, chars, elapsed, audio_duration, npc_name, voice_name, success=True, error_msg=None):
    """
    Log a job summary line to both console and file.
    
    Creates a consistent formatted string containing all job information
    (STRREF, filename, timing, NPC name, etc.) and writes it using the
    appropriate log level (INFO for success, WARNING for failure).
    
    Args:
        idx (int): Current job index (1-based).
        total_jobs (int): Total number of jobs.
        strref (str): STRREF identifier.
        filename (str): Output filename.
        chars (int): Number of characters in the text.
        elapsed (float): Generation time in seconds.
        audio_duration (float): Duration of generated audio in seconds.
        npc_name (str): NPC name.
        voice_name (str): Voice profile name used.
        success (bool, optional): Whether generation succeeded. Defaults to True.
        error_msg (str, optional): Error message if failed. Defaults to None.
    
    Note:
        The summary line is formatted identically for both console and file
        output to ensure consistency across both sinks.
    """
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


def log_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats):
    """
    Log the final summary after all generation jobs complete.
    
    Writes a comprehensive summary section showing:
        - Total files processed
        - Total characters processed
        - Average time per character
        - Already generated files (detailed if COMPACT_SUMMARY is False)
        - Missing voices (detailed if COMPACT_SUMMARY is False)
        - Completion timestamp
    
    Args:
        total_jobs (int): Total number of jobs processed.
        total_chars_processed (int): Total characters processed.
        avg_time_per_char (float): Average generation time per character.
        npc_stats (dict): Statistics dictionary per NPC.
    
    Note:
        The summary is logged at INFO level. When COMPACT_SUMMARY is True,
        only totals are shown. When False, detailed breakdowns are included.
    """
    # Extract statistics
    total_done = 0
    total_skipped = 0
    done_summary = {}
    skipped_summary = {}

    for voice, stats in npc_stats.items():
        done = stats.get("done", 0)
        skipped = stats.get("skipped", 0)
        
        if done > 0:
            total_done += done
            done_summary[voice] = done
        
        if skipped > 0:
            total_skipped += skipped
            skipped_summary[voice] = skipped

    # Build the summary string
    lines = []
    lines.append("")
    lines.append("=" * 70)
    lines.append("FINAL SUMMARY")
    lines.append("=" * 70)
    lines.append(f"Processed: {total_jobs:,} files")
    lines.append(f"Total characters: {total_chars_processed:,}")
    
    if avg_time_per_char:
        lines.append(f"Average time per character: {avg_time_per_char:.4f}s")

    # Show "already generated" details based on COMPACT_SUMMARY
    if total_done:
        if COMPACT_SUMMARY:
            # Compact mode: just the total
            lines.append(f"Skipped already generated: {total_done:,} files")
        else:
            # Full mode: show all details
            done_details = ", ".join(f"{voice}: {count:,}" for voice, count in done_summary.items())
            lines.append(f"Skipped already generated: {total_done:,} ({done_details})")

    # Show "missing voices" details based on COMPACT_SUMMARY
    if total_skipped:
        if COMPACT_SUMMARY:
            # Compact mode: just the total
            lines.append(f"Skipped missing voices: {total_skipped:,} files")
        else:
            # Full mode: show all details
            skipped_details = ", ".join(f"{voice}: {count:,}" for voice, count in skipped_summary.items())
            lines.append(f"Skipped missing voices: {total_skipped:,} ({skipped_details})")

    lines.append(f"# Finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append("")

    summary = "\n".join(lines)
    logging.info(summary)
#endregion Logging

#region Utility Functions
def format_time(seconds):
    """
    Convert a time duration in seconds to a human-readable string format.

    The function automatically selects the most appropriate time unit based on
    the duration, providing a compact but readable representation suitable for
    progress displays and logging.

    Examples:
        45.6 seconds -> "45.6s"
        125 seconds -> "2m5s"
        3720 seconds -> "1h2m"
        172800 seconds -> "2d0h"

    Args:
        seconds (float): Time duration in seconds. Can be fractional.

    Returns:
        str: Human-readable time string with appropriate units (s, m, h, or d).
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


def format_finish_time(eta_seconds):
    """
    Format an estimated time remaining into a human-readable finish time string.

    Converts the ETA (in seconds) to an absolute date/time that the process is
    expected to complete. The format automatically adapts based on how far
    in the future the finish time is:
    - Same day: Shows time only (e.g., "14:30:45")
    - Future day: Shows date and time using the system's locale settings

    This provides users with a clear, at-a-glance understanding of when their
    long-running generation job will complete, making it easier to plan
    around the process.

    Args:
        eta_seconds (float): Estimated time remaining in seconds.
            Can be fractional (e.g., 125.5 seconds).

    Returns:
        str: Formatted finish time string.
            - If eta_seconds > 0: Returns formatted date/time.
            - If eta_seconds <= 0: Returns "..." (indicates ETA is being
              calculated or process is nearly complete).

    Examples:
        >>> # Assuming current time is 2026-08-12 14:00:00 in US locale
        >>> format_finish_time(2745)  # 45 minutes 45 seconds
        "14:45:45"
        >>> format_finish_time(86400)  # 24 hours from now
        "08/13/2026 14:00"  # US locale
        "13.08.2026 14:00"  # German locale

    Note:
        The function uses the system's local time and locale settings for
        date formatting. This means the output will automatically adapt to
        the user's regional preferences (e.g., MM/DD/YYYY in US, DD.MM.YYYY
        in Europe). For multi-day runs, this is generally accurate enough
        for practical purposes.
    """
    if eta_seconds > 0:
        finish_time = datetime.now() + timedelta(seconds=eta_seconds)
        # If finishing today, show time only
        if finish_time.date() == datetime.now().date():
            finish_time_str = finish_time.strftime("%H:%M:%S")
        else:
            # Use locale-aware date format with time
            # %x = locale's appropriate date representation
            # %X = locale's appropriate time representation
            finish_time_str = finish_time.strftime("%x %X")
    else:
        finish_time_str = "..."
    return finish_time_str


def progress_bar(percent, width=30):
    """
    Generate a visual progress bar string for console output.

    Creates a horizontal bar filled with block characters (█) proportional to
    the completion percentage, with unfilled portions shown as dots (░).
    The percentage is displayed numerically at the end.

    Args:
        percent (float): Completion percentage, expected to be between 0 and 100.
        width (int, optional): Total character width of the progress bar.
            Defaults to 30 characters.

    Returns:
        str: Formatted progress bar string, e.g., "[████████░░░░] 66.7%".

    Note:
        The function does not clamp percent values; values outside 0-100 will
        produce visually incorrect bars but won't crash.
    """
    filled = int(width * percent / 100)
    bar = '█' * filled + '░' * (width - filled)
    return f"[{bar}] {percent:.1f}%"


def sanitize_filename(name):
    r"""
    Clean a string to be safe for use as a Windows file or directory name.

    This function ensures the resulting name is valid on Windows systems by:
    1. Replacing invalid characters (<>:"/\|?*) with underscores
    2. Removing trailing spaces and dots (which Windows doesn't allow)
    3. Handling reserved device names (CON, PRN, AUX, COM1-9, LPT1-9)
    4. Providing a fallback if the result would be empty

    Args:
        name (str): The original string to sanitize.

    Returns:
        str: A safe filename/directory name string.

    Note:
        The function is Windows-focused but produces safe names for most
        modern filesystems. It does not enforce length limits.
    """
    # Replace Windows-invalid characters with underscore
    name = re.sub(r'[<>:"/\\|?*]', '_', name)

    # Windows does not allow names ending in a space or dot
    name = name.rstrip(' .')

    # Windows reserved device names
    reserved_names = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5",
        "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
        "LPT6", "LPT7", "LPT8", "LPT9",
    }

    if name.upper() in reserved_names:
        name = f"_{name}_"

    # Just in case the result became empty
    if not name:
        name = "_unnamed_"

    return name


def to_base36(value):
    """
    Convert an integer to its base36 representation.
    
    Args:
        value (int): Non-negative integer to convert.
        
    Returns:
        str: Base36 representation of the value.
        
    Raises:
        ValueError: If value is negative.
    """
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
    """
    Generate an 8-character resref from a StrRef number.
    
    Format: 2-character prefix + 6-character base36 number.
    Example: prefix "TS" + StrRef 12345 -> "TS0009IX"
    
    Args:
        strref (int or str): The StrRef number (can be int or string).
        prefix (str): 2-character prefix. Defaults to "TS".
        
    Returns:
        str: 8-character resref in uppercase.
        
    Raises:
        ValueError: If prefix is not exactly 2 characters or strref is invalid.
    """
    # Validate prefix
    if len(prefix) != 2:
        raise ValueError(f"Prefix must be exactly 2 characters, got '{prefix}'")
    
    # Convert strref to int if it's a string
    if isinstance(strref, str):
        try:
            strref_int = int(strref)
        except ValueError:
            raise ValueError(f"StrRef must be a valid integer, got '{strref}'")
    else:
        strref_int = strref
    
    # Validate strref is non-negative
    if strref_int < 0:
        raise ValueError(f"StrRef must be non-negative, got {strref_int}")
    
    # Convert to base36 and pad to 6 characters
    suffix = to_base36(strref_int).rjust(6, '0')
    
    # Return as uppercase
    return (prefix + suffix).upper()
#endregion Utility Functions

#region Voice Profile Management
def load_voice_substitutions(file_path, default=None):
    """
    Load voice substitution rules from a JSON file.

    Args:
        file_path (str): Path to the JSON file.
        default (dict, optional): Default dictionary if file not found.

    Returns:
        dict: The loaded substitution dictionary, or default if file not found.
    """
    if default is None:
        default = {}
    
    if not os.path.exists(file_path):
        logger.warning(f"⚠️ Voice substitution file not found: {file_path}")
        return default
    
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        if not isinstance(data, dict):
            logger.warning(f"⚠️ Voice substitution file must contain a JSON object: {file_path}")
            return default
        
        return data
    except Exception as e:
        logger.warning(f"⚠️ Could not load voice substitutions from {file_path}: {e}")
        return default


def load_voice_substitutions_all():
    """
    Load all voice substitution rules from their respective JSON files.

    Returns:
        tuple: (substitutions, substitutions_gender, substitutions_sysname)
            - substitutions (dict): NPC name -> voice profile
            - substitutions_gender (dict): NPC name|gender -> voice profile
            - substitutions_sysname (dict): sysname -> voice profile
    """
    substitutions = load_voice_substitutions(VOICE_SUBSTITUTIONS_FILE, {})
    substitutions_gender = load_voice_substitutions(VOICE_SUBSTITUTIONS_GENDER_FILE, {})
    substitutions_sysname = load_voice_substitutions(VOICE_SUBSTITUTIONS_SYSNAME_FILE, {})
    
    return substitutions, substitutions_gender, substitutions_sysname


def get_voice_profile_name(npc_name, gender=None, profile_map=None, sysname=None,
                           substitutions=None, substitutions_gender=None, 
                           substitutions_sysname=None):
    r"""
    Resolve an NPC name to the corresponding Voicebox profile name.
    
    Priority order (highest to lowest):
    1. System name substitution (substitutions_sysname)
    2. NPC name + Gender substitution (substitutions_gender)
    3. NPC name only substitution (substitutions)
    4. NPC name as profile name (if it exists in profile_map)
    5. Gender-based fallback (if USE_VOICE_FALLBACK is True)
    6. Neutral/unknown fallback

    Uses the substitution mappings to translate NPC names to specific
    voice profiles. This allows multiple NPCs to share a voice profile or
    to use a profile name that differs from the NPC's display name.

    If no substitution exists for the NPC, the NPC name itself is used
    as the profile name.

    If voice fallback is enabled and the profile doesn't exist, falls back
    to gender-based voices.

    Args:
        npc_name (str): The name of the NPC as it appears in the CSV data.
            Can be empty for descriptions/lore entries.
        gender (str, optional): Gender from CSV ("M", "F", or empty).
        profile_map (dict, optional): Map of available voice profiles for fallback checking.
        sysname (str, optional): System name from CSV (column 1).
        substitutions (dict, optional): NPC name -> voice profile mappings.
        substitutions_gender (dict, optional): NPC name|gender -> voice profile mappings.
        substitutions_sysname (dict, optional): sysname -> voice profile mappings.

    Returns:
        str: The Voicebox profile name to use for generating speech, or None
            if no valid voice could be resolved.

    Example:
        >>> get_voice_profile_name("Nym Khalazza")
        "BG1 Narrator"
        >>> get_voice_profile_name("Bandit", "M")
        "Bandit male"
        >>> get_voice_profile_name("Bandit", "F")
        "Bandit female"
        >>> get_voice_profile_name("", "M")  # Empty NPC name
        "BG1 Narrator"  # Uses FALLBACK_VOICE_MALE
        >>> get_voice_profile_name("Unknown NPC", "", None)
        "Unknown NPC"  # No fallback, returns the name as-is
    """
    # Use loaded substitutions or fallback to defaults
    if substitutions is None:
        substitutions = {}
    if substitutions_gender is None:
        substitutions_gender = {}
    if substitutions_sysname is None:
        substitutions_sysname = {}
    
    # 1. Check system name substitution first (highest priority)
    if sysname and sysname in substitutions_sysname:
        return substitutions_sysname[sysname]
    
    # 2. Check NPC name + gender substitution
    if npc_name and gender:
        gender_key = f"{npc_name}|{gender}"
        if gender_key in substitutions_gender:
            return substitutions_gender[gender_key]
    
    # 3. Check NPC name only substitution
    if npc_name and npc_name in substitutions:
        return substitutions[npc_name]
    
    # 4. Check if the NPC name exists as a profile (only if npc_name exists)
    if npc_name and profile_map is not None and npc_name in profile_map:
        return npc_name
    
    # 5. Fallback if enabled
    if USE_VOICE_FALLBACK:
        if gender == "M":
            return FALLBACK_VOICE_MALE
        elif gender == "F":
            return FALLBACK_VOICE_FEMALE
        else:
            return FALLBACK_VOICE_NEUTRAL
    
    # 6. No fallback and no valid voice found - return None
    return None 


def get_all_profiles():
    """
    Fetch all available voice profiles from the Voicebox TTS API.

    Sends a GET request to the /profiles endpoint of the Voicebox server
    and returns a mapping of profile names to their numeric IDs.

    Returns:
        dict: A dictionary mapping profile names (str) to profile IDs (int).

    Raises:
        requests.exceptions.RequestException: If the API request fails
            due to network issues, server errors, or invalid responses.

    Note:
        The API is expected to return a JSON array of profile objects,
        each containing at least "name" and "id" fields. Profiles without
        a name or ID will be silently skipped.
    """
    resp = requests.get(f"{BASE_URL}/profiles")
    resp.raise_for_status()
    return {p["name"]: p["id"] for p in resp.json()}
#endregion Voice Profile Management

#region Generation Memory
def load_generation_memory(memory_path):
    """
    Load the generation history from a JSON file.

    The generation memory tracks which voice lines have already been successfully
    generated, organized by NPC name and STRREF. This enables the script to
    skip already-completed generations on subsequent runs.

    Memory structure:
        {
            "NPC Name 1": {
                "12345": true,
                "12346": true
            },
            "NPC Name 2": {
                "78901": true
            }
        }

    Args:
        memory_path (str): Filesystem path to the JSON memory file.

    Returns:
        dict: The loaded memory dictionary, or an empty dict if the file
            doesn't exist or is corrupted.

    Note:
        If the file exists but contains invalid JSON or is not a dict,
        the function logs a warning and returns an empty dict to allow
        the script to continue with a fresh memory.
    """
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
    """
    Save the generation history to a JSON file.

    Writes the memory dictionary to disk with pretty-printing (4-space indent)
    for human readability. The file is overwritten completely on each save.

    Args:
        memory (dict): The generation memory dictionary to save.
        memory_path (str): Filesystem path where the JSON file should be written.

    Raises:
        OSError: If the file cannot be written due to permissions or
            filesystem errors.
        TypeError: If the memory contains data that is not JSON-serializable.

    Note:
        The function does not create parent directories; they should exist
        before calling this function.
    """
    with open(memory_path, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=4, ensure_ascii=False)


def is_already_generated(memory, npc_name, strref):
    """
    Check if a specific voice line has already been generated.

    Queries the generation memory to determine if a given NPC/STRREF
    combination has been previously successfully processed.

    Args:
        memory (dict): The generation memory dictionary.
        npc_name (str): The name of the NPC.
        strref (str): The STRREF identifier for the voice line.

    Returns:
        bool: True if the combination exists in memory, False otherwise.

    Note:
        STRREF values are converted to strings for dictionary lookup
        to ensure consistent matching regardless of input type.
    """
    return str(strref) in memory.get(npc_name, {})


def mark_as_generated(memory, npc_name, strref):
    """
    Record a successfully generated voice line in memory.

    Updates the generation memory to mark a specific NPC/STRREF
    combination as completed. Creates the necessary nested dictionary
    structure if it doesn't already exist.

    Args:
        memory (dict): The generation memory dictionary (modified in place).
        npc_name (str): The name of the NPC.
        strref (str): The STRREF identifier for the voice line.

    Note:
        STRREF is stored as a string key with value True to indicate
        successful generation. The function modifies the memory dict
        in place and does not automatically save to disk.
    """
    voice_memory = memory.setdefault(npc_name, {})
    voice_memory[str(strref)] = True
#endregion Generation Memory

#region TTS API Client
def submit_generation(profile_id, text, engine, model_size):
    """
    Submit a text-to-speech generation request to the Voicebox API.

    Sends the provided text to the Voicebox server for processing with the
    specified voice profile, engine, and model size. The server responds
    with a generation ID that can be used to track the job's progress.

    Args:
        profile_id (int): The numeric ID of the voice profile to use.
        text (str): The text content to convert to speech.
        engine (str): The TTS engine to use (e.g., "qwen").
        model_size (str): The model size (e.g., "0.6B", "1.5B").

    Returns:
        str: The generation ID assigned by the Voicebox server.

    Raises:
        requests.exceptions.RequestException: If the API request fails.
        RuntimeError: If the response does not contain an "id" field.

    Note:
        The generation ID is required for subsequent status polling
        and audio download operations.
    """
    payload = {
        "text": text,
        "profile_id": profile_id,
        "language": "en",
        "engine": engine,
        "model_size": model_size,
    }
    resp = requests.post(f"{BASE_URL}/generate", json=payload)
    resp.raise_for_status()
    data = resp.json()
    gen_id = data.get("id")
    if not gen_id:
        raise RuntimeError(f"Response missing 'id': {data}")
    return gen_id


def wait_for_completion(gen_id):
    """
    Wait for a generation job to complete by streaming Server-Sent Events.

    Connects to the Voicebox API's status endpoint and listens for SSE events
    until the generation reaches either "completed" or "failed" status.

    The function streams events in real-time, allowing the server to push
    progress updates, though this implementation only cares about the final
    status event.

    Args:
        gen_id (str): The generation ID returned by submit_generation().

    Returns:
        dict: The final event data containing at least "status" and
            potentially "duration" (for completed jobs) or "error"
            (for failed jobs).

    Raises:
        requests.exceptions.RequestException: If the SSE connection fails.

    Note:
        The function blocks until the generation completes or fails.
        For very slow generations, this may take a long time.
    """
    url = f"{BASE_URL}/generate/{gen_id}/status"
    headers = {"Accept": "text/event-stream"}
    final_event = None

    with requests.get(url, headers=headers, stream=True) as response:
        response.raise_for_status()

        for line in response.iter_lines(decode_unicode=True):
            if isinstance(line, bytes):
                line = line.decode("utf-8")

            if not line:
                continue

            if line.startswith("data: "):
                json_str = line[6:]

                try:
                    event = json.loads(json_str)
                except json.JSONDecodeError:
                    # Malformed JSON in SSE stream - skip this event
                    continue

                status = event.get("status")

                # The API sends a final event when generation ends
                if status in ("completed", "failed"):
                    final_event = event
                    break

    return final_event


def cancel_generation(gen_id):
    """
    Cancel a queued or running generation on the Voicebox server.

    Sends a POST request to the /generate/{generation_id}/cancel endpoint
    to stop the generation if it's still running.

    Args:
        gen_id (str): The generation ID to cancel.

    Returns:
        tuple: (success, message)
            - success (bool): True if cancellation was successful, False otherwise.
            - message (str): Status message describing the result.
    """
    try:
        cancel_url = f"{BASE_URL}/generate/{gen_id}/cancel"
        resp = requests.post(cancel_url)
        
        if resp.status_code == 200:
            return True, "Cancellation successful"
        else:
            return False, f"Cancellation returned: {resp.status_code}"
    except Exception as e:
        return False, f"Cancellation error: {e}"


def download_audio(gen_id, output_path):
    """
    Download the generated audio file from the Voicebox API.

    Retrieves the audio for a completed generation job and saves it to
    the specified output path. The audio is downloaded as raw WAV data
    (or whatever format the server provides).

    Args:
        gen_id (str): The generation ID to download.
        output_path (str): Filesystem path where the audio should be saved.

    Raises:
        requests.exceptions.RequestException: If the download request fails.
        OSError: If the output file cannot be written.

    Note:
        The function does not perform any audio conversion; it saves the
        raw audio data exactly as received from the server.
    """
    url = f"{BASE_URL}/audio/{gen_id}"
    resp = requests.get(url)
    resp.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(resp.content)
#endregion TTS API Client

#region Text Processing
def load_patcher_config(config_path):
    """
    Load the patcher configuration from a JSON file.

    The patcher config contains text transformation rules including:
    - Identity token mappings (e.g., <CHARNAME> -> actual character name)
    - Gender token variations (e.g., <HE> -> "he", "she", or "they")
    - Phonetic substitution rules for improved TTS pronunciation

    Args:
        config_path (str): Filesystem path to the JSON configuration file.

    Returns:
        dict: The loaded configuration object.

    Raises:
        FileNotFoundError: If the configuration file doesn't exist.
        json.JSONDecodeError: If the file contains invalid JSON.

    Note:
        The configuration structure must match what the patcher system expects.
        Missing optional fields will default to empty lists/dicts.
    """
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def preprocess_text(text, patcher_config):
    """
    Apply comprehensive text transformations for TTS preparation.

    Processes the input text through several transformation stages to
    convert placeholder tokens and apply pronunciation rules before
    sending to the TTS engine.

    Transformation stages (executed in order):
        1. Identity tokens: Replace <CHARNAME>, <GABBER>, <RACE>, <PRO_RACE>
           with the actual PC name/race from the patcher config.

        2. Gender tokens: Replace <HE>, <SHE>, <HIS>, <HER>, <HIM>, etc.
           with appropriate forms based on the PC's gender (male/female/neutral).

        3. Phonetic rules: Apply regex-based substitutions to improve
           TTS pronunciation (e.g., expanding "Mr." to "Mister").

        4. Token cleanup: Remove any remaining <...> tokens that weren't
           processed (treats them as formatting artifacts).

    Args:
        text (str): The raw input text from the CSV file.
        patcher_config (dict): Loaded patcher configuration containing
            identity tokens, gender tokens, and phonetic rules.

    Returns:
        str: The preprocessed text, ready for TTS generation.

    Note:
        The function gracefully handles missing configuration keys by
        skipping that transformation stage. Regex errors in phonetic rules
        are caught and ignored to prevent total failure.
    """
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
            
            # If replacement contains C#-style backreferences ($1, $2, etc.)
            if replacement_template and '$' in replacement_template:
                def repl_func(match):
                    result = replacement_template
                    # Replace $1, $2, etc. with match groups
                    for i in range(1, match.lastindex + 1 if match.lastindex else 0):
                        group_value = match.group(i) or ''
                        result = result.replace(f'${i}', group_value)
                    return result
                
                text = compiled_pattern.sub(repl_func, text)
            else:
                text = compiled_pattern.sub(replacement_template, text)
                
        except re.error:
            # Skip malformed regex patterns rather than crashing
            continue

    # Remove any leftover <...> tokens that weren't processed
    text = re.sub(r'<[^>]+>', '', text)

    return text
#endregion Text Processing

#region Audio Processing
def convert_to_ogg(input_path, output_path=None, quality=2):
    """
    Convert an audio file to Ogg Vorbis format using ffmpeg.

    Uses ffmpeg to convert audio to the Ogg Vorbis codec with libvorbis.
    The output file can be either specified or overwrite the input file.

    Args:
        input_path (str): Path to the source audio file (typically WAV).
        output_path (str, optional): Path for the output file. If None,
            overwrites the input file.
        quality (int, optional): libvorbis quality scale from 0 (lowest)
            to 10 (highest). Defaults to 2, which provides reasonable
            quality-to-size ratio for dialog.

    Returns:
        None: The converted audio is written to the output file.

    Raises:
        subprocess.CalledProcessError: If ffmpeg fails to convert the file.
        FileNotFoundError: If ffmpeg is not installed or not in PATH.

    Note:
        The function forces the Ogg container format regardless of file
        extension using `-f ogg`. This means a file named "example.wav"
        will still be properly formatted as Ogg Vorbis despite the .wav
        extension (which is often required by game engines for compatibility).
    """
    if output_path is None:
        output_path = input_path

    cmd = [
        'ffmpeg',
        '-y',                      # Overwrite output files
        '-i', input_path,          # Input file
        '-c:a', 'libvorbis',       # Use libvorbis codec
        '-qscale:a', str(quality), # Quality setting
        '-f', 'ogg',               # Force Ogg container format
        output_path
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"ffmpeg stderr:\n{e.stderr.decode('utf-8', errors='ignore') if isinstance(e.stderr, bytes) else e.stderr}")
        raise
#endregion Audio Processing

#region Progress Display
def format_overall_line(total_chars_processed, total_chars_all, total_jobs, idx,
                        overall_regressor, avg_time_per_char, elapsed_total):
    """
    Build the Overall progress line string without printing it.

    Constructs a single-line status summary that shows global generation
    progress across all jobs. The returned string is designed to be kept
    as the permanent last line of the console while individual jobs run,
    giving the user a continuous view of overall completion, elapsed time,
    and estimated finish time.

    Calculation logic:
        - Percentage is based on characters processed versus total characters.
        - ETA is predicted with linear regression when enough historical data
          exists (len(overall_regressor) > 1). Otherwise it falls back to a
          simple average-time-per-character estimate.
        - When insufficient data is available the function returns a static
          "Overall: processing..." placeholder.

    Args:
        total_chars_processed (int): Characters successfully generated so far.
        total_chars_all (int): Total characters across all selected jobs.
        total_jobs (int): Total number of jobs in the current run.
        idx (int): Number of jobs already completed (0-based for remaining
            calculation). Used to weight the intercept term of the regression.
        overall_regressor (Regression): Accumulated (chars, time) samples used
            for slope/intercept prediction of remaining work.
        avg_time_per_char (float or None): Running average seconds per character.
            None on the first job before any data exists.
        elapsed_total (float): Wall-clock seconds since the start of the whole
            generation batch.

    Returns:
        str: A fully formatted Overall line, or the placeholder
            "Overall: processing..." when statistics are not yet available.
    """
    if avg_time_per_char is not None and total_chars_all > 0:
        remaining_chars = total_chars_all - total_chars_processed

        if len(overall_regressor) > 1:
            eta_seconds = (overall_regressor.slope() * remaining_chars
                           + overall_regressor.intercept() * (total_jobs - idx))
        else:
            eta_seconds = remaining_chars * avg_time_per_char if remaining_chars > 0 else 0

        overall_percent = (total_chars_processed / total_chars_all) * 100
        chars_processed_str = f"{total_chars_processed:,}"
        chars_total_str = f"{total_chars_all:,}"

        return (
            f"Overall: "
            f"{progress_bar(overall_percent)}  "
            f"{chars_processed_str:>8}/{chars_total_str:<8} chars  "
            f"Elapsed: {format_time(elapsed_total):>9}  "
            f"ETA: {format_time(eta_seconds):>9}  "
            f"@ {format_finish_time(eta_seconds)}"
        )
    return "Overall: processing..."


def progress_worker(stop_event, job_idx, total_jobs, filename, estimated_sec, timeout_sec,
                    npc_name, voice_name, chars, overall_line, strref):
    r"""
    Background thread that maintains a two-line live progress display.

    While a single TTS generation is running this worker continuously updates
    two console lines:

        [job progress bar]          <-- rewritten every 0.5 s
        Overall: ...                <-- always kept as the LAST line

    The job line shows the current file, a percentage bar based on elapsed
    versus estimated time, character count, and NPC name. The Overall line
    (supplied by the caller) remains permanently at the bottom so the user
    can always see global progress and ETA without scrolling.

    Implementation notes:
        - On the first iteration both lines are printed normally so the
          Overall line becomes the final visible line.
        - Subsequent iterations use ANSI cursor movement (\033[2A, \033[K)
          to rewrite the two lines in place without scrolling the terminal.
        - When stop_event is set the worker clears both lines and repositions
          the cursor so the main thread can print the permanent ✅ summary
          cleanly on the same screen real-estate.

    Args:
        stop_event (threading.Event): Event that signals the thread to exit
            (generation completed or failed).
        job_idx (int): Current job number (1-indexed) in the queue.
        total_jobs (int): Total number of jobs to process.
        filename (str): The filename being generated (for display).
        estimated_sec (float): Estimated duration for this job in seconds.
        timeout_sec (float): Maximum allowed duration for this job in seconds.
        npc_name (str): The NPC name being processed (for display).
        voice_name (str): Voice profile name; shown in parentheses only when
            it differs from npc_name.
        chars (int): Number of characters in the text being generated.
        overall_line (str): Pre-formatted Overall progress string that must
            stay as the last line of the display.
        strref (str): The STRREF identifier being generated.

    Note:
        This function is intended to be run as a daemon thread and does
        not return a value. It updates the console in place using ANSI
        escape sequences and exits when stop_event is set.
    """
    start_time = time.time()
    first = True
    job_width = len(str(total_jobs))

    while not stop_event.is_set():
        elapsed = time.time() - start_time

        if estimated_sec > 0:
            percent = min(100, (elapsed / estimated_sec) * 100)
        else:
            percent = 0

        bar = progress_bar(percent)
        time_str = f"{format_time(elapsed)} / {format_time(estimated_sec)} (max: {format_time(timeout_sec)})"

        # Job progress line with STRREF - keep Grok's formatting
        job_msg = (
            f"[{job_idx:>{job_width}}/{total_jobs:>{job_width}}] "
            f"{strref}/{filename}  "
            f"{bar}  "
            f"{time_str}  "
            f"({chars} chars)  "
            f"{npc_name}"
        )
        if voice_name != npc_name:
            job_msg += f" ({voice_name})"

        if first:
            # First draw: print both lines so Overall becomes the last line
            sys.stdout.write(job_msg + "\n")
            sys.stdout.write(overall_line + "\n")
            sys.stdout.flush()
            first = False
        else:
            # Subsequent draws: move up 2 lines, rewrite both, leave cursor
            # after the Overall line so it stays last.
            sys.stdout.write("\033[2A")              # up to job line
            sys.stdout.write("\r\033[K" + job_msg)  # clear + rewrite job
            sys.stdout.write("\n")
            sys.stdout.write("\r\033[K" + overall_line)  # clear + rewrite Overall
            sys.stdout.write("\n")
            sys.stdout.flush()

        time.sleep(0.5)

    # Job finished: clear the two progress lines so the caller can print the ✅ summary
    if not first:
        sys.stdout.write("\033[2A")      # up to job line
        sys.stdout.write("\r\033[K")     # clear job line
        sys.stdout.write("\n")
        sys.stdout.write("\r\033[K")     # clear Overall line
        sys.stdout.write("\n")
        sys.stdout.write("\033[2A")      # go back up so next print starts on the cleared job line
        sys.stdout.flush()
#endregion Progress Display

#region CSV Processing
def filter_and_sort_rows(selected_rows, profile_map):
    """
    Filter out rows with missing voice profiles and sort for optimal processing.

    Removes rows where the voice profile doesn't exist on the server, then sorts
    by voice name to group similar voices together for better caching and
    performance.

    Args:
        selected_rows (list): List of (strref, display_name, voice_name, filename, text)
        profile_map (dict): Map of profile names to IDs.

    Returns:
        list: Filtered and sorted rows.
    """
    # Filter out rows where the voice profile does not exist
    valid_rows = [row for row in selected_rows if row[2] in profile_map]  # voice_name is at index 2
    # Sort by voice name (case-insensitive) to group files by voice
    valid_rows.sort(key=lambda row: row[2].lower())  # voice_name is at index 2
    return valid_rows


def load_strref_filter(filter_file):
    """
    Load the STRREF filter list from a JSON file.
    
    Args:
        filter_file (str): Path to the JSON file containing strref list.
        
    Returns:
        tuple: (strref_set, messages)
            - strref_set (set): Set of strref strings to process, or empty set
            - messages (list): Informational messages collected during loading
    """
    messages = []

    if not os.path.exists(filter_file):
        messages.append(f"⚠️ STRREF filter file not found: {filter_file}")
        messages.append("   Processing all rows (no filter).")
        return set(), messages
    
    try:
        with open(filter_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        if not isinstance(data, list):
            messages.append(f"⚠️ STRREF filter file must contain a JSON array, got {type(data)}")
            return set(), messages
        
        # Convert to set of strings for fast lookup
        return {str(item) for item in data}, messages
    
    except Exception as e:
        messages.append(f"⚠️ Could not load STRREF filter: {e}")
        messages.append("   Processing all rows (no filter).")
        return set(), messages


def load_and_filter_csv(csv_path, target_voices, filename_pattern, patcher_config, 
                       generation_memory, skip_generated, limit, profile_map=None,
                       use_strref_filter=False, strref_filter_file="strrefs.json", 
                       force_generated_filenames=False,
                       substitutions=None, substitutions_gender=None, 
                       substitutions_sysname=None):
    """
    Load CSV data, apply filters, and prepare rows for generation.
    
    Args:
        csv_path (str): Path to the CSV file.
        target_voices (list): List of NPC names to process (empty = all).
        filename_pattern (str): Regex pattern for filename filtering.
        patcher_config (dict): Patcher configuration for text preprocessing.
        generation_memory (dict): Generation memory for skip checking.
        skip_generated (bool): Whether to skip already generated files.
        limit (int): Maximum number of rows to process (0 = all).
        profile_map (dict, optional): Map of available voice profiles for validation.
        use_strref_filter (bool): Whether to use STRREF filter instead of voice/filename filters.
        strref_filter_file (str): Path to STRREF filter JSON file.
        force_generated_filenames (bool): If True, always use generated; if False, use CSV with fallback.
        substitutions (dict, optional): NPC name -> voice profile mappings.
        substitutions_gender (dict, optional): NPC name|gender -> voice profile mappings.
        substitutions_sysname (dict, optional): sysname -> voice profile mappings.
        
    Returns:
        tuple: (selected_rows, npc_stats, messages)
            - selected_rows (list): List of (strref, display_name, voice_name, filename, text)
            - npc_stats (dict): Statistics per NPC
            - messages (list): Informational messages collected during processing
    """
    selected_rows = []
    npc_stats = {}
    strref_filter = set()
    messages = []
    
    # Load STRREF filter if enabled
    if use_strref_filter:
        strref_filter, filter_messages = load_strref_filter(strref_filter_file)
        messages.extend(filter_messages)
        if strref_filter:
            messages.append(f"Loaded {len(strref_filter)} STRREFs from filter file.")
        else:
            messages.append("⚠️ No STRREFs loaded from filter file. Processing all rows.")
    
    try:
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
                
                # Apply STRREF filter if enabled
                if use_strref_filter and strref_filter:
                    if strref not in strref_filter:
                        continue

                # STRREF filter overrides all other filters when enabled
                # When disabled, we fall back to voice and filename filtering
                if not use_strref_filter and not npc_name:
                    continue                    
                
                # Apply voice filter (only if not using STRREF filter)
                if not use_strref_filter and target_voices and npc_name not in target_voices:
                    continue
                
                # Apply filename pattern filter
                if filename_pattern and csv_filename and not re.match(filename_pattern, csv_filename):
                    continue

                # Skip rows without text
                if not text:
                    continue
                
                # Determine filename with fallback
                if force_generated_filenames:
                    # Always use generated filename
                    filename = generate_resref(strref, RESREF_PREFIX)
                else:
                    # Use CSV filename if it exists and is valid, otherwise fallback to generated
                    if csv_filename and csv_filename.strip():
                        filename = csv_filename
                    else:
                        # CSV filename is empty or invalid - generate one
                        filename = generate_resref(strref, RESREF_PREFIX)
                
                # Get voice profile with fallback
                voice_name = get_voice_profile_name(
                    npc_name, gender, profile_map, sysname,
                    substitutions, substitutions_gender, substitutions_sysname
                )

                # Use npc_name for stats, or "Descriptions" if empty
                display_name = npc_name if npc_name else "Descriptions"
                
                # Initialize NPC stats (ALWAYS, even if voice is missing)
                if display_name not in npc_stats:
                    npc_stats[display_name] = {
                        "voice_name": voice_name if voice_name else "MISSING",
                        "total": 0,
                        "done": 0,
                        "skipped": 0,
                        "to_generate": 0,
                        "chars": 0
                    }
                
                npc_stats[display_name]["total"] += 1
                npc_stats[display_name]["chars"] += len(text)
                
                # Check if voice is missing
                if voice_name is None:
                    npc_stats[display_name]["skipped"] += 1
                    continue

                # Check if voice_name exists in profile_map
                if profile_map is not None and voice_name not in profile_map:
                    npc_stats[display_name]["skipped"] += 1
                    continue
                
                # Skip already generated if enabled
                if skip_generated and is_already_generated(generation_memory, display_name, strref):
                    npc_stats[display_name]["done"] += 1
                    continue
                
                npc_stats[display_name]["to_generate"] += 1
                
                # Preprocess text
                text = preprocess_text(text, patcher_config) if patcher_config else text
                
                # Store the row with display_name for folder structure
                selected_rows.append((strref, display_name, voice_name, filename, text))
                
                if limit and len(selected_rows) >= limit:
                    break
    
    except Exception as e:
        logger.error(f"❌ Error reading CSV: {e}")
        sys.exit(1)
    
    return selected_rows, npc_stats, messages
#endregion CSV Processing

#region Generation Execution
def estimate_generation_time(regressor, chars):
    """
    Estimate generation time based on historical data or fallback.

    Uses linear regression from previous jobs to predict time for the current
    text length. Falls back to a conservative estimate if insufficient data.

    Args:
        regressor (Regression): Regression object with historical (chars, time) data.
        chars (int): Number of characters in the current text.

    Returns:
        float: Estimated time in seconds.
    """
    if len(regressor) > 1:
        estimated_sec = regressor.slope() * chars + regressor.intercept()
        return max(estimated_sec, 2.0)
    return 10.0  # Initial guess when no historical data


def timeout_monitor(stop_event, gen_id, timeout_sec, idx, start_time):
    """
    Monitor thread that checks for timeout and cancels the generation if exceeded.
    
    Runs in a separate thread and periodically checks if the generation has
    exceeded its time limit. If it has, it sends a cancellation request.
    
    Args:
        stop_event (threading.Event): Event to signal when generation completes.
        gen_id (str): The generation ID to cancel.
        timeout_sec (float): Timeout in seconds.
        idx (int): Job index for logging.
        start_time (float): Timestamp when the job started.
    """
    elapsed = 0
    while not stop_event.is_set():
        elapsed = time.time() - start_time
        if elapsed > timeout_sec:
            success, message = cancel_generation(gen_id)
            break
        time.sleep(1.0)


def process_generation_job(idx, total_jobs, strref, npc_name, voice_name, filename, text,
                           profile_id, regressor, generation_memory, overall_line):
    """
    Execute a single TTS generation job.

    Handles the complete lifecycle of one generation: submitting the request,
    waiting for completion, downloading the audio, and converting to Ogg Vorbis.

    The progress_worker keeps the job progress line and the Overall line
    (passed in as overall_line) visible, with Overall always last.

    If ENABLE_TIMEOUT_SAFEGUARD is True, a separate monitor thread watches
    the elapsed time and cancels the generation if it exceeds the threshold.

    Args:
        idx (int): Current job index (1-based).
        total_jobs (int): Total number of jobs.
        strref (str): STRREF identifier.
        npc_name (str): NPC name (for memory and folder).
        voice_name (str): Voice profile name.
        filename (str): Output filename base.
        text (str): Preprocessed text to generate.
        profile_id (int): Voice profile ID.
        regressor (Regression): Regression for time estimation.
        generation_memory (dict): Generation memory for recording completion.
        overall_line (str): Pre-formatted Overall progress string to keep as last line.

    Returns:
        tuple: (success, elapsed_time, audio_duration, chars_processed)
            where success is bool indicating if generation succeeded.
    """
    chars = len(text)
    estimated_sec = estimate_generation_time(regressor, chars)
    stop_event = threading.Event()
    cancel_event = threading.Event()  # Signals that generation is done (for monitor)

    # Calculate timeout threshold
    timeout_sec = None
    if ENABLE_TIMEOUT_SAFEGUARD:
        # Always use hard maximum
        timeout_sec = TIMEOUT_MAX_SECONDS
        
        # Use estimated time if we have enough data
        if len(regressor) >= TIMEOUT_MIN_ESTIMATES:
            estimated_timeout = estimated_sec * TIMEOUT_MULTIPLIER
            # Use the more conservative (smaller) timeout
            timeout_sec = min(timeout_sec, estimated_timeout)

    # Start progress bar thread – it will draw job line + Overall line
    worker = threading.Thread(
        target=progress_worker,
        args=(stop_event, idx, total_jobs, filename, estimated_sec, timeout_sec,
              npc_name, voice_name, chars, overall_line, strref)
    )
    worker.daemon = True
    worker.start()

    start_time = time.time()
    success = False
    elapsed = 0
    audio_duration = 0
    gen_id = None
    final_event = None
    monitor_thread = None

    try:
        gen_id = submit_generation(profile_id, text, ENGINE, MODEL_SIZE)
        
        # Start timeout monitor thread if timeout is enabled
        monitor_thread = None
        if timeout_sec is not None:
            monitor_thread = threading.Thread(
                target=timeout_monitor,
                args=(cancel_event, gen_id, timeout_sec, idx, start_time)
            )
            monitor_thread.daemon = True
            monitor_thread.start()

        # Wait for completion (blocking - uses SSE)
        final_event = wait_for_completion(gen_id)
        
        # Signal that generation is done (stop timeout monitor)
        cancel_event.set()
        if monitor_thread:
            monitor_thread.join(timeout=0.5)

        elapsed = time.time() - start_time

        # Stop the progress thread (it clears the two lines for us)
        stop_event.set()
        worker.join(timeout=1.0)

        if final_event and final_event.get("status") == "completed":
            audio_duration = final_event.get("duration", 0.0)

            # Create output directory if it doesn't exist
            safe_npc = sanitize_filename(npc_name)
            npc_output_dir = os.path.join(OUTPUT_DIR, safe_npc)
            os.makedirs(npc_output_dir, exist_ok=True)
            output_path = os.path.join(npc_output_dir, f"{filename}.wav")

            # Download and convert audio
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

            # Record success in memory
            mark_as_generated(generation_memory, npc_name, strref)
            save_generation_memory(generation_memory, GENERATION_MEMORY_PATH)

            success = True

    except Exception as e:
        # If there was an error and we had a gen_id, try to cancel it
        if gen_id:
            try:
                cancel_generation(gen_id)
            except Exception:
                pass  # Ignore cancellation errors during exception handling
        
        cancel_event.set()
        if monitor_thread:
            monitor_thread.join(timeout=0.5)
        
        stop_event.set()
        worker.join(timeout=1.0)
        
        # Log the error
        logger.error(f"❌ Generation failed for {filename}: {e}")

    return success, elapsed, audio_duration, chars
#endregion Generation Execution

# ---------- Main ----------
def main():
    """
    Main entry point for the TTS generation script.

    Orchestrates the entire generation workflow:
    1. Load config files
    2. Load voice profiles from Voicebox API
    3. Load patcher configuration for text preprocessing
    4. Load generation memory to skip already processed files
    5. Read and filter CSV data
    6. Display pre-generation summary
    7. Process each generation job with progress feedback
    8. Display final summary
    """
    header_messages = []

    # 1. Load voice substitution rules from JSON files
    substitutions, substitutions_gender, substitutions_sysname = load_voice_substitutions_all()
    if substitutions:
        header_messages.append(f"Loaded {len(substitutions)} voice substitutions (NPC name).")
    if substitutions_gender:
        header_messages.append(f"Loaded {len(substitutions_gender)} voice substitutions (NPC + gender).")
    if substitutions_sysname:
        header_messages.append(f"Loaded {len(substitutions_sysname)} voice substitutions (System name).")

    # 2. Load profiles
    try:
        profile_map = get_all_profiles()
        header_messages.append(f"Loaded {len(profile_map)} voice profiles.")
    except Exception as e:
        logger.error(f"❌ Failed to fetch profiles: {e}")
        sys.exit(1)

    # 3. Load patcher config (optional - generation continues without it)
    try:
        patcher_config = load_patcher_config(PATCHER_CONFIG_PATH)
        header_messages.append("Loaded patcher config.")
    except Exception as e:
        patcher_config = None
        header_messages.append(f"⚠️ Could not load patcher config: {e}")

    # 4. Load generation memory to skip already processed files
    generation_memory = load_generation_memory(GENERATION_MEMORY_PATH)
    if SKIP_ALREADY_GENERATED:
        header_messages.append("Already generated files will be skipped.")
    else:
        header_messages.append("Skipping already generated files is disabled.")

    # 5. Read CSV, filter, and select rows
    selected_rows, npc_stats, filter_messages = load_and_filter_csv(
        CSV_PATH,
        TARGET_VOICES,
        FILENAME_PATTERN,
        patcher_config,
        generation_memory,
        SKIP_ALREADY_GENERATED,
        LIMIT,
        profile_map,
        USE_STRREF_FILTER,
        STRREF_FILTER_FILE,
        FORCE_GENERATED_FILENAMES,
        substitutions,
        substitutions_gender,
        substitutions_sysname
    )

    header_messages.extend(filter_messages)

    selected_rows = filter_and_sort_rows(selected_rows, profile_map)
    total_jobs = len(selected_rows)

    total_chars_all = sum(len(text) for _, _, _, _, text in selected_rows)

    if FORCE_GENERATED_FILENAMES:
        filename_mode = "FORCED generated (base36)"
    else:
        filename_mode = "CSV with base36 fallback"

    header_messages.append(f"Selected {total_jobs} rows. Total characters: {total_chars_all}")
    header_messages.append(f"Filename mode: {filename_mode}")

    log_header(total_jobs, total_chars_all, header_messages)

    if total_jobs == 0:
        logger.info("No jobs to process. Exiting.")
        return

    # 6. Pre-generation summary
    log_pregeneration_summary(npc_stats, profile_map)

    # 7. Process all generation jobs
    total_chars_processed = 0
    total_start_time = time.time()
    avg_time_per_char = None
    overall_regressor = Regression()
    regressor = Regression()

    for idx, (strref, display_name, voice_name, filename, text) in enumerate(selected_rows, start=1):
        profile_id = profile_map.get(voice_name)
        if not profile_id:
            logger.warning(f"Skipping {strref}/{filename}: Voice '{voice_name}' not found.")
            continue

        # Snapshot Overall line (static for the duration of this job)
        elapsed_total = time.time() - total_start_time
        overall_line = format_overall_line(
            total_chars_processed, total_chars_all, total_jobs, idx - 1,
            overall_regressor, avg_time_per_char, elapsed_total
        )

        # Process the generation job
        success, elapsed, audio_duration, chars = process_generation_job(
            idx, total_jobs, strref, display_name, voice_name, filename, text,
            profile_id, regressor, generation_memory, overall_line
        )

        if success:
            # Update statistics
            regressor.push(chars, elapsed)
            total_chars_processed += chars
            avg_time_per_char = (time.time() - total_start_time) / total_chars_processed
            overall_regressor.push(chars, elapsed)

            # Print job summary
            log_job_summary(idx, total_jobs, strref, filename, chars, elapsed, 
                            audio_duration, display_name, voice_name, success=True)

        else:
            # Handle failure
            error_msg = "Generation failed"
            log_job_summary(idx, total_jobs, strref, filename, chars, elapsed, 
                            audio_duration, display_name, voice_name, success=False, error_msg=error_msg)

    # 8. Final summary
    log_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats)

if __name__ == "__main__":
    main()