"""
Voice Profile Manager (PySide6 desktop version)

A native desktop GUI for managing voice profile assignments across three levels:
- NPC Level: Assign a voice to an entire NPC
- Gender Level: Override voice for a specific gender (M/F)
- System Name Level: Override voice for specific system names

The hierarchy is: System Name > Gender > NPC > Existing Voice File

This tool is part of the Infinity Engine Voice Generation toolchain:
1. profiles-prepare.py - Extract source samples from game files
2. profiles-audit.py - Review and approve samples
3. profiles-manage.py - Assign voices to NPCs (THIS TOOL)
4. profiles-upload.py - Upload profiles to VoiceBox
5. generate.py - Generate TTS audio

Usage:
    python profiles-manage.py
"""

import os
import sys
import json
import time
import re
import subprocess
import logging
import requests
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem, QLineEdit, QLabel, QPushButton, QComboBox,
    QScrollArea, QSplitter, QGroupBox, QFrame, QMessageBox, QStatusBar,
    QTextEdit, QDialog, QProgressBar, QFileDialog,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

# ============================================================================
# Configuration
# ============================================================================

# CSV filtering
FILENAME_PATTERN = r"^TS"      # regex pattern for filename (column 6)

# Audio settings
OGG_QUALITY = 4                # Vorbis quality (0-10, 4 = good quality/size balance)
MAX_DURATION = 30.0            # Maximum duration in seconds for a single sample

# File paths (relative to script directory)
CSV_PATH = "dialog-report.csv"
VOICES_DIR = "voices"
VOICE_SUBSTITUTIONS_FILE = "voice-substitutions.json"
LOG_FILE_PATH = Path("logs/profiles-manage.log")

# ============================================================================
# Logging Setup
# ============================================================================

# Ensure log directory exists
LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Configure logging to both file and console
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
        output_path: Path to output Ogg file (will be overwritten if exists)
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
    
    Allows users to:
        - View all samples for a voice profile
        - Play audio samples
        - Edit transcript text for each sample
        - Add new samples (converts to Ogg Vorbis)
        - Delete existing samples (with confirmation)
    
    Samples are stored in /voices/ directory with naming:
        - {profile_name} 1.WAV (first sample)
        - {profile_name} 2.WAV (second sample)
        - etc.
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
        """Build the editor UI."""
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
        
        self.add_btn = QPushButton("➕ Add Sample (File)")
        self.add_btn.clicked.connect(self._add_sample)
        btn_layout.addWidget(self.add_btn)
        
        self.url_btn = QPushButton("🌐 Add Sample (URL)")
        self.url_btn.clicked.connect(self._add_sample_from_url)
        btn_layout.addWidget(self.url_btn)

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
        """
        Load all samples for this profile from /voices directory.
        
        Finds all WAV files matching the profile name pattern and loads
        their corresponding TXT files. Files are sorted by sample number.
        Also displays audio duration for each sample.
        """
        self.samples_list.clear()
        self._sample_data = []
        
        voices_dir = Path(VOICES_DIR)
        if not voices_dir.exists():
            logger.debug(f"Voices directory does not exist: {VOICES_DIR}")
            return
        
        # Build a regex pattern to match ONLY this profile's files
        escaped_name = re.escape(self.profile_name)
        pattern = re.compile(rf'^{escaped_name}(?: \d+)?$', re.IGNORECASE)
        
        # Use a set to avoid duplicates
        unique_files = set()
        
        # Find all WAV files (case-insensitive on Windows)
        for wav_path in voices_dir.glob("*.wav"):
            unique_files.add(wav_path)
        for wav_path in voices_dir.glob("*.WAV"):
            unique_files.add(wav_path)
        
        for wav_path in unique_files:
            stem = wav_path.stem
            
            # Check if this file belongs to this profile using the regex
            if not pattern.match(stem):
                continue
            
            txt_path = wav_path.with_suffix('.txt')
            
            # Read text if exists
            text = ""
            if txt_path.exists():
                try:
                    text = txt_path.read_text(encoding='utf-8')
                except:
                    pass
            
            # Get audio duration
            duration = self._get_audio_duration(wav_path)
            
            self._sample_data.append({
                'stem': stem,
                'wav_path': wav_path,
                'txt_path': txt_path,
                'text': text,
                'duration': duration
            })
        
        # Sort by sample number (extract number from stem)
        def get_sample_num(stem):
            match = re.search(r'(\d+)$', stem)
            return int(match.group(1)) if match else 0
        
        self._sample_data.sort(key=lambda x: get_sample_num(x['stem']))
        
        # Populate list with duration info
        for sample in self._sample_data:
            # Try to get sample number
            match = re.search(r'(\d+)$', sample['stem'])
            label = f"Sample {match.group(1) if match else '?'}: {sample['stem']}"
            
            # Add duration if available
            if sample['duration'] is not None:
                duration = sample['duration']
                if duration >= 60:
                    minutes = int(duration // 60)
                    seconds = int(duration % 60)
                    duration_str = f"{minutes}:{seconds:02d}"
                else:
                    duration_str = f"{duration:.1f}s"
                label += f" [{duration_str}]"
            
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, sample['stem'])
            self.samples_list.addItem(item)
        
        logger.debug(f"Loaded {len(self._sample_data)} samples for profile {self.profile_name}")
        self.status_label.setText(f"Loaded {len(self._sample_data)} samples")    

    def _on_sample_selected(self, current: QListWidgetItem, previous):
        """Handle sample selection from the list."""
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
        """Play the selected sample using Qt Multimedia."""
        if not self._current_sample:
            return
        
        wav_path = self._current_sample['wav_path']
        if not wav_path.exists():
            self.status_label.setText(f"⚠️ Audio file not found: {wav_path.name}")
            return
        
        # Stop any existing playback first
        self.media_player.stop()
        # Ensure the previous source is cleared
        self.media_player.setSource(QUrl())
        QApplication.processEvents()
        
        self.audio_output.setVolume(0.7)
        url = QUrl.fromLocalFile(str(wav_path.absolute()))
        self.media_player.setSource(url)
        self.media_player.play()
        self.status_label.setText(f"🔊 Playing: {wav_path.name}")

    def _on_text_changed(self):
        """Auto-save text when it changes."""
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
        """Get duration of audio file using ffprobe."""
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

    def _import_audio_file(self, input_path: Path):
        """Import a local audio file (from disk) as a new sample."""
        logger.info(f"Importing sample from: {input_path}")
        
        # Check duration and warn
        duration = self._get_audio_duration(input_path)
        if duration and duration > MAX_DURATION:
            logger.warning(f"Audio too long: {duration:.1f}s (max: {MAX_DURATION}s)")
            reply = QMessageBox.warning(
                self,
                "Audio Too Long",
                f"The audio file is {duration:.1f}s long.\n\n"
                f"VoiceBox has a maximum duration of {MAX_DURATION}s.\n"
                "The audio will be uploaded but may be truncated or rejected by the server.\n\n"
                "Consider trimming it manually in an audio editor.\n\n"
                "Do you want to continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

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
        
        # Get duration of the converted file
        duration = self._get_audio_duration(output_wav)
        
        # Add to sample data
        self._sample_data.append({
            'stem': stem,
            'wav_path': output_wav,
            'txt_path': output_txt,
            'text': "",
            'duration': duration
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

    def _import_downloaded_file(self, temp_path: Path):
        """Import a downloaded temporary file, then clean up."""
        try:
            # Use the main import function
            self._import_audio_file(temp_path)
        finally:
            # Clean up the temporary file
            try:
                if temp_path.exists():
                    temp_path.unlink()
                    logger.debug(f"Removed temporary file: {temp_path}")
            except Exception as e:
                logger.warning(f"Could not remove temporary file {temp_path}: {e}")       

    def _normalize_escaping(self, raw: str) -> str:
        """
        Fish Audio's Next.js page streams data via self.__next_f.push(...) calls,
        where the payload is JSON-encoded as a JS string literal -- and some
        fields are nested an extra level deep (JSON-within-JSON), so the number
        of backslashes escaping a given quote varies field to field (\" vs \\").
        Collapsing all runs of backslashes-before-a-quote, then decoding \\uXXXX
        escapes, normalizes everything to plain JSON-like text regexes can match.
        """
        cleaned = re.sub(r'\\+(?=")', '', raw)
        cleaned = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), cleaned)
        return cleaned


    def _parse_fish_audio_page(self, url: str) -> Optional[Dict[str, str]]:
        """
        Fetch a Fish Audio model page and extract the default sample's audio
        URL and text, straight from the embedded (escaped) JSON -- no browser
        required.

        Returns {'audio_url': ..., 'text': ...} or None if this voice has no
        sample (page will contain "no audio samples yet") or parsing fails.
        """
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            }
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            raw = response.text

            cleaned = self._normalize_escaping(raw)

            # Anchor on the "samples" block that pairs title/text/audio together,
            # rather than matching "text" or "audio" keys in isolation -- those
            # keys appear elsewhere on the page (related voices, marketing copy,
            # other sample sets like "s2/sample-N.mp3") and a loose match can grab
            # the wrong one.
            sample_match = re.search(
                r'"title":"Default Sample","text":"(?P<text>[^"]+?)"'
                r'.*?"audio":"(?P<audio>https://[^"]+?\.mp3\?[^"]+?)"',
                cleaned,
            )

            if sample_match:
                return {
                    "audio_url": sample_match.group("audio"),
                    "text": sample_match.group("text"),
                }

            # Only trust the page's "no samples" UI message once the real JSON
            # block genuinely isn't there -- that phrase also appears as a
            # translation-string constant even on pages that DO have a sample,
            # so checking it first can produce false negatives.
            if "no audio samples yet" in raw.lower():
                logger.info("Page reports no audio samples for this voice.")
            else:
                logger.warning("Could not find the Default Sample block, and no explicit 'no samples' message either.")
            return None
            
        except Exception as e:
            logger.exception(f"Error parsing Fish Audio page: {e}")
            return None
    
    def _download_from_url(self, url: str):
        """Download audio from a URL and add it as a sample."""
        self.status_label.setText(f"⬇️ Downloading from URL...")
        QApplication.processEvents()

        try:
            # Download the file with a timeout
            response = requests.get(url, timeout=30, stream=True)
            response.raise_for_status()

            # Determine file extension from Content-Type or URL
            content_type = response.headers.get('content-type', '')
            ext = '.mp3'  # default
            if 'audio/wav' in content_type or 'audio/x-wav' in content_type:
                ext = '.wav'
            elif 'audio/flac' in content_type or 'audio/x-flac' in content_type:
                ext = '.flac'
            elif 'audio/mpeg' in content_type:
                ext = '.mp3'
            elif 'audio/m4a' in content_type:
                ext = '.m4a'
            elif 'audio/aac' in content_type:
                ext = '.aac'
            elif 'audio/ogg' in content_type:
                ext = '.ogg'
            # Could also try to get from URL path
            else:
                # Try to get extension from URL
                url_path = url.split('?')[0]
                if '.' in url_path:
                    possible_ext = Path(url_path).suffix.lower()
                    if possible_ext in ['.mp3', '.wav', '.flac', '.aac', '.m4a', '.ogg']:
                        ext = possible_ext

            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
                for chunk in response.iter_content(chunk_size=8192):
                    tmp_file.write(chunk)
                temp_path = Path(tmp_file.name)

            self.status_label.setText(f"✅ Downloaded. Importing...")
            QApplication.processEvents()

            self._import_downloaded_file(temp_path)

        except requests.exceptions.Timeout:
            self.status_label.setText("❌ Download timed out. URL may be slow or inaccessible.")
            logger.error(f"Download timeout for URL: {url}")
        except requests.exceptions.HTTPError as e:
            if e.response is not None:
                status_code = e.response.status_code
                if status_code == 403:
                    msg = "Access Denied (403) - The URL may require authentication or be expired."
                elif status_code == 404:
                    msg = "File Not Found (404) - The URL is invalid or the file has been removed."
                elif status_code == 410:
                    msg = "File Gone (410) - This URL is no longer available."
                else:
                    msg = f"HTTP Error: {status_code}"
                self.status_label.setText(f"❌ {msg}")
                logger.error(f"HTTP Error {status_code} for URL: {url}")
            else:
                self.status_label.setText(f"❌ HTTP Error occurred")
                logger.error(f"HTTP Error for URL: {url} - {e}")
        except requests.exceptions.RequestException as e:
            self.status_label.setText(f"❌ Download failed: {e}")
            logger.error(f"Download failed for URL {url}: {e}")
        except Exception as e:
            self.status_label.setText(f"❌ An unexpected error occurred.")
            logger.exception(f"Unexpected error during download from URL {url}: {e}")

    def _add_sample_from_url(self):
        """Open dialog to import audio from a URL."""
        dialog = URLImportDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            url = dialog.get_url()
            mode = dialog.get_mode()
            if url:
                if mode == "fish":
                    self._import_from_fish_audio(url)
                else:
                    self._download_from_url(url)

    def _import_from_fish_audio(self, url: str):
        """Import audio and text from a Fish Audio page."""
        self.status_label.setText(f"🔍 Parsing Fish Audio page...")
        QApplication.processEvents()
        
        # Use the updated parser
        result = self._parse_fish_audio_page(url)
        
        if not result:
            self.status_label.setText(f"❌ Failed to parse Fish Audio page")
            QMessageBox.warning(
                self,
                "Parsing Failed",
                "Could not extract audio from the Fish Audio page.\n\n"
                "Try using the direct audio URL instead."
            )
            return
        
        audio_url = result['audio_url']
        sample_text = result['text']
        
        # First download the audio
        self.status_label.setText(f"⬇️ Downloading audio from Fish Audio...")
        QApplication.processEvents()
        
        # Download and import the audio
        self._download_from_url(audio_url)
        
        # After import, set the text on the newly added sample
        if self._sample_data:
            latest_sample = self._sample_data[-1]
            try:
                latest_sample['txt_path'].write_text(sample_text, encoding='utf-8')
                latest_sample['text'] = sample_text
                # Refresh the display if this sample is selected
                if self._current_sample and self._current_sample['stem'] == latest_sample['stem']:
                    self.text_edit.blockSignals(True)
                    self.text_edit.setPlainText(sample_text)
                    self.text_edit.blockSignals(False)
                self.status_label.setText(f"✅ Imported audio with sample text")
                logger.info(f"Successfully imported from Fish Audio: {url}")
            except Exception as e:
                logger.error(f"Failed to save sample text: {e}")
                self.status_label.setText(f"⚠️ Audio imported but text failed: {e}")
    
    def _add_sample(self):
        """Add a new sample from a local file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Audio File",
            "",
            "Audio Files (*.wav *.mp3 *.flac *.aac *.m4a);;All Files (*.*)"
        )
        
        if not file_path:
            return

        self._import_audio_file(Path(file_path))
    
    def _delete_sample(self):
        """
        Delete the selected sample with double confirmation.
        
        Requires two separate confirmations to prevent accidental deletion.
        Deletes both the WAV and TXT files permanently.
        """
        if not self._current_sample:
            return
        
        sample = self._current_sample
        stem = sample['stem']
        
        # First warning dialog
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
        
        # ⭐ CRITICAL: Stop playback and release the file BEFORE deletion
        self.media_player.stop()
        # Set source to empty to release the file handle
        self.media_player.setSource(QUrl())
        # Process events to ensure the release happens
        QApplication.processEvents()
        
        # Also stop any pending audio operations
        if self.audio_output:
            self.audio_output.setVolume(0.0)
        
        # Small delay to ensure the file is released
        time.sleep(0.1)
        
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
            
        except PermissionError as e:
            self.status_label.setText(f"❌ File in use - couldn't delete")
            logger.error(f"Permission error deleting sample {stem}: {e}")
            QMessageBox.warning(
                self, 
                "File in Use", 
                f"Could not delete '{stem}' because the file is still in use.\n\n"
                "Make sure the sample is not playing and try again."
            )
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
# URL Paste Editor Dialog
# ============================================================================

class URLImportDialog(QDialog):
    """Dialog for importing audio from a URL."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📥 Import Audio from URL")
        self.resize(600, 250)
        self.url = None
        self.mode = "direct"  # "direct" or "fish_page"
        
        layout = QVBoxLayout(self)
        
        # Instructions - now mentions both options
        instructions = QLabel(
            "Paste a URL to an audio file OR a Fish Audio page URL.\n"
            "Fish Audio pages will be parsed to extract the audio and sample text."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        
        # URL text edit
        self.url_edit = QTextEdit()
        self.url_edit.setPlaceholderText(
            "Direct audio URL:\nhttps://example.com/audio.mp3\n\n"
            "OR Fish Audio page:\nhttps://fish.audio/app/m/..."
        )
        self.url_edit.setMaximumHeight(80)
        layout.addWidget(self.url_edit)
        
        # Buttons
        btn_layout = QHBoxLayout()
        
        paste_btn = QPushButton("📋 Paste from Clipboard")
        paste_btn.clicked.connect(self._paste_from_clipboard)
        btn_layout.addWidget(paste_btn)
        
        btn_layout.addStretch()
        
        # Mode indicator
        self.mode_label = QLabel("🔹 Mode: Auto-detect")
        btn_layout.addWidget(self.mode_label)
        
        ok_btn = QPushButton("✅ Download")
        ok_btn.clicked.connect(self._accept_url)
        ok_btn.setDefault(True)
        btn_layout.addWidget(ok_btn)
        
        cancel_btn = QPushButton("❌ Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        layout.addLayout(btn_layout)
        
        # Status label
        self.status_label = QLabel("")
        layout.addWidget(self.status_label)
        
        # Update mode indicator when text changes
        self.url_edit.textChanged.connect(self._update_mode_indicator)
    
    def _update_mode_indicator(self):
        """Update the mode indicator based on the URL."""
        url = self.url_edit.toPlainText().strip()
        if "fish.audio/app/m/" in url:
            self.mode_label.setText("🔹 Mode: Fish Audio (parse page)")
        elif url.startswith(('http://', 'https://')):
            self.mode_label.setText("🔹 Mode: Direct download")
        else:
            self.mode_label.setText("🔹 Mode: Auto-detect")
    
    def _paste_from_clipboard(self):
        """Paste from clipboard into the text edit."""
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if text:
            self.url_edit.setText(text)
            self.status_label.setText("✅ Pasted from clipboard")
            self.url_edit.selectAll()
            self._update_mode_indicator()
    
    def _accept_url(self):
        """Validate and accept the URL."""
        url = self.url_edit.toPlainText().strip()
        if not url:
            self.status_label.setText("⚠️ Please paste a URL first.")
            return
        
        if not url.startswith(('http://', 'https://')):
            self.status_label.setText("⚠️ URL must start with http:// or https://")
            return
        
        # Auto-detect mode
        if "fish.audio/app/m/" in url:
            self.mode = "fish"
            self.status_label.setText("✅ Fish Audio page detected - will parse for audio + text")
        else:
            self.mode = "direct"
            self.status_label.setText("✅ Direct audio URL - will download file")
        
        self.url = url
        self.accept()
    
    def get_url(self) -> Optional[str]:
        """Return the validated URL."""
        return self.url
    
    def get_mode(self) -> str:
        """Return the import mode ('direct' or 'fish')."""
        return self.mode

# ============================================================================
# Data Loading / Saving Functions
# ============================================================================

def load_csv(csv_path: str) -> pd.DataFrame:
    """Load the dialog report CSV file."""
    logger.info(f"Loading CSV from: {csv_path}")
    try:
        df = pd.read_csv(csv_path)
        logger.info(f"CSV loaded: {len(df)} rows, {len(df.columns)} columns")
        return df
    except Exception as e:
        logger.error(f"Error loading CSV: {e}")
        return pd.DataFrame()
    

def filter_csv_for_assignment(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter the CSV to only include lines that need voice assignment.
    
    Rules:
    1. Lines with SoundResRef already set are skipped (already have voice)
    2. Lines where SoundResRef matches FILENAME_PATTERN are always kept (exception)
    
    Args:
        df: Original DataFrame from CSV
    
    Returns:
        Filtered DataFrame with only lines needing assignment
    """
    original_count = len(df)
    
    # Convert to string and clean
    sound_refs = df['SoundResRef'].fillna('').astype(str).str.strip()
    
    # Check if SoundResRef is empty (needs assignment)
    is_empty_sound = (sound_refs == '') | (sound_refs == 'nan') | (sound_refs == 'None')
    
    # Check if SoundResRef starts with "TS" (exception - always keep these)
    is_ts = sound_refs.str.match(FILENAME_PATTERN, na=False)
    
    # Keep if: empty SoundResRef OR SoundResRef matches TS pattern
    keep_mask = is_empty_sound | is_ts
    
    filtered_df = df[keep_mask].copy()
    
    logger.info(f"CSV filter: {original_count} rows → {len(filtered_df)} rows kept")
    removed_count = original_count - len(filtered_df)
    logger.info(f"  Removed {removed_count} lines with existing SoundResRef (not matching TS pattern)")
    
    return filtered_df


def load_json_files():
    """
    Load voice substitution rules from a single JSON file.
    
    File structure:
    {
        "npc": {"NPC Name": "voice_profile"},
        "gender": {"NPC|gender": "voice_profile"},
        "sysname": {"SystemName": "voice_profile"}
    }
    
    Returns:
        tuple: (substitutions, gender_substitutions, sys_substitutions)
    """
    substitutions = {}
    gender_substitutions = {}
    sys_substitutions = {}
    
    path = Path(VOICE_SUBSTITUTIONS_FILE)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Extract each section, defaulting to empty dict if missing
            substitutions = data.get("npc", {})
            gender_substitutions = data.get("gender", {})
            sys_substitutions = data.get("sysname", {})
            
            logger.info(f"Loaded substitutions from {VOICE_SUBSTITUTIONS_FILE}:")
            logger.info(f"  NPC-level: {len(substitutions)} entries")
            logger.info(f"  Gender-level: {len(gender_substitutions)} entries")
            logger.info(f"  SysName-level: {len(sys_substitutions)} entries")
        except Exception as e:
            logger.error(f"Error loading {VOICE_SUBSTITUTIONS_FILE}: {e}")
    else:
        logger.info(f"No substitution file found, starting fresh: {VOICE_SUBSTITUTIONS_FILE}")
    
    return substitutions, gender_substitutions, sys_substitutions


def save_json_files(substitutions: Dict, gender_substitutions: Dict, sys_substitutions: Dict) -> bool:
    """
    Save all substitution rules to a single JSON file.
    
    File structure:
    {
        "npc": {"NPC Name": "voice_profile"},
        "gender": {"NPC|gender": "voice_profile"},
        "sysname": {"SystemName": "voice_profile"}
    }
    """
    data = {
        "npc": substitutions,
        "gender": gender_substitutions,
        "sysname": sys_substitutions
    }
    
    try:
        path = Path(VOICE_SUBSTITUTIONS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved substitutions to {VOICE_SUBSTITUTIONS_FILE}")
        return True
    except Exception as e:
        logger.error(f"Error saving {VOICE_SUBSTITUTIONS_FILE}: {e}")
        return False


def clean_redundant_substitutions(substitutions: Dict, existing_voices: Set[str]) -> Dict:
    """
    Remove redundant substitutions where NPC name equals the voice profile name.
    
    These are unnecessary because the system already defaults to using the NPC name
    if it exists as a voice file (e.g., "Morul" -> "Morul" is redundant).
    
    Args:
        substitutions: The substitutions dictionary to clean
        existing_voices: Set of voice filenames that exist
    
    Returns:
        Cleaned substitutions dictionary
    """
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
    """
    Get unique voice profile names from the /voices directory.
    
    Groups files like "Boy.wav", "Boy 2.wav", "Boy 3.wav" into a single
    profile "Boy" for the dropdown list.
    """
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
    """
    Build the hierarchy entry for a single NPC.
    
    The hierarchy structure:
        {
            "assigned_voice": str or None,     # NPC-level assignment
            "has_existing_voice": bool,        # Voice file exists in /voices/
            "genders": {
                "M": {
                    "assigned_voice": str or None,  # Gender-level assignment
                    "sysnames": [
                        {"name": "SYSNAME", "assigned_voice": str or None}
                    ]
                }
            }
        }
    """
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
    """Build the full NPC hierarchy for all characters."""
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

    Uses a single groupby pass instead of re-filtering the whole DataFrame
    once per NPC, making it much faster on large datasets.
    """
    counts = {}
    if df.empty:
        return counts

    df_clean = df.dropna(subset=['RealName'])
    for npc_name, npc_df in df_clean.groupby('RealName', sort=False):
        npc_data = hierarchy.get(npc_name)
        if npc_data is None:
            continue

        total_lines = len(npc_df)
        counts[npc_name] = {
            'total_lines': total_lines,
            'genders': {}
        }

        gender_groups = npc_df.dropna(subset=['Gender']).groupby('Gender', sort=False)
        for gender, gender_df in gender_groups:
            if gender not in npc_data.get('genders', {}):
                continue

            gender_count = len(gender_df)
            counts[npc_name]['genders'][gender] = {
                'total_lines': gender_count,
                'sysnames': {}
            }

            # One value_counts() call covers every sysname for this NPC+gender
            sysname_counts = gender_df['SystemName'].value_counts()
            for sys in npc_data['genders'][gender].get('sysnames', []):
                sysname = sys['name']
                counts[npc_name]['genders'][gender]['sysnames'][sysname] = int(
                    sysname_counts.get(sysname, 0)
                )

    return counts


def calculate_covered_lines_for_npc(npc_data: Dict, npc_line_counts: Dict) -> int:
    """
    Count how many of an NPC's CSV lines are covered by a voice assignment.

    Follows the hierarchy: NPC-level > Gender-level > SystemName-level.
    A higher-level assignment covers everything beneath it.

    Uses cached line counts, not the DataFrame, for fast performance.
    """
    total_lines = npc_line_counts.get('total_lines', 0)

    # Voice file with NPC name covers everything
    if npc_data.get('has_existing_voice', False):
        return total_lines

    # NPC-level assignment covers ALL lines (highest priority)
    if npc_data['assigned_voice'] is not None:
        return total_lines

    covered = 0
    if npc_data['genders']:
        for gender, gender_data in npc_data['genders'].items():
            gender_counts = npc_line_counts.get('genders', {}).get(gender, {})
            gender_total = gender_counts.get('total_lines', 0)

            if gender_data['assigned_voice'] is not None:
                # Gender-level assignment covers all its sysnames' lines
                covered += gender_total
            elif gender_data['sysnames']:
                sys_counts = gender_counts.get('sysnames', {})
                for sys in gender_data['sysnames']:
                    if sys['assigned_voice'] is not None:
                        covered += sys_counts.get(sys['name'], 0)

    return covered


def calculate_all_covered_lines(hierarchy: Dict, line_counts: Dict) -> Dict[str, int]:
    """Calculate covered-line count for every NPC in the hierarchy."""
    return {
        name: calculate_covered_lines_for_npc(data, line_counts.get(name, {}))
        for name, data in hierarchy.items()
    }


# ============================================================================
# Main Application Window
# ============================================================================

class VoiceProfileManager(QMainWindow):
    """
    Main window for the Voice Profile Manager.
    
    Features:
        - NPC list with search, sort, and filter
        - Coverage statistics with progress bar
        - Three-level voice assignment (NPC, Gender, System Name)
        - Voice profile editor with sample management
        - Real-time updates of coverage and icons
        - Persistent storage via JSON files
    
    UI Layout:
        - Left panel: Stats, search, sort/filter, NPC list
        - Right panel: Details for selected NPC with assignment controls
    """
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎯 Voice Profile Manager")
        self.resize(1400, 900)

        logger.info("Starting Voice Profile Manager...")

        # --- Load everything once at startup ---
        self.df = load_csv(CSV_PATH)

        # Apply the filter to remove lines with SoundResRef (except TS files)
        if not self.df.empty:
            self.df = filter_csv_for_assignment(self.df)

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
        self.covered_lines_by_npc = calculate_all_covered_lines(self.hierarchy, self.line_counts)
        self.total_lines_all = sum(v.get('total_lines', 0) for v in self.line_counts.values())

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
    # UI Construction
    # ------------------------------------------------------------------
    
    def _build_ui(self):
        """Build the main application UI with split layout."""
        central = QWidget()
        self.setCentralWidget(central)
        outer_layout = QHBoxLayout(central)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer_layout.addWidget(splitter)

        # --- Left: Stats + Search + NPC List ---
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
        self.stats_lines_covered_label = QLabel()

        # Coverage progress bar
        self.coverage_progress = QProgressBar()
        self.coverage_progress.setRange(0, 100)
        self.coverage_progress.setValue(0)
        self.coverage_progress.setFixedHeight(12)
        self.coverage_progress.setTextVisible(False)
        self.coverage_progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #555;
                border-radius: 6px;
                background-color: #2d2d2d;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #4CAF50,
                    stop: 1 #8BC34A);
                border-radius: 6px;
            }
        """)

        stats_layout.addRow("Total NPCs:", self.stats_total_label)
        stats_layout.addRow("Available Voices:", self.stats_voices_label)
        stats_layout.addRow("NPCs with Voice Files:", self.stats_existing_label)
        stats_layout.addRow("NPC-level assignments:", self.stats_npc_level_label)
        stats_layout.addRow("Gender-level assignments:", self.stats_gender_level_label)
        stats_layout.addRow("SysName-level assignments:", self.stats_sys_level_label)
        stats_layout.addRow("Lines with voice assigned:", self.stats_lines_covered_label)
        stats_layout.addRow("Coverage:", self.coverage_progress)
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
        self.sort_combo.setCurrentIndex(1)  # Default: By Lines
        self.sort_combo.currentTextChanged.connect(self._on_filter_changed)
        controls_layout.addWidget(self.sort_combo)
        
        controls_layout.addWidget(QLabel("Filter:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All", "Modified", "Missing", "Has Voice File"])
        self.filter_combo.setCurrentIndex(2)  # Default: Missing
        self.filter_combo.currentTextChanged.connect(self._on_filter_changed)
        controls_layout.addWidget(self.filter_combo)
        
        left_layout.addLayout(controls_layout)

        # NPC count
        self.npc_count_label = QLabel()
        left_layout.addWidget(self.npc_count_label)

        # NPC list
        self.npc_list = QListWidget()
        self.npc_list.currentItemChanged.connect(self._on_npc_selected)
        left_layout.addWidget(self.npc_list, stretch=1)

        splitter.addWidget(left_widget)

        # --- Right: Detail Panel ---
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
    # NPC List Handling
    # ------------------------------------------------------------------

    def _get_coverage_status(self, npc_name: str) -> Dict:
        """
        Get coverage status for an NPC using cached data.
        
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
        
        # Use cached line counts
        npc_counts = self.line_counts.get(npc_name, {})
        total_lines = npc_counts.get('total_lines', 0)
        
        # Use cached covered lines
        covered_lines = self.covered_lines_by_npc.get(npc_name, 0)
        
        # If NPC has a voice file, it's fully covered
        if has_existing:
            return {
                'total_lines': total_lines,
                'covered_lines': total_lines,
                'has_existing': True,
                'is_fully_covered': True,
                'has_partial': False
            }
        
        return {
            'total_lines': total_lines,
            'covered_lines': covered_lines,
            'has_existing': False,
            'is_fully_covered': covered_lines == total_lines and total_lines > 0,
            'has_partial': 0 < covered_lines < total_lines
        }

    def _npc_icon(self, npc_name: str) -> str:
        """
        Get the appropriate icon for an NPC based on coverage status.
        
        Returns:
            str: Icon character
                - 🟢 Voice file exists (full coverage)
                - ✅ Fully covered (all lines have assignments)
                - 🔵 Partially covered
                - 🔴 Nothing assigned
        """
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
        Apply all filters (search, status filter, sort) to determine which NPCs to show.
        
        Filter options:
            - All: Show all NPCs
            - Modified: Show NPCs with assignments
            - Missing: Show NPCs with NO assignments and NO voice file
            - Has Voice File: Show NPCs with existing voice files
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
            icon = self._npc_icon(name)
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
        """Select an NPC in the list by name."""
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
        """Handle NPC selection from the list."""
        if current is None:
            return
        self.selected_npc = current.data(Qt.ItemDataRole.UserRole)
        self._render_detail_panel()

    # ------------------------------------------------------------------
    # Detail Panel
    # ------------------------------------------------------------------

    def _clear_layout(self, layout):
        """Recursively clear all widgets from a layout."""
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
        The button shows ✏️ if the profile exists, or ➕ if it doesn't.
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
        """Render the detail panel for the currently selected NPC."""
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
        """Copy NPC name to clipboard."""
        QApplication.clipboard().setText(npc_name)
        self.statusBar().showMessage(f"Copied '{npc_name}' to clipboard!", 3000)

    # ------------------------------------------------------------------
    # Change Handlers
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

        # Keep the covered-lines total in sync
        self.covered_lines_by_npc[npc_name] = calculate_covered_lines_for_npc(
            npc_data, self.line_counts[npc_name]
        )

    def _on_npc_voice_changed(self, npc_name: str, new_voice: str):
        """Handle NPC-level voice assignment change."""
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
        
        if save_json_files(self.substitutions, self.gender_substitutions, self.sys_substitutions):
            # Update hierarchy
            self._refresh_npc_entry(npc_name)
            self._update_stats()
            # Update ONLY the changed NPC's icon (fast!)
            self._refresh_npc_list_icon(npc_name)
            self.statusBar().showMessage(f"✅ Updated {npc_name} → {new_voice or 'unassigned'}", 3000)

    def _on_gender_voice_changed(self, npc_name: str, gender: str, new_voice: str):
        """Handle gender-level voice assignment change."""
        gender_key = f"{npc_name}|{gender}"
        current = self.gender_substitutions.get(gender_key) or ""
        if new_voice == current:
            return
        logger.info(f"Changing gender voice: {gender_key} -> {new_voice or 'unassigned'}")
        if new_voice:
            self.gender_substitutions[gender_key] = new_voice
        else:
            self.gender_substitutions.pop(gender_key, None)
        if save_json_files(self.substitutions, self.gender_substitutions, self.sys_substitutions):
            self._refresh_npc_entry(npc_name)
            self._update_stats()
            self._refresh_npc_list_icon(npc_name)  # Target only this NPC
            self.statusBar().showMessage(f"✅ Updated {npc_name}|{gender} → {new_voice or 'unassigned'}", 3000)

    def _on_sys_voice_changed(self, sysname: str, new_voice: str):
        """Handle system-name-level voice assignment change."""
        current = self.sys_substitutions.get(sysname) or ""
        if new_voice == current:
            return
        logger.info(f"Changing system voice: {sysname} -> {new_voice or 'unassigned'}")
        if new_voice:
            self.sys_substitutions[sysname] = new_voice
        else:
            self.sys_substitutions.pop(sysname, None)
        if save_json_files(self.substitutions, self.gender_substitutions, self.sys_substitutions):
            if self.selected_npc is not None:
                self._refresh_npc_entry(self.selected_npc)
                self._refresh_npc_list_icon(self.selected_npc)  # Target only this NPC
            self._update_stats()
            self.statusBar().showMessage(f"✅ Updated {sysname} → {new_voice or 'unassigned'}", 3000)

    def _update_stats(self):
        """Update all statistics displays."""
        npcs_with_voice = sum(1 for d in self.hierarchy.values() if d.get("has_existing_voice", False))
        
        self.stats_total_label.setText(str(len(self.hierarchy)))
        self.stats_voices_label.setText(str(len(self.available_voices)))
        self.stats_existing_label.setText(str(npcs_with_voice))
        self.stats_npc_level_label.setText(str(len(self.substitutions)))
        self.stats_gender_level_label.setText(str(len(self.gender_substitutions)))
        self.stats_sys_level_label.setText(str(len(self.sys_substitutions)))

        total_covered = sum(self.covered_lines_by_npc.values())
        pct = (100 * total_covered / self.total_lines_all) if self.total_lines_all else 0
        self.stats_lines_covered_label.setText(
            f"{total_covered:,} / {self.total_lines_all:,} ({pct:.1f}%)"
        )
        
        # Update progress bar
        self.coverage_progress.setValue(int(pct))


# ============================================================================
# Application Entry Point
# ============================================================================

def main():
    """Application entry point."""
    # Suppress Qt multimedia debug messages
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