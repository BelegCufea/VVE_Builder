"""
Unit tests for profiles-manage_gui.py

Tests the core functionality of the Voice Profile Manager GUI including:
- Data loading and filtering
- Hierarchy building
- Substitution management
- Coverage calculations
- Voice profile operations
"""

import unittest
import tempfile
import json
import sys
import os
import subprocess
import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
import pandas as pd
import numpy as np

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock PySide6 and Qt imports before importing the module under test.
#
# Plain `MagicMock()` works for names used call-style (QUrl.fromLocalFile(...),
# QMessageBox.warning(...)) since attribute/call access just chains further
# mocks. But profiles-manage_gui.py also *subclasses* some of these names
# (class CheckAllSamplesDialog(QDialog): ...), and subclassing a MagicMock
# silently collapses the new class into another MagicMock, discarding every
# method the class body defined. So the small set of Qt classes this module
# actually subclasses need to resolve to real (dummy) classes instead.
_QT_BASE_CLASSES = {"QDialog", "QMainWindow", "QObject", "QTableWidgetItem", "QWidget"}


class _QtModuleMock(MagicMock):
    def __getattr__(self, name):
        if name in _QT_BASE_CLASSES:
            dummy_cls = type(name, (object,), {})
            setattr(self, name, dummy_cls)
            return dummy_cls
        return super().__getattr__(name)


sys.modules['PySide6'] = MagicMock()
sys.modules['PySide6.QtCore'] = _QtModuleMock()
sys.modules['PySide6.QtWidgets'] = _QtModuleMock()
sys.modules['PySide6.QtMultimedia'] = MagicMock()
sys.modules['PySide6.QtGui'] = MagicMock()

# Import the module with hyphens in the name using importlib
module_path = Path(__file__).parent.parent / "profiles-manage_gui.py"
if not module_path.exists():
    raise ImportError(f"Cannot find module at {module_path}")

# Use a source file loader since the file has hyphens in name.
# spec_from_file_location (rather than spec_from_loader) is required here
# so the resulting spec has has_location=True - otherwise module_from_spec
# never sets __file__ on the loaded module, and profiles-manage_gui.py's
# own module-level code (which reads __file__) raises NameError.
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("profiles_manage_gui", str(module_path))
module_spec = importlib.util.spec_from_file_location(
    "profiles_manage_gui", str(module_path), loader=loader
)
if module_spec is None:
    raise ImportError(f"Cannot create module spec for {module_path}")
assert module_spec.loader is not None
profiles_manage_gui = importlib.util.module_from_spec(module_spec)
sys.modules['profiles_manage_gui'] = profiles_manage_gui
module_spec.loader.exec_module(profiles_manage_gui)

# Import the functions we need to test
from libs.utils import convert_to_ogg
load_csv = profiles_manage_gui.load_csv
prepare_dataframe = profiles_manage_gui.prepare_dataframe
filter_csv_for_assignment = profiles_manage_gui.filter_csv_for_assignment
load_json_files = profiles_manage_gui.load_json_files
save_json_files = profiles_manage_gui.save_json_files
clean_redundant_substitutions = profiles_manage_gui.clean_redundant_substitutions
remove_orphaned_substitutions = profiles_manage_gui.remove_orphaned_substitutions
get_available_voice_profiles = profiles_manage_gui.get_available_voice_profiles
get_existing_voice_files = profiles_manage_gui.get_existing_voice_files
get_prep_npc_names = profiles_manage_gui.get_prep_npc_names
build_hierarchy = profiles_manage_gui.build_hierarchy
build_hierarchy_for_npc = profiles_manage_gui.build_hierarchy_for_npc
_build_npc_entry = profiles_manage_gui._build_npc_entry
calculate_line_counts = profiles_manage_gui.calculate_line_counts
calculate_covered_lines_for_npc = profiles_manage_gui.calculate_covered_lines_for_npc
calculate_all_covered_lines = profiles_manage_gui.calculate_all_covered_lines
is_missing_realname_npc = profiles_manage_gui.is_missing_realname_npc

# Import appconfig for configuration
import libs.appconfig as appconfig


class TestVoiceProfileManager(unittest.TestCase):
    """Main test class for profiles-manage_gui.py"""
    
    def setUp(self):
        """Set up test fixtures"""
        # Create temporary directories for testing
        self.test_dir = Path(tempfile.mkdtemp())
        self.voices_dir = self.test_dir / "voices"
        self.voices_prep_dir = self.test_dir / "voices_prep"
        self.logs_dir = self.test_dir / "logs"

        self.voices_dir.mkdir()
        self.voices_prep_dir.mkdir()
        self.logs_dir.mkdir()

        # Point appconfig at a throwaway config file instead of the real
        # libs/appconfig.json, so overrides set below never touch the
        # developer's actual configuration (and never race with OneDrive
        # syncing that file, which was causing sporadic PermissionErrors).
        self._original_config_path = appconfig._CONFIG_PATH
        self._original_overrides = appconfig._overrides
        self._original_loaded = appconfig._loaded
        appconfig._CONFIG_PATH = self.test_dir / "appconfig.json"
        appconfig._overrides = {}
        appconfig._loaded = True

        # Store test config values as attributes
        self.test_voices_dir = str(self.voices_dir)
        self.test_voices_prep_dir = str(self.voices_prep_dir)
        self.test_log_dir = str(self.logs_dir)
        self.test_csv_path = str(self.test_dir / "test.csv")
        self.test_substitutions_file = str(self.test_dir / "substitutions.json")
        self.test_filename_prefix = "TS"
        self.test_realname_not_found = "RealNameMissing"

        # Set test config values
        appconfig.cfg.VOICES_DIR = self.test_voices_dir
        appconfig.cfg.VOICES_PREP_DIR = self.test_voices_prep_dir
        appconfig.cfg.LOG_DIR = self.test_log_dir
        appconfig.cfg.CSV_PATH = self.test_csv_path
        appconfig.cfg.VOICE_SUBSTITUTIONS_FILE = self.test_substitutions_file
        appconfig.cfg.FILENAME_PREFIX = self.test_filename_prefix
        appconfig.cfg.REALNAME_NOT_FOUND = self.test_realname_not_found

        # Create test voice files
        self._create_test_voice_files()

    def tearDown(self):
        """Clean up test fixtures"""
        appconfig._CONFIG_PATH = self._original_config_path
        appconfig._overrides = self._original_overrides
        appconfig._loaded = self._original_loaded

    def _create_test_voice_files(self):
        """Create test voice files in the test directories"""
        # Create approved voice files
        test_voices = ["Morul", "Anomen", "Nalia", "Jan"]
        for voice in test_voices:
            # Create WAV files
            (self.voices_dir / f"{voice}.WAV").touch()
            (self.voices_dir / f"{voice} 2.WAV").touch()
            # Create text files
            (self.voices_dir / f"{voice}.txt").write_text(f"Sample text for {voice}")
            (self.voices_dir / f"{voice} 2.txt").write_text(f"Second sample for {voice}")
        
        # Create unapproved/prep voice files
        prep_voices = ["Viconia", "Korgan", "Mazzy"]
        for voice in prep_voices:
            (self.voices_prep_dir / f"{voice}.WAV").touch()
            (self.voices_prep_dir / f"{voice}.txt").touch()
    
    def test_is_missing_realname_npc(self):
        """Test identification of missing RealName NPCs"""
        # Test positive cases
        self.assertTrue(is_missing_realname_npc("RealNameMissing - BREG"))
        self.assertTrue(is_missing_realname_npc("RealNameMissing - ABCD"))
        
        # Test negative cases
        self.assertFalse(is_missing_realname_npc("Morul"))
        self.assertFalse(is_missing_realname_npc("RealNameMissing"))
        self.assertFalse(is_missing_realname_npc(""))
    
    def test_load_csv_empty(self):
        """Test loading empty CSV file"""
        # Create empty CSV
        csv_path = self.test_dir / "empty.csv"
        csv_path.write_text("StrRef,SystemName,RealName,Gender,Text,SoundResRef\n")
        
        df = load_csv(str(csv_path))
        self.assertTrue(df.empty)
        self.assertEqual(len(df), 0)
    
    def test_load_csv_valid(self):
        """Test loading valid CSV file"""
        # Create test CSV data
        csv_content = """StrRef,SystemName,RealName,Gender,Text,SoundResRef
1001,MORUL,Morul,M,Hello there,
1002,ANOMEN,Anomen,M,Greetings,MORUL
1003,NALIA,Nalia,F,Good day,
1004,BREG,,M,Unknown speaker,TSBREG
"""
        csv_path = self.test_dir / "test.csv"
        csv_path.write_text(csv_content)
        
        df = load_csv(str(csv_path))
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 4)
        self.assertListEqual(list(df.columns), 
                           ['StrRef', 'SystemName', 'RealName', 'Gender', 'Text', 'SoundResRef'])
    
    def test_prepare_dataframe_empty(self):
        """Test preparing empty dataframe"""
        df = pd.DataFrame()
        result = prepare_dataframe(df)
        self.assertTrue(result.empty)
    
    def test_prepare_dataframe_no_systemname(self):
        """Test preparing dataframe with rows lacking SystemName"""
        data = {
            'SystemName': ['MORUL', '', None, 'ANOMEN'],
            'RealName': ['Morul', 'Unknown', 'Test', 'Anomen'],
            'Gender': ['M', 'F', 'M', 'M'],
            'Text': ['Text 1', 'Text 2', 'Text 3', 'Text 4']
        }
        df = pd.DataFrame(data)
        
        result = prepare_dataframe(df)
        self.assertEqual(len(result), 2)  # Only rows with SystemName
        self.assertTrue(all(result['SystemName'].notna()))
        self.assertTrue(all(result['SystemName'].astype(str).str.strip() != ''))
    
    def test_prepare_dataframe_missing_realname(self):
        """Test preparing dataframe with missing RealName"""
        data = {
            'SystemName': ['MORUL', 'BREG', 'ABCD'],
            'RealName': ['Morul', '', None],
            'Gender': ['M', 'M', 'F'],
            'Text': ['Text 1', 'Text 2', 'Text 3']
        }
        df = pd.DataFrame(data)
        
        result = prepare_dataframe(df)
        self.assertEqual(len(result), 3)
        
        # Check placeholder RealName was assigned
        missing_rows = result[result['RealName'].str.startswith('RealNameMissing - ')]
        self.assertEqual(len(missing_rows), 2)
        
        # Check valid RealName remains unchanged
        morul_row = result[result['RealName'] == 'Morul']
        self.assertEqual(len(morul_row), 1)
    
    def test_filter_csv_for_assignment(self):
        """Test filtering CSV for voice assignment"""
        data = {
            'StrRef': [1001, 1002, 1003, 1004, 1005],
            'SystemName': ['MORUL', 'ANOMEN', 'NALIA', 'BREG', 'JAN'],
            'RealName': ['Morul', 'Anomen', 'Nalia', '', 'Jan'],
            'Gender': ['M', 'M', 'F', 'M', 'M'],
            'Text': ['T1', 'T2', 'T3', 'T4', 'T5'],
            'SoundResRef': ['', 'MORUL', None, 'TSBREG', 'JANVOICE']
        }
        df = pd.DataFrame(data)
        
        result = filter_csv_for_assignment(df)
        
        # Should keep: empty, None, and TS-prefixed rows
        self.assertEqual(len(result), 3)  # rows 0, 2, 3
        
        # Verify specific rows
        self.assertTrue(1001 in result['StrRef'].values)  # Empty SoundResRef
        self.assertTrue(1003 in result['StrRef'].values)  # None SoundResRef
        self.assertTrue(1004 in result['StrRef'].values)  # TS-prefixed SoundResRef
        
        # Verify excluded rows
        self.assertFalse(1002 in result['StrRef'].values)  # Has existing SoundResRef
        self.assertFalse(1005 in result['StrRef'].values)  # Has existing SoundResRef
    
    def test_load_json_files_not_exist(self):
        """Test loading JSON files when they don't exist"""
        # Use test-specific path
        json_path = Path(self.test_substitutions_file)
        if json_path.exists():
            json_path.unlink()
        
        # Temporarily set the config value for this test
        original = appconfig.cfg.VOICE_SUBSTITUTIONS_FILE
        try:
            appconfig.cfg.VOICE_SUBSTITUTIONS_FILE = self.test_substitutions_file
            subs, gender_subs, sys_subs = load_json_files()
        finally:
            appconfig.cfg.VOICE_SUBSTITUTIONS_FILE = original
        
        # Should return empty dictionaries
        self.assertEqual(subs, {})
        self.assertEqual(gender_subs, {})
        self.assertEqual(sys_subs, {})
    
    def test_load_json_files_valid(self):
        """Test loading valid JSON files"""
        json_data = {
            "npc": {"Morul": "DeepVoice", "Anomen": "NobleVoice"},
            "gender": {"Morul|M": "MaleVoice", "Nalia|F": "FemaleVoice"},
            "sysname": {"BREG": "OrcVoice", "ABCD": "MysteryVoice"}
        }
        
        json_path = Path(appconfig.cfg.VOICE_SUBSTITUTIONS_FILE)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(json_data, indent=2))
        
        subs, gender_subs, sys_subs = load_json_files()
        
        self.assertEqual(subs, {"Morul": "DeepVoice", "Anomen": "NobleVoice"})
        self.assertEqual(gender_subs, {"Morul|M": "MaleVoice", "Nalia|F": "FemaleVoice"})
        self.assertEqual(sys_subs, {"BREG": "OrcVoice", "ABCD": "MysteryVoice"})
    
    def test_save_json_files(self):
        """Test saving JSON files"""
        subs = {"Morul": "DeepVoice", "Anomen": "NobleVoice"}
        gender_subs = {"Morul|M": "MaleVoice", "Nalia|F": "FemaleVoice"}
        sys_subs = {"BREG": "OrcVoice", "ABCD": "MysteryVoice"}
        
        # Temporarily set the config value for this test
        original = appconfig.cfg.VOICE_SUBSTITUTIONS_FILE
        try:
            appconfig.cfg.VOICE_SUBSTITUTIONS_FILE = self.test_substitutions_file
            result = save_json_files(subs, gender_subs, sys_subs)
        finally:
            appconfig.cfg.VOICE_SUBSTITUTIONS_FILE = original
        
        self.assertTrue(result)
        
        # Verify file was created
        json_path = Path(self.test_substitutions_file)
        self.assertTrue(json_path.exists())
        
        # Verify content
        loaded_data = json.loads(json_path.read_text())
        self.assertEqual(loaded_data["npc"], subs)
        self.assertEqual(loaded_data["gender"], gender_subs)
        self.assertEqual(loaded_data["sysname"], sys_subs)
    
    def test_clean_redundant_substitutions(self):
        """Test cleaning redundant substitutions"""
        existing_voices = {"Morul", "Anomen", "Nalia", "Jan"}
        
        # Test with redundant entries
        substitutions = {
            "Morul": "Morul",  # Redundant - same as NPC name and exists
            "Anomen": "DeepVoice",  # Not redundant - different voice
            "Nalia": "Nalia",  # Redundant - same as NPC name and exists
            "Unknown": "Unknown"  # Not redundant - doesn't exist in voices
        }
        
        cleaned = clean_redundant_substitutions(substitutions, existing_voices)
        
        # Should remove Morul and Nalia (redundant and exists)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned["Anomen"], "DeepVoice")
        self.assertEqual(cleaned["Unknown"], "Unknown")
        self.assertNotIn("Morul", cleaned)
        self.assertNotIn("Nalia", cleaned)
    
    def test_remove_orphaned_substitutions(self):
        """Test removing orphaned substitutions"""
        available_voices = ["DeepVoice", "NobleVoice", "FemaleVoice"]
        
        subs = {"Morul": "DeepVoice", "Anomen": "OldVoice"}  # OldVoice is orphaned
        gender_subs = {"Morul|M": "MaleVoice", "Nalia|F": "FemaleVoice"}  # MaleVoice is orphaned
        sys_subs = {"BREG": "OrcVoice", "ABCD": "DeepVoice"}  # OrcVoice is orphaned
        
        new_subs, new_gender, new_sys, removed_npc, removed_gender, removed_sys = \
            remove_orphaned_substitutions(subs, gender_subs, sys_subs, available_voices)
        
        # Check cleaned dictionaries
        self.assertEqual(new_subs, {"Morul": "DeepVoice"})
        self.assertEqual(new_gender, {"Nalia|F": "FemaleVoice"})
        self.assertEqual(new_sys, {"ABCD": "DeepVoice"})
        
        # Check removed keys
        self.assertEqual(removed_npc, ["Anomen"])
        self.assertEqual(removed_gender, ["Morul|M"])
        self.assertEqual(removed_sys, ["BREG"])
    
    def test_get_available_voice_profiles(self):
        """Test getting available voice profiles"""
        profiles = get_available_voice_profiles()
        
        # Should find test voice files created in setUp
        expected = {"Morul", "Anomen", "Nalia", "Jan"}
        self.assertEqual(set(profiles), expected)
        
        # Should be sorted alphabetically
        self.assertEqual(profiles, sorted(expected))
    
    def test_get_existing_voice_files(self):
        """Test getting existing voice files"""
        voices = get_existing_voice_files()
        
        # Should find test voice files created in setUp
        expected = {"Morul", "Anomen", "Nalia", "Jan"}
        self.assertEqual(voices, expected)
    
    def test_get_prep_npc_names(self):
        """Test getting NPC names from prep directory"""
        prep_names = get_prep_npc_names()
        
        # Should find test prep voice files created in setUp
        expected = {"Viconia", "Korgan", "Mazzy"}
        self.assertEqual(prep_names, expected)
    
    @patch('subprocess.run')
    def test_convert_to_ogg_success(self, mock_run):
        """Test successful OGG conversion"""
        mock_run.return_value = Mock(returncode=0)
        
        input_path = Path("test.wav")
        output_path = Path("test.ogg")
        
        result = convert_to_ogg(input_path, output_path, quality=4)
        
        self.assertTrue(result)
        mock_run.assert_called_once()
    
    @patch('subprocess.run')
    def test_convert_to_ogg_failure(self, mock_run):
        """Test failed OGG conversion"""
        mock_run.side_effect = subprocess.CalledProcessError(1, 'ffmpeg', stderr="Error")
        
        input_path = Path("test.wav")
        output_path = Path("test.ogg")
        
        result = convert_to_ogg(input_path, output_path, quality=4)
        
        self.assertFalse(result)
    
    @patch('subprocess.run')
    def test_convert_to_ogg_ffmpeg_not_found(self, mock_run):
        """Test OGG conversion when ffmpeg is not found"""
        mock_run.side_effect = FileNotFoundError()
        
        input_path = Path("test.wav")
        output_path = Path("test.ogg")
        
        result = convert_to_ogg(input_path, output_path, quality=4)
        
        self.assertFalse(result)

    def test_check_all_detail_play_button_tracks_selection(self):
        """The bulk-check detail player is enabled only when a row is selected."""
        dialog = Mock()
        row = {
            "text": "Expected sample text",
            "transcribed_text": "Transcribed sample text",
            "success": True,
        }
        dialog._selected_row_data.return_value = row

        profiles_manage_gui.CheckAllSamplesDialog._on_selection_changed(dialog)

        self.assertIs(dialog._current_detail_row, row)
        dialog.detail_play_btn.setEnabled.assert_called_once_with(True)

        dialog._selected_row_data.return_value = None
        dialog._clear_detail_panel.reset_mock()
        profiles_manage_gui.CheckAllSamplesDialog._on_selection_changed(dialog)

        dialog._clear_detail_panel.assert_called_once_with()

    def test_check_all_plays_selected_detail_sample(self):
        """Detail playback resets the player and starts the selected WAV file."""
        wav_path = self.voices_dir / "Morul.WAV"
        dialog = Mock()
        dialog._current_detail_row = {"wav_path": wav_path}

        profiles_manage_gui.CheckAllSamplesDialog._play_selected_detail_sample(dialog)

        dialog.detail_media_player.stop.assert_called_once_with()
        dialog.detail_audio_output.setVolume.assert_called_once_with(0.7)
        profiles_manage_gui.QUrl.fromLocalFile.assert_called_once_with(str(wav_path.absolute()))
        self.assertEqual(dialog.detail_media_player.setSource.call_count, 2)
        dialog.detail_media_player.play.assert_called_once_with()
        dialog.stage_label.setText.assert_called_once_with(f"🔊 Playing: {wav_path.name}")

    def test_check_all_detail_play_reports_missing_audio(self):
        """Detail playback does not start when the selected WAV file is missing."""
        wav_path = self.voices_dir / "missing.WAV"
        dialog = Mock()
        dialog._current_detail_row = {"wav_path": wav_path}

        profiles_manage_gui.CheckAllSamplesDialog._play_selected_detail_sample(dialog)

        dialog.detail_media_player.stop.assert_called_once_with()
        dialog.detail_media_player.setSource.assert_not_called()
        dialog.detail_media_player.play.assert_not_called()
        dialog.stage_label.setText.assert_called_once_with(f"⚠️ Audio file not found: {wav_path.name}")


if __name__ == '__main__':
    unittest.main()
