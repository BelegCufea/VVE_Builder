"""
Voice Profile Manager (PySide6 desktop version)

A native desktop GUI for managing voice profile assignments across
three levels: NPC Name, NPC+Gender, and System Name.
"""

import os
import sys
import json
import time
import re
import subprocess
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem, QLineEdit, QLabel, QPushButton, QComboBox,
    QScrollArea, QSplitter, QGroupBox, QFrame, QMessageBox, QStatusBar,
    QTextEdit, QDialog, QDialogButtonBox, QFileDialog,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

# ============================================================================
# Configuration
# ============================================================================

OGG_QUALITY = 4      # Vorbis quality setting (0-10, 4 is good quality/size balance)
MAX_DURATION = 30.0  # Maximum duration in seconds (single sample file)
CSV_PATH = "dialog-report.csv"
VOICES_DIR = "voices"
VOICE_SUBSTITUTIONS_FILE = "voice-substitutions.json"
VOICE_SUBSTITUTIONS_GENDER_FILE = "voice-substitutions-gender.json"
VOICE_SUBSTITUTIONS_SYSNAME_FILE = "voice-substitutions-sysname.json"
LOG_FILE_PATH = Path("logs/profiles-manage.log")
LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

# ============================================================================
# Logging
# ============================================================================

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# Audio Processing Functions
# ============================================================================

def convert_to_ogg(input_path: Path, output_path: Path, quality: int = OGG_QUALITY) -> bool:
    """
    Convert audio file to Ogg Vorbis format using ffmpeg.
    
    Args:
        input_path: Path to input audio file
        output_path: Path to output Ogg file
        quality: Vorbis quality (0-10, 4 is good quality/size balance)
    
    Returns:
        bool: True if conversion succeeded, False otherwise
    """
    cmd = [
        'ffmpeg',
        '-y',                      # Overwrite output files
        '-i', str(input_path),     # Input file
        '-c:a', 'libvorbis',       # Use libvorbis codec
        '-qscale:a', str(quality), # Quality setting
        '-f', 'ogg',               # Force Ogg container format
        str(output_path)
    ]
    
    try:
        logger.debug(f"Converting: {input_path} -> {output_path}")
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.debug(f"Conversion successful")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg error: {e.stderr}")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found! Please install ffmpeg.")
        return False


# ============================================================================
# Voice Profile Editor Dialog
# ============================================================================

class VoiceProfileEditor(QDialog):
    """
    Dialog for editing or creating a voice profile.
    
    Allows viewing/editing samples (WAV + TXT), adding new samples,
    and deleting existing samples.
    """
    
    def __init__(self, profile_name: str, parent=None):
        super().__init__(parent)
        self.profile_name = profile_name
        self.parent_window = parent
        self.setWindowTitle(f"🎵 Voice Profile Editor: {profile_name}")
        self.resize(800, 600)
        self._current_sample = None
        self._sample_data = []
        
        # Audio player
        self.audio_output = QAudioOutput()
        self.media_player = QMediaPlayer()
        self.media_player.setAudioOutput(self.audio_output)
        
        self._build_ui()
        self._load_samples()
        logger.info(f"Opened voice profile editor: {profile_name}")
    
    def _build_ui(self):
        layout = QVBoxLayout(self)
        
        # Header
        header = QLabel(f"<h2>Editing Profile: {self.profile_name}</h2>")
        layout.addWidget(header)
        
        # Samples list
        layout.addWidget(QLabel("Samples:"))
        
        self.samples_list = QListWidget()
        self.samples_list.currentItemChanged.connect(self._on_sample_selected)
        layout.addWidget(self.samples_list)
        
        # Sample details area
        details_group = QGroupBox("Sample Details")
        details_layout = QVBoxLayout(details_group)
        
        # Audio player
        self.play_btn = QPushButton("▶️ Play")
        self.play_btn.clicked.connect(self._play_selected_sample)
        self.play_btn.setEnabled(False)
        details_layout.addWidget(self.play_btn)
        
        # Text editor
        details_layout.addWidget(QLabel("Text:"))
        self.text_edit = QTextEdit()
        self.text_edit.setMaximumHeight(150)
        self.text_edit.textChanged.connect(self._on_text_changed)
        self.text_edit.setEnabled(False)
        details_layout.addWidget(self.text_edit)
        
        layout.addWidget(details_group)
        
        # Action buttons
        btn_layout = QHBoxLayout()
        
        self.add_btn = QPushButton("➕ Add Sample")
        self.add_btn.clicked.connect(self._add_sample)
        btn_layout.addWidget(self.add_btn)
        
        self.delete_btn = QPushButton("🗑️ Delete Sample")
        self.delete_btn.clicked.connect(self._delete_sample)
        self.delete_btn.setEnabled(False)
        btn_layout.addWidget(self.delete_btn)
        
        btn_layout.addStretch()
        
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_btn)
        
        layout.addLayout(btn_layout)
        
        # Status bar
        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)
        
        # Audio error handler
        self.media_player.errorOccurred.connect(self._on_audio_error)
    
    def _load_samples(self):
        """Load all samples for this profile from /voices directory."""
        self.samples_list.clear()
        self._sample_data = []
        
        voices_dir = Path(VOICES_DIR)
        if not voices_dir.exists():
            logger.debug(f"Voices directory does not exist: {VOICES_DIR}")
            return
        
        # Find all WAV files matching this profile
        pattern = f"{self.profile_name}*.WAV"
        for wav_path in voices_dir.glob(pattern):
            stem = wav_path.stem
            txt_path = wav_path.with_suffix('.txt')
            
            # Read text if exists
            text = ""
            if txt_path.exists():
                try:
                    text = txt_path.read_text(encoding='utf-8')
                except:
                    pass
            
            self._sample_data.append({
                'stem': stem,
                'wav_path': wav_path,
                'txt_path': txt_path,
                'text': text
            })
        
        # Also check lowercase .wav
        pattern = f"{self.profile_name}*.wav"
        for wav_path in voices_dir.glob(pattern):
            stem = wav_path.stem
            # Check if already added
            if any(s['stem'] == stem for s in self._sample_data):
                continue
            
            txt_path = wav_path.with_suffix('.txt')
            text = ""
            if txt_path.exists():
                try:
                    text = txt_path.read_text(encoding='utf-8')
                except:
                    pass
            
            self._sample_data.append({
                'stem': stem,
                'wav_path': wav_path,
                'txt_path': txt_path,
                'text': text
            })
        
        # Sort by sample number (extract number from stem)
        def get_sample_num(stem):
            match = re.search(r'(\d+)$', stem)
            return int(match.group(1)) if match else 0
        
        self._sample_data.sort(key=lambda x: get_sample_num(x['stem']))
        
        # Populate list
        for sample in self._sample_data:
            # Try to get sample number
            match = re.search(r'(\d+)$', sample['stem'])
            label = f"Sample {match.group(1) if match else '?'}: {sample['stem']}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, sample['stem'])
            self.samples_list.addItem(item)
        
        logger.debug(f"Loaded {len(self._sample_data)} samples for profile {self.profile_name}")
        self.status_label.setText(f"Loaded {len(self._sample_data)} samples")
    
    def _on_sample_selected(self, current: QListWidgetItem, previous):
        """Handle sample selection."""
        if current is None:
            self.play_btn.setEnabled(False)
            self.text_edit.setEnabled(False)
            self.delete_btn.setEnabled(False)
            self._current_sample = None
            return
        
        stem = current.data(Qt.ItemDataRole.UserRole)
        sample = next((s for s in self._sample_data if s['stem'] == stem), None)
        
        if sample:
            self._current_sample = sample
            self.play_btn.setEnabled(True)
            self.text_edit.setEnabled(True)
            self.delete_btn.setEnabled(True)
            
            # Load text
            self.text_edit.blockSignals(True)
            self.text_edit.setPlainText(sample['text'])
            self.text_edit.blockSignals(False)
    
    def _play_selected_sample(self):
        """Play the selected sample."""
        if not self._current_sample:
            return
        
        wav_path = self._current_sample['wav_path']
        if not wav_path.exists():
            self.status_label.setText(f"⚠️ Audio file not found: {wav_path.name}")
            return
        
        self.media_player.stop()
        self.audio_output.setVolume(0.7)
        url = QUrl.fromLocalFile(str(wav_path.absolute()))
        self.media_player.setSource(url)
        self.media_player.play()
        self.status_label.setText(f"🔊 Playing: {wav_path.name}")
    
    def _on_text_changed(self):
        """Save text when it changes."""
        if not self._current_sample:
            return
        
        new_text = self.text_edit.toPlainText()
        sample = self._current_sample
        
        if new_text != sample['text']:
            sample['text'] = new_text
            try:
                sample['txt_path'].write_text(new_text, encoding='utf-8')
                self.status_label.setText(f"💾 Saved text for {sample['stem']}")
                logger.debug(f"Saved text for sample: {sample['stem']}")
            except Exception as e:
                self.status_label.setText(f"❌ Error saving text: {e}")
                logger.error(f"Error saving text for {sample['stem']}: {e}")

    def _get_audio_duration(self, audio_path: Path) -> Optional[float]:
        """Get duration of audio file using ffmpeg."""
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
    
    def _add_sample(self):
        """Add a new sample to the profile."""
        # Ask user for file
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Audio File",
            "",
            "Audio Files (*.wav *.mp3 *.flac *.aac *.m4a);;All Files (*.*)"
        )
        
        if not file_path:
            return
        
        input_path = Path(file_path)
        logger.info(f"Adding sample from: {file_path}")
        
        # Check duration and warn
        duration = self._get_audio_duration(input_path)
        if duration and duration > MAX_DURATION:
            logger.warning(f"Audio too long: {duration:.1f}s (max: {MAX_DURATION}s)")
            QMessageBox.warning(
                self,
                "Audio Too Long",
                f"The audio file is {duration:.1f}s long.\n\n"
                f"VoiceBox has a maximum duration of {MAX_DURATION}s.\n"
                "The audio will be uploaded but may be truncated or rejected by the server.\n\n"
                "Consider trimming it manually in an audio editor."
            )
        
        # Determine next sample number
        max_num = 0
        for sample in self._sample_data:
            match = re.search(r'(\d+)$', sample['stem'])
            if match:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num
        
        next_num = max_num + 1
        stem = f"{self.profile_name} {next_num}"
        output_wav = Path(VOICES_DIR) / f"{stem}.WAV"
        output_txt = Path(VOICES_DIR) / f"{stem}.txt"
        
        # Create voices directory if it doesn't exist
        Path(VOICES_DIR).mkdir(parents=True, exist_ok=True)
        
        # Convert to Ogg Vorbis
        self.status_label.setText(f"🔄 Converting audio...")
        QApplication.processEvents()
        
        if not convert_to_ogg(input_path, output_wav, OGG_QUALITY):
            logger.error(f"Failed to convert audio: {input_path}")
            QMessageBox.warning(self, "Conversion Error", 
                "Failed to convert audio file. Please ensure ffmpeg is installed.")
            return
        
        # Create empty text file
        try:
            output_txt.write_text("", encoding='utf-8')
        except:
            pass
        
        # Add to sample data
        self._sample_data.append({
            'stem': stem,
            'wav_path': output_wav,
            'txt_path': output_txt,
            'text': ""
        })
        
        logger.info(f"Added sample: {stem}")
        
        # Refresh list
        self._load_samples()
        self.status_label.setText(f"✅ Added sample: {stem}")
        
        # Select the new sample
        for i in range(self.samples_list.count()):
            item = self.samples_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == stem:
                self.samples_list.setCurrentItem(item)
                break
    
    def _delete_sample(self):
        """Delete the selected sample with warnings."""
        if not self._current_sample:
            return
        
        sample = self._current_sample
        stem = sample['stem']
        
        # Warning dialog
        reply = QMessageBox.warning(
            self,
            "Delete Sample",
            f"Are you sure you want to delete sample '{stem}'?\n\n"
            "This will delete the WAV and TXT files permanently.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        # Double-check with user
        reply2 = QMessageBox.warning(
            self,
            "Final Confirmation",
            f"⚠️ This action cannot be undone!\n\n"
            f"Delete '{stem}.WAV' and '{stem}.txt'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply2 != QMessageBox.StandardButton.Yes:
            return
        
        # Delete files
        try:
            if sample['wav_path'].exists():
                sample['wav_path'].unlink()
            if sample['txt_path'].exists():
                sample['txt_path'].unlink()
            
            # Remove from list
            self._sample_data = [s for s in self._sample_data if s['stem'] != stem]
            
            # Refresh list
            self._load_samples()
            self.status_label.setText(f"🗑️ Deleted sample: {stem}")
            logger.info(f"Deleted sample: {stem}")
            
            # Clear current sample
            self._current_sample = None
            
        except Exception as e:
            self.status_label.setText(f"❌ Error deleting: {e}")
            logger.error(f"Error deleting sample {stem}: {e}")
            QMessageBox.warning(self, "Error", f"Failed to delete files: {e}")
    
    def _on_audio_error(self, error):
        """Handle audio playback errors."""
        error_messages = {
            0: "No error",
            1: "Resource error (file not found or inaccessible)",
            2: "Format error (unsupported audio format)",
            3: "Network error",
            4: "Access denied",
            5: "Service missing (media service not available)"
        }
        msg = error_messages.get(error, f"Unknown error code: {error}")
        self.status_label.setText(f"⚠️ Audio error: {msg}")
        logger.warning(f"Audio error: {msg}")
    
    def closeEvent(self, event):
        """Clean up on close."""
        self.media_player.stop()
        logger.info(f"Closed voice profile editor: {self.profile_name}")
        event.accept()


# ============================================================================
# Data loading / saving functions
# ============================================================================

def load_csv(csv_path: str) -> pd.DataFrame:
    logger.info(f"Loading CSV from: {csv_path}")
    try:
        df = pd.read_csv(csv_path)
        logger.info(f"CSV loaded: {len(df)} rows, {len(df.columns)} columns")
        return df
    except Exception as e:
        logger.error(f"Error loading CSV: {e}")
        return pd.DataFrame()


def load_json_files():
    substitutions, gender_substitutions, sys_substitutions = {}, {}, {}
    for path_str, target in [
        (VOICE_SUBSTITUTIONS_FILE, "sub"),
        (VOICE_SUBSTITUTIONS_GENDER_FILE, "gender"),
        (VOICE_SUBSTITUTIONS_SYSNAME_FILE, "sys"),
    ]:
        path = Path(path_str)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if target == "sub":
                    substitutions = data
                elif target == "gender":
                    gender_substitutions = data
                else:
                    sys_substitutions = data
                logger.info(f"Loaded {len(data)} entries from {path_str}")
            except Exception as e:
                logger.error(f"Error loading {path_str}: {e}")
    return substitutions, gender_substitutions, sys_substitutions


def save_json_file(file_path: str, data: Dict) -> bool:
    try:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {file_path} ({len(data)} entries)")
        return True
    except Exception as e:
        logger.error(f"Error saving {file_path}: {e}")
        return False


def clean_redundant_substitutions(substitutions: Dict, existing_voices: Set[str]) -> Dict:
    """Remove redundant substitutions where NPC name equals the voice profile name."""
    # Quick check if any redundant entries exist
    has_redundant = any(npc == voice and voice in existing_voices for npc, voice in substitutions.items())
    if not has_redundant:
        return substitutions  # No changes needed
    
    cleaned = {}
    for npc_name, voice_profile in substitutions.items():
        if npc_name == voice_profile and voice_profile in existing_voices:
            logger.debug(f"Removing redundant substitution: {npc_name} -> {voice_profile}")
            continue
        cleaned[npc_name] = voice_profile
    return cleaned


def get_available_voice_profiles() -> List[str]:
    """Get unique voice profile names (grouping 'Boy 2.wav' as 'Boy')."""
    voices_dir = Path(VOICES_DIR)
    if not voices_dir.exists():
        return []
    
    profiles = set()
    for wav_path in list(voices_dir.glob("*.WAV")) + list(voices_dir.glob("*.wav")):
        base_name = re.sub(r'\s+\d+$', '', wav_path.stem)
        profiles.add(base_name)
    
    logger.debug(f"Found {len(profiles)} unique voice profiles")
    return sorted(profiles)


def get_existing_voice_files() -> Set[str]:
    """Get set of base voice filenames (without number suffixes)."""
    voices_dir = Path(VOICES_DIR)
    if not voices_dir.exists():
        return set()
    
    voices = set()
    for wav_path in list(voices_dir.glob("*.WAV")) + list(voices_dir.glob("*.wav")):
        base_name = re.sub(r'\s+\d+$', '', wav_path.stem)
        voices.add(base_name)
    return voices


def build_hierarchy_for_npc(df: pd.DataFrame, npc_name: str, substitutions: Dict,
                             gender_substitutions: Dict, sys_substitutions: Dict,
                             existing_voices: Set[str]) -> Dict:
    """Build the hierarchy entry for a single NPC."""
    npc_df = df[df["RealName"] == npc_name]
    entry = {
        "assigned_voice": substitutions.get(npc_name),
        "has_existing_voice": npc_name in existing_voices,
        "genders": {},
    }
    gender_groups = npc_df.dropna(subset=["Gender"]).groupby("Gender", sort=False)
    for gender, gender_df in gender_groups:
        if gender == "":
            continue
        gender_key = f"{npc_name}|{gender}"
        entry["genders"][gender] = {
            "assigned_voice": gender_substitutions.get(gender_key),
            "sysnames": [],
        }
        for sysname in gender_df["SystemName"].dropna().unique():
            if sysname == "":
                continue
            entry["genders"][gender]["sysnames"].append({
                "name": sysname,
                "assigned_voice": sys_substitutions.get(sysname),
            })
    return entry


def build_hierarchy(df: pd.DataFrame, substitutions: Dict, gender_substitutions: Dict,
                     sys_substitutions: Dict, existing_voices: Set[str]) -> Dict:
    logger.info("Building NPC hierarchy...")
    start_time = time.time()
    hierarchy = {}
    if df.empty:
        return hierarchy

    df = df.dropna(subset=["RealName"])
    for npc_name, npc_df in df.groupby("RealName", sort=False):
        if npc_name == "":
            continue
        entry = {
            "assigned_voice": substitutions.get(npc_name),
            "has_existing_voice": npc_name in existing_voices,
            "genders": {},
        }
        gender_groups = npc_df.dropna(subset=["Gender"]).groupby("Gender", sort=False)
        for gender, gender_df in gender_groups:
            if gender == "":
                continue
            gender_key = f"{npc_name}|{gender}"
            entry["genders"][gender] = {
                "assigned_voice": gender_substitutions.get(gender_key),
                "sysnames": [],
            }
            for sysname in gender_df["SystemName"].dropna().unique():
                if sysname == "":
                    continue
                entry["genders"][gender]["sysnames"].append({
                    "name": sysname,
                    "assigned_voice": sys_substitutions.get(sysname),
                })
        hierarchy[npc_name] = entry

    logger.info(f"Built hierarchy with {len(hierarchy)} NPCs in {time.time() - start_time:.2f}s")
    return hierarchy


def calculate_line_counts(df: pd.DataFrame, hierarchy: Dict) -> Dict:
    """
    Calculate how many CSV lines each NPC, gender, and system name affects.
    """
    counts = {}
    
    for npc_name, npc_data in hierarchy.items():
        npc_rows = df[df['RealName'] == npc_name]
        total_lines = len(npc_rows)
        
        counts[npc_name] = {
            'total_lines': total_lines,
            'genders': {}
        }
        
        for gender in npc_data.get('genders', {}).keys():
            gender_rows = npc_rows[npc_rows['Gender'] == gender]
            gender_count = len(gender_rows)
            
            counts[npc_name]['genders'][gender] = {
                'total_lines': gender_count,
                'sysnames': {}
            }
            
            for sys in npc_data['genders'][gender].get('sysnames', []):
                sysname = sys['name']
                sys_rows = gender_rows[gender_rows['SystemName'] == sysname]
                sys_count = len(sys_rows)
                
                counts[npc_name]['genders'][gender]['sysnames'][sysname] = sys_count
    
    return counts


# ============================================================================
# Main window
# ============================================================================

class VoiceProfileManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎯 Voice Profile Manager")
        self.resize(1400, 900)

        logger.info("Starting Voice Profile Manager...")

        # --- Load everything once at startup ---
        self.df = load_csv(CSV_PATH)
        self.substitutions, self.gender_substitutions, self.sys_substitutions = load_json_files()
        self.available_voices = get_available_voice_profiles()
        self.existing_voices = get_existing_voice_files()

        if self.df.empty:
            logger.error(f"Could not load CSV file: {CSV_PATH}")
            QMessageBox.critical(self, "Error", f"Could not load CSV file: {CSV_PATH}")

        self.hierarchy = build_hierarchy(
            self.df, self.substitutions, self.gender_substitutions,
            self.sys_substitutions, self.existing_voices,
        )
        
        self.line_counts = calculate_line_counts(self.df, self.hierarchy)

        self.npc_names = sorted(self.hierarchy.keys())
        self.selected_npc: Optional[str] = self.npc_names[0] if self.npc_names else None
        self._filtered_items = []

        self._build_ui()
        self._apply_filters()
        self._populate_npc_list()
        if self.npc_list.count() > 0:
            self.npc_list.setCurrentRow(0)        
        if self.selected_npc:
            self._select_npc_in_list(self.selected_npc)

        logger.info(f"Initialization complete: {len(self.npc_names)} NPCs loaded")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer_layout = QHBoxLayout(central)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer_layout.addWidget(splitter)

        # --- Left: stats + search + NPC list ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)

        stats_box = QGroupBox("📊 Stats")
        stats_layout = QFormLayout(stats_box)
        self.stats_total_label = QLabel()
        self.stats_voices_label = QLabel()
        self.stats_existing_label = QLabel()
        self.stats_npc_level_label = QLabel()
        self.stats_gender_level_label = QLabel()
        self.stats_sys_level_label = QLabel()
        stats_layout.addRow("Total NPCs:", self.stats_total_label)
        stats_layout.addRow("Available Voices:", self.stats_voices_label)
        stats_layout.addRow("NPCs with Voice Files:", self.stats_existing_label)
        stats_layout.addRow("NPC-level assignments:", self.stats_npc_level_label)
        stats_layout.addRow("Gender-level assignments:", self.stats_gender_level_label)
        stats_layout.addRow("SysName-level assignments:", self.stats_sys_level_label)
        left_layout.addWidget(stats_box)

        left_layout.addWidget(QLabel("📋 NPC List"))

        # Search box
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("🔍 Type to filter...")
        self.search_box.textChanged.connect(self._on_filter_changed)
        left_layout.addWidget(self.search_box)

        # Sorting and Filtering Controls
        controls_layout = QHBoxLayout()
        
        controls_layout.addWidget(QLabel("Sort:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Alphabetical", "By Lines"])
        self.sort_combo.setCurrentIndex(1)
        self.sort_combo.currentTextChanged.connect(self._on_filter_changed)
        controls_layout.addWidget(self.sort_combo)
        
        controls_layout.addWidget(QLabel("Filter:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All", "Modified", "Missing", "Has Voice File"])
        self.filter_combo.setCurrentIndex(2)
        self.filter_combo.currentTextChanged.connect(self._on_filter_changed)
        controls_layout.addWidget(self.filter_combo)
        
        left_layout.addLayout(controls_layout)

        # NPC count
        self.npc_count_label = QLabel()
        left_layout.addWidget(self.npc_count_label)

        self.npc_list = QListWidget()
        self.npc_list.currentItemChanged.connect(self._on_npc_selected)
        left_layout.addWidget(self.npc_list, stretch=1)

        splitter.addWidget(left_widget)

        # --- Right: detail panel ---
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_content = QWidget()
        self.detail_layout = QVBoxLayout(self.detail_content)
        self.detail_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.detail_scroll.setWidget(self.detail_content)
        splitter.addWidget(self.detail_scroll)

        splitter.setSizes([400, 1000])

        self.setStatusBar(QStatusBar())
        self._update_stats()

    # ------------------------------------------------------------------
    # NPC list handling
    # ------------------------------------------------------------------
    def _get_coverage_status(self, npc_name: str) -> Dict:
        """
        Get coverage status for an NPC.
        
        Returns:
            Dict with:
                - total_lines: Total number of lines
                - covered_lines: Number of lines with voice assignment
                - has_existing: Whether voice file exists for NPC name
                - is_fully_covered: Whether all lines are covered
                - has_partial: Whether some lines are covered (but not all)
        """
        data = self.hierarchy[npc_name]
        has_existing = data.get("has_existing_voice", False)
        
        npc_rows = self.df[self.df['RealName'] == npc_name]
        total_lines = len(npc_rows)
        
        # If NPC has a voice file, it's fully covered
        if has_existing:
            return {
                'total_lines': total_lines,
                'covered_lines': total_lines,
                'has_existing': True,
                'is_fully_covered': True,
                'has_partial': False
            }
        
        covered_lines = 0
        
        # Check if NPC has genders
        if data["genders"]:
            for gender, gender_data in data["genders"].items():
                gender_rows = npc_rows[npc_rows['Gender'] == gender]
                gender_total = len(gender_rows)
                
                # Check if this gender has sysnames
                if gender_data["sysnames"]:
                    # Count sysname-level assignments
                    gender_covered = 0
                    for sys in gender_data["sysnames"]:
                        sys_rows = gender_rows[gender_rows['SystemName'] == sys['name']]
                        if sys["assigned_voice"] is not None:
                            gender_covered += len(sys_rows)
                    
                    # Also check if gender has an assignment (covers ALL sysnames for this gender)
                    # This is the key fix!
                    if gender_data["assigned_voice"] is not None:
                        gender_covered = gender_total
                    
                    covered_lines += gender_covered
                else:
                    # No sysnames - check gender level
                    if gender_data["assigned_voice"] is not None:
                        covered_lines += gender_total
        else:
            # No genders - check NPC level
            if data["assigned_voice"] is not None:
                covered_lines = total_lines
        
        return {
            'total_lines': total_lines,
            'covered_lines': covered_lines,
            'has_existing': False,
            'is_fully_covered': covered_lines == total_lines and total_lines > 0,
            'has_partial': 0 < covered_lines < total_lines
        }

    def _npc_icon(self, npc_name: str) -> str:
        status = self._get_coverage_status(npc_name)
        if status['has_existing']:
            return "🟢"
        elif status['is_fully_covered']:
            return "✅"
        elif status['has_partial']:
            return "🔵"
        return "🔴"

    def _apply_filters(self):
        """
        Apply all filters (search, status filter) to determine which NPCs to show.
        """
        search_term = self.search_box.text().strip().lower()
        status_filter = self.filter_combo.currentText()
        sort_by = "alphabetical" if self.sort_combo.currentText() == "Alphabetical" else "lines"
        
        self._filtered_items = []
        
        for name in self.npc_names:
            if search_term and search_term not in name.lower():
                continue
            
            data = self.hierarchy[name]
            has_existing = data.get("has_existing_voice", False)
            has_assignments = (
                data["assigned_voice"] is not None
                or any(g["assigned_voice"] is not None for g in data["genders"].values())
                or any(
                    s["assigned_voice"] is not None
                    for g in data["genders"].values()
                    for s in g["sysnames"]
                )
            )
            
            if status_filter == "Modified" and not has_assignments:
                continue
            elif status_filter == "Missing" and (has_assignments or has_existing):
                continue
            elif status_filter == "Has Voice File" and not has_existing:
                continue
            
            line_count = self.line_counts.get(name, {}).get('total_lines', 0)
            self._filtered_items.append((name, line_count))
        
        if sort_by == "alphabetical":
            self._filtered_items.sort(key=lambda x: x[0].lower())
        else:
            self._filtered_items.sort(key=lambda x: x[1], reverse=True)
        
        logger.debug(f"Filter applied: {len(self._filtered_items)} NPCs visible")

    def _populate_npc_list(self):
        """Populate the NPC list with filtered and sorted items."""
        self.npc_list.blockSignals(True)
        self.npc_list.clear()
        
        items = self._filtered_items if self._filtered_items else [(name, 0) for name in self.npc_names]
        
        for name, line_count in items:
            data = self.hierarchy[name]
            has_existing = data.get("has_existing_voice", False)
            has_assignments = (
                data["assigned_voice"] is not None
                or any(g["assigned_voice"] is not None for g in data["genders"].values())
                or any(
                    s["assigned_voice"] is not None
                    for g in data["genders"].values()
                    for s in g["sysnames"]
                )
            )
            if has_existing:
                icon = "🟢"
            elif has_assignments:
                icon = "✅"
            else:
                icon = "🔴"
            
            item_text = f"{icon} {name} [{line_count} lines]"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.npc_list.addItem(item)

        self.npc_count_label.setText(f"Showing {len(items)} of {len(self.npc_names)} NPCs")
        self.npc_list.blockSignals(False)

    def _refresh_npc_list_icon(self, npc_name: str):
        """Update the icon for a single NPC in the list."""
        for i in range(self.npc_list.count()):
            item = self.npc_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == npc_name:
                line_count = self.line_counts.get(npc_name, {}).get('total_lines', 0)
                icon = self._npc_icon(npc_name)
                item.setText(f"{icon} {npc_name} [{line_count} lines]")
                return

    def _select_npc_in_list(self, npc_name: str):
        for i in range(self.npc_list.count()):
            item = self.npc_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == npc_name:
                self.npc_list.setCurrentItem(item)
                return
        self.selected_npc = npc_name
        self._render_detail_panel()

    def _on_filter_changed(self, text: Optional[str] = None):
        """Handle changes to search, sort, or filter controls."""
        self._apply_filters()
        self._populate_npc_list()
        
        if self.selected_npc:
            visible_names = [name for name, _ in self._filtered_items]
            if self.selected_npc not in visible_names:
                self.selected_npc = None
                self._render_detail_panel()
                if hasattr(self, 'npc_title'):
                    self.npc_title.setText("<h2>Select an NPC</h2>")
        
        if self.npc_list.count() > 0 and not self.selected_npc:
            self.npc_list.setCurrentRow(0)

    def _on_npc_selected(self, current: QListWidgetItem, previous: QListWidgetItem):
        if current is None:
            return
        self.selected_npc = current.data(Qt.ItemDataRole.UserRole)
        self._render_detail_panel()

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------
    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            else:
                sub_layout = item.layout()
                if sub_layout is not None:
                    self._clear_layout(sub_layout)

    def _make_voice_combo_with_editor(self, current_voice: Optional[str], on_change, profile_name: str) -> QWidget:
        """
        Create a combo box with an Edit/Create button next to it.
        
        Returns a widget containing the combo and button.
        """
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        
        # Combo box
        combo = QComboBox()
        options = [""] + self.available_voices
        combo.addItems(options)
        current_voice = current_voice or ""
        combo.blockSignals(True)
        if current_voice in options:
            combo.setCurrentIndex(options.index(current_voice))
        combo.blockSignals(False)
        combo.currentTextChanged.connect(on_change)
        layout.addWidget(combo, stretch=1)
        
        # Store reference to combo for the button callback
        setattr(container, 'combo', combo)
        
        # Edit/Create button
        has_voice = current_voice in self.existing_voices
        btn_text = "✏️" if has_voice else "➕"
        btn_tooltip = f"Edit '{current_voice or 'new'}' profile" if has_voice else f"Create '{combo.currentText() or 'new'}' profile"
        
        btn = QPushButton(btn_text)
        btn.setFixedWidth(30)
        btn.setToolTip(btn_tooltip)
        
        # Capture combo reference for the lambda
        def on_btn_clicked():
            # Get the current text from the combo
            current_text = combo.currentText() or profile_name
            # Open the editor
            self._open_profile_editor(current_text)
            # After editor closes, update the combo to select the profile if it exists
            if current_text in self.available_voices:
                combo.blockSignals(True)
                combo.setCurrentText(current_text)
                combo.blockSignals(False)
                # Trigger the change event
                on_change(current_text)
        
        btn.clicked.connect(on_btn_clicked)
        layout.addWidget(btn)
        
        # Store reference to button
        setattr(container, 'btn', btn)
        
        return container
    
    def _open_profile_editor(self, profile_name: str):
        """Open the voice profile editor for the given profile name."""
        if not profile_name:
            profile_name = "New Profile"
        
        editor = VoiceProfileEditor(profile_name, self)
        editor.exec()
        
        # After editor closes, refresh available voices
        self.available_voices = get_available_voice_profiles()
        self.existing_voices = get_existing_voice_files()
        
        # Update the current NPC's has_existing_voice flag
        if self.selected_npc:
            # Refresh the NPC entry to update has_existing_voice
            self.hierarchy[self.selected_npc] = build_hierarchy_for_npc(
                self.df, self.selected_npc, self.substitutions, self.gender_substitutions,
                self.sys_substitutions, self.existing_voices,
            )
            # Update line counts for this NPC
            self._update_line_counts_for_npc(self.selected_npc)
            # Update the list icon
            self._refresh_npc_list_icon(self.selected_npc)
        
        # Update stats (Available Voices count)
        self._update_stats()
        
        # Re-render detail panel to update comboboxes and voice file message
        self._render_detail_panel()

    def _render_detail_panel(self):
        self._clear_layout(self.detail_layout)

        if not self.selected_npc:
            self.detail_layout.addWidget(QLabel("ℹ️ No NPC selected."))
            return

        npc_name = self.selected_npc
        npc_data = self.hierarchy[npc_name]
        npc_count = self.line_counts.get(npc_name, {})
        
        has_existing = npc_data.get("has_existing_voice", False)

        icon = self._npc_icon(npc_name)

        # --- Header ---
        header_frame = QFrame()
        header_frame.setFrameShape(QFrame.Shape.StyledPanel)
        header_layout = QHBoxLayout(header_frame)

        title_box = QVBoxLayout()
        total_lines = npc_count.get('total_lines', 0)
        title_label = QLabel(f"<h2>{icon} {npc_name}</h2>")
        title_label2 = QLabel(f"<i>Affects {total_lines} CSV lines</i>")
        title_box.addWidget(title_label)
        title_box.addWidget(title_label2)
        self.npc_title = title_label
        header_layout.addLayout(title_box, stretch=1)

        copy_btn = QPushButton("📋 Copy Name")
        copy_btn.clicked.connect(lambda: self._copy_name(npc_name))
        header_layout.addWidget(copy_btn)

        self.detail_layout.addWidget(header_frame)

        if has_existing:
            self.detail_layout.addWidget(
                QLabel(f"🎵 Voice file exists: <code>{npc_name}.WAV</code> in /{VOICES_DIR} directory")
            )

        self.detail_layout.addWidget(QLabel("---"))

        # --- NPC level (always shown) ---
        npc_group = QGroupBox(f"📌 NPC Level Assignment ({total_lines} lines)")
        npc_form = QFormLayout(npc_group)
        
        # Use the NPC name as the profile name for the editor
        # This ensures the edit button appears even if the profile exists as a file
        npc_container = self._make_voice_combo_with_editor(
            npc_data["assigned_voice"] or npc_name,  # Pass NPC name as default
            lambda text: self._on_npc_voice_changed(npc_name, text),
            npc_name
        )
        npc_form.addRow("Voice:", npc_container)
        self.detail_layout.addWidget(npc_group)

        # --- Gender level (shown if genders exist) ---
        if npc_data["genders"]:
            gender_group = QGroupBox("🚻 Gender Overrides")
            gender_layout = QVBoxLayout(gender_group)

            # First, render all gender comboboxes
            for gender, gender_data in npc_data["genders"].items():
                gender_count = npc_count.get('genders', {}).get(gender, {}).get('total_lines', 0)
                gform = QFormLayout()
                
                gender_container = self._make_voice_combo_with_editor(
                    gender_data["assigned_voice"],
                    lambda text, g=gender: self._on_gender_voice_changed(npc_name, g, text),
                    f"{npc_name}_{gender}"
                )
                gform.addRow(f"Voice for {gender} ({gender_count} lines):", gender_container)
                gender_layout.addLayout(gform)

            # Then, render all system name groups (one per gender)
            for gender, gender_data in npc_data["genders"].items():
                if gender_data["sysnames"]:
                    sys_group = QGroupBox(f"📋 System Name Overrides ({gender})")
                    sys_form = QFormLayout(sys_group)
                    
                    # Sort sysnames by line count (descending)
                    sorted_sysnames = sorted(
                        gender_data["sysnames"],
                        key=lambda s: npc_count.get('genders', {}).get(gender, {}).get('sysnames', {}).get(s["name"], 0),
                        reverse=True
                    )
                    
                    for sys in sorted_sysnames:
                        sysname = sys["name"]
                        sys_count = npc_count.get('genders', {}).get(gender, {}).get('sysnames', {}).get(sysname, 0)
                        
                        sys_container = self._make_voice_combo_with_editor(
                            sys["assigned_voice"],
                            lambda text, s=sysname: self._on_sys_voice_changed(s, text),
                            sysname
                        )
                        sys_form.addRow(f"{sysname} ({sys_count} lines):", sys_container)
                    gender_layout.addWidget(sys_group)

                if gender_data["sysnames"] and list(npc_data["genders"].keys())[-1] != gender:
                    line = QFrame()
                    line.setFrameShape(QFrame.Shape.HLine)
                    gender_layout.addWidget(line)

            self.detail_layout.addWidget(gender_group)

    def _copy_name(self, npc_name: str):
        QApplication.clipboard().setText(npc_name)
        self.statusBar().showMessage(f"Copied '{npc_name}' to clipboard!", 3000)

    # ------------------------------------------------------------------
    # Change handlers
    # ------------------------------------------------------------------
    def _refresh_npc_entry(self, npc_name: str):
        """Refresh the hierarchy entry for a single NPC (fast)."""
        self.hierarchy[npc_name] = build_hierarchy_for_npc(
            self.df, npc_name, self.substitutions, self.gender_substitutions,
            self.sys_substitutions, self.existing_voices,
        )
        # Update line counts for this NPC only
        self._update_line_counts_for_npc(npc_name)
        logger.debug(f"Refreshed NPC entry: {npc_name}")
    
    def _update_line_counts_for_npc(self, npc_name: str):
        """Update line counts for a single NPC only (fast)."""
        npc_rows = self.df[self.df['RealName'] == npc_name]
        total_lines = len(npc_rows)
        
        self.line_counts[npc_name] = {
            'total_lines': total_lines,
            'genders': {}
        }
        
        # Get the NPC data from hierarchy
        npc_data = self.hierarchy[npc_name]
        for gender in npc_data.get('genders', {}).keys():
            gender_rows = npc_rows[npc_rows['Gender'] == gender]
            gender_count = len(gender_rows)
            
            self.line_counts[npc_name]['genders'][gender] = {
                'total_lines': gender_count,
                'sysnames': {}
            }
            
            for sys in npc_data['genders'][gender].get('sysnames', []):
                sysname = sys['name']
                sys_rows = gender_rows[gender_rows['SystemName'] == sysname]
                sys_count = len(sys_rows)
                self.line_counts[npc_name]['genders'][gender]['sysnames'][sysname] = sys_count

    def _on_npc_voice_changed(self, npc_name: str, new_voice: str):
        current = self.substitutions.get(npc_name) or ""
        if new_voice == current:
            return
        logger.info(f"Changing NPC voice: {npc_name} -> {new_voice or 'unassigned'}")
        if new_voice:
            self.substitutions[npc_name] = new_voice
        else:
            self.substitutions.pop(npc_name, None)
        
        # Clean redundant substitutions before saving
        cleaned_substitutions = clean_redundant_substitutions(self.substitutions, self.existing_voices)
        self.substitutions = cleaned_substitutions
        
        if save_json_file(VOICE_SUBSTITUTIONS_FILE, self.substitutions):
            # Update hierarchy
            self._refresh_npc_entry(npc_name)
            self._update_stats()
            # Update ONLY the changed NPC's icon (fast!)
            self._refresh_npc_list_icon(npc_name)
            self.statusBar().showMessage(f"✅ Updated {npc_name} → {new_voice or 'unassigned'}", 3000)

    def _on_gender_voice_changed(self, npc_name: str, gender: str, new_voice: str):
        gender_key = f"{npc_name}|{gender}"
        current = self.gender_substitutions.get(gender_key) or ""
        if new_voice == current:
            return
        logger.info(f"Changing gender voice: {gender_key} -> {new_voice or 'unassigned'}")
        if new_voice:
            self.gender_substitutions[gender_key] = new_voice
        else:
            self.gender_substitutions.pop(gender_key, None)
        if save_json_file(VOICE_SUBSTITUTIONS_GENDER_FILE, self.gender_substitutions):
            self._refresh_npc_entry(npc_name)
            self._update_stats()
            self._refresh_npc_list_icon(npc_name)  # Target only this NPC
            self.statusBar().showMessage(f"✅ Updated {npc_name}|{gender} → {new_voice or 'unassigned'}", 3000)

    def _on_sys_voice_changed(self, sysname: str, new_voice: str):
        current = self.sys_substitutions.get(sysname) or ""
        if new_voice == current:
            return
        logger.info(f"Changing system voice: {sysname} -> {new_voice or 'unassigned'}")
        if new_voice:
            self.sys_substitutions[sysname] = new_voice
        else:
            self.sys_substitutions.pop(sysname, None)
        if save_json_file(VOICE_SUBSTITUTIONS_SYSNAME_FILE, self.sys_substitutions):
            if self.selected_npc is not None:
                self._refresh_npc_entry(self.selected_npc)
                self._refresh_npc_list_icon(self.selected_npc)  # Target only this NPC
            self._update_stats()
            self.statusBar().showMessage(f"✅ Updated {sysname} → {new_voice or 'unassigned'}", 3000)

    def _update_stats(self):
        npcs_with_voice = sum(1 for d in self.hierarchy.values() if d.get("has_existing_voice", False))
        
        self.stats_total_label.setText(str(len(self.hierarchy)))
        self.stats_voices_label.setText(str(len(self.available_voices)))
        self.stats_existing_label.setText(str(npcs_with_voice))
        self.stats_npc_level_label.setText(str(len(self.substitutions)))
        self.stats_gender_level_label.setText(str(len(self.gender_substitutions)))
        self.stats_sys_level_label.setText(str(len(self.sys_substitutions)))


def main():
    os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.multimedia.*=false"

    logger.info("=" * 60)
    logger.info("Voice Profile Manager started")
    logger.info("=" * 60)
    
    app = QApplication(sys.argv)
    window = VoiceProfileManager()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()