"""
Transcription Check Tool - GUI wrapper for generate-check.py's workflow.

Runs the same NPC-directory walk -> Voicebox /transcribe -> similarity-score
pipeline, but as a PySide6 desktop app with:
  - Parallel transcription via QThreadPool (cfg.SAMPLE_CONCURRENCY threads)
  - NPC-centric result grid sorted by worst similarity score (lowest first)
  - Drill-down panel showing all samples for the selected NPC
  - Side-by-side text comparison dialog with copy buttons
  - Real-time progress + ETA

All cfg.* values are read once at startup and treated as immutable during
the run. No Settings dialog - configuration lives in appconfig.py.

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
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from PySide6.QtCore import Qt, QObject, QRunnable, QThread, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QTextCursor, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QTextEdit, QGroupBox,
    QStatusBar, QTableWidget, QTableWidgetItem,
    QDialog, QSplitter, QAbstractItemView,
)

from appconfig import cfg
from utils import from_base36, filename_re, load_patcher_config, preprocess_text


# ============================================================================
# Logging
# ============================================================================

class LogSignal(QObject):
    """Bridges Python logging records into Qt signals."""
    message = Signal(str, int)


class QtLogHandler(logging.Handler):
    """Intercepts logging calls and emits them as Qt signals."""
    def __init__(self, log_signal: LogSignal):
        super().__init__()
        self.log_signal = log_signal

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.log_signal.message.emit(msg, record.levelno)
        except Exception:
            pass


def log_initialize(log_signal: LogSignal) -> logging.Logger:
    """Configure logging: file (DEBUG), console (ERROR), GUI panel (INFO)."""
    log_dir = Path(cfg.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "check_gui.log"
    logger = logging.getLogger("check_gui")
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

    for noisy in ("urllib3", "urllib3.connectionpool", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.propagate = False
    return logger


# ============================================================================
# Formatting helpers
# ============================================================================

def format_time(seconds: float) -> str:
    """Format seconds as a human-readable string."""
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
    """Return expected finish time as absolute HH:MM:SS or date+time."""
    if eta_seconds > 0:
        finish = datetime.now() + timedelta(seconds=eta_seconds)
        if finish.date() == datetime.now().date():
            return finish.strftime("%H:%M:%S")
        return finish.strftime("%x %X")
    return "..."


# ============================================================================
# TranscribeTask - one QRunnable per wav file
# ============================================================================

class TranscribeTask(QRunnable, QObject):
    """
    Transcribe a single .wav file via Voicebox /transcribe.

    Lives on QThreadPool; result and any error are delivered via the
    worker's done signal so the main thread is never blocked on network I/O.
    """

    done = Signal(dict)

    def __init__(
        self,
        wav_path: Path,
        strref: int,
        npc_name: str,
        csv_text: str,
        patcher_config: Optional[dict],
        timeout: float,
        retry_count: int,
        retry_delay: float,
    ):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.wav_path = wav_path
        self.strref = strref
        self.npc_name = npc_name
        self.csv_text = csv_text
        self.patcher_config = patcher_config
        self.timeout = timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay

    def run(self) -> None:
        url = (
            cfg.BASE_URL.rstrip("/")
            + "/"
            + cfg.TRANSCRIBE_ENDPOINT.lstrip("/")
        )

        transcribed_text = ""
        success = False
        last_error = ""

        for attempt in range(self.retry_count + 1):
            try:
                with open(self.wav_path, "rb") as f:
                    resp = requests.post(
                        url,
                        files={"file": (self.wav_path.name, f, "audio/wav")},
                        timeout=self.timeout,
                    )
                resp.raise_for_status()
                transcribed_text = resp.json().get("text", "")
                success = True
                break
            except Exception as ex:
                last_error = str(ex)
                if attempt < self.retry_count:
                    time.sleep(self.retry_delay)

        if not success:
            transcribed_text = f"<ERROR: {last_error}>"

        # Apply the same preprocessing generate_gui.py does before scoring
        text_for_scoring = self.csv_text
        if self.patcher_config:
            text_for_scoring = preprocess_text(text_for_scoring, self.patcher_config)

        score = round(
            SequenceMatcher(
                None,
                text_for_scoring.strip().lower(),
                transcribed_text.strip().lower(),
            ).ratio() * 100,
            2,
        )

        row = {
            "NPC": self.npc_name,
            "StrRef": self.strref,
            "AudioFile": self.wav_path.name,
            "CSVText": self.csv_text,
            "TranscribedText": transcribed_text,
            "SimilarityScore": score,
        }

        self.done.emit(row)


# ============================================================================
# CheckWorker - orchestrates NPC scan + parallel transcription
# ============================================================================

class CheckWorker(QObject):
    """
    Background worker that walks NPC directories, transcribes samples, and
    emits progress/results to the GUI via Qt signals.

    Uses QThreadPool for parallel /transcribe calls (maxConcurrency set from
    cfg.SAMPLE_CONCURRENCY at construction time).
    """

    # Signals (thread-safe, delivered via Qt::QueuedConnection by default)
    stage = Signal(str)             # short status for the status bar
    overall_progress = Signal(dict)  # overall bar update
    npc_completed = Signal(dict)     # one NPC's results ready
    finished = Signal(dict)          # entire run complete
    failed = Signal(str)             # fatal error

    def __init__(self):
        super().__init__()
        self._stop_requested = threading.Event()
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(cfg.SAMPLE_CONCURRENCY)
        self._npc_results: Dict[str, List[dict]] = {}
        self._pending_per_npc: Dict[str, int] = {}
        self._total_samples_done = 0

    def request_stop(self) -> None:
        """Ask the worker to stop after the current NPC."""
        self._stop_requested.set()

    def run(self) -> None:
        """Scan + transcribe all NPC directories on the worker thread."""
        logger = logging.getLogger("check_gui")
        try:
            self._run_impl(logger)
        except Exception as ex:
            logger.error(f"Fatal error: {ex}")
            self.failed.emit(str(ex))

    def _run_impl(self, logger: logging.Logger) -> None:
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

        # Phase 1: discover all NPC directories and their wav files
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
                batch.append((wav_file, strref, csv_text))
            if batch:
                npc_batches[npc_dir.name] = batch

        total_samples = sum(len(v) for v in npc_batches.values())
        logger.info(
            f"Will check {len(npc_batches)} NPCs ({total_samples} total samples)."
        )

        # Phase 2: submit all transcription tasks in parallel
        self.stage.emit(
            f"Transcribing {total_samples} samples across {len(npc_batches)} NPCs..."
        )
        self._npc_results = {}
        self._pending_per_npc = {}
        self._total_samples_done = 0
        start_time = time.time()

        for npc_name, batch in npc_batches.items():
            if self._stop_requested.is_set():
                break
            self._npc_results[npc_name] = []
            self._pending_per_npc[npc_name] = 0

            for wav_path, strref, csv_text in batch:
                task = TranscribeTask(
                    wav_path=wav_path,
                    strref=strref,
                    npc_name=npc_name,
                    csv_text=csv_text,
                    patcher_config=patcher_config,
                    timeout=cfg.SAMPLE_TIMEOUT_SECONDS,
                    retry_count=cfg.SAMPLE_RETRY_COUNT,
                    retry_delay=cfg.SAMPLE_RETRY_DELAY,
                )
                # QueuedConnection keeps slot_sample_done on the worker thread
                task.done.connect(
                    self.slot_sample_done,
                    type=Qt.ConnectionType.QueuedConnection,
                )
                self._pending_per_npc[npc_name] += 1
                self._pool.start(task)

            # Emit "in progress" placeholder so the UI shows the NPC name immediately
            self.npc_completed.emit({
                "npc": npc_name,
                "samples": [],
                "worst_score": None,
                "avg_score": None,
                "done": False,
            })

        # Phase 3: wait for all tasks to drain
        self.stage.emit("Waiting for transcription to complete...")
        poll_interval = 0.25
        last_overall_emit = 0.0

        while True:
            if self._stop_requested.is_set():
                time.sleep(1.0)
                break
            active = self._pool.activeThreadCount()
            if active == 0 and not any(self._pending_per_npc.values()):
                break
            now = time.time()
            if now - last_overall_emit >= poll_interval:
                self._emit_overall_progress(
                    total_samples=total_samples,
                    elapsed=now - start_time,
                )
                last_overall_emit = now
            time.sleep(poll_interval)

        # Phase 4: finalize
        if self._stop_requested.is_set():
            logger.warning("Stop requested - discarding incomplete results.")

        for npc_name in list(self._npc_results.keys()):
            samples = self._npc_results.get(npc_name, [])
            if not samples:
                continue
            scores = [
                s["SimilarityScore"] for s in samples
                if isinstance(s["SimilarityScore"], (int, float))
            ]
            worst = min(scores) if scores else None
            avg = (sum(scores) / len(scores)) if scores else None
            self.npc_completed.emit({
                "npc": npc_name,
                "samples": samples,
                "worst_score": worst,
                "avg_score": avg,
                "done": True,
            })

        self._emit_overall_progress(
            total_samples=total_samples,
            elapsed=time.time() - start_time,
        )

        logger.info("=" * 60)
        logger.info("TRANSCRIPTION CHECK COMPLETE")
        logger.info(f"  NPCs checked : {len(self._npc_results)}")
        logger.info(f"  Samples done : {self._total_samples_done}")
        logger.info("=" * 60)

        self.finished.emit({
            "total_npcs": len(self._npc_results),
            "total_samples": self._total_samples_done,
        })

    @Slot(dict)
    def slot_sample_done(self, row: dict) -> None:
        """Called by TranscribeTask.done on the CheckWorker thread."""
        npc = row["NPC"]
        self._npc_results.setdefault(npc, []).append(row)
        pending = self._pending_per_npc.get(npc, 1)
        self._pending_per_npc[npc] = max(0, pending - 1)
        self._total_samples_done += 1

        if self._pending_per_npc.get(npc, 0) == 0:
            samples = self._npc_results.get(npc, [])
            scores = [
                s["SimilarityScore"] for s in samples
                if isinstance(s["SimilarityScore"], (int, float))
            ]
            worst = min(scores) if scores else None
            avg = (sum(scores) / len(scores)) if scores else None
            self.npc_completed.emit({
                "npc": npc,
                "samples": samples,
                "worst_score": worst,
                "avg_score": avg,
                "done": True,
            })

    def _load_text_lookup(self, csv_path) -> Dict[int, str]:
        """Load StrRef -> Text lookup from CSV."""
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
            logging.getLogger("check_gui").warning(
                f"CSV not found: {csv_path} - using empty text for scoring."
            )
        return lookup

    def _emit_overall_progress(self, total_samples: int, elapsed: float) -> None:
        """Emit an overall_progress dict for the GUI progress bar."""
        if self._total_samples_done == 0 or elapsed == 0:
            ready = False
            eta_seconds = 0.0
        else:
            rate = self._total_samples_done / elapsed
            remaining = total_samples - self._total_samples_done
            eta_seconds = remaining / rate if rate > 0 else 0.0
            ready = True

        percent = (
            (self._total_samples_done / total_samples * 100)
            if total_samples > 0 else 0
        )

        self.overall_progress.emit({
            "ready": ready,
            "percent": min(percent, 100.0),
            "samples_done": self._total_samples_done,
            "samples_total": total_samples,
            "elapsed": elapsed,
            "eta_seconds": eta_seconds,
            "finish_str": format_finish_time(eta_seconds),
        })


# ============================================================================
# Numeric table widget item (numeric sort for ratio columns)
# ============================================================================

class _NumericTableWidgetItem(QTableWidgetItem):
    """Sorts numerically instead of lexicographically."""
    def __lt__(self, other):
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
    Popup showing one sample's CSV text and transcribed text side-by-side.

    Features:
      - Color-coded similarity score header
      - Two read-only text panes (CSV TEXT | TRANSCRIBED)
      - Copy-to-clipboard buttons for each pane (with brief "Copied!" flash)
    """

    def __init__(self, row: dict, thresholds: Tuple[float, float, float],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"StrRef {row['StrRef']} - {row['AudioFile']}")
        self.resize(1000, 600)

        outer = QVBoxLayout(self)

        # Header with score
        score = row["SimilarityScore"]
        excellent, good, poor = thresholds
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

        # Text panes (horizontal split)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        mono_font = QFont("Consolas", 9)

        # Left: CSV Text
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

        # Right: Transcribed Text
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

        # Close button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

    def _copy_to_clipboard(self, text: str, btn: QPushButton) -> None:
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        orig = btn.text()
        btn.setEnabled(False)
        btn.setText("Copied!")
        QTimer.singleShot(1500, lambda: self._restore_button(btn, orig))

    def _restore_button(self, btn: QPushButton, original_text: str) -> None:
        btn.setText(original_text)
        btn.setEnabled(True)


# ============================================================================
# Main Window
# ============================================================================

class CheckWindow(QMainWindow):
    """
    Main window for the Transcription Check GUI.

    Layout (top to bottom):
      1. Toolbar: Start / Stop / Export CSV + summary stats label
      2. Overall progress bar + ETA label
      3. NPC results table (one row per NPC, sorted by worst score)
      4. Detail panel (shows samples for the selected NPC)
      5. Log panel (scrolling text)
      6. Status bar
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Transcription Check")
        self.resize(1100, 850)

        self.log_signal = LogSignal()
        self.log_signal.message.connect(self._append_log)

        global logger
        logger = log_initialize(self.log_signal)

        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[CheckWorker] = None
        self._all_npc_data: Dict[str, dict] = {}
        self._selected_npc: Optional[str] = None

        self._build_ui()
        logger.info("Transcription Check ready.")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
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
            f"(config: {cfg.SAMPLES_PER_NPC} samples/NPC, concurrency {cfg.SAMPLE_CONCURRENCY})"
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
        layout.addWidget(tg, stretch=2)

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
        layout.addWidget(dg, stretch=1)

        # Log panel
        lg = QGroupBox("Log")
        lg_layout = QVBoxLayout(lg)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        lg_layout.addWidget(self.log_view)
        layout.addWidget(lg, stretch=1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready", 3000)

    # ------------------------------------------------------------------
    # Log panel
    # ------------------------------------------------------------------

    def _append_log(self, message: str, levelno: int) -> None:
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
        """Start the check on a background thread."""
        self.log_view.clear()
        self._all_npc_data.clear()
        self._selected_npc = None
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

    def _on_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"Failed: {message}", 8000)
        self.overall_label.setText(f"Failed: {message}")

    # ------------------------------------------------------------------
    # NPC table helpers
    # ------------------------------------------------------------------

    def _append_npc_row(self, npc_data: dict) -> int:
        row = self.npc_table.rowCount()
        self.npc_table.insertRow(row)
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

        self._apply_row_background(row, worst)

    def _find_npc_row(self, npc_name: str) -> Optional[int]:
        for row in range(self.npc_table.rowCount()):
            item = self.npc_table.item(row, 0)
            if item and item.text() == npc_name:
                return row
        return None

    def _apply_status_color(self, item: QTableWidgetItem, worst: Optional[float]) -> None:
        if worst is None:
            item.setText("In progress")
            return
        if worst >= cfg.SIMILARITY_EXCELLENT:
            item.setText("Excellent")
            item.setForeground(QColor("#2ecc71"))
        elif worst >= cfg.SIMILARITY_GOOD:
            item.setText("Good")
            item.setForeground(QColor("#c8a900"))
        elif worst >= cfg.SIMILARITY_POOR:
            item.setText("Poor")
            item.setForeground(QColor("#e67e22"))
        else:
            item.setText("Bad")
            item.setForeground(QColor("#e74c3c"))

    def _apply_row_background(self, row: int, worst: Optional[float]) -> None:
        if worst is None:
            return
        if worst >= cfg.SIMILARITY_EXCELLENT:
            color = QColor("#d5f5e3")
        elif worst >= cfg.SIMILARITY_GOOD:
            color = QColor("#fef9e7")
        elif worst >= cfg.SIMILARITY_POOR:
            color = QColor("#fdebd0")
        else:
            color = QColor("#fdedec")
        for col in range(self.npc_table.columnCount()):
            cell = self.npc_table.item(row, col)
            if cell:
                cell.setBackground(color)

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
        if score >= cfg.SIMILARITY_EXCELLENT:
            item.setForeground(QColor("#2ecc71"))
        elif score >= cfg.SIMILARITY_GOOD:
            item.setForeground(QColor("#c8a900"))
        elif score >= cfg.SIMILARITY_POOR:
            item.setForeground(QColor("#e67e22"))
        else:
            item.setForeground(QColor("#e74c3c"))

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
        thresholds = (
            cfg.SIMILARITY_EXCELLENT,
            cfg.SIMILARITY_GOOD,
            cfg.SIMILARITY_POOR,
        )
        dlg = SampleDetailDialog(samples[row_idx], thresholds, self)
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
