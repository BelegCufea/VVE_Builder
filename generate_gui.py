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
import sys
import threading
import time
import uuid
import shutil
import zipfile
from typing import Dict, Iterator, List, Optional, Set, Tuple, Union, cast
import requests
import logging
from appconfig import cfg, set_many as _appconfig_set_many
from collections import defaultdict
from runstats import Regression
from datetime import datetime
from pathlib import Path

from utils import (
    CaseInsensitiveDict,
    get_canonical_key,
    to_base36,
    load_patcher_config,
    preprocess_text,
    convert_to_ogg,
    format_time,
    format_finish_time
)

from tts_voicebox import (
    submit_generation as tts_submit_generation,
    wait_for_completion as tts_wait_for_completion,
    cancel_generation as tts_cancel_generation,
    download_generated_audio as tts_download_generated_audio,
    delete_voice_profile as tts_delete_voice_profile,
    list_profiles as tts_list_profiles,
    check_health as tts_check_health,
    import_profile as tts_import_profile,
)

from PySide6.QtCore import QObject, QThread, Signal, QTimer, Qt, Slot, QMetaObject
from PySide6.QtGui import QFont, QTextCursor, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QTextEdit, QGroupBox, QStatusBar,
    QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox,
    QDialog, QTabWidget, QFormLayout, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView,
)

# ============================================================================
# Logging
# ============================================================================

class LogSignal(QObject):
    """
    Bridges Python logging records into Qt signals.

    Attributes:
        message: Emitted with (message_string, log_level) when a log record
            is received from the logging system.

    Note:
        This object must be created in the main GUI thread so its signals
        are properly connected to slots in the same thread.
    """
    message = Signal(str, int)


class QtLogHandler(logging.Handler):
    """
    Custom logging handler that forwards records to a LogSignal.

    This handler intercepts all Python logging calls (logger.info(),
    logger.warning(), etc.) and emits them as Qt signals, allowing log
    messages from background threads to safely update the GUI log panel.

    Args:
        log_signal: The signal to emit log messages on.
    """

    def __init__(self, log_signal: LogSignal):
        super().__init__()
        self.log_signal = log_signal

    def emit(self, record: logging.LogRecord) -> None:
        """
        Process a log record and emit it as a Qt signal.

        Args:
            record: The log record to process.

        Note:
            The record is formatted using the handler's formatter before
            being emitted. Any exceptions during emission are suppressed
            to prevent logging failures from crashing the application.
        """
        try:
            msg = self.format(record)
            self.log_signal.message.emit(msg, record.levelno)
        except Exception:
            pass


def log_initialize(log_signal: LogSignal) -> logging.Logger:
    """
    Initialize the logging system with three sinks.

    Sets up a logger that writes to:
        - File: Full debug-level logs with timestamps (YYYY-MM-DD HH:MM:SS)
        - Console: Clean info-level messages (if a console is attached)
        - GUI log panel: Clean info-level messages via QtLogHandler

    Args:
        log_signal: The signal to connect the GUI handler to.

    Returns:
        The configured logger instance.

    Note:
        The logger is configured once at application startup. Although the
        file handler accepts DEBUG records, the root logger itself is set to
        INFO, so DEBUG records are currently filtered before reaching it.
        The console handler, when attached, shows ERROR and above; the GUI
        handler shows INFO and above.
    """
    log_dir = Path(cfg.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{Path(__file__).stem}.log"
    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(file_handler)

    if sys.stdout and hasattr(sys.stdout, 'isatty') and sys.stdout.isatty():
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.ERROR)
        console_handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(console_handler)

    gui_handler = QtLogHandler(log_signal)
    gui_handler.setLevel(logging.INFO)
    gui_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(gui_handler)

    for noisy_logger in ("urllib3", "urllib3.connectionpool", "requests"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    logger.propagate = False
    return logger


def log_header_start() -> None:
    """Log the run-start banner immediately."""
    lines = ["", "=" * 70, "Voice over Generation",
             f"# Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "=" * 70,
             f"TTS Engine: {cfg.ENGINE}" + (f" ({cfg.MODEL_SIZE})" if cfg.MODEL_SIZE and cfg.MODEL_SIZE.strip() else "")]
    logger.info("\n".join(lines))


def log_header_summary(total_jobs: int, total_chars_all: int) -> None:
    """
    Log the closing lines of the header block once totals are known.

    Args:
        total_jobs: Total number of generation jobs to process.
        total_chars_all: Total character count across all jobs.
    """
    logger.info("\n".join([f"Total jobs: {total_jobs}, Total chars: {total_chars_all}", "=" * 70]))


def log_pregeneration_summary(voice_stats: dict, profile_map: dict) -> None:
    """
    Build and log the pre-generation summary table.

    Displays a formatted table showing for each NPC + voice profile combination:
        - NPC name
        - Voice profile status (valid or missing)
        - Total lines to process
        - Lines already generated (Done)
        - Lines with missing voices (Missing)
        - Lines remaining to generate (To Gen)
        - Total character count

    If cfg.COMPACT_SUMMARY is True:
        - Only NPCs with valid voices AND pending work are shown in detail
        - NPCs with missing voices are summarized in a single line
        - NPCs with valid voices but nothing to generate are summarized

    Args:
        voice_stats: Statistics dictionary per voice profile + NPC combination.
        profile_map: Voice profile name -> ID mapping.
    """
    trunc = lambda text, width: (text[:width - 3] + "...") if len(text) > width else text
    fmt = lambda v, w: f"{v:{w},}" if v != 0 else ' ' * w

    class COL_WIDTH:
        """Column widths used to format the pre-generation summary table."""
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

    if cfg.TARGET_VOICES:
        header_lines.append(f"   🔍 Filter mode: cfg.TARGET_VOICES ({len(cfg.TARGET_VOICES)} NPCs)")
    else:
        header_lines.append("   📡 Scan mode: ALL lines (no cfg.TARGET_VOICES filter)")

    if cfg.USE_VOICE_FALLBACK:
        header_lines.append(f"   🔄 Voice fallback ENABLED: M->{cfg.FALLBACK_VOICE_MALE}, F->{cfg.FALLBACK_VOICE_FEMALE}, NEUTRAL->{cfg.FALLBACK_VOICE_NEUTRAL}")
    else:
        header_lines.append("   ⛔ Voice fallback DISABLED")

    if cfg.USE_STRREF_FILTER:
        try:
            with open(cfg.STRREF_FILTER_FILE, "r") as f:
                count = len(json.load(f))
            header_lines.append(f"   📋 STRREF filter ENABLED: {count} STRREFs from {cfg.STRREF_FILTER_FILE}")
        except Exception:
            header_lines.append(f"   📋 STRREF filter ENABLED (file: {cfg.STRREF_FILTER_FILE})")
    else:
        header_lines.append("   📋 STRREF filter DISABLED")

    if cfg.FORCE_GENERATED_FILENAMES:
        header_lines.append(f"   🔧 Filenames: FORCED generated (base36) with prefix: {cfg.FILENAME_PREFIX}")
    else:
        header_lines.append(f"   🔧 Filenames: CSV with base36 fallback (prefix: {cfg.FILENAME_PREFIX})")

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

    for key, stats in sorted(voice_stats.items(), key=lambda item: ((item[1].get("display_name") or "").lower(), (item[1].get("voice_name") or "").lower())):
        voice_name = stats.get("voice_name") or "None"
        display_name = stats.get("display_name") or "Unknown"
        has_profile = voice_name in profile_map if voice_name else False

        total = stats["total"]
        done = stats.get("done", 0)
        skipped = stats.get("skipped", 0)
        to_gen = stats["to_generate"]

        chars_total = stats["chars"]["total"]
        chars_done = stats["chars"].get("done", 0)
        chars_skipped = stats["chars"].get("skipped", 0)
        chars_to_gen = stats["chars"].get("to_generate", 0)

        grand_total += total
        grand_done += done
        grand_skipped += skipped
        grand_to_gen += to_gen
        grand_chars += chars_total

        show_in_detail = False
        profile_str = ""

        if has_profile:
            if to_gen > 0:
                generate_total += to_gen
                generate_chars += chars_to_gen
            done_npcs.append(display_name)
            done_chars_total += chars_done
            done_done_total += done
            done_skipped_total += skipped
            show_in_detail = (not cfg.COMPACT_SUMMARY) or (to_gen > 0)
            profile_str = f"✅ {voice_name}"
        else:
            missing_npcs.append(display_name)
            missing_chars_total += chars_skipped
            missing_done_total += done
            missing_skipped_total += skipped
            if not cfg.COMPACT_SUMMARY:
                show_in_detail = True
                profile_str = f"❌ Missing"

        if show_in_detail:
            detail_lines.append(
                f"{trunc(display_name, COL_WIDTH.NPC):<{COL_WIDTH.NPC}} "
                f"{trunc(profile_str, COL_WIDTH.PROFILE):<{COL_WIDTH.PROFILE - 1}} "
                f"{fmt(total, COL_WIDTH.TOTAL)} "
                f"{fmt(done, COL_WIDTH.DONE)} "
                f"{fmt(skipped, COL_WIDTH.SKIPPED)} "
                f"{fmt(to_gen, COL_WIDTH.TO_GEN)} "
                f"{fmt(chars_total, COL_WIDTH.CHARS)}"
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
            f"{'🔊 Already done':<{COL_WIDTH.NPC - 1}} "
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

    logger.info("\n".join(header_lines + detail_lines + totals_lines))


def log_job_summary(idx: int, total_jobs: int, strref: str, filename: str, chars: int,
                    elapsed: float, audio_duration: float, npc_name: str, voice_name: str,
                    success: bool = True, error_msg: Optional[str] = None) -> None:
    """
    Log a single job's completion result.

    Args:
        idx: Job index (1-based).
        total_jobs: Total number of jobs.
        strref: STRREF identifier.
        filename: Output filename.
        chars: Number of characters in the text.
        elapsed: Generation time in seconds.
        audio_duration: Duration of generated audio.
        npc_name: NPC name.
        voice_name: Voice profile name used.
        success: True if generation succeeded.
        error_msg: Error message if failed.
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


def log_final_summary(total_jobs: int, total_chars_processed: int, avg_time_per_char: Optional[float],
                      voice_stats: dict, retry_stats: Optional[dict] = None,
                      was_stopped: bool = False, successful_jobs: int = 0) -> None:
    """
    Log the final summary after all generation jobs complete.

    Displays a comprehensive summary including:
        - Job statistics (total, processed, skipped, failed)
        - Character statistics (total, processed)
        - Performance metrics (average time per character)
        - Status (completed or stopped)
        - Already generated files (detailed if cfg.COMPACT_SUMMARY is False)
        - Missing voices (detailed if cfg.COMPACT_SUMMARY is False)
        - Retry statistics (if provided)
        - Completion timestamp

    Args:
        total_jobs: Total number of jobs in the queue.
        total_chars_processed: Total characters successfully generated.
        avg_time_per_char: Average generation time per character.
        voice_stats: Statistics dictionary per voice profile + NPC combination.
        retry_stats: Retry statistics from the generation run.
        was_stopped: True if the user manually stopped the process.
        successful_jobs: Number of successfully generated files.
    """
    total_done = 0
    total_skipped = 0
    done_summary, skipped_summary = {}, {}

    for key, stats in voice_stats.items():
        display_name = stats.get("display_name", "Unknown")
        done = stats.get("done", 0)
        skipped = stats.get("skipped", 0)

        if done > 0:
            total_done += done
            done_summary[display_name] = done_summary.get(display_name, 0) + done
        if skipped > 0:
            total_skipped += skipped
            skipped_summary[display_name] = skipped_summary.get(display_name, 0) + skipped

    total_failed = retry_stats.get('failed_tasks', 0) if retry_stats else 0
    processed_jobs = successful_jobs + total_failed

    total_done_chars = 0
    total_skipped_chars = 0
    total_chars_all = 0

    for key, stats in voice_stats.items():
        chars = stats.get("chars", {})
        total_chars_all += chars.get("total", 0)
        total_done_chars += chars.get("done", 0)
        total_skipped_chars += chars.get("skipped", 0)

    completion_rate = (successful_jobs / total_jobs * 100) if total_jobs > 0 else 0

    LABEL_WIDTH = 30

    job_numbers = [
        total_jobs,
        successful_jobs,
        total_failed,
        total_done,
        total_skipped,
        processed_jobs,
        total_jobs,
    ]
    job_width = max((len(str(n)) for n in job_numbers if n > 0), default=9) + 1

    char_numbers = [
        total_chars_all,
        total_chars_processed,
        total_done_chars,
        total_skipped_chars,
    ]
    char_width = max((len(str(n)) for n in char_numbers if n > 0), default=9) + 1

    retry_numbers = [
        retry_stats.get('failed_attempts', 0) if retry_stats else 0,
        retry_stats.get('successful_retries', 0) if retry_stats else 0,
        retry_stats.get('failed_tasks', 0) if retry_stats else 0,
    ]
    retry_width = max((len(str(n)) for n in retry_numbers if n > 0), default=9) + 1 if retry_stats else 10

    job_width = max(job_width, 10)
    char_width = max(char_width, 10)

    lines = []
    lines.append("")
    lines.append("=" * 70)

    if was_stopped:
        lines.append("⏹ GENERATION STOPPED (User Request)")
    else:
        lines.append("✅ GENERATION COMPLETE")

    lines.append("=" * 70)

    lines.append("📊 JOB STATISTICS")
    lines.append("-" * 70)

    lines.append(f"{'  Total jobs in queue:':<{LABEL_WIDTH}} {total_jobs:>{job_width},}")
    lines.append(f"{'  ✅ Successfully generated:':<{LABEL_WIDTH - 1}} {successful_jobs:>{job_width},}")

    if total_failed > 0:
        lines.append(f"{'  ❌ Failed:':<{LABEL_WIDTH - 1}} {total_failed:>{job_width},}")

    if total_done > 0:
        lines.append(f"{'  ⏭️ Already generated:':<{LABEL_WIDTH}} {total_done:>{job_width},} (skipped)")

    if total_skipped > 0:
        lines.append(f"{'  ⏭️ Missing voices:':<{LABEL_WIDTH}} {total_skipped:>{job_width},} (skipped)")

    if processed_jobs > 0 and total_jobs > 0:
        lines.append(f"{'  📈 Completion rate:':<{LABEL_WIDTH - 1}} {completion_rate:>{job_width-1}.1f}% ({successful_jobs:,} of {total_jobs:,})")

    lines.append("")

    lines.append("📝 CHARACTER STATISTICS")
    lines.append("-" * 70)

    lines.append(f"{'  Total chars in queue:':<{LABEL_WIDTH}} {total_chars_all:>{char_width},}")
    lines.append(f"{'  ✅ Generated:':<{LABEL_WIDTH - 1}} {total_chars_processed:>{char_width},}")

    if total_done_chars > 0:
        lines.append(f"{'  ⏭️ Already generated:':<{LABEL_WIDTH}} {total_done_chars:>{char_width},} (skipped)")

    if total_skipped_chars > 0:
        lines.append(f"{'  ⏭️ Missing voices:':<{LABEL_WIDTH}} {total_skipped_chars:>{char_width},} (skipped)")

    if total_chars_processed > 0 and avg_time_per_char:
        lines.append(f"{'  ⚡ Avg time/char (s):':<{LABEL_WIDTH - 1}} {avg_time_per_char:>{char_width}.4f}")

    lines.append("")

    if not cfg.COMPACT_SUMMARY:
        lines.append("📋 DETAILED BREAKDOWN")
        lines.append("-" * 70)

        if done_summary:
            done_details = ", ".join(f"{npc}: {count:,}" for npc, count in done_summary.items())
            lines.append(f"  Already generated: {done_details}")

        if skipped_summary:
            skipped_details = ", ".join(f"{npc}: {count:,}" for npc, count in skipped_summary.items())
            lines.append(f"  Missing voices:     {skipped_details}")

    if retry_stats:
        has_retries = (retry_stats.get('failed_attempts', 0) > 0 or 
                    retry_stats.get('successful_retries', 0) > 0 or 
                    retry_stats.get('failed_tasks', 0) > 0 or
                    retry_stats.get('failed_task_details', []))
        
        if has_retries:
            lines.append("")
            lines.append("🔄 RETRY STATISTICS")
            lines.append("-" * 70)
            lines.append(f"{'  Total retry attempts:':<{LABEL_WIDTH}} {retry_stats.get('failed_attempts', 0):>{retry_width},}")
            lines.append(f"{'  Successful retries:':<{LABEL_WIDTH}} {retry_stats.get('successful_retries', 0):>{retry_width},}")
            lines.append(f"{'  Failed tasks:':<{LABEL_WIDTH}} {retry_stats.get('failed_tasks', 0):>{retry_width},}")

            failed_tasks = retry_stats.get('failed_task_details', [])
            if failed_tasks:
                lines.append("")
                lines.append("  ❌ Failed tasks:")
                for task in failed_tasks:
                    lines.append(f"    [{task['idx']}] {task['strref']}/{task['filename']} - {task['npc_name']}")

    lines.append("")
    lines.append("=" * 70)

    if was_stopped:
        lines.append(f"⏹ Stopped at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        lines.append(f"✅ Completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    lines.append("=" * 70)
    lines.append("")

    logging.info("\n".join(lines))

logger: logging.Logger = logging.getLogger()


# ============================================================================
# Utility Functions
# ============================================================================

def sanitize_filename(name: str) -> str:
    r"""
    Clean a string to be safe for use as a Windows file or directory name.

    Replaces invalid characters with underscores, removes trailing spaces and dots,
    and handles Windows reserved device names.

    Args:
        name: The original string to sanitize.

    Returns:
        A safe filename/directory name string.
    """
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


def generate_resref(strref: Union[int, str], prefix: str = "TS") -> str:
    """
    Generate an 8-character resref from a StrRef number.

    Format: 2-character prefix + 6-character base36 number.
    Example: prefix "TS" + StrRef 12345 -> "TS0009IX"

    Args:
        strref: The StrRef number.
        prefix: 2-character prefix. Defaults to "TS".

    Returns:
        8-character resref in uppercase.

    Raises:
        ValueError: If prefix is not exactly 2 characters or strref is invalid.
    """
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
    suffix = to_base36(strref_int, width=6)
    return (prefix + suffix).upper()


# ============================================================================
# Voice Profile Management
# ============================================================================

def load_voice_substitutions_all() -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """
    Load all voice substitution rules from a single JSON file.

    File structure:
    {
        "npc": {"NPC Name": "voice_profile"},
        "gender": {"NPC|gender": "voice_profile"},
        "sysname": {"SystemName": "voice_profile"}
    }

    Returns:
        A tuple containing:
            - substitutions: NPC name -> voice profile
            - substitutions_gender: NPC name|gender -> voice profile
            - substitutions_sysname: sysname -> voice profile
    """
    substitutions, substitutions_gender, substitutions_sysname = {}, {}, {}

    path = Path(cfg.VOICE_SUBSTITUTIONS_FILE)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            substitutions = data.get("npc", {})
            substitutions_gender = data.get("gender", {})
            substitutions_sysname = data.get("sysname", {})
            logger.info(f"Loaded substitutions from {cfg.VOICE_SUBSTITUTIONS_FILE}:")
            logger.info(f"  NPC-level: {len(substitutions)} entries")
            logger.info(f"  Gender-level: {len(substitutions_gender)} entries")
            logger.info(f"  SysName-level: {len(substitutions_sysname)} entries")
        except Exception as e:
            logger.warning(f"⚠️ Could not load voice substitutions from {cfg.VOICE_SUBSTITUTIONS_FILE}: {e}")
    else:
        logger.info(f"No substitution file found, using defaults: {cfg.VOICE_SUBSTITUTIONS_FILE}")

    return substitutions, substitutions_gender, substitutions_sysname


def resolve_voice_substitution(npc_name: Optional[str], gender: Optional[str] = None,
                               sysname: Optional[str] = None,
                               substitutions: Optional[Dict[str, str]] = None,
                               substitutions_gender: Optional[Dict[str, str]] = None,
                               substitutions_sysname: Optional[Dict[str, str]] = None) -> Optional[str]:
    """
    Apply the substitution-lookup priority: system name, then NPC+gender, then NPC name.

    Args:
        npc_name: NPC name from the CSV.
        gender: Gender from CSV ("M", "F", or empty).
        sysname: System name from CSV (column 1).
        substitutions: NPC name -> voice profile mappings.
        substitutions_gender: NPC name|gender -> voice profile mappings.
        substitutions_sysname: sysname -> voice profile mappings.

    Returns:
        The substituted profile name if any rule matches, else None.
    """
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


def get_voice_profile_name(npc_name: Optional[str], gender: Optional[str] = None,
                           profile_map: Optional[CaseInsensitiveDict] = None, sysname: Optional[str] = None,
                           substitutions: Optional[Dict[str, str]] = None,
                           substitutions_gender: Optional[Dict[str, str]] = None,
                           substitutions_sysname: Optional[Dict[str, str]] = None) -> Optional[str]:
    """
    Resolve an NPC name to a Voicebox profile name.

    Priority order (highest to lowest):
    1. System name substitution (substitutions_sysname)
    2. NPC name + Gender substitution (substitutions_gender)
    3. NPC name only substitution (substitutions)
    4. NPC name as profile name (if it exists in profile_map)
    5. Gender-based fallback (if cfg.USE_VOICE_FALLBACK is True)
    6. Neutral/unknown fallback

    Args:
        npc_name: NPC name from CSV. Can be empty for descriptions.
        gender: Gender from CSV ("M", "F", or empty).
        profile_map: Map of available voice profiles.
        sysname: System name from CSV (column 1).
        substitutions: NPC name -> voice profile mappings.
        substitutions_gender: NPC name|gender -> voice profile mappings.
        substitutions_sysname: sysname -> voice profile mappings.

    Returns:
        Voice profile name, or None if no valid voice found.
    """
    substituted = resolve_voice_substitution(
        npc_name, gender, sysname, substitutions, substitutions_gender, substitutions_sysname
    )
    if substituted:
        if profile_map is not None and substituted in profile_map:
            return get_canonical_key(profile_map, substituted)
        return substituted

    if npc_name and profile_map is not None and npc_name in profile_map:
        return get_canonical_key(profile_map, npc_name)

    if cfg.USE_VOICE_FALLBACK:
        if gender == "M":
            return cfg.FALLBACK_VOICE_MALE
        elif gender == "F":
            return cfg.FALLBACK_VOICE_FEMALE
        else:
            return cfg.FALLBACK_VOICE_NEUTRAL

    return None


def delete_profile(profile_id: str) -> Tuple[bool, str]:
    """
    Delete a voice profile from the Voicebox server.

    Args:
        profile_id: The ID of the profile to delete.

    Returns:
        A tuple containing:
            - success: True if deletion was successful.
            - message: Status message describing the result.
    """
    try:
        return tts_delete_voice_profile(profile_id)
    except Exception as e:
        return False, f"Deletion error: {e}"


def get_all_profiles() -> Tuple[CaseInsensitiveDict, CaseInsensitiveDict]:
    """
    Fetch all voice profiles from Voicebox, filtering out zero-sample ones.

    Profiles with sample_count == 0 are unusable for generation and are
    tracked separately for potential rebuilding.

    Returns:
        A tuple containing:
            - profile_map: Profile name -> ID mapping (valid profiles only)
            - zero_sample_profiles: Profile name -> ID mapping for zero-sample profiles

    Raises:
        requests.exceptions.RequestException: If the API request fails.
    """
    resp_json = tts_list_profiles()

    profile_map = CaseInsensitiveDict()
    zero_sample_profiles = CaseInsensitiveDict()
    total_profiles = 0

    for p in resp_json:
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

def get_candidate_voice_name(npc_name: Optional[str], gender: Optional[str] = None,
                             sysname: Optional[str] = None,
                             substitutions: Optional[Dict[str, str]] = None,
                             substitutions_gender: Optional[Dict[str, str]] = None,
                             substitutions_sysname: Optional[Dict[str, str]] = None) -> Optional[str]:
    """
    Resolve the profile name a CSV row *would* use, without checking existence.

    This is used during the provisioning phase to determine what profiles
    are needed before checking if they exist on Voicebox. It stops before
    the fallback logic because fallback voices are never something we'd
    want to auto-compose a profile for.

    Args:
        npc_name: NPC name from the CSV.
        gender: Gender from CSV.
        sysname: System name from CSV (column 1).
        substitutions: NPC name -> voice profile mappings.
        substitutions_gender: NPC name|gender -> voice profile mappings.
        substitutions_sysname: sysname -> voice profile mappings.

    Returns:
        The candidate profile name, or None if the row has no NPC name
        to resolve (e.g. description/lore lines).
    """
    substituted = resolve_voice_substitution(
        npc_name, gender, sysname, substitutions, substitutions_gender, substitutions_sysname
    )
    return substituted or npc_name or None


def scan_csv_needed_voice_names(substitutions: Optional[Dict[str, str]] = None,
                                substitutions_gender: Optional[Dict[str, str]] = None,
                                substitutions_sysname: Optional[Dict[str, str]] = None) -> Set[str]:
    """
    Scan the dialog CSV and collect the set of voice profile names it needs.

    Shares row filtering with load_and_filter_csv() via iter_filtered_csv_rows()
    so the "needed" set matches what would actually be generated. If fallback
    mode is enabled (cfg.USE_VOICE_FALLBACK), configured fallback profiles are
    also included to ensure pre-flight sync and repair.

    Args:
        substitutions: NPC name -> voice profile mappings.
        substitutions_gender: NPC name|gender -> voice profile mappings.
        substitutions_sysname: sysname -> voice profile mappings.

    Returns:
        Voice profile names referenced by the filtered CSV rows, along with
        any configured fallback voices if fallback mode is active.
    """
    needed = set()

    for strref, sysname, npc_name, gender, csv_filename, text in iter_filtered_csv_rows():
        candidate = get_candidate_voice_name(
            npc_name, gender, sysname, substitutions, substitutions_gender, substitutions_sysname
        )
        if candidate:
            needed.add(candidate)

    if cfg.USE_VOICE_FALLBACK:
            fallbacks = {cfg.FALLBACK_VOICE_MALE, cfg.FALLBACK_VOICE_FEMALE, cfg.FALLBACK_VOICE_NEUTRAL}
            needed.update({fb for fb in fallbacks if fb})            

    return needed


def scan_available_voice_dirs(voices_dir: str) -> CaseInsensitiveDict:
    """
    Scan a voices/ directory and group WAV+TXT sample pairs by NPC name.

    Only files with both a WAV and matching TXT transcript are counted as
    usable samples for composing a profile.

    Args:
        voices_dir: Path to the directory of NPC WAV/TXT samples.

    Returns:
        NPC name -> list of sample dicts, each with 'number', 'wav_path',
        'txt_path', 'transcript'. Only entries with at least one valid
        sample pair are included.
    """
    voices_path = Path(voices_dir)
    voice_groups: defaultdict[str, list] = defaultdict(list)

    if not voices_path.exists():
        return CaseInsensitiveDict()

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

    return CaseInsensitiveDict(voice_groups)


def create_profile_package(voice_name: str, files: list, output_dir: Path) -> Optional[Path]:
    """
    Build a .voicebox.zip package for a voice from its sample files.

    Args:
        voice_name: The profile name to embed in the manifest.
        files: Sample dicts as produced by scan_available_voice_dirs().
        output_dir: Directory to write the .voicebox.zip into.

    Returns:
        Path to the created zip file, or None on failure.
    """
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


def import_profile_zip(zip_path: Path) -> Optional[dict]:
    """
    Import a composed .voicebox.zip package into Voicebox.

    Args:
        zip_path: Path to the .voicebox.zip file.

    Returns:
        Parsed JSON response on success, None on failure.
    """
    try:
        result = tts_import_profile(zip_path)
        if result is None:
            logger.error(f"❌ Error importing {zip_path.name}: import returned None")
        return result
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Error importing {zip_path.name}: {e}")
        return None


def sync_profiles(substitutions: Optional[Dict[str, str]] = None,
                  substitutions_gender: Optional[Dict[str, str]] = None,
                  substitutions_sysname: Optional[Dict[str, str]] = None,
                  sync_all: bool = False) -> CaseInsensitiveDict:
    """
    Reconcile Voicebox's profile list against local voices/ directory.

    If sync_all is True:
        Syncs ALL voice sample groups found in cfg.VOICES_DIR with Voicebox:
        - Deletes and rebuilds any zero-sample profiles on Voicebox that have local samples.
        - Composes and imports any missing profiles that have local samples.
    If sync_all is False:
        Reconciles Voicebox's profile list against what the filtered CSV needs and what
        cfg.VOICES_DIR can provide, composing and importing missing/zero-sample profiles.

    Args:
        substitutions: NPC name -> voice profile mappings.
        substitutions_gender: NPC name|gender -> voice profile mappings.
        substitutions_sysname: sysname -> voice profile mappings.
        sync_all: If True, process all available voices in cfg.VOICES_DIR.

    Returns:
        Freshest profile name -> id map available.
    """
    profile_map, zero_sample_profiles = get_all_profiles()

    if not cfg.AUTO_PROVISION_PROFILES and not sync_all:
        return profile_map

    available = scan_available_voice_dirs(cfg.VOICES_DIR)

    if sync_all:
        logger.info(f"🔄 Starting full voice profile sync with Voicebox from {cfg.VOICES_DIR}/...")
        target_names = list(available.keys())
        zero_sample_targets = [name for name in zero_sample_profiles if name in available]
        missing_targets = [name for name in target_names if name not in profile_map and name not in zero_sample_profiles]
        already_up_to_date = [name for name in target_names if name in profile_map and name not in zero_sample_profiles]
    else:
        needed = scan_csv_needed_voice_names(
            substitutions, substitutions_gender, substitutions_sysname
        )
        zero_sample_targets = [name for name in needed if name in zero_sample_profiles and name in available]
        missing_targets = [name for name in needed if name not in profile_map and name not in zero_sample_profiles and name in available]
        already_up_to_date = [name for name in needed if name in profile_map]

        truly_missing = [name for name in needed if name not in profile_map and name not in zero_sample_profiles and name not in available]
        unfixable_zero = [name for name in needed if name in zero_sample_profiles and name not in available]
        if truly_missing or unfixable_zero:
            logger.warning(
                f"⚠️ {len(truly_missing)} needed voice(s) missing from Voicebox and not found in {cfg.VOICES_DIR}/, "
                f"and {len(unfixable_zero)} zero-sample profile(s) cannot be rebuilt."
            )

    renew_targets: List[str] = []
    if cfg.PROFILE_SYNC_RENEW:
        renew_targets = [name for name in already_up_to_date if name in available]
        if renew_targets:
            already_up_to_date = [name for name in already_up_to_date if name not in renew_targets]
            logger.info(
                f"🔁 PROFILE_SYNC_RENEW is on: forcing delete+rebuild of "
                f"{len(renew_targets)} already-valid profile(s)."
            )

    rebuildable = sorted(set(zero_sample_targets) | set(renew_targets), key=str.lower)
    composable = sorted(missing_targets, key=str.lower)

    if not rebuildable and not composable:
        logger.info(f"✅ All {len(already_up_to_date)} profile(s) are already up to date on Voicebox.")
        return profile_map

    imported, reimported, failed = [], [], []

    if rebuildable:
        logger.info(f"♻️ Rebuilding {len(rebuildable)} zero-sample/renewed profile(s) from {cfg.VOICES_DIR}/...")
        for voice_name in rebuildable:
            profile_id = zero_sample_profiles.get(voice_name, profile_map.get(voice_name))
            canonical_name = get_canonical_key(available, voice_name)
            if not profile_id:
                failed.append(voice_name)
                continue
            logger.info(f"  Deleting zero-sample profile: {voice_name} (ID: {profile_id})...")
            success, message = delete_profile(profile_id)
            if not success:
                logger.warning(f"  ✗ Failed to delete {voice_name}: {message}")
                failed.append(voice_name)
                continue
            logger.info(f"  ✓ Deleted: {voice_name}")
            time.sleep(cfg.PROFILE_SYNC_RETRY_DELAY)

            logger.info(f"  Rebuilding profile: {canonical_name}...")
            if not canonical_name:
                failed.append(voice_name)
                continue
            zip_path = create_profile_package(canonical_name, available[voice_name], cfg.PROFILE_PACKAGES_DIR)
            if not zip_path:
                failed.append(voice_name)
                continue
            result = import_profile_zip(zip_path)
            if result:
                reimported.append(canonical_name)
                logger.info(f"  ✓ Re-imported: {canonical_name}")
            else:
                logger.warning(f"  ✗ Failed to re-import: {canonical_name}")
                failed.append(voice_name)

    if composable:
        logger.info(f"🧩 Composing and importing {len(composable)} profile(s) from {cfg.VOICES_DIR}/...")
        for voice_name in composable:
            canonical_name = get_canonical_key(available, voice_name)
            if not canonical_name:
                failed.append(voice_name)
                continue
            zip_path = create_profile_package(canonical_name, available[voice_name], cfg.PROFILE_PACKAGES_DIR)
            if not zip_path:
                failed.append(voice_name)
                continue
            result = import_profile_zip(zip_path)
            if result:
                imported.append(canonical_name)
                logger.info(f"  ✓ Imported: {canonical_name}")
            else:
                logger.warning(f"  ✗ Failed to import: {canonical_name}")
                failed.append(voice_name)

    all_imported = imported + reimported
    if not all_imported:
        logger.warning("⚠️ Could not import any profiles.")
        return profile_map

    still_missing = set(all_imported)
    for attempt in range(1, cfg.PROFILE_SYNC_MAX_ATTEMPTS + 1):
        profile_map, _ = get_all_profiles()
        still_missing = {name for name in still_missing if name not in profile_map}
        if not still_missing:
            break
        time.sleep(cfg.PROFILE_SYNC_RETRY_DELAY)

    if still_missing:
        logger.warning(
            f"⚠️ {len(still_missing)} imported/re-imported profile(s) not yet visible after "
            f"{cfg.PROFILE_SYNC_MAX_ATTEMPTS} attempts: {', '.join(sorted(still_missing))}"
        )

    logger.info("=" * 60)
    logger.info("VOICE PROFILE SYNC SUMMARY")
    logger.info("=" * 60)
    if sync_all:
        logger.info(f"  Total local voices in {cfg.VOICES_DIR}/: {len(available)}")
    logger.info(f"  New profiles created:             {len(imported)}")
    logger.info(f"  Zero-sample/renewed profiles fixed: {len(reimported)}")
    logger.info(f"  Already up to date:               {len(already_up_to_date)}")
    if failed:
        logger.warning(f"  Failed:                           {len(failed)} ({', '.join(failed)})")
    logger.info("=" * 60)

    return profile_map


def sync_missing_profiles(substitutions: Optional[Dict[str, str]] = None,
                          substitutions_gender: Optional[Dict[str, str]] = None,
                          substitutions_sysname: Optional[Dict[str, str]] = None) -> CaseInsensitiveDict:
    """
    Reconcile Voicebox's profile list against what the CSV needs and what
    voices/ can provide, composing and importing any missing-but-available
    profiles before generation starts.

    Convenience wrapper around sync_profiles(sync_all=False).

    Args:
        substitutions: NPC name -> voice profile mappings.
        substitutions_gender: NPC name|gender -> voice profile mappings.
        substitutions_sysname: sysname -> voice profile mappings.

    Returns:
        Updated profile name -> ID map from Voicebox.
    """
    return sync_profiles(
        substitutions=substitutions,
        substitutions_gender=substitutions_gender,
        substitutions_sysname=substitutions_sysname,
        sync_all=False,
    )


# ============================================================================
# Generation Memory
# ============================================================================

def load_generation_memory(memory_path: str) -> dict:
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
        memory_path: Path to the JSON memory file.

    Returns:
        The loaded memory dictionary, or an empty dict if the file
        doesn't exist or is corrupted.
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


def save_generation_memory(memory: dict, memory_path: str) -> None:
    """
    Save the generation history to a JSON file.

    Writes the memory dictionary to disk with pretty-printing (4-space indent)
    for human readability. The file is overwritten completely on each save.

    Args:
        memory: The generation memory dictionary to save.
        memory_path: Path where the JSON file should be written.

    Raises:
        OSError: If the file cannot be written due to permissions or
            filesystem errors.
        TypeError: If the memory contains data that is not JSON-serializable.
    """
    with open(memory_path, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=4, ensure_ascii=False)


def is_already_generated(memory: dict, npc_name: str, strref: str) -> bool:
    """
    Check if a specific voice line has already been generated.

    Args:
        memory: The generation memory dictionary.
        npc_name: The name of the NPC.
        strref: The STRREF identifier for the voice line.

    Returns:
        True if the combination exists in memory, False otherwise.
    """
    return str(strref) in memory.get(npc_name, {})


def mark_as_generated(memory: dict, npc_name: str, strref: str) -> None:
    """
    Record a successfully generated voice line in memory.

    Updates the generation memory to mark a specific NPC/STRREF
    combination as completed. Creates the necessary nested dictionary
    structure if it doesn't already exist.

    Args:
        memory: The generation memory dictionary (modified in place).
        npc_name: The name of the NPC.
        strref: The STRREF identifier for the voice line.
    """
    voice_memory = memory.setdefault(npc_name, {})
    voice_memory[str(strref)] = True


# ============================================================================
# TTS API Client
# ============================================================================

def submit_generation(profile_id: str, text: str) -> str:
    """
    Submit a text-to-speech generation request to the Voicebox API.

    Args:
        profile_id: The numeric ID of the voice profile to use.
        text: The text content to convert to speech.
        engine: The TTS engine to use (e.g., "qwen").
        model_size: The model size (e.g., "0.6B", "1.5B").

    Returns:
        The generation ID assigned by the Voicebox server.

    Raises:
        requests.exceptions.RequestException: If the API request fails.
        RuntimeError: If the response does not contain an "id" field.
    """
    # tts_voicebox.submit_generation uses cfg.ENGINE and cfg.MODEL_SIZE from config
    return tts_submit_generation(text, profile_id)


def wait_for_completion(gen_id: str) -> Optional[dict]:
    """
    Wait for a generation job to complete by streaming Server-Sent Events.

    Connects to the Voicebox API's status endpoint and listens for SSE events
    until the generation reaches either "completed" or "failed" status.

    Args:
        gen_id: The generation ID returned by submit_generation().

    Returns:
        The final event data containing at least "status" and
        potentially "duration" (for completed jobs) or "error"
        (for failed jobs).

    Raises:
        requests.exceptions.RequestException: If the SSE connection fails.
    """
    return tts_wait_for_completion(gen_id)


def cancel_generation(gen_id: str) -> Tuple[bool, str]:
    """
    Cancel a queued or running generation on the Voicebox server.

    Sends a POST request to the /generate/{generation_id}/cancel endpoint.

    Args:
        gen_id: The generation ID to cancel.

    Returns:
        A tuple containing:
            - success: True if cancellation was successful.
            - message: Status message describing the result.
    """
    try:
        return tts_cancel_generation(gen_id)
    except Exception as e:
        return False, f"Cancellation error: {e}"


def download_audio(gen_id: str, output_path: str) -> None:
    """
    Download the generated audio file from the Voicebox API.

    Retrieves the audio for a completed generation job and saves it to
    the specified output path. The audio is downloaded as raw WAV data.

    Args:
        gen_id: The generation ID to download.
        output_path: Filesystem path where the audio should be saved.

    Raises:
        requests.exceptions.RequestException: If the download request fails.
        OSError: If the output file cannot be written.
    """
    tts_download_generated_audio(gen_id, output_path)


# ============================================================================
# Text Processing
# ============================================================================


# ============================================================================
# CSV Processing
# ============================================================================

def is_valid_text(text: str) -> bool:
    """
    Check if text contains meaningful content for TTS generation.

    Valid text must be non-empty and contain at least one alphanumeric
    character (letter or digit). This filters out:
        - Empty strings or strings with only whitespace
        - Strings with only punctuation (e.g., "?", "...", "!!")
        - Strings with only special characters

    Args:
        text: The text to validate.

    Returns:
        True if text contains at least one alphanumeric character, False otherwise.
    """
    return bool(text and any(char.isalnum() for char in text))


def scan_csv_for_npc_targets() -> dict:
    """
    Scan CSV with ALL existing filters applied and build NPC target candidates.

    Reads configuration directly from cfg.* at runtime:
    - CSV filtering (STRREF filter, filename prefix, TARGET_VOICES)
    - SKIP_ALREADY_GENERATED and generation memory
    - Voice profile availability (Voicebox and voices/ directory)

    Returns:
        A dictionary keyed by NPC display name, where each value contains:
            display_name: str
            voice_name: str
            status: "on_voicebox" | "importable" | "missing"
            lines_count: int
            chars_count: int
            in_target_voices: bool
    """
    skip_already_generated = cfg.SKIP_ALREADY_GENERATED
    memory_path = cfg.GENERATION_MEMORY_PATH
    patcher_config_path = cfg.PATCHER_CONFIG_PATH
    voices_dir = cfg.VOICES_DIR

    substitutions, substitutions_gender, substitutions_sysname = load_voice_substitutions_all()
    profile_map, _ = get_all_profiles()
    available_voices = scan_available_voice_dirs(voices_dir)

    generation_memory = {}
    if skip_already_generated:
        generation_memory = load_generation_memory(memory_path)

    patcher_config = None
    try:
        patcher_config = load_patcher_config(patcher_config_path)
    except Exception:
        pass

    npc_data = {}

    for strref, sysname, npc_name, gender, csv_filename, text in iter_filtered_csv_rows(scanning_npc_list=True):
        if patcher_config:
            text = preprocess_text(text, patcher_config)

        if not is_valid_text(text):
            continue

        voice_name = get_voice_profile_name(
            npc_name, gender, profile_map, sysname,
            substitutions, substitutions_gender, substitutions_sysname
        )

        if npc_name:
            display_name = npc_name
        elif sysname:
            display_name = f"Unknown ({sysname})"
        else:
            display_name = "Description"

        if skip_already_generated and is_already_generated(generation_memory, display_name, strref):
            continue

        if display_name not in npc_data:
            if voice_name and voice_name in profile_map:
                status = "on_voicebox"
            elif (voice_name and voice_name in available_voices) or display_name in available_voices:
                status = "importable"
            else:
                status = "missing"

            npc_data[display_name] = {
                "display_name": display_name,
                "voice_name": voice_name or "None",
                "status": status,
                "lines_count": 0,
                "chars_count": 0,
                "in_target_voices": display_name in cfg.TARGET_VOICES,
            }

        npc_data[display_name]["lines_count"] += 1
        npc_data[display_name]["chars_count"] += len(text)

    return npc_data


def filter_and_sort_rows(selected_rows: list, profile_map: CaseInsensitiveDict) -> list:
    """
    Filter out rows with missing voice profiles and sort for optimal processing.

    Removes rows where the voice profile doesn't exist on the server, then sorts
    by voice name to group similar voices together for better caching.

    Args:
        selected_rows: List of (strref, display_name, voice_name, filename, text).
        profile_map: Map of profile names to IDs.

    Returns:
        Filtered and sorted rows.
    """
    valid_rows = [row for row in selected_rows if row[2] in profile_map]
    valid_rows.sort(key=lambda row: (row[2].lower(), row[1]))
    return valid_rows


def load_strref_filter() -> Set[str]:
    """
    Load the STRREF filter list from cfg.STRREF_FILTER_FILE.

    Returns:
        Set of strref strings to process, or empty set if the filter
        couldn't be loaded (in which case a warning is logged).
    """
    filter_file = cfg.STRREF_FILTER_FILE
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


def iter_filtered_csv_rows(scanning_npc_list: bool = False) -> Iterator[Tuple[str, str, str, str, str, str]]:
    """
    Parse the dialog CSV and yield rows that pass the shared row-level filters.

    Filters applied, in order:
        - Row must have at least 8 columns.
        - If cfg.USE_STRREF_FILTER and strref_filter is non-empty: strref must be in it.
        - If not cfg.USE_STRREF_FILTER: sysname must be non-empty.
        - If not cfg.USE_STRREF_FILTER and cfg.TARGET_VOICES given and
          scanning_npc_list is False: npc_name must be in target_voices.
        - If cfg.FILENAME_PREFIX and csv_filename are both present: csv_filename must start with it.
        - text must be non-empty.

    Args:
        scanning_npc_list: Whether to restrict rows to cfg.TARGET_VOICES.
            Defaults to False (the normal generation scope). Pass True for
            callers that need to see every NPC regardless of the current
            TARGET_VOICES selection.

    Yields:
        Tuples of (strref, sysname, npc_name, gender, csv_filename, text)
    """
    csv_path = cfg.CSV_PATH
    if not os.path.exists(csv_path):
        return

    strref_filter = set()
    if cfg.USE_STRREF_FILTER:
        strref_filter = load_strref_filter()

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

            if cfg.USE_STRREF_FILTER and strref_filter:
                if strref not in strref_filter:
                    continue
            if not cfg.USE_STRREF_FILTER and not sysname:
                continue
            if not scanning_npc_list:
                if not cfg.USE_STRREF_FILTER and cfg.TARGET_VOICES and npc_name not in cfg.TARGET_VOICES:
                    continue
            if cfg.FILENAME_PREFIX and csv_filename and not csv_filename.startswith(cfg.FILENAME_PREFIX):
                continue
            if not is_valid_text(text):
                continue

            yield strref, sysname, npc_name, gender, csv_filename, text


def load_and_filter_csv(patcher_config: Optional[dict], generation_memory: dict,
                        profile_map: Optional[CaseInsensitiveDict] = None,
                        substitutions: Optional[Dict[str, str]] = None,
                        substitutions_gender: Optional[Dict[str, str]] = None,
                        substitutions_sysname: Optional[Dict[str, str]] = None) -> Tuple[List[tuple], dict]:
    """
    Load CSV data, apply filters, and prepare rows for generation.

    Statistics are grouped by voice profile + NPC name combination,
    so we can see how each NPC uses each voice profile.

    Args:
        patcher_config: Patcher configuration for text preprocessing.
        generation_memory: Generation memory for skip checking.
        profile_map: Map of available voice profiles for validation.
        substitutions: NPC name -> voice profile mappings.
        substitutions_gender: NPC name|gender -> voice profile mappings.
        substitutions_sysname: sysname -> voice profile mappings.

    Returns:
        A tuple containing:
            - selected_rows: List of (strref, display_name, voice_name, filename, text)
            - voice_stats: Statistics per voice profile + NPC combination
    """
    selected_rows = []
    voice_stats = {}

    for strref, sysname, npc_name, gender, csv_filename, text in iter_filtered_csv_rows():
        if cfg.FORCE_GENERATED_FILENAMES:
            filename = generate_resref(strref, cfg.FILENAME_PREFIX)
        else:
            filename = csv_filename if csv_filename and csv_filename.strip() else generate_resref(strref, cfg.FILENAME_PREFIX)

        voice_name = get_voice_profile_name(
            npc_name, gender, profile_map, sysname,
            substitutions, substitutions_gender, substitutions_sysname
        )

        if npc_name:
            display_name = npc_name
        elif sysname:
            display_name = f"Unknown ({sysname})"
        else:
            display_name = "Description"

        text = preprocess_text(text, patcher_config) if patcher_config else text

        if not is_valid_text(text):
            continue

        key = f"{voice_name}|{display_name}" if voice_name else f"None|{display_name}"

        if key not in voice_stats:
            voice_stats[key] = {
                "voice_name": voice_name,
                "display_name": display_name,
                "total": 0,
                "done": 0,
                "skipped": 0,
                "to_generate": 0,
                "chars": {
                    "total": 0,
                    "done": 0,
                    "skipped": 0,
                    "to_generate": 0,
                },
            }

        voice_stats[key]["total"] += 1
        voice_stats[key]["chars"]["total"] += len(text)

        if voice_name is None:
            voice_stats[key]["skipped"] += 1
            voice_stats[key]["chars"]["skipped"] += len(text)
            continue

        if profile_map is not None and voice_name not in profile_map:
            voice_stats[key]["skipped"] += 1
            voice_stats[key]["chars"]["skipped"] += len(text)
            continue

        if cfg.SKIP_ALREADY_GENERATED and is_already_generated(generation_memory, display_name, strref):
            voice_stats[key]["done"] += 1
            voice_stats[key]["chars"]["done"] += len(text)
            continue

        voice_stats[key]["to_generate"] += 1
        voice_stats[key]["chars"]["to_generate"] += len(text)

        selected_rows.append((strref, display_name, voice_name, filename, text))

        if cfg.LIMIT and len(selected_rows) >= cfg.LIMIT:
            break

    return selected_rows, voice_stats


def estimate_generation_time(regressor: Regression, chars: int) -> float:
    """
    Estimate generation time from historical data or fallback.

    Uses linear regression from previous jobs to predict time for the current
    text length. Falls back to a conservative estimate if insufficient data.

    Args:
        regressor: Regression object with historical (chars, time) data.
        chars: Number of characters in the current text.

    Returns:
        Estimated time in seconds.
    """
    if len(regressor) > 1:
        estimated_sec = regressor.slope() * chars + regressor.intercept()
        return max(estimated_sec, 2.0)
    return 10.0


# ============================================================================
# Accessibility of VoiceBox backend
# ============================================================================

class HealthCheckWorker(QObject):
    """
    Periodically polls the Voicebox /health endpoint on a background thread.

    Runs its own QTimer (created after moveToThread, so it lives on the
    worker thread) and emits health_checked(reachable, info) on every tick,
    keeping HTTP requests off the UI thread entirely.
    """
    health_checked = Signal(bool, dict)

    def __init__(self, interval_ms: int = 10000):
        super().__init__()
        self.interval_ms = interval_ms
        self._timer: Optional[QTimer] = None

    def start(self) -> None:
        """Create and start the polling QTimer. Call only after moveToThread."""
        self._timer = QTimer()
        self._timer.timeout.connect(self.check_now)
        self._timer.start(self.interval_ms)
        self.check_now()

    @Slot()
    def stop(self) -> None:
        """Stop the polling QTimer. Must be called on the worker thread."""
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

    def check_now(self) -> None:
        """Perform a single health check immediately."""
        success, payload = tts_check_health()
        self.health_checked.emit(success, payload)


# ============================================================================
# Profile fetcher
# ============================================================================

class ProfilesFetchWorker(QObject):
    """
    One-shot background fetch of voice profile names, used to populate
    the fallback-voice comboboxes without blocking the UI thread.
    """
    profiles_loaded = Signal(dict)
    failed = Signal(str)

    def fetch(self) -> None:
        try:
            profile_map, _ = get_all_profiles()
            self.profiles_loaded.emit(dict(profile_map))
        except Exception as e:
            self.failed.emit(str(e))


# ============================================================================
# Profile Sync Worker (runs on a background QThread)
# ============================================================================

class ProfileSyncWorker(QObject):
    """
    Runs full voice profile synchronization with Voicebox on a background thread.

    Signals:
        stage(str): Short status message for the status bar.
        finished(): Emitted when synchronization completes successfully.
        failed(str): Emitted with an error message if synchronization fails.
    """
    stage = Signal(str)
    finished = Signal()
    failed = Signal(str)

    def run(self) -> None:
        """
        Execute full voice profile synchronization on the background thread.

        Scans the local voices directory, compares profiles against Voicebox,
        rebuilds any zero-sample profiles, and creates missing profiles. Emits
        stage updates, finished on success, or failed with an error message.
        """
        try:
            self.stage.emit("Syncing all voice profiles to Voicebox...")
            sync_profiles(sync_all=True)
            self.stage.emit("Voice profile sync complete.")
            self.finished.emit()
        except Exception as e:
            logger.error(f"❌ Failed to sync profiles: {e}")
            self.failed.emit(str(e))


class _NumericTableWidgetItem(QTableWidgetItem):
    """
    QTableWidgetItem that sorts numerically instead of lexicographically.

    QTableWidgetItem's default __lt__ compares displayed text as strings, so
    a comma-formatted "1,234" vs "890" (or even plain "9" vs "10") sorts
    wrong. The real numeric value is stashed in UserRole + 1 (UserRole
    itself is used elsewhere on other columns to hold the row's NPC data,
    so this uses a role one past it to avoid colliding with that).
    """
    def __lt__(self, other):
        self_val = self.data(Qt.ItemDataRole.UserRole + 1)
        other_val = other.data(Qt.ItemDataRole.UserRole + 1) if isinstance(other, QTableWidgetItem) else None
        if self_val is not None and other_val is not None:
            return self_val < other_val
        return super().__lt__(other)


# ============================================================================
# NPC Targets Scan Worker (runs on a background QThread)
# ============================================================================

class NPCTargetsScanWorker(QObject):
    """
    Scan CSV and build NPC target list on background thread.

    Reads all configuration directly from cfg.* - no parameters needed
    to avoid stale state issues.
    """
    progress = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self):
        super().__init__()

    def run(self) -> None:
        """
        Execute NPC target scan.

        All cfg values are read at runtime inside scan_csv_for_npc_targets()
        and its helper functions, ensuring we always use current config state.
        """
        try:
            self.progress.emit("Scanning CSV for NPCs...")
            npc_data = scan_csv_for_npc_targets()
            self.progress.emit(f"Found {len(npc_data)} NPCs")
            self.finished.emit(npc_data)
        except Exception as e:
            logger.error(f"Failed to scan NPCs: {e}")
            self.failed.emit(str(e))


# ============================================================================
# Generation Worker (runs on a background QThread)
# ============================================================================

class GenerationWorker(QObject):
    """
    Runs the full generation pipeline on a background thread.

    This worker encapsulates the entire generation workflow from the console
    version (generate.py) and emits progress signals to update the GUI
    without blocking the UI thread.

    The worker handles:
        - Loading voice substitutions from JSON
        - Syncing profiles with Voicebox (auto-provisioning)
        - Loading patcher configuration
        - Loading generation memory
        - Parsing and filtering the CSV
        - Processing each generation job with retry support
        - Reporting real-time progress via signals

    Signals:
        stage(str): Short status message for the status bar.
        job_progress(dict): Live progress for the current generation job.
        overall_progress(dict): Live progress across all jobs.
        finished(dict): Final run statistics on normal completion.
        failed(str): Fatal error message (instead of finished).
    """
    stage = Signal(str)
    job_progress = Signal(dict)
    overall_progress = Signal(dict)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._stop_requested = threading.Event()
        self._current_gen_id: Optional[str] = None

    def request_stop(self) -> None:
        """
        Ask the worker to stop after the current job.

        Attempts to cancel any in-flight generation for a snappier response.
        The worker will finish the current job then halt before starting
        the next one.
        """
        self._stop_requested.set()
        if self._current_gen_id:
            try:
                cancel_generation(self._current_gen_id)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Progress reporting
    # ------------------------------------------------------------------

    def _job_progress_ticker(self, stop_event: threading.Event, job_idx: int, total_jobs: int,
                             filename: str, strref: str, estimated_sec: float,
                             timeout_sec: Optional[float], npc_name: str, voice_name: str,
                             chars: int) -> None:
        """
        Background thread that emits job_progress every 0.5s while a single
        generation is in flight, driving the job QProgressBar in the UI.

        Args:
            stop_event: Event to signal when the job completes.
            job_idx: Current job index (1-based).
            total_jobs: Total number of jobs.
            filename: The filename being generated.
            strref: STRREF identifier.
            estimated_sec: Estimated duration for this job in seconds.
            timeout_sec: Maximum allowed duration for this job in seconds.
            npc_name: NPC name being processed.
            voice_name: Voice profile name used.
            chars: Number of characters in the text.
        """
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

    def _compute_overall_progress(self, total_chars_processed: int, total_chars_all: int,
                                  total_jobs: int, idx: int, overall_regressor: Regression,
                                  avg_time_per_char: Optional[float], elapsed_total: float) -> dict:
        """
        Compute the numbers for the overall progress bar and label.

        Args:
            total_chars_processed: Characters processed so far.
            total_chars_all: Total characters to process.
            total_jobs: Total number of jobs.
            idx: Number of jobs already completed (0-based).
            overall_regressor: Regression for overall time estimation.
            avg_time_per_char: Running average seconds per character.
            elapsed_total: Total elapsed time in seconds.

        Returns:
            Overall progress data with "ready" flag, or {"ready": False}
            if insufficient data.
        """
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
    # Job execution
    # ------------------------------------------------------------------

    def _timeout_monitor(self, stop_event: threading.Event, gen_id: str,
                         timeout_sec: float, start_time: float) -> None:
        """
        Monitor thread that checks for timeout and cancels the generation.

        Args:
            stop_event: Event to signal when generation completes.
            gen_id: The generation ID to cancel.
            timeout_sec: Timeout in seconds.
            start_time: Timestamp when the job started.
        """
        while not stop_event.is_set():
            if time.time() - start_time > timeout_sec:
                cancel_generation(gen_id)
                break
            time.sleep(1.0)

    def process_generation_job(self, idx: int, total_jobs: int, strref: str,
                               npc_name: str, voice_name: str, filename: str, text: str,
                               profile_id: str, regressor: Regression,
                               generation_memory: dict, retry_count: int = 0,
                               retry_delay: float = 0.0) -> Tuple[bool, float, float, int, int]:
        """
        Execute a single TTS generation job with optional retry on failure.

        Handles the complete lifecycle of one generation:
            1. Estimates time based on historical data
            2. Starts a progress ticker thread (emits job_progress every 0.5s)
            3. Submits the generation request to the Voicebox API
            4. Starts a timeout monitor thread (if enabled)
            5. Waits for completion via SSE streaming
            6. Emits status updates for post-processing phases:
               - "Downloading..." during audio download
               - "Converting to OGG..." during ffmpeg conversion
               - "Saving memory..." during memory save
            7. Downloads and converts the audio
            8. Records success in generation memory
            9. Returns results for statistics tracking

        If retry_count > 0, failed generations are retried up to retry_count
        times with a delay of retry_delay seconds between attempts.

        Args:
            idx: Current job index (1-based).
            total_jobs: Total number of jobs.
            strref: STRREF identifier.
            npc_name: NPC name (for memory and folder).
            voice_name: Voice profile name.
            filename: Output filename base.
            text: Preprocessed text to generate.
            profile_id: Voice profile ID.
            regressor: Regression for time estimation.
            generation_memory: Generation memory dictionary.
            retry_count: Number of retry attempts on failure.
            retry_delay: Delay in seconds between retries.

        Returns:
            A tuple containing:
                - success: True if generation succeeded.
                - elapsed_time: Time taken for generation in seconds.
                - audio_duration: Duration of generated audio.
                - chars_processed: Number of characters in the text.
                - retry_attempts: Number of retries made (0 for first success).
        """
        chars = len(text)
        estimated_sec = estimate_generation_time(regressor, chars)

        max_attempts = retry_count + 1
        attempt = 0
        retry_attempts = 0
        last_elapsed = 0
        last_audio_duration = 0

        while attempt < max_attempts:
            if self._stop_requested.is_set():
                logger.info(f"⏹ Stop requested - aborting retries for {filename}")
                break

            attempt += 1

            if attempt > 1:
                logger.info(f"🔄 Retry {attempt - 1}/{retry_count} for {filename} (STRREF: {strref})")
                time.sleep(retry_delay)

            stop_event = threading.Event()
            cancel_event = threading.Event()

            timeout_sec = None
            if cfg.ENABLE_TIMEOUT_SAFEGUARD:
                timeout_sec = cfg.TIMEOUT_MAX_SECONDS
                if len(regressor) >= cfg.TIMEOUT_MIN_ESTIMATES:
                    timeout_sec = min(timeout_sec, estimated_sec * cfg.TIMEOUT_MULTIPLIER)

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
                gen_id = submit_generation(profile_id, text)
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

                self.job_progress.emit({
                    "idx": idx, "total": total_jobs, "strref": strref, "filename": filename,
                    "npc_name": npc_name, "voice_name": voice_name, "chars": chars,
                    "percent": 100, "elapsed": elapsed, "estimated": estimated_sec,
                    "timeout": timeout_sec, "status": "Downloading..."
                })

                stop_event.set()
                ticker.join(timeout=1.0)
                self._current_gen_id = None

                if final_event and final_event.get("status") == "completed":
                    audio_duration = final_event.get("duration", 0.0)

                    safe_npc = sanitize_filename(npc_name)
                    npc_output_dir = os.path.join(cfg.OUTPUT_DIR, safe_npc)
                    os.makedirs(npc_output_dir, exist_ok=True)
                    output_path = os.path.join(npc_output_dir, f"{filename}.wav")

                    temp_path = output_path + ".tmp"
                    try:
                        download_audio(gen_id, temp_path)

                        if cfg.CONVERT_TO_OGG:
                            self.job_progress.emit({
                                "idx": idx, "total": total_jobs, "strref": strref, "filename": filename,
                                "npc_name": npc_name, "voice_name": voice_name, "chars": chars,
                                "percent": 100, "elapsed": elapsed, "estimated": estimated_sec,
                                "timeout": timeout_sec, "status": "Converting to OGG..."
                            })
                            if not convert_to_ogg(temp_path, output_path, cfg.OGG_QUALITY):
                                raise RuntimeError(f"OGG conversion failed for {filename}")
                            os.remove(temp_path)
                        else:
                            os.rename(temp_path, output_path)

                        self.job_progress.emit({
                            "idx": idx, "total": total_jobs, "strref": strref, "filename": filename,
                            "npc_name": npc_name, "voice_name": voice_name, "chars": chars,
                            "percent": 100, "elapsed": elapsed, "estimated": estimated_sec,
                            "timeout": timeout_sec, "status": "Saving memory..."
                        })

                    except Exception as e:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        raise e

                    mark_as_generated(generation_memory, npc_name, strref)
                    save_generation_memory(generation_memory, cfg.GENERATION_MEMORY_PATH)

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
                logger.info(f"⏹ Stop requested after attempt {attempt} for {filename}")
                break

        return False, last_elapsed, last_audio_duration, chars, retry_attempts

    def process_generation_jobs_all(self, profile_map: dict, generation_memory: dict,
                                    selected_rows: list, total_jobs: int,
                                    total_chars_all: int) -> Tuple[int, Optional[float], dict, int]:
        """
        Process all generation jobs in the selected rows with real-time progress tracking.

        Iterates through each row in selected_rows, submitting each to the TTS API
        with optional retry support. Maintains real-time performance statistics
        (regression-based time estimation) and tracks retry/error statistics for
        reporting in the final summary.

        Args:
            profile_map: Voice profile name -> ID mapping.
            generation_memory: Generation memory dictionary.
            selected_rows: List of (strref, display_name, voice_name, filename, text).
            total_jobs: Total number of jobs.
            total_chars_all: Total character count across all jobs.

        Returns:
            A tuple containing:
                - total_chars_processed: Total characters successfully generated.
                - avg_time_per_char: Running average seconds per character.
                - retry_stats: Statistics tracking retry behavior.
                - successful_jobs: Number of successfully generated files.
        """
        total_chars_processed = 0
        total_start_time = time.time()
        avg_time_per_char = None
        overall_regressor = Regression()
        regressor = Regression()

        successful_jobs = 0

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
                profile_id, regressor, generation_memory, cfg.RETRY_COUNT, cfg.RETRY_DELAY
            )

            retry_stats["failed_attempts"] += retry_attempts

            if success:
                successful_jobs += 1
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

        return total_chars_processed, avg_time_per_char, retry_stats, successful_jobs

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Execute the complete generation pipeline on the background thread.

        This is the GUI equivalent of generate.py's main() function. It:
            1. Logs the start banner
            2. Loads voice substitutions from voice-substitutions.json
            3. Syncs profiles with Voicebox (auto-provisioning)
            4. Loads patcher configuration
            5. Loads generation memory (generation-memory.json)
            6. Reads and filters the CSV
            7. Displays the pre-generation summary
            8. Processes all generation jobs with progress reporting
            9. Displays the final summary

        Signals emitted:
            - stage(str): For each major step
            - job_progress(dict): Every 0.5s during a job
            - overall_progress(dict): Before each job
            - finished(dict): On successful completion
            - failed(str): On fatal error
        """
        try:
            log_header_start()

            self.stage.emit("Loading voice substitutions...")
            substitutions, substitutions_gender, substitutions_sysname = load_voice_substitutions_all()

            self.stage.emit("Syncing voice profiles...")
            try:
                profile_map = sync_missing_profiles(
                    substitutions, substitutions_gender, substitutions_sysname
                )
                logger.info(f"Loaded {len(profile_map)} voice profiles.")
            except Exception as e:
                logger.error(f"❌ Failed to fetch/sync profiles: {e}")
                self.failed.emit(str(e))
                return

            self.stage.emit("Loading patcher configuration...")
            try:
                patcher_config = load_patcher_config(cfg.PATCHER_CONFIG_PATH)
                logger.info("Loaded patcher config.")
            except Exception as e:
                patcher_config = None
                logger.warning(f"⚠️ Could not load patcher config: {e}")

            generation_memory = load_generation_memory(cfg.GENERATION_MEMORY_PATH)
            if cfg.SKIP_ALREADY_GENERATED:
                logger.info("Already generated files will be skipped.")
            else:
                logger.info("Skipping already generated files is disabled.")

            self.stage.emit("Reading and filtering dialog CSV...")
            try:
                selected_rows, voice_stats = load_and_filter_csv(
                    patcher_config, generation_memory, profile_map,
                    substitutions, substitutions_gender, substitutions_sysname
                )
            except Exception as e:
                self.failed.emit(str(e))
                return

            selected_rows = filter_and_sort_rows(selected_rows, profile_map)
            total_jobs = len(selected_rows)
            total_chars_all = sum(len(text) for *_, text in selected_rows)

            filename_mode = "FORCED generated (base36)" if cfg.FORCE_GENERATED_FILENAMES else "CSV with base36 fallback"
            logger.info(f"Selected {total_jobs} rows. Total characters: {total_chars_all}")
            logger.info(f"Filename mode: {filename_mode}")
            log_header_summary(total_jobs, total_chars_all)

            if total_jobs == 0:
                logger.info("No jobs to process. Exiting.")
                self.finished.emit({
                    "total_jobs": 0, "total_chars_processed": 0,
                    "avg_time_per_char": None, "npc_stats": voice_stats, "retry_stats": None,
                })
                return

            log_pregeneration_summary(voice_stats, profile_map)

            self.stage.emit(f"Generating {total_jobs} lines...")
            total_chars_processed, avg_time_per_char, retry_stats, successful_jobs = self.process_generation_jobs_all(
                profile_map, generation_memory, selected_rows, total_jobs, total_chars_all
            )

            was_stopped = self._stop_requested.is_set()

            log_final_summary(
                total_jobs,
                total_chars_processed,
                avg_time_per_char,
                voice_stats,
                retry_stats,
                was_stopped,
                successful_jobs
            )

            self.stage.emit("Stopped." if was_stopped else "Done.")
            self.finished.emit({
                "total_jobs": total_jobs,
                "total_chars_processed": total_chars_processed,
                "avg_time_per_char": avg_time_per_char,
                "npc_stats": voice_stats,
                "retry_stats": retry_stats,
                "was_stopped": was_stopped,
            })

        except Exception as e:
            logger.error(f"❌ Unexpected error: {e}")
            self.failed.emit(str(e))


# ============================================================================
# NPC Targets Picker + Configuration Dialog
# ============================================================================

class NPCTargetsDialog(QDialog):
    """
    Standalone dialog for picking which NPCs to generate for (TARGET_VOICES)
    and for toggling "Skip already generated" (SKIP_ALREADY_GENERATED).

    These two settings are deliberately kept together: the NPC list's line
    counts and "already generated" status depend on SKIP_ALREADY_GENERATED,
    so toggling it needs to be reflected in the list immediately. To make
    that work, SKIP_ALREADY_GENERATED is written to appconfig the instant
    its checkbox is toggled (live, like every other cfg.* value in this
    app - no parameter passing), and the list is rescanned right away
    against the new value.

    TARGET_VOICES itself is NOT live - it's only written when the user
    clicks "Save & Close". Selecting NPCs is naturally a batch edit
    (check/uncheck many rows, then commit), so only SKIP_ALREADY_GENERATED
    needed the live treatment here.

    The NPC list loads automatically when this dialog opens, and again
    whenever SKIP_ALREADY_GENERATED changes - there is no manual refresh
    button.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NPC Targets")
        self.resize(800, 600)
        self._npc_data_loaded = False
        self._raw_npc_data = {}
        self.npc_scan_thread: Optional[QThread] = None
        self.npc_scan_worker: Optional[NPCTargetsScanWorker] = None
        self._build_ui()
        self._refresh_npc_targets()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.skip_generated_check = QCheckBox("Skip already generated")
        self.skip_generated_check.setChecked(cfg.SKIP_ALREADY_GENERATED)
        self.skip_generated_check.setToolTip(
            "Saved immediately when toggled, and rescans the list below "
            "right away to reflect it - no need to click Save & Close."
        )
        self.skip_generated_check.toggled.connect(self._on_skip_generated_toggled)
        layout.addWidget(self.skip_generated_check)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        filter_layout = QHBoxLayout()

        search_label = QLabel("🔍 Search:")
        self.npc_search_edit = QLineEdit()
        self.npc_search_edit.setPlaceholderText("Filter by NPC name...")
        self.npc_search_edit.textChanged.connect(self._filter_npc_table)

        clear_search_btn = QPushButton("Clear")
        clear_search_btn.clicked.connect(lambda: self.npc_search_edit.clear())

        status_label = QLabel("Status:")
        self.npc_status_filter = QComboBox()
        self.npc_status_filter.addItems(["All", "✅ On Voicebox", "📁 Importable", "❌ Missing"])
        self.npc_status_filter.currentTextChanged.connect(self._filter_npc_table)

        self.npc_only_selected_check = QCheckBox("Only selected")
        self.npc_only_selected_check.setToolTip("Show only NPCs that are currently checked")
        self.npc_only_selected_check.toggled.connect(self._filter_npc_table)

        sort_label = QLabel("Sort:")
        self.npc_sort_combo = QComboBox()
        self.npc_sort_combo.addItems(["Name (A-Z)", "Lines (High-Low)", "Lines (Low-High)"])
        self.npc_sort_combo.currentTextChanged.connect(self._sort_npc_table)

        filter_layout.addWidget(search_label)
        filter_layout.addWidget(self.npc_search_edit, stretch=1)
        filter_layout.addWidget(clear_search_btn)
        filter_layout.addWidget(status_label)
        filter_layout.addWidget(self.npc_status_filter)
        filter_layout.addWidget(self.npc_only_selected_check)
        filter_layout.addWidget(sort_label)
        filter_layout.addWidget(self.npc_sort_combo)

        layout.addLayout(filter_layout)

        self.npc_targets_table = QTableWidget()
        self.npc_targets_table.setColumnCount(4)
        self.npc_targets_table.setHorizontalHeaderLabels(["☑", "NPC Name", "Status", "Lines"])
        self.npc_targets_table.horizontalHeader().setStretchLastSection(False)
        self.npc_targets_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.npc_targets_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.npc_targets_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.npc_targets_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.npc_targets_table.setColumnWidth(0, 40)
        self.npc_targets_table.setColumnWidth(2, 150)
        self.npc_targets_table.setColumnWidth(3, 100)
        self.npc_targets_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        layout.addWidget(self.npc_targets_table, stretch=1)

        buttons_layout = QHBoxLayout()

        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(lambda: self._set_all_npc_checkboxes(True))

        self.select_none_btn = QPushButton("Select None")
        self.select_none_btn.clicked.connect(lambda: self._set_all_npc_checkboxes(False))

        self.select_profiles_btn = QPushButton("✅ Only with Profiles")
        self.select_profiles_btn.setToolTip("Select only NPCs with available voice profiles")
        self.select_profiles_btn.clicked.connect(self._select_npcs_with_profiles)

        self.clear_targets_btn = QPushButton("🗑️ Clear All Targets")
        self.clear_targets_btn.setToolTip("Clear TARGET_VOICES to process all NPCs")
        self.clear_targets_btn.clicked.connect(self._clear_all_targets)

        buttons_layout.addWidget(self.select_all_btn)
        buttons_layout.addWidget(self.select_none_btn)
        buttons_layout.addWidget(self.select_profiles_btn)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self.clear_targets_btn)

        layout.addLayout(buttons_layout)

        self.npc_status_label = QLabel("Loading NPC list...")
        self.npc_status_label.setStyleSheet("color: gray; font-style: italic;")
        layout.addWidget(self.npc_status_label)

        dialog_buttons = QHBoxLayout()
        dialog_buttons.addStretch()
        self.cancel_npc_btn = QPushButton("Cancel")
        self.cancel_npc_btn.clicked.connect(self.reject)
        dialog_buttons.addWidget(self.cancel_npc_btn)
        self.save_npc_btn = QPushButton("💾 Save && Close")
        self.save_npc_btn.setDefault(True)
        self.save_npc_btn.clicked.connect(self._save_and_close)
        dialog_buttons.addWidget(self.save_npc_btn)
        layout.addLayout(dialog_buttons)

        self._set_actions_enabled(False)

    # ------------------------------------------------------------------
    def _set_actions_enabled(self, enabled: bool) -> None:
        """Enable/disable everything that depends on a completed scan."""
        for widget in (
            self.select_all_btn, self.select_none_btn, self.select_profiles_btn,
            self.clear_targets_btn, self.save_npc_btn, self.npc_search_edit,
            self.npc_status_filter, self.npc_sort_combo, self.npc_only_selected_check,
        ):
            widget.setEnabled(enabled)

    # ------------------------------------------------------------------
    def _on_skip_generated_toggled(self, checked: bool) -> None:
        """
        Write SKIP_ALREADY_GENERATED to appconfig immediately, then rescan.

        The new value must be persisted *before* the rescan is triggered
        since scan_csv_for_npc_targets() reads cfg.SKIP_ALREADY_GENERATED
        at runtime.
        """
        cfg.SKIP_ALREADY_GENERATED = checked
        self._refresh_npc_targets()

    # ------------------------------------------------------------------
    def _refresh_npc_targets(self) -> None:
        """
        (Re)scan the CSV for NPC targets on a background thread.

        Reads all configuration directly from cfg.* inside the worker.
        """
        self._set_actions_enabled(False)
        self.skip_generated_check.setEnabled(False)
        self.npc_status_label.setText("Scanning CSV...")
        self.npc_status_label.setStyleSheet("color: orange;")

        self.npc_targets_table.setRowCount(0)

        self.npc_scan_thread = QThread()
        self.npc_scan_worker = NPCTargetsScanWorker()
        self.npc_scan_worker.moveToThread(self.npc_scan_thread)

        self.npc_scan_thread.started.connect(self.npc_scan_worker.run)
        self.npc_scan_worker.progress.connect(self._on_npc_scan_progress)
        self.npc_scan_worker.finished.connect(self._on_npc_scan_finished)
        self.npc_scan_worker.failed.connect(self._on_npc_scan_failed)
        self.npc_scan_worker.finished.connect(self.npc_scan_thread.quit)
        self.npc_scan_worker.failed.connect(self.npc_scan_thread.quit)
        self.npc_scan_thread.finished.connect(self._on_npc_scan_thread_finished)

        self.npc_scan_thread.start()

    def _on_npc_scan_progress(self, message: str) -> None:
        """Update the status label with a progress message from the scan worker."""
        self.npc_status_label.setText(message)

    def _on_npc_scan_finished(self, npc_data: dict) -> None:
        """
        Handle a completed scan: populate the table and refresh the status label.

        Args:
            npc_data: NPC data as returned by scan_csv_for_npc_targets(),
                keyed by display name.
        """
        self._raw_npc_data = npc_data
        self._npc_data_loaded = True

        self._populate_npc_table(npc_data)

        selected_count = sum(1 for d in npc_data.values() if d["in_target_voices"])
        selected_lines = sum(d["lines_count"] for d in npc_data.values() if d["in_target_voices"])

        self.npc_status_label.setText(
            f"{selected_count} of {len(npc_data)} selected, {selected_lines:,} lines total"
        )
        self.npc_status_label.setStyleSheet("color: green;")

        self._filter_npc_table()
        self._sort_npc_table()

    def _on_npc_scan_failed(self, error: str) -> None:
        """Show the scan failure in the status label."""
        self.npc_status_label.setText(f"❌ Scan failed: {error}")
        self.npc_status_label.setStyleSheet("color: red;")

    def _on_npc_scan_thread_finished(self) -> None:
        """Re-enable the checkbox and row-dependent actions once the scan thread exits."""
        self.skip_generated_check.setEnabled(True)
        self._set_actions_enabled(self._npc_data_loaded)

    # ------------------------------------------------------------------
    def _populate_npc_table(self, npc_data: dict) -> None:
        """
        Fill the table with one row per NPC, replacing its current contents.

        Args:
            npc_data: NPC data as returned by scan_csv_for_npc_targets(),
                keyed by display name.
        """
        table = self.npc_targets_table

        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(npc_data))

            for row, (npc_name, data) in enumerate(npc_data.items()):
                checkbox = QCheckBox()
                checkbox.setChecked(data["in_target_voices"])
                checkbox.toggled.connect(self._on_npc_checkbox_toggled)
                checkbox_widget = QWidget()
                checkbox_layout = QHBoxLayout(checkbox_widget)
                checkbox_layout.addWidget(checkbox)
                checkbox_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                checkbox_layout.setContentsMargins(0, 0, 0, 0)
                table.setCellWidget(row, 0, checkbox_widget)

                table.setItem(row, 1, QTableWidgetItem(data["display_name"]))

                status_text = {
                    "on_voicebox": "✅ On Voicebox",
                    "importable": "📁 Importable",
                    "missing": "❌ Missing"
                }[data["status"]]
                table.setItem(row, 2, QTableWidgetItem(status_text))

                lines_item = _NumericTableWidgetItem(f"{data['lines_count']:,}")
                lines_item.setData(Qt.ItemDataRole.UserRole + 1, data['lines_count'])
                lines_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, 3, lines_item)

                name_item = table.item(row, 1)
                if name_item is not None:
                    name_item.setData(Qt.ItemDataRole.UserRole, data)
        finally:
            table.setUpdatesEnabled(True)

    # ------------------------------------------------------------------
    def _on_npc_checkbox_toggled(self, _checked: bool) -> None:
        """Handle a per-row checkbox being toggled by the user."""
        if self.npc_only_selected_check.isChecked():
            self._filter_npc_table()
        else:
            self._update_npc_status_label()

    # ------------------------------------------------------------------
    def _filter_npc_table(self) -> None:
        """Apply search, status, and selected-only filters to NPC table."""
        if not self._npc_data_loaded:
            return

        search_text = self.npc_search_edit.text().lower()
        status_filter = self.npc_status_filter.currentText()
        only_selected = self.npc_only_selected_check.isChecked()

        for row in range(self.npc_targets_table.rowCount()):
            show = True

            name_item = self.npc_targets_table.item(row, 1)
            npc_name = name_item.text().lower() if name_item is not None else ""
            if search_text and search_text not in npc_name:
                show = False

            if status_filter != "All":
                status_item = self.npc_targets_table.item(row, 2)
                status_text = status_item.text() if status_item is not None else ""
                if status_filter not in status_text:
                    show = False

            if only_selected and show:
                checkbox_widget = self.npc_targets_table.cellWidget(row, 0)
                checkbox = checkbox_widget.findChild(QCheckBox) if checkbox_widget else None
                if not (checkbox and checkbox.isChecked()):
                    show = False

            self.npc_targets_table.setRowHidden(row, not show)

        self._update_npc_status_label()

    # ------------------------------------------------------------------
    def _sort_npc_table(self) -> None:
        """Sort NPC table based on selected criterion."""
        if not self._npc_data_loaded:
            return

        sort_mode = self.npc_sort_combo.currentText()

        if "Name" in sort_mode:
            self.npc_targets_table.sortItems(1, Qt.SortOrder.AscendingOrder)
        elif "High-Low" in sort_mode:
            self.npc_targets_table.sortItems(3, Qt.SortOrder.DescendingOrder)
        elif "Low-High" in sort_mode:
            self.npc_targets_table.sortItems(3, Qt.SortOrder.AscendingOrder)

    # ------------------------------------------------------------------
    def _set_all_npc_checkboxes(self, checked: bool) -> None:
        """Set all visible NPC checkboxes to checked state."""
        if not self._npc_data_loaded:
            return

        for row in range(self.npc_targets_table.rowCount()):
            if not self.npc_targets_table.isRowHidden(row):
                checkbox_widget = self.npc_targets_table.cellWidget(row, 0)
                if checkbox_widget:
                    checkbox = checkbox_widget.findChild(QCheckBox)
                    if checkbox:
                        checkbox.setChecked(checked)

        self._filter_npc_table()

    # ------------------------------------------------------------------
    def _select_npcs_with_profiles(self) -> None:
        """Select only NPCs with available profiles (on Voicebox or importable)."""
        if not self._npc_data_loaded:
            return

        for row in range(self.npc_targets_table.rowCount()):
            if not self.npc_targets_table.isRowHidden(row):
                status_item = self.npc_targets_table.item(row, 2)
                status_text = status_item.text() if status_item is not None else ""
                has_profile = "✅" in status_text or "📁" in status_text

                checkbox_widget = self.npc_targets_table.cellWidget(row, 0)
                if checkbox_widget:
                    checkbox = checkbox_widget.findChild(QCheckBox)
                    if checkbox:
                        checkbox.setChecked(has_profile)

        self._filter_npc_table()

    # ------------------------------------------------------------------
    def _clear_all_targets(self) -> None:
        """Clear all TARGET_VOICES (uncheck all + set to empty list)."""
        if not self._npc_data_loaded:
            return
        self._set_all_npc_checkboxes(False)

    # ------------------------------------------------------------------
    def _update_npc_status_label(self) -> None:
        """Update status label with current selection stats."""
        if not self._npc_data_loaded:
            return

        selected_count = 0
        selected_lines = 0
        total_count = self.npc_targets_table.rowCount()

        for row in range(total_count):
            checkbox_widget = self.npc_targets_table.cellWidget(row, 0)
            if checkbox_widget:
                checkbox = checkbox_widget.findChild(QCheckBox)
                if checkbox and checkbox.isChecked():
                    selected_count += 1
                    lines_item = self.npc_targets_table.item(row, 3)
                    lines_text = lines_item.text().replace(',', '') if lines_item is not None else "0"
                    selected_lines += int(lines_text)

        self.npc_status_label.setText(
            f"{selected_count} of {total_count} selected, {selected_lines:,} lines total"
        )

    # ------------------------------------------------------------------
    def _save_and_close(self) -> None:
        """Write TARGET_VOICES from the current checkbox selection and close."""
        if not self._npc_data_loaded:
            self.reject()
            return

        selected_npcs = []
        for row in range(self.npc_targets_table.rowCount()):
            checkbox_widget = self.npc_targets_table.cellWidget(row, 0)
            if checkbox_widget:
                checkbox = checkbox_widget.findChild(QCheckBox)
                npc_name_item = self.npc_targets_table.item(row, 1)
                if checkbox and checkbox.isChecked() and npc_name_item:
                    data = npc_name_item.data(Qt.ItemDataRole.UserRole)
                    if data:
                        selected_npcs.append(data["display_name"])

        cfg.TARGET_VOICES = selected_npcs
        self.accept()


class ConfigDialog(QDialog):
    """
    Modal dialog holding every user-editable setting, grouped into tabs:

        Connection      - Voicebox URL/health, engine, model size
        Generation      - retry, timeout safeguard, ogg conversion,
                          job limit, and the button that opens
                          NPCTargetsDialog (which owns TARGET_VOICES and
                          SKIP_ALREADY_GENERATED)
        Voices          - fallback enable/refresh + male/female/neutral
                          combos, plus the forced profile-renewal toggle

    The dialog owns the widgets; GenerateWindow reaches them via
    ``self.config_dialog.<widget_name>`` rather than this class exposing
    bespoke getters for every field.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuration")
        self.setMinimumWidth(480)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        tabs = QTabWidget()
        outer.addWidget(tabs)

        tabs.addTab(self._build_connection_tab(), "🔌 Connection")
        tabs.addTab(self._build_generation_tab(), "⚙️ Generation")
        tabs.addTab(self._build_fallback_tab(), "🗣️ Voices")

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.cancel_config_btn = QPushButton("Cancel")
        self.cancel_config_btn.clicked.connect(self.close)
        button_row.addWidget(self.cancel_config_btn)
        self.save_config_btn = QPushButton("💾 Save Config")
        self.save_config_btn.setDefault(True)
        button_row.addWidget(self.save_config_btn)
        outer.addLayout(button_row)

    # ------------------------------------------------------------------
    def _build_connection_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.base_url_edit = QLineEdit(cfg.BASE_URL)
        self.health_dot = QLabel()
        self.health_dot.setFixedSize(14, 14)
        self.health_dot.setStyleSheet("background-color: gray; border-radius: 7px;")
        self.health_dot.setToolTip("Checking Voicebox API health...")
        form.addRow("<b>Voicebox URL:</b>", self.base_url_edit)

        self.engine_edit = QLineEdit(cfg.ENGINE)
        form.addRow("<b>Engine:</b>", self.engine_edit)

        self.model_size_edit = QLineEdit(cfg.MODEL_SIZE)
        form.addRow("<b>Model size:</b>", self.model_size_edit)

        engine_warning = QLabel(
            "⚠️ Engine/model size are free text - Voicebox's /generate and "
            "/models/status use different naming for the same model. Check "
            "/models/status before changing this."
        )
        engine_warning.setWordWrap(True)
        engine_warning.setStyleSheet("color: #b8860b; font-size: 10px;")
        form.addRow(engine_warning)

        return tab

    # ------------------------------------------------------------------
    def _build_generation_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        retry_box = QHBoxLayout()
        self.retry_count_spin = QSpinBox()
        self.retry_count_spin.setRange(0, 20)
        self.retry_count_spin.setSuffix(" x times")
        self.retry_count_spin.setValue(cfg.RETRY_COUNT)
        self.retry_delay_spin = QDoubleSpinBox()
        self.retry_delay_spin.setRange(0.0, 120.0)
        self.retry_delay_spin.setSingleStep(0.5)
        self.retry_delay_spin.setSuffix(" s delay")
        self.retry_delay_spin.setValue(cfg.RETRY_DELAY)
        retry_box.addWidget(self.retry_count_spin)
        retry_box.addWidget(self.retry_delay_spin)
        form.addRow("<b>Retry:</b>", retry_box)

        self.timeout_enable_check = QCheckBox("Enabled")
        self.timeout_enable_check.setChecked(cfg.ENABLE_TIMEOUT_SAFEGUARD)
        form.addRow("<b>Timeout safeguard:</b>", self.timeout_enable_check)

        timeout_box = QHBoxLayout()
        self.timeout_max_spin = QSpinBox()
        self.timeout_max_spin.setRange(10, 7200)
        self.timeout_max_spin.setSuffix(" s max")
        self.timeout_max_spin.setValue(cfg.TIMEOUT_MAX_SECONDS)
        self.timeout_multiplier_spin = QDoubleSpinBox()
        self.timeout_multiplier_spin.setRange(1.0, 10.0)
        self.timeout_multiplier_spin.setSingleStep(0.5)
        self.timeout_multiplier_spin.setSuffix(" x estimate")
        self.timeout_multiplier_spin.setValue(cfg.TIMEOUT_MULTIPLIER)
        timeout_box.addWidget(self.timeout_max_spin)
        timeout_box.addWidget(self.timeout_multiplier_spin)
        form.addRow("", timeout_box)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        form.addRow(sep1)

        self.convert_ogg_check = QCheckBox("Convert generated audio to Ogg")
        self.convert_ogg_check.setChecked(cfg.CONVERT_TO_OGG)
        form.addRow("<b>Audio:</b>", self.convert_ogg_check)

        ogg_quality_box = QHBoxLayout()
        self.ogg_quality_spin = QSpinBox()
        self.ogg_quality_spin.setRange(-1, 10)
        self.ogg_quality_spin.setValue(cfg.OGG_QUALITY)
        self.ogg_quality_spin.setToolTip(
            "libvorbis -qscale:a. -1 = smallest/worst, 10 = largest/best. "
            "4 is a good default (~128 kbps)."
        )
        ogg_quality_box.addWidget(self.ogg_quality_spin)
        ogg_quality_hint = QLabel("Ogg quality (-1 worst … 10 best)")
        ogg_quality_hint.setStyleSheet("font-size: 10px; color: gray;")
        ogg_quality_box.addWidget(ogg_quality_hint)
        ogg_quality_box.addStretch()
        form.addRow("", ogg_quality_box)
        self.convert_ogg_check.toggled.connect(self.ogg_quality_spin.setEnabled)
        self.ogg_quality_spin.setEnabled(self.convert_ogg_check.isChecked())

        self.npc_targets_btn = QPushButton("🎯 Manage NPC Targets…")
        self.npc_targets_btn.setToolTip(
            "Pick which NPCs to generate for, and toggle 'Skip already "
            "generated' - both live: changes there take effect immediately, "
            "no need to Save Config."
        )
        self.npc_targets_btn.clicked.connect(self._open_npc_targets_dialog)
        form.addRow("<b>NPC Targets:</b>", self.npc_targets_btn)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        form.addRow(sep2)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(0, 1_000_000)
        self.limit_spin.setValue(cfg.LIMIT)
        self.limit_spin.setSpecialValueText("No limit")
        form.addRow("<b>Limit:</b>", self.limit_spin)
        limit_hint = QLabel("Max jobs to process this run. Set to 0 to remove the limit.")
        limit_hint.setWordWrap(True)
        limit_hint.setStyleSheet("font-size: 10px; color: gray;")
        form.addRow("", limit_hint)

        return tab

    # ------------------------------------------------------------------
    def _open_npc_targets_dialog(self) -> None:
        """Open the standalone NPC Targets dialog (modal)."""
        dlg = NPCTargetsDialog(self)
        dlg.exec()

    # ------------------------------------------------------------------
    def _build_fallback_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        enable_row = QHBoxLayout()
        self.fallback_enable_check = QCheckBox("Enabled")
        self.fallback_enable_check.setChecked(cfg.USE_VOICE_FALLBACK)
        self.refresh_voices_btn = QPushButton("🔄 Refresh voices")
        enable_row.addWidget(self.fallback_enable_check)
        enable_row.addStretch()
        enable_row.addWidget(self.refresh_voices_btn)
        form.addRow("<b>Voice fallback:</b>", enable_row)

        self.fallback_male_combo = QComboBox()
        self.fallback_female_combo = QComboBox()
        self.fallback_neutral_combo = QComboBox()

        for label, combo, current in [
            ("Male:", self.fallback_male_combo, cfg.FALLBACK_VOICE_MALE),
            ("Female:", self.fallback_female_combo, cfg.FALLBACK_VOICE_FEMALE),
            ("Neutral:", self.fallback_neutral_combo, cfg.FALLBACK_VOICE_NEUTRAL),
        ]:
            combo.setEditable(True)
            combo.addItem(current)
            form.addRow(label, combo)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        form.addRow(sep)

        self.profile_sync_renew_check = QCheckBox("Force delete + rebuild before generating")
        self.profile_sync_renew_check.setChecked(cfg.PROFILE_SYNC_RENEW)
        self.profile_sync_renew_check.setToolTip(
            "When enabled, needed/all voice profiles that already exist on "
            "Voicebox are deleted and re-imported from scratch during sync, "
            "instead of being left alone. A profile is only ever deleted if "
            f"its WAV+TXT samples are still present in {cfg.VOICES_DIR}/, so "
            "nothing unrebuildable is ever lost."
        )
        form.addRow("<b>Profile renewal:</b>", self.profile_sync_renew_check)
        renew_hint = QLabel(
            "Useful after re-training/tweaking a voice: forces existing "
            "profiles to be rebuilt from the current samples instead of "
            "being skipped as already up to date."
        )
        renew_hint.setWordWrap(True)
        renew_hint.setStyleSheet("font-size: 10px; color: gray;")
        form.addRow("", renew_hint)

        layout.addLayout(form)
        layout.addStretch()
        return tab


# ============================================================================
# Main Application Window
# ============================================================================

class GenerateWindow(QMainWindow):
    """
    Main window for the TTS Voice Generator GUI application.

    Provides a native desktop interface to the same generation pipeline
    as generate.py, replacing console progress bars with real widgets.

    UI Components:
        - Configuration summary (read-only display of current settings)
        - Start/Stop buttons
        - Job progress bar (current generation)
        - Overall progress bar (entire run)
        - Log panel (scrolling text display of all logger output)
        - Status bar (stage messages)

    The window creates a background thread with a GenerationWorker to
    run the generation pipeline without blocking the UI. All progress
    updates are delivered via Qt signals and safely update the widgets.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎙️ TTS Voice Generator")
        self.resize(1100, 750)

        self.log_signal = LogSignal()
        self.log_signal.message.connect(self._append_log)

        global logger
        logger = log_initialize(self.log_signal)

        self.gen_thread: Optional[QThread] = None
        self.worker: Optional[GenerationWorker] = None
        self.sync_thread: Optional[QThread] = None
        self.sync_worker: Optional[ProfileSyncWorker] = None
        self.profiles_thread: Optional[QThread] = None
        self.profiles_worker: Optional[ProfilesFetchWorker] = None

        self.health_thread = QThread()
        self.health_worker = HealthCheckWorker(interval_ms=10000)
        self.health_worker.moveToThread(self.health_thread)
        self.health_thread.started.connect(self.health_worker.start)
        self.health_worker.health_checked.connect(self._on_health_checked)
        self.health_thread.start()

        self._build_ui()
        self._refresh_fallback_voices()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """
        Build the main window's UI components.

        Creates and arranges:
            1. Configuration group - Read-only summary of current settings
            2. Controls group - Start/Stop buttons
            3. Progress group - Job progress bar and Overall progress bar
            4. Log group - Scrollable text display
            5. Status bar - For temporary status messages
        """
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.config_dialog = ConfigDialog(self)
        self.config_dialog.save_config_btn.clicked.connect(self._save_config_edits)
        self.config_dialog.refresh_voices_btn.clicked.connect(self._refresh_fallback_voices)

        config_bar = QGroupBox("⚙️ Configuration")
        config_bar_layout = QHBoxLayout(config_bar)
        config_bar_layout.addWidget(self.config_dialog.health_dot)
        self.config_summary_label = QLabel()
        self.config_summary_label.setStyleSheet("color: gray;")
        config_bar_layout.addWidget(self.config_summary_label, stretch=1)
        self.open_config_btn = QPushButton("🛠️ Settings…")
        self.open_config_btn.clicked.connect(self._open_config_dialog)
        config_bar_layout.addWidget(self.open_config_btn)
        layout.addWidget(config_bar)
        self._update_config_summary()

        controls_layout = QHBoxLayout()
        self.start_btn = QPushButton("▶️ Start Generation")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        self.sync_btn = QPushButton("🔄 Sync All Profiles")
        self.sync_btn.setFlat(True)
        self.sync_btn.setToolTip("Scan voices/ and create/repair all voice profiles on Voicebox")
        self.sync_btn.clicked.connect(self._start_sync_all)
        controls_layout.addWidget(self.start_btn)
        controls_layout.addWidget(self.stop_btn)
        controls_layout.addStretch()
        controls_layout.addWidget(self.sync_btn)
        layout.addLayout(controls_layout)

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

    def _append_log(self, message: str, levelno: int) -> None:
        """
        Append a log message to the GUI log panel with appropriate coloring.

        Args:
            message: The log message to display.
            levelno: Logging level number (logging.INFO, logging.WARNING, etc.).
        """
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

    def _start(self) -> None:
        """
        Start the generation process on a background thread.

        Creates a QThread and a GenerationWorker, connects all signals,
        and starts the thread. The UI is updated to reflect the running state.
        """
        self.log_view.clear()
        self.job_bar.setValue(0)
        self.overall_bar.setValue(0)
        self.job_label.setText("Preparing...")
        self.overall_label.setText("Overall: preparing...")

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.sync_btn.setEnabled(False)

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

    def _stop(self) -> None:
        """
        Request the generation to stop after the current job.

        Calls request_stop() on the worker, which sets a stop flag and
        cancels any in-flight generation.
        """
        if self.worker:
            self.worker.request_stop()
            self.stop_btn.setEnabled(False)
            self.statusBar().showMessage("Stopping after current job...", 5000)

    def _on_thread_finished(self) -> None:
        """Re-enable UI controls when the generation background thread finishes."""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.sync_btn.setEnabled(True)

    def _start_sync_all(self) -> None:
        """
        Start full voice profile synchronization on a background thread.

        Creates a QThread and a ProfileSyncWorker, connects worker signals to UI
        handlers, updates UI controls to disabled running state, and launches
        the synchronization process.
        """
        self.log_view.clear()
        self.job_bar.setValue(0)
        self.overall_bar.setValue(0)
        self.job_label.setText("Syncing voice profiles with Voicebox...")
        self.overall_label.setText("Overall: syncing profiles...")

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.sync_btn.setEnabled(False)

        self.sync_thread = QThread()
        self.sync_worker = ProfileSyncWorker()
        self.sync_worker.moveToThread(self.sync_thread)

        self.sync_thread.started.connect(self.sync_worker.run)
        self.sync_worker.stage.connect(self._on_stage)
        self.sync_worker.finished.connect(self._on_sync_finished)
        self.sync_worker.failed.connect(self._on_sync_failed)
        self.sync_worker.finished.connect(self.sync_thread.quit)
        self.sync_worker.failed.connect(self.sync_thread.quit)
        self.sync_thread.finished.connect(self._on_sync_thread_finished)

        self.sync_thread.start()

    def _on_sync_finished(self) -> None:
        """
        Handle successful completion of voice profile synchronization.

        Updates progress bars to 100%, displays completion messages in status
        bar and job labels, and resets overall status indicator.
        """
        self.job_bar.setValue(100)
        self.overall_bar.setValue(100)
        self.statusBar().showMessage("Voice profile sync complete.", 5000)
        self.job_label.setText("Voice profile sync complete.")
        self.overall_label.setText("Overall: idle")

    def _on_sync_failed(self, error: str) -> None:
        """
        Handle failure during voice profile synchronization.

        Args:
            error: Error message describing the synchronization failure.
        """
        self.statusBar().showMessage(f"Voice profile sync failed: {error}", 8000)
        self.job_label.setText(f"❌ {error}")
        self.overall_label.setText("Overall: error")

    def _on_sync_thread_finished(self) -> None:
        """Re-enable UI controls when the profile sync thread finishes."""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.sync_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------

    def _on_stage(self, text: str) -> None:
        """
        Update the status bar with the current stage message.

        Args:
            text: Short status message for the current pipeline stage.
        """
        self.statusBar().showMessage(text, 5000)

    def _on_job_progress(self, data: dict) -> None:
        """
        Update the job progress bar and label with status information.

        Args:
            data: Job progress data containing:
                - idx: Current job index (1-based)
                - total: Total number of jobs
                - strref: STRREF identifier
                - filename: Output filename
                - npc_name: NPC name being processed
                - voice_name: Voice profile name used
                - chars: Number of characters in the text
                - percent: Completion percentage (0-100)
                - elapsed: Elapsed time in seconds
                - estimated: Estimated total time in seconds
                - timeout: Maximum allowed time in seconds (optional)
                - status: Post-processing status message (optional)
        """
        if data.get("status"):
            status_text = data["status"]
            self.job_bar.setValue(100)
            job_width = len(str(data["total"]))
            voice_part = f" ({data['voice_name']})" if data["voice_name"] != data["npc_name"] else ""
            self.job_label.setText(
                f"[{data['idx']:>{job_width}}/{data['total']:>{job_width}}] "
                f"{data['strref']}/{data['filename']}  "
                f"⏳ {status_text}  "
                f"({data['chars']} chars)  {data['npc_name']}{voice_part}"
            )
            return

        self.job_bar.setValue(int(data["percent"]))
        job_width = len(str(data["total"]))
        voice_part = f" ({data['voice_name']})" if data["voice_name"] != data["npc_name"] else ""

        time_part = f"{format_time(data['elapsed'])} / {format_time(data['estimated'])}"
        if data.get("timeout"):
            time_part += f" (max: {format_time(data['timeout'])})"

        self.job_label.setText(
            f"[{data['idx']:>{job_width}}/{data['total']:>{job_width}}] "
            f"{data['strref']}/{data['filename']}  "
            f"{time_part}  "
            f"({data['chars']} chars)  {data['npc_name']}{voice_part}"
        )

    def _on_overall_progress(self, data: dict) -> None:
        """
        Update the overall progress bar and label.

        Args:
            data: Overall progress data containing percent,
                chars_processed, chars_total, elapsed, eta_seconds, finish_str.
        """
        if not data.get("ready"):
            self.overall_label.setText("Overall: processing...")
            return
        self.overall_bar.setValue(int(data["percent"]))
        elapsed = data.get("elapsed", 0)
        chars_per_sec = data["chars_processed"] / elapsed if elapsed > 0 else 0.0
        self.overall_label.setText(
            f"Overall: {data['chars_processed']:,}/{data['chars_total']:,} chars  "
            f"Elapsed: {format_time(data['elapsed'])}  "
            f"ETA: {format_time(data['eta_seconds'])}  "
            f"@ {data['finish_str']}  "
            f"({chars_per_sec:.1f} chars/sec)"
        )

    def _on_finished(self, stats: dict) -> None:
        """
        Handle successful completion of the generation pipeline.

        Args:
            stats: Final statistics from the run:
                - total_jobs: Total jobs processed
                - total_chars_processed: Total characters generated
                - avg_time_per_char: Average time per character
                - npc_stats: Per-NPC statistics
                - retry_stats: Retry statistics
                - was_stopped: Whether the run was stopped by the user
        """
        total_jobs = stats["total_jobs"]
        if total_jobs == 0:
            self.job_label.setText("Nothing to generate.")
            self.overall_label.setText("Overall: nothing to generate.")
        else:
            self.job_bar.setValue(100)
            self.overall_bar.setValue(100)
            self.job_label.setText(f"Finished {total_jobs} job(s).")
        self.statusBar().showMessage("Finished.", 5000)

    def _on_failed(self, message: str) -> None:
        """
        Handle a fatal error during the generation pipeline.

        Args:
            message: Error message describing the failure.
        """
        self.statusBar().showMessage(f"Failed: {message}", 8000)
        self.job_label.setText(f"❌ {message}")

    def _open_config_dialog(self) -> None:
        """Show the configuration dialog (non-modal so the log/progress stay visible)."""
        self.config_dialog.show()
        self.config_dialog.raise_()
        self.config_dialog.activateWindow()

    def _update_config_summary(self) -> None:
        """Refresh the one-line summary shown on the compact configuration bar."""
        d = self.config_dialog
        bits = [
            d.base_url_edit.text().strip(),
            f"{d.engine_edit.text().strip()}/{d.model_size_edit.text().strip()}",
            f"retry {d.retry_count_spin.value()}x",
        ]
        if d.fallback_enable_check.isChecked():
            bits.append("fallback on")
        if d.profile_sync_renew_check.isChecked():
            bits.append("profile renew on")
        self.config_summary_label.setText("  •  ".join(bits))

    def _save_config_edits(self) -> None:
        """Apply edited configuration fields to appconfig in one batched write."""
        d = self.config_dialog
        _appconfig_set_many({
            "BASE_URL": d.base_url_edit.text().strip(),
            "ENGINE": d.engine_edit.text().strip(),
            "MODEL_SIZE": d.model_size_edit.text().strip(),
            "RETRY_COUNT": d.retry_count_spin.value(),
            "RETRY_DELAY": d.retry_delay_spin.value(),
            "CONVERT_TO_OGG": d.convert_ogg_check.isChecked(),
            "OGG_QUALITY": d.ogg_quality_spin.value(),
            "ENABLE_TIMEOUT_SAFEGUARD": d.timeout_enable_check.isChecked(),
            "TIMEOUT_MAX_SECONDS": d.timeout_max_spin.value(),
            "TIMEOUT_MULTIPLIER": d.timeout_multiplier_spin.value(),
            "LIMIT": d.limit_spin.value(),
            "USE_VOICE_FALLBACK": d.fallback_enable_check.isChecked(),
            "FALLBACK_VOICE_MALE": d.fallback_male_combo.currentText().strip(),
            "FALLBACK_VOICE_FEMALE": d.fallback_female_combo.currentText().strip(),
            "FALLBACK_VOICE_NEUTRAL": d.fallback_neutral_combo.currentText().strip(),
            "PROFILE_SYNC_RENEW": d.profile_sync_renew_check.isChecked(),
        })

        logger.info("Configuration updated from UI.")
        self.statusBar().showMessage("Configuration saved.", 3000)
        self._update_config_summary()
        QTimer.singleShot(0, self.health_worker.check_now)
        self.config_dialog.close()

    def _on_health_checked(self, reachable: bool, info: dict) -> None:
        """Update the health dot color and tooltip from a /health poll result."""
        if reachable:
            color = "#2ecc71"
            tooltip = "Reachable\n" + "\n".join(f"{k}: {v}" for k, v in info.items())
        else:
            color = "#e74c3c"
            tooltip = f"Unreachable: {info.get('error', 'unknown error')}"
        self.config_dialog.health_dot.setStyleSheet(f"background-color: {color}; border-radius: 7px;")
        self.config_dialog.health_dot.setToolTip(tooltip)

    def _refresh_fallback_voices(self) -> None:
        """Fetch the current profile list in the background and repopulate the fallback combos."""
        self.config_dialog.refresh_voices_btn.setEnabled(False)
        self.profiles_thread = QThread()
        self.profiles_worker = ProfilesFetchWorker()
        self.profiles_worker.moveToThread(self.profiles_thread)
        self.profiles_thread.started.connect(self.profiles_worker.fetch)
        self.profiles_worker.profiles_loaded.connect(self._on_profiles_loaded)
        self.profiles_worker.failed.connect(self._on_profiles_failed)
        self.profiles_worker.profiles_loaded.connect(self.profiles_thread.quit)
        self.profiles_worker.failed.connect(self.profiles_thread.quit)
        self.profiles_thread.start()

    def _on_profiles_loaded(self, profile_map: dict) -> None:
        names = sorted(profile_map.keys(), key=str.lower)
        d = self.config_dialog
        for combo in (d.fallback_male_combo, d.fallback_female_combo, d.fallback_neutral_combo):
            current = combo.currentText()
            combo.clear()
            combo.addItems(names)
            idx = combo.findText(current)
            combo.setCurrentIndex(idx if idx >= 0 else (combo.addItem(current) or combo.count() - 1))
        d.refresh_voices_btn.setEnabled(True)
        self.statusBar().showMessage(f"Loaded {len(names)} voice profiles.", 3000)

    def _on_profiles_failed(self, message: str) -> None:
        self.config_dialog.refresh_voices_btn.setEnabled(True)
        self.statusBar().showMessage(f"Could not load voice profiles: {message}", 5000)

    def closeEvent(self, event) -> None:
        """
        Stop background threads cleanly before the window closes.

        The health-check QTimer lives on health_thread, so it must be
        stopped *from* that thread to avoid "Timers cannot be stopped
        from another thread" warnings.
        """
        QMetaObject.invokeMethod(self.health_worker, "stop", Qt.ConnectionType.QueuedConnection)
        self.health_thread.quit()
        self.health_thread.wait(2000)

        if self.gen_thread is not None and self.gen_thread.isRunning():
            if self.worker:
                self.worker.request_stop()
            self.gen_thread.quit()
            self.gen_thread.wait(2000)

        if self.sync_thread is not None and self.sync_thread.isRunning():
            self.sync_thread.quit()
            self.sync_thread.wait(2000)

        super().closeEvent(event)


# ============================================================================
# Application Entry Point
# ============================================================================

def main() -> None:
    """
    Application entry point.

    Creates the QApplication, instantiates the main window, and starts
    the Qt event loop. The GUI will remain responsive while the generation
    pipeline runs on a background thread.
    """
    app = QApplication(sys.argv)
    window = GenerateWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()