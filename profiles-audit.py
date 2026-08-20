"""
Infinity Engine Voice Sample Auditor (PySide6 desktop version)

A native desktop GUI for auditing and managing voice sample files
for Infinity Engine game mods. This tool helps organize voice samples by NPC,
edit their associated text files, and move approved samples between the
preparation directory and the final voices directory.

Features:
    - Two modes: Review new voices (voices_prep/) and review approved voices (voices/)
    - NPC list with filtering and skip functionality
    - Audio playback for WAV files
    - Text editing for associated .txt files
    - Move files between directories with a single click
    - Persistent skip list across sessions

Usage:
    python profiles-audit.py
"""

import sys
import json
import shutil
import re
import os
from pathlib import Path
from typing import Dict, List, Set, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QLineEdit, QLabel, QPushButton,
    QSplitter, QGroupBox, QCheckBox, QTextEdit, QMessageBox, QStatusBar,
    QScrollArea, QFrame, QGridLayout, QSizePolicy,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

# ============================================================================
# Configuration Constants
# ============================================================================

VOICES_PREP_DIR = "voices_prep"          # Directory for raw/unedited voice samples
VOICES_DIR = "voices"                    # Directory for approved voice samples
SKIPPED_CONFIG_PATH = "profiles-audit-skipped.json"  # Persistent skip list


def debug_print(*args, **kwargs):
    """Print timestamped debug messages to stderr."""
    timestamp = __import__('time').strftime("%H:%M:%S")
    print(f"[{timestamp}] DEBUG:", *args, **kwargs, file=sys.stderr)


# ============================================================================
# Persistent Storage Functions
# ============================================================================

def load_skipped_npcs() -> Set[str]:
    """
    Load the set of skipped NPC names from the JSON configuration file.
    
    Returns:
        Set[str]: A set of NPC names that have been marked as skipped.
                 Returns an empty set if the file doesn't exist or is corrupted.
    
    The skipped NPCs are stored persistently so that the skip status survives
    application restarts. This allows users to mark NPCs as "skip" during
    auditing and have that preference remembered.
    """
    path = Path(SKIPPED_CONFIG_PATH)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_skipped_npcs(skipped_set: Set[str]) -> None:
    """
    Save the set of skipped NPC names to the JSON configuration file.
    
    Args:
        skipped_set: A set of NPC names to be persisted as skipped.
    
    This function writes the skipped NPC list to a JSON file to maintain
    state across application sessions. Called whenever the user toggles
    the skip checkbox.
    """
    path = Path(SKIPPED_CONFIG_PATH)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(skipped_set), f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving skipped NPCs: {e}")


# ============================================================================
# Data Loading Functions
# ============================================================================

def load_npc_groups(prep_dir: Path) -> Dict[str, List[Dict]]:
    """
    Scan the voices_prep directory and group files by NPC name.
    
    Args:
        prep_dir: Path to the voices_prep directory.
    
    Returns:
        Dict[str, List[Dict]]: A dictionary where:
            - Key: NPC name (string)
            - Value: List of sample dictionaries, each containing:
                - 'sample_num': The sample number (int, defaults to 1 if not present)
                - 'wav_path': Path to the WAV file
                - 'txt_path': Path to the TXT file
                - 'stem': The base filename without extension
    
    The function looks for WAV files and pairs them with corresponding TXT files.
    NPC names are derived from the filename pattern: "NPCNAME [number]" where
    the number is optional and defaults to 1 if not present.
    
    Example filename patterns:
        - "NPCNAME.WAV" -> sample_num = 1
        - "NPCNAME 2.WAV" -> sample_num = 2
        - "NPCNAME 3.WAV" -> sample_num = 3
    """
    npcs = {}
    
    if not prep_dir.exists():
        return npcs
    
    for wav_path in prep_dir.glob("*.WAV"):
        stem = wav_path.stem
        match = re.match(r'^(.*?)(?:\s+(\d+))?$', stem)
        if match:
            npc_name = match.group(1).strip()
            idx = match.group(2)
            sample_num = int(idx) if idx else 1
            
            txt_path = wav_path.with_suffix('.txt')
            
            if npc_name not in npcs:
                npcs[npc_name] = []
            
            npcs[npc_name].append({
                "sample_num": sample_num,
                "wav_path": wav_path,
                "txt_path": txt_path,
                "stem": stem
            })
    
    for npc in npcs:
        npcs[npc].sort(key=lambda x: x["sample_num"])
    
    return dict(sorted(npcs.items()))


def load_approved_npc_groups(voices_dir: Path) -> Dict[str, List[Dict]]:
    """
    Scan the voices directory and group files by NPC name.
    
    Args:
        voices_dir: Path to the voices directory.
    
    Returns:
        Dict[str, List[Dict]]: Same structure as load_npc_groups() but from VOICES_DIR.
    
    This is identical to load_npc_groups() but reads from the approved voices directory
    instead of the preparation directory. Used in "Review Approved Voices" mode.
    """
    npcs = {}
    
    if not voices_dir.exists():
        return npcs
    
    for wav_path in voices_dir.glob("*.WAV"):
        stem = wav_path.stem
        match = re.match(r'^(.*?)(?:\s+(\d+))?$', stem)
        if match:
            npc_name = match.group(1).strip()
            idx = match.group(2)
            sample_num = int(idx) if idx else 1
            
            txt_path = wav_path.with_suffix('.txt')
            
            if npc_name not in npcs:
                npcs[npc_name] = []
            
            npcs[npc_name].append({
                "sample_num": sample_num,
                "wav_path": wav_path,
                "txt_path": txt_path,
                "stem": stem
            })
    
    for npc in npcs:
        npcs[npc].sort(key=lambda x: x["sample_num"])
    
    return dict(sorted(npcs.items()))


# ============================================================================
# Main Application Window
# ============================================================================

class VoiceAuditor(QMainWindow):
    """
    Main window for the Infinity Engine Voice Sample Auditor.
    
    This class manages the entire application UI and logic, including:
        - NPC list with filtering and search
        - Sample viewing, editing, and audio playback
        - File movement between preparation and approval directories
        - Mode switching between reviewing new and approved voices
    
    The UI is split into two panels:
        - Left: NPC list with stats, search, and filtering
        - Right: Sample details with text editor and audio controls
    """
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎙️ Infinity Engine Voice Auditor")
        self.resize(1400, 900)

        # Initialize application state
        self.skipped_npcs: Set[str] = load_skipped_npcs()
        self.audit_mode = "prep"  # "prep" or "approved"
        self.selected_npc: Optional[str] = None
        self.npcs: Dict[str, List[Dict]] = {}
        self.visible_npcs: Dict[str, List[Dict]] = {}
        
        # Set up audio player with explicit audio output
        self.audio_output = QAudioOutput()
        self.media_player = QMediaPlayer()
        self.media_player.setAudioOutput(self.audio_output)
        
        # Build UI and load initial data
        self._build_ui()
        self._load_data()
        self._update_stats()
    
    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------
    
    def _build_ui(self):
        """
        Build the main application UI.
        
        Creates a split layout with:
            - Left panel: Mode selector, stats, search, and NPC list
            - Right panel: NPC header, sample editor, and action buttons
        
        The layout uses QSplitter to allow the user to resize panels.
        """
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        
        # Main splitter for resizable panels
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)
        
        # ---------- LEFT PANEL: NPC List ----------
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        
        # Mode selection buttons (mutually exclusive)
        mode_group = QGroupBox("🔄 Mode")
        mode_layout = QHBoxLayout(mode_group)
        
        self.prep_btn = QPushButton("📝 Review New Voices")
        self.prep_btn.setCheckable(True)
        self.prep_btn.setChecked(True)
        self.prep_btn.clicked.connect(lambda: self._switch_mode("prep"))
        
        self.approved_btn = QPushButton("🔍 Review Approved Voices")
        self.approved_btn.setCheckable(True)
        self.approved_btn.clicked.connect(lambda: self._switch_mode("approved"))
        
        mode_layout.addWidget(self.prep_btn)
        mode_layout.addWidget(self.approved_btn)
        left_layout.addWidget(mode_group)
        
        # Statistics display
        stats_group = QGroupBox("📊 Stats")
        stats_layout = QGridLayout(stats_group)
        
        self.total_label = QLabel("0")
        self.visible_label = QLabel("0")
        self.skipped_label = QLabel("0")
        
        stats_layout.addWidget(QLabel("Total:"), 0, 0)
        stats_layout.addWidget(self.total_label, 0, 1)
        stats_layout.addWidget(QLabel("Visible:"), 1, 0)
        stats_layout.addWidget(self.visible_label, 1, 1)
        stats_layout.addWidget(QLabel("Skipped:"), 2, 0)
        stats_layout.addWidget(self.skipped_label, 2, 1)
        
        left_layout.addWidget(stats_group)
        
        # NPC list header with "Hide Skipped" checkbox
        list_header = QHBoxLayout()
        list_header.addWidget(QLabel("📋 NPC List"))
        
        self.hide_skipped_cb = QCheckBox("Hide Skipped")
        self.hide_skipped_cb.setChecked(True)
        self.hide_skipped_cb.stateChanged.connect(self._on_filter_changed)
        list_header.addWidget(self.hide_skipped_cb)
        
        left_layout.addLayout(list_header)
        
        # Search input
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("🔍 Filter NPCs...")
        self.search_box.textChanged.connect(self._on_filter_changed)
        left_layout.addWidget(self.search_box)
        
        # NPC count indicator
        self.npc_count_label = QLabel("Showing 0 of 0 NPCs")
        left_layout.addWidget(self.npc_count_label)
        
        # NPC list widget
        self.npc_list = QListWidget()
        self.npc_list.currentItemChanged.connect(self._on_npc_selected)
        left_layout.addWidget(self.npc_list, stretch=1)
        
        splitter.addWidget(left_widget)
        
        # ---------- RIGHT PANEL: Sample Editor ----------
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        
        # Header with NPC name and action buttons
        self.header_frame = QFrame()
        self.header_frame.setFrameShape(QFrame.Shape.StyledPanel)
        header_layout = QHBoxLayout(self.header_frame)
        
        self.npc_title = QLabel("<h2>Select an NPC</h2>")
        header_layout.addWidget(self.npc_title, stretch=1)
        
        self.copy_btn = QPushButton("📋 Copy Name")
        self.copy_btn.clicked.connect(self._copy_name)
        self.copy_btn.setEnabled(False)
        header_layout.addWidget(self.copy_btn)
        
        self.skip_cb = QCheckBox("Skip this NPC")
        self.skip_cb.stateChanged.connect(self._on_skip_toggled)
        self.skip_cb.setEnabled(False)
        header_layout.addWidget(self.skip_cb)
        
        right_layout.addWidget(self.header_frame)
        
        # Samples area (scrollable)
        self.samples_scroll = QScrollArea()
        self.samples_scroll.setWidgetResizable(True)
        self.samples_content = QWidget()
        # Prevent the content widget from stretching vertically
        self.samples_content.setSizePolicy(
            QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        )
        self.samples_layout = QVBoxLayout(self.samples_content)
        self.samples_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.samples_layout.setSpacing(4)
        self.samples_layout.setContentsMargins(0, 0, 0, 0)
        self.samples_scroll.setWidget(self.samples_content)
        right_layout.addWidget(self.samples_scroll, stretch=1)
        
        # Action buttons (Approve/Unapprove)
        action_layout = QHBoxLayout()
        self.approve_btn = QPushButton("✅ Approve & Move to Voices")
        self.approve_btn.setEnabled(False)
        self.approve_btn.clicked.connect(self._approve_samples)
        action_layout.addWidget(self.approve_btn)
        action_layout.addStretch()
        right_layout.addLayout(action_layout)
        
        splitter.addWidget(right_widget)
        splitter.setSizes([400, 1000])
        
        # Status bar for user feedback
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready", 3000)
        
        # Connect audio error handler
        self.media_player.errorOccurred.connect(self._on_audio_error)
    
    # ------------------------------------------------------------------
    # Data Loading and Filtering
    # ------------------------------------------------------------------
    
    def _load_data(self):
        """
        Load NPC data based on the current audit mode.
        
        Uses load_npc_groups() for "prep" mode or load_approved_npc_groups()
        for "approved" mode. After loading, applies filters, populates the
        list, and updates statistics.
        """
        # Release any media file handles
        self._release_media()
        
        prep_dir = Path(VOICES_PREP_DIR)
        voices_dir = Path(VOICES_DIR)
        
        if self.audit_mode == "prep":
            self.npcs = load_npc_groups(prep_dir)
        else:
            self.npcs = load_approved_npc_groups(voices_dir)
        
        self._apply_filters()
        self._populate_npc_list()
        self._update_stats()
        
        # Auto-select first NPC if available
        if self.npc_list.count() > 0:
            self.npc_list.setCurrentRow(0)
        else:
            self.selected_npc = None
            self._clear_samples()
            self.npc_title.setText("<h2>No NPCs found</h2>")
    
    def _apply_filters(self):
        """
        Apply hide skipped and search filters to the NPC list.
        
        Filters NPCs based on:
            1. Skip status (if "Hide Skipped" is checked)
            2. Search term (case-insensitive substring match)
        
        The filtered result is stored in self.visible_npcs.
        """
        search_term = self.search_box.text().strip().lower()
        hide_skipped = self.hide_skipped_cb.isChecked()
        
        self.visible_npcs = {}
        for name, samples in self.npcs.items():
            is_skipped = name in self.skipped_npcs
            
            if hide_skipped and is_skipped:
                continue
            
            if search_term and search_term not in name.lower():
                continue
            
            self.visible_npcs[name] = samples
    
    def _populate_npc_list(self):
        """
        Populate the NPC list widget with filtered items.
        
        Adds icons to indicate skip status:
            - 👤: Normal NPC
            - ⏭️: Skipped NPC
        
        Each item stores the NPC name as user data for selection handling.
        """
        self.npc_list.blockSignals(True)
        self.npc_list.clear()
        
        for name in sorted(self.visible_npcs.keys()):
            is_skipped = name in self.skipped_npcs
            icon = "⏭️ " if is_skipped else "👤 "
            item = QListWidgetItem(f"{icon}{name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.npc_list.addItem(item)
        
        self.npc_list.blockSignals(False)
        total = len(self.npcs)
        visible = len(self.visible_npcs)
        self.npc_count_label.setText(f"Showing {visible} of {total} NPCs")
    
    def _update_stats(self):
        """Update the statistics display in the sidebar."""
        self.total_label.setText(str(len(self.npcs)))
        self.visible_label.setText(str(len(self.visible_npcs)))
        self.skipped_label.setText(str(len(self.skipped_npcs)))
    
    # ------------------------------------------------------------------
    # Filter and Mode Handlers
    # ------------------------------------------------------------------
    
    def _on_filter_changed(self):
        """
        Handle changes to search text or hide skipped checkbox.
        
        Re-applies filters, updates the list, and adjusts selection
        if the currently selected NPC is no longer visible.
        """
        self._apply_filters()
        self._populate_npc_list()
        self._update_stats()
        
        # If selected NPC is no longer visible, clear selection
        if self.selected_npc and self.selected_npc not in self.visible_npcs:
            self.selected_npc = None
            self._clear_samples()
            self.npc_title.setText("<h2>Select an NPC</h2>")
            self.copy_btn.setEnabled(False)
            self.skip_cb.setEnabled(False)
            self.skip_cb.blockSignals(True)
            self.skip_cb.setChecked(False)
            self.skip_cb.blockSignals(False)
            self.approve_btn.setEnabled(False)
        
        # Auto-select first available NPC if nothing is selected
        if self.npc_list.count() > 0 and not self.selected_npc:
            self.npc_list.setCurrentRow(0)
    
    def _switch_mode(self, mode: str):
        """
        Switch between "prep" and "approved" audit modes.
        
        Args:
            mode: Either "prep" or "approved"
        
        Updates button states, reloads data, and refreshes the UI.
        """
        if mode == self.audit_mode:
            return
        
        self.audit_mode = mode
        
        # Update button states
        self.prep_btn.setChecked(mode == "prep")
        self.approved_btn.setChecked(mode == "approved")
        
        self._load_data()
        self.statusBar().showMessage(f"Switched to {mode} mode", 3000)
    
    # ------------------------------------------------------------------
    # NPC Selection Handling
    # ------------------------------------------------------------------
    
    def _on_npc_selected(self, current: QListWidgetItem, previous):
        """
        Handle NPC selection from the list.
        
        Args:
            current: The newly selected list item
            previous: The previously selected list item (unused)
        """
        if current is None:
            return
        
        self.selected_npc = current.data(Qt.ItemDataRole.UserRole)
        self._render_samples()
    
    def _render_samples(self):
        """
        Render the sample list for the currently selected NPC.
        
        Displays:
            - Sample count header
            - For each sample:
                - Sample number and filename
                - Text editor for the .txt file
                - Play button for audio playback
        
        The layout is compact with minimal spacing between elements.
        """
        if not self.selected_npc or self.selected_npc not in self.visible_npcs:
            return
        
        samples = self.visible_npcs[self.selected_npc]
        is_skipped = self.selected_npc in self.skipped_npcs
        
        # Update header with NPC name
        if self.audit_mode == "prep":
            self.npc_title.setText(f"<h2>📝 {self.selected_npc}</h2>")
        else:
            self.npc_title.setText(f"<h2>🔍 {self.selected_npc}</h2>")
        
        # Enable action buttons
        self.copy_btn.setEnabled(True)
        self.skip_cb.setEnabled(True)
        self.skip_cb.blockSignals(True)
        self.skip_cb.setChecked(is_skipped)
        self.skip_cb.blockSignals(False)
        
        # Update approve button text based on mode
        if self.audit_mode == "prep":
            self.approve_btn.setText("✅ Approve & Move to Voices")
        else:
            self.approve_btn.setText("↩️ Unapprove & Move Back")
        self.approve_btn.setEnabled(True)
        
        # Clear and rebuild samples layout
        self._clear_layout(self.samples_layout)
        
        # Ensure compact spacing
        self.samples_layout.setSpacing(4)
        self.samples_layout.setContentsMargins(0, 0, 0, 0)
        self.samples_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        # Sample count header
        header_label = QLabel(f"<b>Total samples: {len(samples)}</b>")
        header_label.setStyleSheet("margin-bottom: 2px;")
        self.samples_layout.addWidget(header_label)
        
        # Render each sample
        for i, sample in enumerate(samples):
            # Sample header with number and filename
            header = QLabel(f"<b>Sample #{sample['sample_num']}</b>  (<code>{sample['stem']}</code>)")
            header.setStyleSheet("margin-top: 2px;")
            self.samples_layout.addWidget(header)
            
            # Text editor for the sample's .txt file
            text_content = ""
            if sample['txt_path'].exists():
                try:
                    text_content = sample['txt_path'].read_text(encoding='utf-8')
                except:
                    pass
            
            text_edit = QTextEdit()
            text_edit.setPlainText(text_content)
            text_edit.setMaximumHeight(60)
            text_edit.setStyleSheet("""
                QTextEdit {
                    border: 1px solid palette(mid);
                    border-radius: 3px;
                    padding: 4px;
                    font-size: 12px;
                }
            """)
            
            # Create a proper closure that captures the current sample
            def on_text_changed(edit=text_edit, s=sample):
                self._save_text(s, edit)
            
            text_edit.textChanged.connect(on_text_changed)
            self.samples_layout.addWidget(text_edit)
            
            # Audio play button
            if sample['wav_path'].exists():
                play_btn = QPushButton("▶️ Play")
                play_btn.setStyleSheet("""
                    QPushButton {
                        border: 1px solid palette(mid);
                        border-radius: 3px;
                        padding: 2px 12px;
                        font-size: 11px;
                    }
                    QPushButton:hover {
                        background-color: palette(highlight);
                        color: palette(highlighted-text);
                    }
                """)
                play_btn.clicked.connect(
                    lambda checked, wav_path=sample['wav_path']: self._play_audio(wav_path)
                )
                self.samples_layout.addWidget(play_btn)
            else:
                missing = QLabel("⚠️ Audio file missing")
                missing.setStyleSheet("color: #ff6b6b; font-size: 11px;")
                self.samples_layout.addWidget(missing)
            
            # Separator between samples
            if i < len(samples) - 1:
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setStyleSheet("color: palette(mid); margin: 2px 0;")
                self.samples_layout.addWidget(line)
    
    def _clear_layout(self, layout):
        """
        Recursively clear all widgets from a layout.
        
        Args:
            layout: The QLayout to clear
        
        This is used when rebuilding the samples panel to remove all
        old widgets before adding new ones.
        """
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            else:
                sub_layout = item.layout()
                if sub_layout is not None:
                    self._clear_layout(sub_layout)
    
    def _clear_samples(self):
        """Clear the samples panel and show a placeholder message."""
        self._clear_layout(self.samples_layout)
        self.samples_layout.addWidget(QLabel("No samples to display."))
    
    def _save_text(self, sample: Dict, text_edit: QTextEdit):
        """
        Save text content to the .txt file when it changes.
        
        Args:
            sample: The sample dictionary containing txt_path
            text_edit: The QTextEdit widget with the new text
        
        Only saves if the text has actually changed, to avoid unnecessary
        file writes and status bar spam.
        """
        new_text = text_edit.toPlainText()
        try:
            existing = ""
            if sample['txt_path'].exists():
                existing = sample['txt_path'].read_text(encoding='utf-8')
            
            if new_text != existing:
                sample['txt_path'].write_text(new_text, encoding='utf-8')
                self.statusBar().showMessage(f"💾 Saved text for {sample['stem']}", 2000)
        except Exception as e:
            debug_print(f"Error saving text: {e}")
    
    # ------------------------------------------------------------------
    # Audio Playback
    # ------------------------------------------------------------------
    
    def _play_audio(self, wav_path: Path):
        """
        Play a WAV file using Qt's multimedia system.
        
        Args:
            wav_path: Path to the WAV file to play
        
        Stops any currently playing audio before starting the new file.
        Volume is set to 70% for comfortable listening.
        """
        if not wav_path.exists():
            return
        
        # Stop any current playback
        self.media_player.stop()
        
        # Set volume to 70%
        self.audio_output.setVolume(0.7)
        
        # Play the file
        url = QUrl.fromLocalFile(str(wav_path.absolute()))
        self.media_player.setSource(url)
        self.media_player.play()
        
        self.statusBar().showMessage(f"🔊 Playing: {wav_path.name}", 3000)

    def _release_media(self):
        """Stop playback and release the media player to free file handles."""
        if self.media_player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.media_player.stop()
        # Clear the source to release the file
        self.media_player.setSource(QUrl())
        # Process events to ensure the release happens
        QApplication.processEvents()
    
    def _on_audio_error(self, error):
        """
        Handle audio playback errors.
        
        Args:
            error: The error code from QMediaPlayer
        
        Uses numeric error codes for compatibility across Qt versions.
        Shows a user-friendly message in the status bar.
        """
        error_messages = {
            0: "No error",
            1: "Resource error (file not found or inaccessible)",
            2: "Format error (unsupported audio format)",
            3: "Network error",
            4: "Access denied",
            5: "Service missing (media service not available)"
        }
        msg = error_messages.get(error, f"Unknown error code: {error}")
        self.statusBar().showMessage(f"⚠️ Audio error: {msg}", 5000)
        debug_print(f"Audio error: {msg}")
    
    # ------------------------------------------------------------------
    # Action Handlers
    # ------------------------------------------------------------------
    
    def _copy_name(self):
        """
        Copy the selected NPC name to the system clipboard.
        
        Used for quickly pasting NPC names into other tools or documents.
        """
        if self.selected_npc:
            QApplication.clipboard().setText(self.selected_npc)
            self.statusBar().showMessage(f"📋 Copied '{self.selected_npc}' to clipboard!", 3000)
    
    def _on_skip_toggled(self, state):
        """
        Handle the "Skip this NPC" checkbox toggle.
        
        Args:
            state: Qt.CheckState.Checked or Qt.CheckState.Unchecked
        
        Adds or removes the NPC from the skipped set and saves to disk.
        Updates the list icon and statistics immediately.
        """
        if not self.selected_npc:
            return
        
        is_skipped = state == Qt.CheckState.Checked.value
        
        if is_skipped and self.selected_npc not in self.skipped_npcs:
            self.skipped_npcs.add(self.selected_npc)
            save_skipped_npcs(self.skipped_npcs)
            self._apply_filters()
            self._populate_npc_list()
            self._update_stats()
            self.statusBar().showMessage(f"⏭️ Skipped {self.selected_npc}", 3000)
        elif not is_skipped and self.selected_npc in self.skipped_npcs:
            self.skipped_npcs.remove(self.selected_npc)
            save_skipped_npcs(self.skipped_npcs)
            self._apply_filters()
            self._populate_npc_list()
            self._update_stats()
            self.statusBar().showMessage(f"✅ Unskipped {self.selected_npc}", 3000)
    
    def _approve_samples(self):
        """
        Approve or unapprove the selected NPC's samples.
        
        In "prep" mode: Moves files from voices_prep/ to voices/
        In "approved" mode: Moves files from voices/ to voices_prep/
        
        After moving, refreshes the data and selects the first NPC.
        """
        if not self.selected_npc:
            return
        
        # Release any media file handles before moving files
        self._release_media()
        
        samples = self.visible_npcs[self.selected_npc]
        prep_dir = Path(VOICES_PREP_DIR)
        voices_dir = Path(VOICES_DIR)
        
        if self.audit_mode == "prep":
            # Move from prep to voices (approve)
            voices_dir.mkdir(parents=True, exist_ok=True)
            moved_count = 0
            
            for sample in samples:
                if sample['wav_path'].exists():
                    shutil.move(str(sample['wav_path']), str(voices_dir / sample['wav_path'].name))
                if sample['txt_path'].exists():
                    shutil.move(str(sample['txt_path']), str(voices_dir / sample['txt_path'].name))
                moved_count += 1
            
            # Remove from skipped if present (approved files shouldn't be skipped)
            if self.selected_npc in self.skipped_npcs:
                self.skipped_npcs.remove(self.selected_npc)
                save_skipped_npcs(self.skipped_npcs)
            
            self.statusBar().showMessage(f"✅ Moved {moved_count} files to {VOICES_DIR}/", 5000)
        else:
            # Move back from voices to prep (unapprove)
            prep_dir.mkdir(parents=True, exist_ok=True)
            moved_count = 0
            
            for sample in samples:
                if sample['wav_path'].exists():
                    shutil.move(str(sample['wav_path']), str(prep_dir / sample['wav_path'].name))
                if sample['txt_path'].exists():
                    shutil.move(str(sample['txt_path']), str(prep_dir / sample['txt_path'].name))
                moved_count += 1
            
            self.statusBar().showMessage(f"↩️ Moved {moved_count} files back to {VOICES_PREP_DIR}/", 5000)
        
        # Refresh data and select first NPC
        self._load_data()
        self._update_stats()
        
        if self.npc_list.count() > 0:
            self.npc_list.setCurrentRow(0)


# ============================================================================
# Application Entry Point
# ============================================================================

def main():
    """
    Application entry point.
    
    Sets up environment variables to suppress Qt multimedia debug messages,
    creates the QApplication, and launches the main window.
    """
    # Suppress Qt multimedia debug messages (reduces console noise)
    os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.multimedia.*=false"
    
    app = QApplication(sys.argv)
    window = VoiceAuditor()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()