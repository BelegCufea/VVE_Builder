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
  - Export and import check results via CSV with audio playback support

Configuration lives in appconfig.py; all cfg.* values are read once at startup.

Usage:
    python check_gui.py
"""

import os
import csv
import logging
import random
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any, Union

from PySide6.QtCore import (
    Qt, QUrl, QObject, QThread, QTimer, Signal,
)
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QTextEdit, QGroupBox,
    QStatusBar, QTableWidget, QTableWidgetItem,
    QDialog, QSplitter, QAbstractItemView, QSpinBox, QFileDialog, QMenu,
    QCheckBox
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

from libs.appconfig import cfg
from libs.utils import (
    format_finish_time, format_time, from_base36, filename_re,
    load_patcher_config, preprocess_text, score_status, setup_logging,
    transcribe_and_score, load_strref_filter, save_strref_filter,
)

logger = setup_logging("check_gui", console_level=logging.ERROR)
for _noisy in ("urllib3", "urllib3.connectionpool", "requests"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def transcribe_sample(
    wav_path: Path,
    strref: int,
    npc_name: str,
    text_for_scoring: str,
) -> Dict[str, Any]:
    """
    Transcribe a single .wav file via the Voicebox /transcribe endpoint.

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
        SimilarityScore, and Duration.
    """
    result = transcribe_and_score(wav_path, text_for_scoring)

    return {
        "NPC": npc_name,
        "StrRef": strref,
        "AudioFile": wav_path.name,
        "AudioPath": wav_path.resolve(),
        "CSVText": text_for_scoring,
        "TranscribedText": result["transcribed_text"],
        "SimilarityScore": result["score"],
        "Duration": result.get("duration", 0.0),
    }


class CheckWorker(QObject):
    """
    Background worker that orchestrates NPC scanning and transcription.

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
        """Initialize the worker with empty state."""
        super().__init__()
        self._stop_requested = threading.Event()
        self._total_samples_done = 0

    def request_stop(self) -> None:
        """Request the worker to stop processing after the current sample."""
        self._stop_requested.set()

    def run(self) -> None:
        """
        Entry point for the worker thread.

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
            wav_files = sorted(
                {
                    p.resolve()
                    for p in npc_dir.iterdir()
                    if p.is_file() and p.suffix.lower() == ".wav"
                }
            )
            if not wav_files:
                continue
            if cfg.SAMPLES_PER_NPC <= 0:
                # 0 means "check every available audio file" for this NPC.
                sample_files = wav_files
            else:
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

            self.npc_completed.emit({
                "npc": npc_name,
                "samples": [],
                "worst_score": None,
                "avg_score": None,
                "done": False,
            })

            npc_samples: List[Dict[str, Any]] = []
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

                scores = [
                    s["SimilarityScore"] for s in npc_samples
                    if isinstance(s["SimilarityScore"], (int, float))
                ]
                durations = [
                    s["Duration"] for s in npc_samples
                    if isinstance(s["Duration"], (int, float))
                ]                
                self.npc_completed.emit({
                    "npc": npc_name,
                    "samples": list(npc_samples),
                    "worst_score": min(scores) if scores else None,
                    "avg_score": (sum(scores) / len(scores)) if scores else None,
                    "sum_duration": (sum(durations)) if durations else None,
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
            durations = [
                s["Duration"] for s in npc_samples
                if isinstance(s["Duration"], (int, float))
            ]
            worst = min(scores) if scores else None
            avg = (sum(scores) / len(scores)) if scores else None
            sum_duration = (sum(durations)) if durations else None
            self.npc_completed.emit({
                "npc": npc_name,
                "samples": npc_samples,
                "worst_score": worst,
                "avg_score": avg,
                "sum_duration": sum_duration,
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
        """
        Load the StrRef to text mapping from a CSV file.

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
        """
        Emit an overall_progress dict for the GUI progress bar.

        Args:
            total_samples: Total number of samples to process.
            elapsed: Elapsed time in seconds since start.
        """
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


class _NumericTableWidgetItem(QTableWidgetItem):
    """
    Table widget item that sorts numerically instead of lexicographically.

    Stores the numeric value in UserRole+1 data and uses it for comparison
    in __lt__, allowing proper numeric sorting for columns like scores
    and counts.
    """

    def __lt__(self, other: QTableWidgetItem) -> bool:
        """
        Compare numeric values for sorting.

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


class SampleDetailDialog(QDialog):
    """
    Dialog showing side-by-side comparison of CSV text and transcribed text.

    Displays one sample's expected text alongside what was transcribed,
    with a color-coded similarity score header and copy-to-clipboard buttons
    for each pane.

    Features:
      - Color-coded similarity score header (Excellent/Good/Poor/Bad)
      - Audio duration display
      - Two read-only text panes with monospace font
      - Copy-to-clipboard buttons with "Copied!" feedback
    """

    def __init__(self, row: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        """
        Initialize the detail dialog.

        Args:
            row: Dict containing sample data (StrRef, AudioFile, CSVText,
                 TranscribedText, SimilarityScore, Duration).
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle(f"StrRef {row['StrRef']} - {row['AudioFile']}")
        self.resize(1000, 600)

        outer = QVBoxLayout(self)

        score = row["SimilarityScore"]
        duration = row.get("Duration", 0.0)
        label, score_color = score_status(score)
        score_label = f"{label.upper()} - {score:.2f}%  |  Duration: {duration:.2f}s"

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
        """
        Copy text to clipboard and show temporary "Copied!" feedback.

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
        """
        Restore button text and enabled state after clipboard copy.

        Args:
            btn: The button to restore.
            original_text: The original button text to restore.
        """
        btn.setText(original_text)
        btn.setEnabled(True)


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
        self._all_npc_data: Dict[str, Dict[str, Any]] = {}
        self._selected_npc: Optional[str] = None
        self._npc_row_index: Dict[str, int] = {}
        self._detail_samples: List[Dict[str, Any]] = []
        self._marked_strrefs: Set[int] = set()

        self.audio_output = QAudioOutput()
        self.media_player = QMediaPlayer()
        self.media_player.setAudioOutput(self.audio_output)

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

        toolbar = QHBoxLayout()
        self.start_btn = QPushButton("Start Check")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        samples_label = QLabel("Samples/NPC:")
        self.samples_spin = QSpinBox()
        self.samples_spin.setMinimum(0)
        self.samples_spin.setMaximum(100_000)
        self.samples_spin.setSpecialValueText("All")
        self.samples_spin.setValue(cfg.SAMPLES_PER_NPC)
        self.samples_spin.setToolTip(
            "Number of random samples to check per NPC. 0 = check every "
            "available audio file."
        )
        self.samples_spin.valueChanged.connect(self._on_samples_per_npc_changed)

        process_group = QGroupBox("Process")
        process_layout = QHBoxLayout(process_group)
        process_layout.addWidget(self.start_btn)
        process_layout.addWidget(self.stop_btn)
        process_layout.addSpacing(12)
        process_layout.addWidget(samples_label)
        process_layout.addWidget(self.samples_spin)
        toolbar.addWidget(process_group)
        toolbar.addStretch()

        self.export_btn = QPushButton("Export")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_csv)
        self.import_btn = QPushButton("Import")
        self.import_btn.clicked.connect(self._import_csv)

        csv_group = QGroupBox("Backup/Restore")
        csv_layout = QHBoxLayout(csv_group)
        csv_layout.addWidget(self.export_btn)
        csv_layout.addWidget(self.import_btn)
        toolbar.addWidget(csv_group)
        toolbar.addStretch()

        self.save_strref_filter_btn = QPushButton("Save")
        self.save_strref_filter_btn.setToolTip(
            "Save the checked StrRefs (from the sample detail table below) "
            "to a JSON filter file, for use with generate_gui.py's StrRef "
            "filter. Overwrites the target file."
        )
        self.save_strref_filter_btn.clicked.connect(self._save_strref_filter)
        self.load_strref_filter_btn = QPushButton("Load")
        self.load_strref_filter_btn.setToolTip(
            "Load a StrRef filter JSON file and check the matching StrRefs "
            "in the sample detail table below."
        )
        self.load_strref_filter_btn.clicked.connect(self._load_strref_filter)
        self.show_only_marked_check = QCheckBox("Show only marked")
        self.show_only_marked_check.setToolTip(
            "Hide NPCs with no marked StrRefs in the table below, and hide "
            "unmarked samples in the open detail table."
        )
        self.show_only_marked_check.toggled.connect(self._refresh_marked_state)

        self.marked_count_label = QLabel("Marked: 0")

        strref_group = QGroupBox("StrRef Filter")
        strref_layout = QHBoxLayout(strref_group)
        strref_layout.addWidget(self.save_strref_filter_btn)
        strref_layout.addWidget(self.load_strref_filter_btn)
        strref_layout.addSpacing(12)
        strref_layout.addWidget(self.marked_count_label)
        strref_layout.addSpacing(12)
        strref_layout.addWidget(self.show_only_marked_check)
        toolbar.addWidget(strref_group)

        layout.addLayout(toolbar)
        layout.addLayout(toolbar)

        self.stats_label = QLabel(
            f"NPCs: -  Samples: - Duration: - Avg: -%"
        )
        self.stats_label.setStyleSheet("color: gray; font-size: 11px;")

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
        self.npc_table.setColumnCount(7)
        self.npc_table.setHorizontalHeaderLabels(
            ["Marked", "NPC Name", "Worst %", "Avg %", "Samples", "Duration", "Status"]
        )
        self.npc_table.horizontalHeader().setStretchLastSection(True)
        self.npc_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.npc_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.npc_table.setColumnWidth(0, 60)
        self.npc_table.setColumnWidth(2, 80)
        self.npc_table.setColumnWidth(3, 80)
        self.npc_table.setColumnWidth(4, 80)
        self.npc_table.setColumnWidth(5, 80)
        self.npc_table.itemSelectionChanged.connect(self._on_npc_selected)
        self.npc_table.setSortingEnabled(True)
        self.npc_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.npc_table.customContextMenuRequested.connect(self._on_npc_table_context_menu)
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
        self.detail_table.setColumnCount(6)
        self.detail_table.setHorizontalHeaderLabels([
            "StrRef", "Audio File", "Score %",
            "Duration (s)", "CSV Text", "Transcribed Text"
        ])
        self.detail_table.horizontalHeader().setStretchLastSection(True)
        self.detail_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.detail_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.detail_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.detail_table.itemDoubleClicked.connect(self._on_detail_double_click)
        self.detail_table.itemChanged.connect(self._on_detail_strref_checked)
        self.detail_table.itemSelectionChanged.connect(
            self._on_detail_sample_selected)
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setColumnWidth(0, 80)
        self.detail_table.setColumnWidth(1, 180)
        self.detail_table.setColumnWidth(2, 80)
        self.detail_table.setColumnWidth(3, 90)
        self.detail_table.setColumnWidth(4, 300)
        self.detail_table.setColumnWidth(5, 300)
        self.detail_table.hide()
        dl.addWidget(self.detail_table)
        layout.addWidget(dg, stretch=3)

        # Full-text panel for whichever sample row is selected above - the
        # grid cells are single-line and elided, this shows both texts in
        # full so nothing is cut off.
        fg = QGroupBox("Full Text (selected sample)")
        fl = QVBoxLayout(fg)
        self.fulltext_header_label = QLabel(
            "Select a sample above to see its full text.")
        self.fulltext_header_label.setStyleSheet("color: gray;")
        fulltext_info_row = QHBoxLayout()
        fulltext_info_row.addWidget(self.fulltext_header_label)
        fulltext_info_row.addStretch()
        self.detail_play_btn = QPushButton("🔊 Play Sample")
        self.detail_play_btn.setToolTip(
            "Play the audio recording for the selected sample."
        )
        self.detail_play_btn.clicked.connect(self._play_selected_detail_sample)
        self.detail_play_btn.setEnabled(False)
        fulltext_info_row.addWidget(self.detail_play_btn)
        fl.addLayout(fulltext_info_row)

        fulltext_splitter = QSplitter(Qt.Orientation.Horizontal)
        mono_font = QFont("Consolas", 9)

        csv_widget = QWidget()
        csv_layout = QVBoxLayout(csv_widget)
        csv_layout.setContentsMargins(0, 0, 0, 0)
        csv_header = QLabel("CSV TEXT")
        csv_header.setStyleSheet("font-weight: bold;")
        csv_layout.addWidget(csv_header)
        self.detail_csv_edit = QTextEdit()
        self.detail_csv_edit.setReadOnly(True)
        self.detail_csv_edit.setFont(mono_font)
        csv_layout.addWidget(self.detail_csv_edit)
        fulltext_splitter.addWidget(csv_widget)

        trans_widget = QWidget()
        trans_layout = QVBoxLayout(trans_widget)
        trans_layout.setContentsMargins(0, 0, 0, 0)
        trans_header = QLabel("TRANSCRIBED TEXT")
        trans_header.setStyleSheet("font-weight: bold;")
        trans_layout.addWidget(trans_header)
        self.detail_trans_edit = QTextEdit()
        self.detail_trans_edit.setReadOnly(True)
        self.detail_trans_edit.setFont(mono_font)
        trans_layout.addWidget(self.detail_trans_edit)
        fulltext_splitter.addWidget(trans_widget)

        fulltext_splitter.setStretchFactor(0, 1)
        fulltext_splitter.setStretchFactor(1, 1)
        fl.addWidget(fulltext_splitter, stretch=1)
        layout.addWidget(fg, stretch=2)

        self.setStatusBar(QStatusBar())
        self.statusBar().addPermanentWidget(self.stats_label)
        self.statusBar().showMessage("Ready", 3000)

    # ------------------------------------------------------------------
    # Run control
    # ------------------------------------------------------------------

    def _start(self) -> None:
        """
        Start the check on a background thread.

        Initializes state, creates the worker and its thread, connects
        signals, and begins processing.
        """
        self._all_npc_data.clear()
        self._npc_row_index.clear()
        self._selected_npc = None

        self.npc_table.setSortingEnabled(False)
        self.npc_table.setRowCount(0)
        self.detail_table.setRowCount(0)
        self.detail_table.hide()
        self.detail_placeholder.show()
        self.overall_bar.setValue(0)
        self.overall_label.setText("Starting...")
        self.stats_label.setText("NPCs: -  Samples: - Duration: - Avg: -%")
        self.export_btn.setEnabled(False)
        self.import_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.samples_spin.setEnabled(False)

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
        """Clean up after the worker thread finishes."""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.samples_spin.setEnabled(True)
        self.import_btn.setEnabled(True)

    def _on_samples_per_npc_changed(self, value: int) -> None:
        """
        Update cfg.SAMPLES_PER_NPC live from the spin box.

        0 means "check every available audio file" for each NPC.

        Args:
            value: New samples per NPC value.
        """
        cfg.SAMPLES_PER_NPC = value
        label = "all available" if value <= 0 else str(value)
        logger.info(f"Samples per NPC set to {label}.")

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------

    def _on_stage(self, text: str) -> None:
        """Handle stage update from worker."""
        self.statusBar().showMessage(text, 5000)

    def _on_overall_progress(self, data: Dict[str, Any]) -> None:
        """Handle overall progress update from worker."""
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

    def _on_npc_completed(self, npc_data: Dict[str, Any]) -> None:
        """
        Insert or update one row in the NPC table from worker data.

        Args:
            npc_data: Dictionary containing NPC data from worker.
        """
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

    def _on_finished(self, stats: Dict[str, Any]) -> None:
        """Handle completion signal from worker."""
        self.overall_bar.setValue(10_000)
        self.overall_label.setText(
            f"Done! {stats['total_npcs']} NPCs, {stats['total_samples']} samples."
        )
        self.export_btn.setEnabled(
            bool(self._all_npc_data) and stats["total_samples"] > 0)
        self.statusBar().showMessage("Check complete.", 5000)
        self.npc_table.setSortingEnabled(True)
        self.npc_table.sortItems(2, Qt.SortOrder.AscendingOrder)

    def _on_failed(self, message: str) -> None:
        """Handle failure signal from worker."""
        self.statusBar().showMessage(f"Failed: {message}", 8000)
        self.overall_label.setText(f"Failed: {message}")
        self.npc_table.setSortingEnabled(True)
        self.import_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # NPC table helpers
    # ------------------------------------------------------------------

    def _append_npc_row(self, npc_data: Dict[str, Any]) -> int:
        """
        Append a new row to the NPC table.

        Args:
            npc_data: Dictionary containing NPC data.

        Returns:
            The row index where the NPC was inserted.
        """
        row = self.npc_table.rowCount()
        self.npc_table.insertRow(row)
        self._npc_row_index[npc_data["npc"]] = row
        self._populate_npc_row(row, npc_data)
        return row

    def _populate_npc_row(self, row: int, npc_data: Dict[str, Any]) -> None:
        """
        Populate a row in the NPC table with data.

        Args:
            row: Row index to populate.
            npc_data: Dictionary containing NPC data.
        """
        npc_name = npc_data["npc"]
        samples = npc_data.get("samples", [])
        worst = npc_data.get("worst_score")
        avg = npc_data.get("avg_score")
        sum_duration = npc_data.get("sum_duration")

        marked_count = self._marked_count_for(npc_data)
        mi = _NumericTableWidgetItem("" if marked_count == 0 else str(marked_count))
        mi.setData(Qt.ItemDataRole.UserRole + 1, marked_count)
        mi.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.npc_table.setItem(row, 0, mi)

        self.npc_table.setItem(row, 1, QTableWidgetItem(npc_name))

        if worst is not None:
            wi = _NumericTableWidgetItem(f"{worst:.1f}")
            wi.setData(Qt.ItemDataRole.UserRole + 1, worst)
        else:
            wi = QTableWidgetItem("...")
        wi.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.npc_table.setItem(row, 2, wi)

        if avg is not None:
            ai = _NumericTableWidgetItem(f"{avg:.1f}")
            ai.setData(Qt.ItemDataRole.UserRole + 1, avg)
        else:
            ai = QTableWidgetItem("-")
        ai.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.npc_table.setItem(row, 3, ai)

        ci = _NumericTableWidgetItem(str(len(samples)))
        ci.setData(Qt.ItemDataRole.UserRole + 1, len(samples))
        ci.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.npc_table.setItem(row, 4, ci)

        # Total duration
        if sum_duration is not None:
            di = _NumericTableWidgetItem(format_time(sum_duration))
            di.setData(Qt.ItemDataRole.UserRole + 1, sum_duration)
        else:
            di = QTableWidgetItem("-")
        di.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.npc_table.setItem(row, 5, di)

        si = QTableWidgetItem()
        self._apply_status_color(si, worst)
        self.npc_table.setItem(row, 6, si)

        self.npc_table.setRowHidden(
            row, self.show_only_marked_check.isChecked() and marked_count == 0
        )

    def _marked_count_for(self, npc_data: Dict[str, Any]) -> int:
        """
        Count how many of an NPC's samples have a marked StrRef.

        Args:
            npc_data: Dictionary containing the NPC's sample list.

        Returns:
            Number of samples whose StrRef is in self._marked_strrefs.
        """
        return sum(
            1 for s in npc_data.get("samples", [])
            if s["StrRef"] in self._marked_strrefs
        )

    def _find_npc_row(self, npc_name: str) -> Optional[int]:
        """
        Find the row index for an NPC by name.

        Uses O(1) cache lookup, falling back to linear scan if cache is stale.

        Args:
            npc_name: Name of the NPC to find.

        Returns:
            Row index if found, None otherwise.
        """
        row = self._npc_row_index.get(npc_name)
        if row is not None and 0 <= row < self.npc_table.rowCount():
            item = self.npc_table.item(row, 1)
            if item and item.text() == npc_name:
                return row
        for r in range(self.npc_table.rowCount()):
            item = self.npc_table.item(r, 1)
            if item and item.text() == npc_name:
                self._npc_row_index[npc_name] = r
                return r
        return None

    def _apply_status_color(self, item: QTableWidgetItem, worst: Optional[float]) -> None:
        """
        Apply status text and color to a table item based on score.

        Args:
            item: Table item to modify.
            worst: Worst score for the NPC.
        """
        label, color = score_status(worst)
        item.setText(label)
        if worst is not None:
            item.setForeground(QColor(color))

    def _update_stats_label(self) -> None:
        """Update the statistics label with current totals."""
        all_samples = [
            s for data in self._all_npc_data.values()
            for s in data.get("samples", [])
        ]
        all_scores = [
            s["SimilarityScore"] for s in all_samples
            if isinstance(s.get("SimilarityScore"), (int, float))
        ]
        all_durations = [
            s["Duration"] for s in all_samples
            if isinstance(s.get("Duration"), (int, float))
        ]
        total_npcs = len(self._all_npc_data)
        total_samples = len(all_scores)
        avg_all = (sum(all_scores) / len(all_scores)) if all_scores else None
        avg_str = f"{avg_all:.1f}%" if avg_all is not None else "-"
        dur_str = format_time(sum(all_durations)) if all_durations else "-"

        self.stats_label.setText(
            f"NPCs: {total_npcs}  Samples: {total_samples}  Duration: {dur_str}  Avg: {avg_str}"
        )

    # ------------------------------------------------------------------
    # NPC selection -> detail panel
    # ------------------------------------------------------------------

    def _on_npc_selected(self) -> None:
        """Handle selection change in the NPC table."""
        rows = self.npc_table.selectedItems()
        if not rows:
            self._selected_npc = None
            self.detail_table.hide()
            self.detail_placeholder.show()
            self._clear_fulltext_panel()
            return
        row_idx = rows[0].row()
        name_item = self.npc_table.item(row_idx, 1)
        if not name_item:
            return
        self._selected_npc = name_item.text()
        self._populate_detail_panel(self._selected_npc)

    def _populate_detail_panel(self, npc_name: str) -> None:
        """
        Populate the detail panel with samples for the selected NPC.

        Args:
            npc_name: Name of the NPC to display samples for.
        """
        npc_data = self._all_npc_data.get(npc_name)
        if not npc_data:
            return
        samples = npc_data.get("samples", [])
        if not samples:
            self.detail_table.hide()
            self.detail_placeholder.setText(
                f"NPC '{npc_name}': no samples collected yet.")
            self.detail_placeholder.show()
            self._detail_samples = []
            self._clear_fulltext_panel()
            return

        self.detail_placeholder.hide()
        self.detail_table.show()
        samples = sorted(
            samples,
            key=lambda s: s["SimilarityScore"]
            if isinstance(s["SimilarityScore"], (int, float))
            else float("inf"),
        )
        self._detail_samples = samples
        self.detail_table.setRowCount(len(samples))
        self.detail_table.blockSignals(True)

        for row, sample in enumerate(samples):
            score = sample["SimilarityScore"]
            duration = sample.get("Duration", 0.0)

            si = _NumericTableWidgetItem(str(sample["StrRef"]))
            si.setData(Qt.ItemDataRole.UserRole + 1, sample["StrRef"])
            si.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            si.setFlags(si.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            si.setCheckState(
                Qt.CheckState.Checked
                if sample["StrRef"] in self._marked_strrefs
                else Qt.CheckState.Unchecked
            )
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

            # Duration column
            di = _NumericTableWidgetItem(f"{duration:.2f}" if duration else "0.00")
            di.setData(Qt.ItemDataRole.UserRole + 1, duration)
            di.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.detail_table.setItem(row, 3, di)

            csv_t = sample["CSVText"]
            csv_item = QTableWidgetItem(csv_t)
            csv_item.setToolTip(csv_t)
            self.detail_table.setItem(row, 4, csv_item)

            tr_t = sample["TranscribedText"]
            tr_item = QTableWidgetItem(tr_t)
            tr_item.setToolTip(tr_t)
            self.detail_table.setItem(row, 5, tr_item)

        self.detail_table.blockSignals(False)
        self.detail_table.setWordWrap(False)
        self.detail_table.setCurrentCell(0, 0)
        self._on_detail_sample_selected()
        if self.show_only_marked_check.isChecked():
            self._refresh_marked_state(rebuild_detail_checkboxes=False)

    def _on_detail_strref_checked(self, item: QTableWidgetItem) -> None:
        """
        Track a StrRef checkbox toggle in the detail table for filter export.

        Args:
            item: The changed item; only column 0 (StrRef) carries a checkbox.
        """
        if item.column() != 0:
            return
        row_idx = item.row()
        if row_idx >= len(self._detail_samples):
            return
        strref = self._detail_samples[row_idx]["StrRef"]
        if item.checkState() == Qt.CheckState.Checked:
            self._marked_strrefs.add(strref)
        else:
            self._marked_strrefs.discard(strref)
        self._refresh_marked_state(rebuild_detail_checkboxes=False)

    def _refresh_marked_state(self, rebuild_detail_checkboxes: bool = True) -> None:
        """
        Reapply everything derived from self._marked_strrefs and the
        "Show only marked" toggle: each NPC row's Marked count/visibility,
        and (if a detail table is open) its rows' checkbox state/visibility.

        Args:
            rebuild_detail_checkboxes: Whether to also re-sync the detail
                table's checkbox states from self._marked_strrefs. Skipped
                when called right after a detail-table checkbox toggle,
                since that row's own checkbox is already correct and
                rewriting it would just be redundant.
        """
        only_marked = self.show_only_marked_check.isChecked()
        self.marked_count_label.setText(f"Marked: {len(self._marked_strrefs)}")

        for npc_name, npc_data in self._all_npc_data.items():
            row = self._find_npc_row(npc_name)
            if row is None:
                continue
            marked_count = self._marked_count_for(npc_data)
            item = self.npc_table.item(row, 0)
            if item is not None:
                item.setText("" if marked_count == 0 else str(marked_count))
                item.setData(Qt.ItemDataRole.UserRole + 1, marked_count)
            self.npc_table.setRowHidden(row, only_marked and marked_count == 0)

        if not self._detail_samples:
            return

        self.detail_table.blockSignals(True)
        for row, sample in enumerate(self._detail_samples):
            is_marked = sample["StrRef"] in self._marked_strrefs
            if rebuild_detail_checkboxes:
                item = self.detail_table.item(row, 0)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked if is_marked else Qt.CheckState.Unchecked
                    )
            self.detail_table.setRowHidden(row, only_marked and not is_marked)
        self.detail_table.blockSignals(False)

    def _on_npc_table_context_menu(self, pos: Any) -> None:
        """
        Show a context menu on the NPC table to bulk mark/unmark its StrRefs.

        Args:
            pos: Position (in npc_table's viewport coordinates) of the
                right-click, as passed by customContextMenuRequested.
        """
        item = self.npc_table.itemAt(pos)
        if not item:
            return
        npc_name_item = self.npc_table.item(item.row(), 1)
        if npc_name_item is None:
            return
        npc_name = npc_name_item.text()
        npc_data = self._all_npc_data.get(npc_name)
        if not npc_data:
            return
        strrefs = [s["StrRef"] for s in npc_data.get("samples", [])]
        if not strrefs:
            return

        menu = QMenu(self)
        mark_action = menu.addAction(f"Mark all StrRefs ({len(strrefs)})")
        unmark_action = menu.addAction("Unmark all StrRefs")
        action = menu.exec(self.npc_table.viewport().mapToGlobal(pos))
        if action is mark_action:
            self._marked_strrefs.update(strrefs)
        elif action is unmark_action:
            self._marked_strrefs.difference_update(strrefs)
        else:
            return

        self._refresh_marked_state()

    def _save_strref_filter(self) -> None:
        """Save the currently checked StrRefs to a JSON filter file (overwrites)."""
        output_file, _ = QFileDialog.getSaveFileName(
            self,
            "Save StrRef Filter",
            cfg.STRREF_FILTER_FILE,
            "JSON Files (*.json);;All Files (*)",
        )
        if not output_file:
            return
        try:
            save_strref_filter(output_file, self._marked_strrefs)
        except Exception as ex:
            logger.error(f"Failed to save StrRef filter: {ex}")
            self.statusBar().showMessage(f"Failed to save StrRef filter: {ex}", 5000)
            return
        logger.info(f"Saved {len(self._marked_strrefs)} StrRefs to {output_file}")
        self.statusBar().showMessage(
            f"Saved {len(self._marked_strrefs)} StrRefs to {output_file}", 5000
        )

    def _load_strref_filter(self) -> None:
        """Load a StrRef filter JSON file and check its StrRefs in the detail table."""
        input_file, _ = QFileDialog.getOpenFileName(
            self,
            "Load StrRef Filter",
            cfg.STRREF_FILTER_FILE,
            "JSON Files (*.json);;All Files (*)",
        )
        if not input_file:
            return
        try:
            loaded = load_strref_filter(input_file)
        except Exception as ex:
            logger.error(f"Failed to load StrRef filter: {ex}")
            self.statusBar().showMessage(f"Failed to load StrRef filter: {ex}", 5000)
            return
        self._marked_strrefs = {int(s) for s in loaded}
        if self._selected_npc:
            self._populate_detail_panel(self._selected_npc)
        self._refresh_marked_state(rebuild_detail_checkboxes=False)
        logger.info(f"Loaded {len(self._marked_strrefs)} StrRefs from {input_file}")
        self.statusBar().showMessage(
            f"Loaded {len(self._marked_strrefs)} StrRefs from {input_file}", 5000
        )

    def _apply_score_color(self, item: QTableWidgetItem, score: float) -> None:
        """
        Apply color to a score item based on its value.

        Args:
            item: Table item to color.
            score: Score value to evaluate.
        """
        if not isinstance(score, (int, float)):
            return
        _, color = score_status(score)
        item.setForeground(QColor(color))

    def _on_detail_double_click(self, item: QTableWidgetItem) -> None:
        """
        Handle double-click on a detail sample row.

        Args:
            item: The item that was double-clicked.
        """
        row_idx = item.row()
        if row_idx >= len(self._detail_samples):
            return
        dlg = SampleDetailDialog(self._detail_samples[row_idx], self)
        dlg.exec()

    def _on_detail_sample_selected(self) -> None:
        """Handle selection change in the detail table."""
        rows = self.detail_table.selectedItems()
        if not rows:
            self._clear_fulltext_panel()
            return
        row_idx = rows[0].row()
        if row_idx >= len(self._detail_samples):
            self._clear_fulltext_panel()
            return
        sample = self._detail_samples[row_idx]
        duration = sample.get("Duration", 0.0)
        score_str = (f"{sample['SimilarityScore']:.1f}%" 
                    if isinstance(sample["SimilarityScore"], (int, float)) 
                    else str(sample["SimilarityScore"]))
        self.fulltext_header_label.setText(
            f"StrRef {sample['StrRef']}  |  {sample['AudioFile']}  |  "
            f"Score: {score_str}  |  Duration: {duration:.2f}s"
        )
        self.fulltext_header_label.setStyleSheet("font-weight: bold;")
        self.detail_csv_edit.setPlainText(sample["CSVText"])
        self.detail_trans_edit.setPlainText(sample["TranscribedText"])
        self.detail_play_btn.setEnabled(True)

    def _clear_fulltext_panel(self) -> None:
        """Clear the full text display panel."""
        self.fulltext_header_label.setText(
            "Select a sample above to see its full text.")
        self.fulltext_header_label.setStyleSheet("color: gray;")
        self.detail_csv_edit.clear()
        self.detail_trans_edit.clear()
        self.detail_play_btn.setEnabled(False)

    def _play_selected_detail_sample(self) -> None:
        """Play the WAV file for the selected detail-table sample."""
        rows = self.detail_table.selectedItems()
        if not rows:
            return
        row_idx = rows[0].row()
        if row_idx >= len(self._detail_samples):
            return

        wav_path = self._detail_samples[row_idx].get("AudioPath")
        if not wav_path or not wav_path.exists():
            self.media_player.stop()
            self.statusBar().showMessage("⚠️ Audio file not found", 5000)
            return

        self.media_player.stop()
        self.media_player.setSource(QUrl())
        QApplication.processEvents()

        self.audio_output.setVolume(0.7)
        self.media_player.setSource(QUrl.fromLocalFile(str(wav_path)))
        self.media_player.play()
        self.statusBar().showMessage(f"🔊 Playing: {wav_path.name}", 3000)

    # ------------------------------------------------------------------
    # Export & Import CSV
    # ------------------------------------------------------------------

    def _export_csv(self) -> None:
        """Export the current transcription data to a CSV file."""
        output_file, _ = QFileDialog.getSaveFileName(
            self,
            "Save Transcription Samples",
            "transcription_samples.csv",
            "CSV Files (*.csv);;All Files (*)",
        )

        if not output_file:
            return  # User cancelled

        rows = [
            row
            for data in self._all_npc_data.values()
            for row in data.get("samples", [])
        ]

        if not rows:
            self.statusBar().showMessage("Nothing to export.", 3000)
            return

        base_columns = [
            "NPC", "StrRef", "AudioFile", "AudioPath",
            "SimilarityScore", "Duration", "CSVText", "TranscribedText",
        ]
        all_keys = list(base_columns)
        for row in rows:
            for k in row.keys():
                if k not in all_keys:
                    all_keys.append(k)

        export_rows = []
        for r in rows:
            clean_row = {}
            for k in all_keys:
                v = r.get(k, "")
                clean_row[k] = str(v) if isinstance(v, Path) else v
            export_rows.append(clean_row)

        try:
            with open(output_file, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=all_keys,
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerows(export_rows)

            logger.info(f"Exported {len(export_rows)} rows to {output_file}")
            self.statusBar().showMessage(
                f"Exported {len(export_rows)} rows to {output_file}", 5000
            )
        except Exception as ex:
            logger.error(f"Failed to export CSV: {ex}")
            self.statusBar().showMessage(f"Failed to export CSV: {ex}", 5000)

    def _parse_imported_sample(self, row: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """
        Parse and normalize a single CSV row into a sample dictionary.

        Args:
            row: Raw CSV row as dict.

        Returns:
            Tuple of (npc_name, sample_dict).
        """
        npc_name = (
            row.get("NPC") or row.get("npc") or row.get("NPC Name") or "UNKNOWN"
        ).strip()

        raw_strref = row.get("StrRef") if "StrRef" in row else row.get("strref", "0")
        try:
            strref = int(raw_strref or "0")
        except (ValueError, TypeError):
            strref = 0

        audio_file = (
            row.get("AudioFile") or row.get("audiofile") or row.get("Audio File") or ""
        ).strip()

        raw_score = (
            row.get("SimilarityScore")
            if "SimilarityScore" in row
            else row.get("score")
            if "score" in row
            else row.get("Score")
            if "Score" in row
            else row.get("Score %")
        )
        try:
            score = (
                float(raw_score)
                if raw_score is not None and str(raw_score).strip() != ""
                else 0.0
            )
        except (ValueError, TypeError):
            score = 0.0

        raw_duration = (
            row.get("Duration")
            if "Duration" in row
            else row.get("duration")
            if "duration" in row
            else row.get("Duration (s)")
        )
        try:
            duration = (
                float(raw_duration)
                if raw_duration is not None and str(raw_duration).strip() != ""
                else 0.0
            )
        except (ValueError, TypeError):
            duration = 0.0

        csv_text = (
            row.get("CSVText")
            if "CSVText" in row
            else row.get("ExpectedText")
            if "ExpectedText" in row
            else row.get("CSV Text")
            if "CSV Text" in row
            else row.get("text", "")
        ) or ""

        trans_text = (
            row.get("TranscribedText")
            if "TranscribedText" in row
            else row.get("transcribed_text")
            if "transcribed_text" in row
            else row.get("Transcribed")
            if "Transcribed" in row
            else ""
        ) or ""

        raw_path = (
            row.get("AudioPath")
            if "AudioPath" in row
            else row.get("audiopath")
            if "audiopath" in row
            else row.get("Audio Path")
        )
        if raw_path and Path(raw_path).exists():
            audio_path = Path(raw_path).resolve()
        elif audio_file:
            fallback = Path(cfg.OUTPUT_DIR) / npc_name / audio_file
            if fallback.exists():
                audio_path = fallback.resolve()
            elif raw_path:
                audio_path = Path(raw_path).resolve()
            else:
                audio_path = fallback.resolve()
        elif raw_path:
            audio_path = Path(raw_path).resolve()
        else:
            audio_path = None

        sample = {
            "NPC": npc_name,
            "StrRef": strref,
            "AudioFile": audio_file,
            "AudioPath": audio_path,
            "CSVText": csv_text,
            "TranscribedText": trans_text,
            "SimilarityScore": score,
            "Duration": duration,
        }

        for k, v in row.items():
            if k is not None and k not in sample:
                sample[k] = v

        return npc_name, sample

    def _import_csv(self) -> None:
        """Import transcription data from a CSV file."""
        input_file, _ = QFileDialog.getOpenFileName(
            self,
            "Import Transcription Samples",
            "",
            "CSV Files (*.csv);;All Files (*)",
        )

        if not input_file:
            return  # User cancelled

        try:
            with open(input_file, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                raw_rows = list(reader)
        except Exception as ex:
            logger.error(f"Failed to read CSV {input_file}: {ex}")
            self.statusBar().showMessage(f"Failed to read CSV: {ex}", 5000)
            return

        if not raw_rows:
            self.statusBar().showMessage("No data found in imported CSV.", 5000)
            return

        npc_groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in raw_rows:
            npc_name, sample = self._parse_imported_sample(row)
            npc_groups.setdefault(npc_name, []).append(sample)

        # Reconstruct window state
        self._all_npc_data.clear()
        self._npc_row_index.clear()
        self._selected_npc = None
        self.npc_table.setSortingEnabled(False)
        self.npc_table.setRowCount(0)
        self.detail_table.setRowCount(0)
        self.detail_table.hide()
        self.detail_placeholder.setText(
            "Select an NPC from the table above to see its samples."
        )
        self.detail_placeholder.show()
        self._clear_fulltext_panel()

        total_samples = 0
        for npc_name, npc_samples in npc_groups.items():
            total_samples += len(npc_samples)
            scores = [
                s["SimilarityScore"] for s in npc_samples
                if isinstance(s["SimilarityScore"], (int, float))
            ]
            durations = [
                s["Duration"] for s in npc_samples
                if isinstance(s["Duration"], (int, float))
            ]
            worst = min(scores) if scores else None
            avg = (sum(scores) / len(scores)) if scores else None
            sum_duration = sum(durations) if durations else None

            npc_data = {
                "npc": npc_name,
                "samples": npc_samples,
                "worst_score": worst,
                "avg_score": avg,
                "sum_duration": sum_duration,
                "done": True,
            }
            self._all_npc_data[npc_name] = npc_data
            self._append_npc_row(npc_data)

        self._update_stats_label()
        self.npc_table.setSortingEnabled(True)
        self.npc_table.sortItems(2, Qt.SortOrder.AscendingOrder)
        self.export_btn.setEnabled(bool(self._all_npc_data) and total_samples > 0)
        self.overall_bar.setValue(10_000)
        file_name = Path(input_file).name
        self.overall_label.setText(
            f"Imported {len(self._all_npc_data)} NPCs, {total_samples} samples from {file_name}."
        )
        self.statusBar().showMessage(
            f"Imported {total_samples} samples across {len(self._all_npc_data)} NPCs.", 5000
        )
        logger.info(f"Imported {total_samples} samples from {input_file}")

        if self.npc_table.rowCount() > 0:
            self.npc_table.selectRow(0)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event: Any) -> None:
        """
        Handle window close event with proper cleanup.

        Args:
            event: Close event.
        """
        if self._worker:
            self._worker.request_stop()
        if (
            self._worker_thread is not None
            and self._worker_thread.isRunning()
        ):
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
        super().closeEvent(event)


def main() -> None:
    """Application entry point."""
    os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.multimedia.*=false"

    app = QApplication(sys.argv)
    window = CheckWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()