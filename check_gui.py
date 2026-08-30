"""
Transcription Check Tool - GUI for validating Voicebox transcription quality.

Scans NPC audio directories, sends each .wav file to the Voicebox /transcribe
endpoint, and scores the result against the expected text from the CSV lookup.
The resulting similarity scores reveal which NPCs have problematic audio or
incorrect transcriptions.

Features:
  - Sequential transcription, one /transcribe call at a time (the local
    Voicebox service handles one request at a time regardless, so this
    keeps things simple and easy to debug without giving up throughput)
  - NPC-centric result grid sorted by worst similarity score (lowest first)
  - Drill-down panel showing all samples for the selected NPC
  - Side-by-side text comparison dialog with copy buttons
  - Real-time progress bar with ETA and throughput metrics

Configuration lives in appconfig.py; all cfg.* values are read once at startup.

Usage:
    python check_gui.py
"""

import csv
import logging
import random
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import (
    Qt, QObject, QThread, QTimer, Signal,
)
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QTextEdit, QGroupBox,
    QStatusBar, QTableWidget, QTableWidgetItem,
    QDialog, QSplitter, QAbstractItemView,
)

from appconfig import cfg
from utils import (
    from_base36, filename_re, load_patcher_config, preprocess_text,
    score_status, setup_logging, transcribe_and_score,
)

logger = setup_logging("check_gui", console_level=logging.ERROR)
for _noisy in ("urllib3", "urllib3.connectionpool", "requests"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def format_time(seconds: float) -> str:
    """Convert a duration in seconds to a human-readable string.

    Converts seconds to a compact format with appropriate units:
    - Under 60 seconds: "Xs" (e.g., "45.5s")
    - 1-59 minutes: "XmYs" (e.g., "5m30s")
    - 1-23 hours: "XhYm" (e.g., "2h15m")
    - 24+ hours: "XdYh" (e.g., "3d5h")

    Args:
        seconds: Duration in seconds.

    Returns:
        A formatted string representing the duration.
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
    """Return the expected finish time as a formatted time string.

    If the estimated time is today, returns time in "HH:MM:SS" format.
    Otherwise, includes the date in locale-appropriate format.

    Args:
        eta_seconds: Estimated seconds until completion.

    Returns:
        Formatted finish time string, or "..." if eta_seconds <= 0.
    """
    if eta_seconds > 0:
        finish = datetime.now() + timedelta(seconds=eta_seconds)
        if finish.date() == datetime.now().date():
            return finish.strftime("%H:%M:%S")
        return finish.strftime("%x %X")
    return "..."


def transcribe_sample(
    wav_path: Path,
    strref: int,
    npc_name: str,
    text_for_scoring: str,
) -> dict:
    """Transcribe a single .wav file via the Voicebox /transcribe endpoint.

    Posts the audio file with retry logic, computes a similarity score
    between the expected and transcribed text, and returns the result row.
    Runs synchronously on the calling thread - called sequentially, once per
    sample, from CheckWorker.run().

    Args:
        wav_path: Path to the .wav file to transcribe.
        strref: String reference ID associated with this audio.
        npc_name: Name of the NPC this audio belongs to.
        text_for_scoring: The expected text for similarity scoring.

    Returns:
        Dict containing NPC, StrRef, AudioFile, CSVText, TranscribedText,
        and SimilarityScore.
    """
    result = transcribe_and_score(wav_path, text_for_scoring)

    return {
        "NPC": npc_name,
        "StrRef": strref,
        "AudioFile": wav_path.name,
        "CSVText": text_for_scoring,
        "TranscribedText": result["transcribed_text"],
        "SimilarityScore": result["score"],
    }


class CheckWorker(QObject):
    """Background worker that orchestrates NPC scanning and transcription.

    This worker discovers NPC directories, loads the text lookup from CSV,
    transcribes samples sequentially (one /transcribe call at a time - the
    local Voicebox service only handles one request at a time anyway, so
    there's no throughput to gain from concurrency, only complexity), tracks
    progress, and emits results to the GUI via Qt signals. It operates
    entirely on a background thread to avoid blocking the UI.

    Signals:
        stage: Short status string for the status bar.
        overall_progress: Dict containing progress metrics (ready, percent,
            samples_done, samples_total, elapsed, eta_seconds, finish_str).
        npc_completed: Dict with npc name, samples, worst_score, avg_score,
            and done flag.
        finished: Dict with total_npcs and total_samples on completion.
        failed: Error message string on fatal error.
    """

    stage = Signal(str)
    overall_progress = Signal(dict)
    npc_completed = Signal(dict)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self) -> None:
        """Initialize the worker."""
        super().__init__()
        self._stop_requested = threading.Event()
        self._total_samples_done = 0

    def request_stop(self) -> None:
        """Request the worker to stop processing after the current sample."""
        self._stop_requested.set()

    def run(self) -> None:
        """Entry point for the worker thread.

        Runs the full scan-transcribe-finalize pipeline and emits signals
        for UI updates. Catches all exceptions to emit them via the
        'failed' signal.
        """
        try:
            self._run_impl()
        except Exception as ex:
            logger.error(f"Fatal error: {ex}")
            self.failed.emit(str(ex))

    def _run_impl(self) -> None:
        """Execute the three-phase workflow: discover, transcribe, finalize."""
        self.stage.emit("Loading CSV lookup...")
        text_lookup = self._load_text_lookup(cfg.CSV_PATH)

        self.stage.emit("Loading patcher config...")
        patcher_config = None
        try:
            patcher_config = load_patcher_config(cfg.PATCHER_CONFIG_PATH)
        except Exception:
            pass

        output_dir = Path(cfg.OUTPUT_DIR)
        if not output_dir.exists():
            self.failed.emit(f"OUTPUT_DIR does not exist: {output_dir}")
            return

        self.stage.emit("Discovering NPC directories...")
        npc_dirs = sorted(p for p in output_dir.iterdir() if p.is_dir())

        if not npc_dirs:
            self.failed.emit(f"No subdirectories found in {output_dir}")
            return

        logger.info(f"Found {len(npc_dirs)} NPC directories.")

        npc_batches: Dict[str, List[Tuple[Path, int, str]]] = {}
        pattern = filename_re()

        for npc_dir in npc_dirs:
            if self._stop_requested.is_set():
                break
            wav_files = list(npc_dir.glob("*.wav")) + list(npc_dir.glob("*.WAV"))
            if not wav_files:
                continue
            sample_files = random.sample(
                wav_files, min(cfg.SAMPLES_PER_NPC, len(wav_files))
            )
            batch = []
            for wav_file in sample_files:
                match = pattern.match(wav_file.name)
                if not match:
                    logger.warning(f"Skipping invalid filename: {wav_file.name}")
                    continue
                strref = from_base36(match.group(1))
                csv_text = text_lookup.get(strref, "")
                if patcher_config:
                    csv_text = preprocess_text(csv_text, patcher_config)
                batch.append((wav_file, strref, csv_text))
            if batch:
                npc_batches[npc_dir.name] = batch

        total_samples = sum(len(v) for v in npc_batches.values())
        logger.info(
            f"Will check {len(npc_batches)} NPCs ({total_samples} total samples)."
        )

        self.stage.emit(
            f"Transcribing {total_samples} samples across {len(npc_batches)} NPCs..."
        )
        self._total_samples_done = 0
        start_time = time.time()
        last_overall_emit = 0.0

        for npc_name, batch in npc_batches.items():
            if self._stop_requested.is_set():
                break

            # Show the NPC immediately as "in progress" rather than waiting
            # for all its samples - sequential processing means that could
            # otherwise be a long wait for NPCs with many samples.
            self.npc_completed.emit({
                "npc": npc_name,
                "samples": [],
                "worst_score": None,
                "avg_score": None,
                "done": False,
            })

            npc_samples: List[dict] = []
            for wav_path, strref, text_for_scoring in batch:
                if self._stop_requested.is_set():
                    break

                row = transcribe_sample(
                    wav_path=wav_path,
                    strref=strref,
                    npc_name=npc_name,
                    text_for_scoring=text_for_scoring,
                )
                npc_samples.append(row)
                self._total_samples_done += 1

                # Incremental update so the sample count/scores tick up live
                # instead of only appearing once the whole NPC is done.
                scores = [
                    s["SimilarityScore"] for s in npc_samples
                    if isinstance(s["SimilarityScore"], (int, float))
                ]
                self.npc_completed.emit({
                    "npc": npc_name,
                    "samples": list(npc_samples),
                    "worst_score": min(scores) if scores else None,
                    "avg_score": (sum(scores) / len(scores)) if scores else None,
                    "done": False,
                })

                now = time.time()
                if now - last_overall_emit >= 0.25:
                    self._emit_overall_progress(
                        total_samples=total_samples,
                        elapsed=now - start_time,
                    )
                    last_overall_emit = now

            if not npc_samples:
                continue

            scores = [
                s["SimilarityScore"] for s in npc_samples
                if isinstance(s["SimilarityScore"], (int, float))
            ]
            worst = min(scores) if scores else None
            avg = (sum(scores) / len(scores)) if scores else None
            self.npc_completed.emit({
                "npc": npc_name,
                "samples": npc_samples,
                "worst_score": worst,
                "avg_score": avg,
                "done": True,
            })

        if self._stop_requested.is_set():
            logger.warning("Stop requested - discarding incomplete NPC's samples.")

        self._emit_overall_progress(
            total_samples=total_samples,
            elapsed=time.time() - start_time,
        )

        total_npcs = len(npc_batches)
        total_done = self._total_samples_done

        logger.info("=" * 60)
        logger.info("TRANSCRIPTION CHECK COMPLETE")
        logger.info(f"  NPCs checked : {total_npcs}")
        logger.info(f"  Samples done : {total_done}")
        logger.info("=" * 60)

        self.finished.emit({
            "total_npcs": total_npcs,
            "total_samples": total_done,
        })

    def _load_text_lookup(self, csv_path: Path) -> Dict[int, str]:
        """Load the StrRef to text mapping from a CSV file.

        Args:
            csv_path: Path to the CSV file containing StrRef and Text columns.

        Returns:
            Dict mapping StrRef (int) to Text (str).
        """
        lookup: Dict[int, str] = {}
        try:
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        strref = int(row["StrRef"])
                    except (KeyError, ValueError):
                        continue
                    lookup[strref] = row.get("Text", "")
        except FileNotFoundError:
            logger.warning(
                f"CSV not found: {csv_path} - using empty text for scoring."
            )
        return lookup

    def _emit_overall_progress(self, total_samples: int, elapsed: float) -> None:
        """Emit an overall_progress dict for the GUI progress bar."""
        samples_done = self._total_samples_done

        if samples_done == 0 or elapsed == 0:
            ready = False
            eta_seconds = 0.0
        else:
            rate = samples_done / elapsed
            remaining = total_samples - samples_done
            eta_seconds = remaining / rate if rate > 0 else 0.0
            ready = True

        percent = (
            (samples_done / total_samples * 100)
            if total_samples > 0 else 0
        )

        self.overall_progress.emit({
            "ready": ready,
            "percent": min(percent, 100.0),
            "samples_done": samples_done,
            "samples_total": total_samples,
            "elapsed": elapsed,
            "eta_seconds": eta_seconds,
            "finish_str": format_finish_time(eta_seconds),
        })


# ============================================================================
# Numeric table widget item (numeric sort for ratio columns)
# ============================================================================

class _NumericTableWidgetItem(QTableWidgetItem):
    """Table widget item that sorts numerically instead of lexicographically.

    Stores the numeric value in UserRole+1 data and uses it for comparison
    in __lt__, allowing proper numeric sorting for columns like scores
    and counts.
    """

    def __lt__(self, other: QTableWidgetItem) -> bool:
        """Compare numeric values for sorting.

        Args:
            other: The item to compare against.

        Returns:
            True if self's numeric value is less than other's.
        """
        self_val = self.data(Qt.ItemDataRole.UserRole + 1)
        other_val = (
            other.data(Qt.ItemDataRole.UserRole + 1)
            if isinstance(other, QTableWidgetItem) else None
        )
        if self_val is not None and other_val is not None:
            return self_val < other_val
        return super().__lt__(other)


# ============================================================================
# SampleDetailDialog - side-by-side text comparison with copy buttons
# ============================================================================

class SampleDetailDialog(QDialog):
    """
    Dialog showing side-by-side comparison of CSV text and transcribed text.

    Displays one sample's expected text alongside what was transcribed,
    with a color-coded similarity score header and copy-to-clipboard buttons
    for each pane.

    Features:
      - Color-coded similarity score header (Excellent/Good/Poor/Bad)
      - Two read-only text panes with monospace font
      - Copy-to-clipboard buttons with "Copied!" feedback
    """

    def __init__(self, row: dict, parent: Optional[QWidget] = None) -> None:
        """Initialize the detail dialog.

        Args:
            row: Dict containing sample data (StrRef, AudioFile, CSVText,
                 TranscribedText, SimilarityScore).
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle(f"StrRef {row['StrRef']} - {row['AudioFile']}")
        self.resize(1000, 600)

        outer = QVBoxLayout(self)

        score = row["SimilarityScore"]
        excellent = cfg.SIMILARITY_EXCELLENT
        good = cfg.SIMILARITY_GOOD
        poor = cfg.SIMILARITY_POOR
        if score >= excellent:
            score_color = "#2ecc71"
            score_label = f"EXCELLENT - {score:.2f}%"
        elif score >= good:
            score_color = "#c8a900"
            score_label = f"GOOD - {score:.2f}%"
        elif score >= poor:
            score_color = "#e67e22"
            score_label = f"POOR - {score:.2f}%"
        else:
            score_color = "#e74c3c"
            score_label = f"BAD - {score:.2f}%"

        score_label_widget = QLabel(score_label)
        score_label_widget.setStyleSheet(
            f"color: {score_color}; font-weight: bold; font-size: 14px;"
        )
        score_label_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(score_label_widget)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        mono_font = QFont("Consolas", 9)

        csv_widget = QWidget()
        csv_layout = QVBoxLayout(csv_widget)
        csv_layout.setContentsMargins(0, 0, 0, 0)
        csv_header = QLabel("CSV TEXT")
        csv_header.setStyleSheet("font-weight: bold;")
        csv_layout.addWidget(csv_header)
        self.csv_edit = QTextEdit()
        self.csv_edit.setReadOnly(True)
        self.csv_edit.setFont(mono_font)
        self.csv_edit.setPlainText(row["CSVText"])
        csv_layout.addWidget(self.csv_edit)
        self.csv_copy_btn = QPushButton("Copy CSV text")
        self.csv_copy_btn.clicked.connect(
            lambda: self._copy_to_clipboard(
                self.csv_edit.toPlainText(), self.csv_copy_btn)
        )
        csv_layout.addWidget(self.csv_copy_btn)

        trans_widget = QWidget()
        trans_layout = QVBoxLayout(trans_widget)
        trans_layout.setContentsMargins(0, 0, 0, 0)
        trans_header = QLabel("TRANSCRIBED")
        trans_header.setStyleSheet("font-weight: bold;")
        trans_layout.addWidget(trans_header)
        self.trans_edit = QTextEdit()
        self.trans_edit.setReadOnly(True)
        self.trans_edit.setFont(mono_font)
        self.trans_edit.setPlainText(row["TranscribedText"])
        trans_layout.addWidget(self.trans_edit)
        self.trans_copy_btn = QPushButton("Copy transcribed text")
        self.trans_copy_btn.clicked.connect(
            lambda: self._copy_to_clipboard(
                self.trans_edit.toPlainText(), self.trans_copy_btn)
        )
        trans_layout.addWidget(self.trans_copy_btn)

        splitter.addWidget(csv_widget)
        splitter.addWidget(trans_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        outer.addWidget(splitter, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

    def _copy_to_clipboard(self, text: str, btn: QPushButton) -> None:
        """Copy text to clipboard and show temporary "Copied!" feedback.

        Args:
            text: The text to copy to clipboard.
            btn: The button to modify for visual feedback.
        """
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        orig = btn.text()
        btn.setEnabled(False)
        btn.setText("Copied!")
        QTimer.singleShot(1500, lambda: self._restore_button(btn, orig))

    def _restore_button(self, btn: QPushButton, original_text: str) -> None:
        """Restore button text and enabled state after clipboard copy.

        Args:
            btn: The button to restore.
            original_text: The original button text to restore.
        """
        btn.setText(original_text)
        btn.setEnabled(True)


# ============================================================================
# Main Window
# ============================================================================

class CheckWindow(QMainWindow):
    """
    Main window for the Transcription Check GUI application.

    Provides a complete interface for running transcription quality checks:
      1. Toolbar with Start/Stop/Export controls and stats display
      2. Overall progress bar with ETA
      3. NPC results table sorted by worst similarity score
      4. Detail panel showing samples for selected NPC
      5. Status bar for short status messages

    The check runs on a background thread, with real-time updates
    as each NPC completes.
    """

    def __init__(self) -> None:
        """Initialize the main window and build the UI."""
        super().__init__()
        self.setWindowTitle("Transcription Check")
        self.resize(1100, 850)

        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[CheckWorker] = None
        self._all_npc_data: Dict[str, dict] = {}
        self._selected_npc: Optional[str] = None
        self._npc_row_index: Dict[str, int] = {}

        self._build_ui()
        logger.info("Transcription Check ready.")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Construct all UI elements and lay them out in the main window."""
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Toolbar
        toolbar = QHBoxLayout()
        self.start_btn = QPushButton("Start Check")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        self.export_btn = QPushButton("Export CSV")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_csv)
        self.stats_label = QLabel(
            f"NPCs: -  Samples: -  Avg: -%  "
            f"(config: {cfg.SAMPLES_PER_NPC} samples/NPC, sequential)"
        )
        self.stats_label.setStyleSheet("color: gray; font-size: 11px;")
        toolbar.addWidget(self.start_btn)
        toolbar.addWidget(self.stop_btn)
        toolbar.addWidget(self.export_btn)
        toolbar.addStretch()
        toolbar.addWidget(self.stats_label)
        layout.addLayout(toolbar)

        # Overall progress
        pg = QGroupBox("Overall Progress")
        pg_layout = QVBoxLayout(pg)
        self.overall_label = QLabel("Ready. Click Start to check all NPCs.")
        pg_layout.addWidget(self.overall_label)
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 10_000)
        self.overall_bar.setValue(0)
        pg_layout.addWidget(self.overall_bar)
        layout.addWidget(pg)

        # NPC results table
        tg = QGroupBox("NPC Results")
        tg_layout = QVBoxLayout(tg)
        self.npc_table = QTableWidget()
        self.npc_table.setColumnCount(5)
        self.npc_table.setHorizontalHeaderLabels(
            ["NPC Name", "Worst %", "Avg %", "Samples", "Status"]
        )
        self.npc_table.horizontalHeader().setStretchLastSection(True)
        self.npc_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.npc_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.npc_table.setColumnWidth(1, 80)
        self.npc_table.setColumnWidth(2, 80)
        self.npc_table.setColumnWidth(3, 80)
        self.npc_table.itemSelectionChanged.connect(self._on_npc_selected)
        self.npc_table.setSortingEnabled(True)
        tg_layout.addWidget(self.npc_table)
        layout.addWidget(tg, stretch=3)

        # Detail panel
        dg = QGroupBox("Sample Details")
        dl = QVBoxLayout(dg)
        self.detail_placeholder = QLabel(
            "Select an NPC from the table above to see its samples."
        )
        self.detail_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_placeholder.setStyleSheet(
            "color: gray; font-style: italic; padding: 20px;")
        dl.addWidget(self.detail_placeholder)
        self.detail_table = QTableWidget()
        self.detail_table.setColumnCount(5)
        self.detail_table.setHorizontalHeaderLabels([
            "StrRef", "Audio File", "Score %",
            "CSV Text (truncated)", "Transcribed Text (truncated)"
        ])
        self.detail_table.horizontalHeader().setStretchLastSection(True)
        self.detail_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.detail_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.detail_table.itemDoubleClicked.connect(self._on_detail_double_click)
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setColumnWidth(0, 80)
        self.detail_table.setColumnWidth(1, 180)
        self.detail_table.setColumnWidth(2, 80)
        self.detail_table.setColumnWidth(3, 300)
        self.detail_table.setColumnWidth(4, 300)
        self.detail_table.hide()
        dl.addWidget(self.detail_table)
        layout.addWidget(dg, stretch=3)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready", 3000)

    # ------------------------------------------------------------------
    # Run control
    # ------------------------------------------------------------------

    def _start(self) -> None:
        """Start the check on a background thread.

        Initializes state, creates the worker and its thread, connects
        signals, and begins processing.
        """
        self._all_npc_data.clear()
        self._npc_row_index.clear()
        self._selected_npc = None
        # Sorting re-shuffles rows on every single insert/update, which both
        # invalidates the row-index cache and gets expensive with hundreds+
        # of NPCs updating live. Keep it off during the run; re-enable and
        # sort once at the end.
        self.npc_table.setSortingEnabled(False)
        self.npc_table.setRowCount(0)
        self.detail_table.setRowCount(0)
        self.detail_table.hide()
        self.detail_placeholder.show()
        self.overall_bar.setValue(0)
        self.overall_label.setText("Starting...")
        self.stats_label.setText("NPCs: -  Samples: -  Avg: -%")
        self.export_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self._worker_thread = QThread()
        self._worker = CheckWorker()
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.stage.connect(self._on_stage)
        self._worker.overall_progress.connect(self._on_overall_progress)
        self._worker.npc_completed.connect(self._on_npc_completed)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.failed.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._on_thread_finished)

        self._worker_thread.start()

    def _stop(self) -> None:
        """Request the worker to stop after the current batch."""
        if self._worker:
            self._worker.request_stop()
            self.stop_btn.setEnabled(False)
            self.statusBar().showMessage("Stopping after current samples...", 5000)

    def _on_thread_finished(self) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------

    def _on_stage(self, text: str) -> None:
        self.statusBar().showMessage(text, 5000)

    def _on_overall_progress(self, data: dict) -> None:
        self.overall_bar.setValue(int(data["percent"] * 100))
        if not data.get("ready"):
            self.overall_label.setText(
                f"{data['samples_done']}/{data['samples_total']} samples - "
                f"Elapsed: {format_time(data['elapsed'])} - waiting..."
            )
            return
        rate = data["samples_done"] / data["elapsed"] if data["elapsed"] > 0 else 0
        self.overall_label.setText(
            f"{data['samples_done']:,}/{data['samples_total']:,} samples  |  "
            f"Elapsed: {format_time(data['elapsed'])}  |  "
            f"ETA: {format_time(data['eta_seconds'])}  |  "
            f"@ {data['finish_str']}  |  "
            f"({rate:.1f} samples/sec)"
        )

    def _on_npc_completed(self, npc_data: dict) -> None:
        """Insert or update one row in the NPC table from worker data."""
        npc_name = npc_data["npc"]
        self._all_npc_data[npc_name] = npc_data

        existing_row = self._find_npc_row(npc_name)
        if existing_row is not None:
            self._populate_npc_row(existing_row, npc_data)
        else:
            self._append_npc_row(npc_data)

        if npc_name == self._selected_npc:
            self._populate_detail_panel(npc_name)

        self._update_stats_label()

    def _on_finished(self, stats: dict) -> None:
        self.overall_bar.setValue(10_000)
        self.overall_label.setText(
            f"Done! {stats['total_npcs']} NPCs, {stats['total_samples']} samples."
        )
        self.export_btn.setEnabled(
            bool(self._all_npc_data) and stats["total_samples"] > 0)
        self.statusBar().showMessage("Check complete.", 5000)
        # Row order was frozen (insertion order) during the run to keep the
        # row-index cache valid; sort once now, worst score first.
        self.npc_table.setSortingEnabled(True)
        self.npc_table.sortItems(1, Qt.SortOrder.AscendingOrder)

    def _on_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"Failed: {message}", 8000)
        self.overall_label.setText(f"Failed: {message}")
        self.npc_table.setSortingEnabled(True)

    # ------------------------------------------------------------------
    # NPC table helpers
    # ------------------------------------------------------------------

    def _append_npc_row(self, npc_data: dict) -> int:
        row = self.npc_table.rowCount()
        self.npc_table.insertRow(row)
        self._npc_row_index[npc_data["npc"]] = row
        self._populate_npc_row(row, npc_data)
        return row

    def _populate_npc_row(self, row: int, npc_data: dict) -> None:
        npc_name = npc_data["npc"]
        samples = npc_data.get("samples", [])
        worst = npc_data.get("worst_score")
        avg = npc_data.get("avg_score")

        self.npc_table.setItem(row, 0, QTableWidgetItem(npc_name))

        if worst is not None:
            wi = _NumericTableWidgetItem(f"{worst:.1f}")
            wi.setData(Qt.ItemDataRole.UserRole + 1, worst)
        else:
            wi = QTableWidgetItem("...")
        wi.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.npc_table.setItem(row, 1, wi)

        if avg is not None:
            ai = _NumericTableWidgetItem(f"{avg:.1f}")
            ai.setData(Qt.ItemDataRole.UserRole + 1, avg)
        else:
            ai = QTableWidgetItem("-")
        ai.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.npc_table.setItem(row, 2, ai)

        ci = _NumericTableWidgetItem(str(len(samples)))
        ci.setData(Qt.ItemDataRole.UserRole + 1, len(samples))
        ci.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.npc_table.setItem(row, 3, ci)

        si = QTableWidgetItem()
        self._apply_status_color(si, worst)
        self.npc_table.setItem(row, 4, si)

    def _find_npc_row(self, npc_name: str) -> Optional[int]:
        # O(1) lookup via cache instead of scanning the table. This matters
        # once there are hundreds/thousands of NPC rows (see cfg batch size).
        row = self._npc_row_index.get(npc_name)
        if row is not None and 0 <= row < self.npc_table.rowCount():
            item = self.npc_table.item(row, 0)
            if item and item.text() == npc_name:
                return row
        # Cache miss (e.g. row order changed) - fall back to a scan and
        # repair the cache so future lookups are O(1) again.
        for r in range(self.npc_table.rowCount()):
            item = self.npc_table.item(r, 0)
            if item and item.text() == npc_name:
                self._npc_row_index[npc_name] = r
                return r
        return None

    def _apply_status_color(self, item: QTableWidgetItem, worst: Optional[float]) -> None:
        label, color = score_status(worst)
        item.setText(label)
        if worst is not None:
            item.setForeground(QColor(color))

    def _update_stats_label(self) -> None:
        all_scores = [
            s["SimilarityScore"]
            for data in self._all_npc_data.values()
            for s in data.get("samples", [])
            if isinstance(s["SimilarityScore"], (int, float))
        ]
        total_npcs = len(self._all_npc_data)
        total_samples = len(all_scores)
        avg_all = (sum(all_scores) / len(all_scores)) if all_scores else None
        avg_str = f"{avg_all:.1f}%" if avg_all is not None else "-"
        self.stats_label.setText(
            f"NPCs: {total_npcs}  Samples: {total_samples}  Avg: {avg_str}"
        )

    # ------------------------------------------------------------------
    # NPC selection -> detail panel
    # ------------------------------------------------------------------

    def _on_npc_selected(self) -> None:
        rows = self.npc_table.selectedItems()
        if not rows:
            self._selected_npc = None
            self.detail_table.hide()
            self.detail_placeholder.show()
            return
        row_idx = rows[0].row()
        name_item = self.npc_table.item(row_idx, 0)
        if not name_item:
            return
        self._selected_npc = name_item.text()
        self._populate_detail_panel(self._selected_npc)

    def _populate_detail_panel(self, npc_name: str) -> None:
        npc_data = self._all_npc_data.get(npc_name)
        if not npc_data:
            return
        samples = npc_data.get("samples", [])
        if not samples:
            self.detail_table.hide()
            self.detail_placeholder.setText(
                f"NPC '{npc_name}': no samples collected yet.")
            self.detail_placeholder.show()
            return

        self.detail_placeholder.hide()
        self.detail_table.show()
        # Sort by score, lowest first (errors float to the end)
        samples = sorted(
            samples,
            key=lambda s: s["SimilarityScore"]
            if isinstance(s["SimilarityScore"], (int, float))
            else float("inf"),
        )
        self.detail_table.setRowCount(len(samples))

        for row, sample in enumerate(samples):
            score = sample["SimilarityScore"]

            si = _NumericTableWidgetItem(str(sample["StrRef"]))
            si.setData(Qt.ItemDataRole.UserRole + 1, sample["StrRef"])
            si.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.detail_table.setItem(row, 0, si)

            self.detail_table.setItem(row, 1, QTableWidgetItem(
                sample["AudioFile"]))

            ci = _NumericTableWidgetItem(
                f"{score:.1f}" if isinstance(score, (int, float)) else str(score)
            )
            ci.setData(Qt.ItemDataRole.UserRole + 1, score)
            ci.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._apply_score_color(ci, score)
            self.detail_table.setItem(row, 2, ci)

            csv_t = sample["CSVText"]
            self.detail_table.setItem(row, 3, QTableWidgetItem(
                csv_t[:120] + "..." if len(csv_t) > 120 else csv_t))

            tr_t = sample["TranscribedText"]
            self.detail_table.setItem(row, 4, QTableWidgetItem(
                tr_t[:120] + "..." if len(tr_t) > 120 else tr_t))

        self.detail_table.resizeRowsToContents()

    def _apply_score_color(self, item: QTableWidgetItem, score: float) -> None:
        if not isinstance(score, (int, float)):
            return
        _, color = score_status(score)
        item.setForeground(QColor(color))

    def _on_detail_double_click(self, item: QTableWidgetItem) -> None:
        if self._selected_npc is None:
            return
        npc_data = self._all_npc_data.get(self._selected_npc)
        if not npc_data:
            return
        samples = npc_data.get("samples", [])
        row_idx = item.row()
        if row_idx >= len(samples):
            return
        dlg = SampleDetailDialog(samples[row_idx], self)
        dlg.exec()

    # ------------------------------------------------------------------
    # Export CSV
    # ------------------------------------------------------------------

    def _export_csv(self) -> None:
        output_file = "transcription_samples.csv"
        rows = [
            row
            for data in self._all_npc_data.values()
            for row in data.get("samples", [])
        ]
        if not rows:
            self.statusBar().showMessage("Nothing to export.", 3000)
            return
        with open(output_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "NPC", "StrRef", "AudioFile",
                "SimilarityScore", "CSVText", "TranscribedText",
            ])
            writer.writeheader()
            writer.writerows(rows)
        logger.info(f"Exported {len(rows)} rows to {output_file}")
        self.statusBar().showMessage(
            f"Exported {len(rows)} rows to {output_file}", 5000)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._worker:
            self._worker.request_stop()
        if (
            self._worker_thread is not None
            and self._worker_thread.isRunning()
        ):
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
        super().closeEvent(event)


# ============================================================================
# Application entry point
# ============================================================================

def main() -> None:
    app = QApplication(sys.argv)
    window = CheckWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
