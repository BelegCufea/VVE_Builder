"""Focused unit tests for audio playback in check_gui.py."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent))
sys.modules['PySide6'] = MagicMock()
sys.modules['PySide6.QtCore'] = MagicMock()
sys.modules['PySide6.QtGui'] = MagicMock()
sys.modules['PySide6.QtWidgets'] = MagicMock()
sys.modules['PySide6.QtMultimedia'] = MagicMock()
sys.modules['requests'] = MagicMock()
# Keep the window class real so its methods can be called unbound with
# lightweight mocks, while the rest of the Qt API stays mocked.
sys.modules['PySide6.QtWidgets'].QMainWindow = type('QMainWindow', (), {})

module_path = Path(__file__).parent.parent / "check_gui.py"
module_spec = importlib.util.spec_from_file_location("check_gui", module_path)
assert module_spec is not None and module_spec.loader is not None
check_gui = importlib.util.module_from_spec(module_spec)
sys.modules['check_gui'] = check_gui
module_spec.loader.exec_module(check_gui)


class TestCheckGuiPlayback(unittest.TestCase):
    def test_transcribed_sample_retains_resolved_audio_path(self):
        wav_path = Path(tempfile.mkdtemp()) / "TS000001.WAV"
        wav_path.touch()
        with patch.object(check_gui, "transcribe_and_score") as transcribe:
            transcribe.return_value = {"transcribed_text": "heard", "score": 95.0, "duration": 4.2}
            row = check_gui.transcribe_sample(wav_path, 1, "NPC", "expected")

        self.assertEqual(row["AudioPath"], wav_path.resolve())
        self.assertEqual(row["AudioFile"], wav_path.name)
        self.assertEqual(row["Duration"], 4.2)

    def test_detail_playback_resets_and_plays_selected_audio(self):
        wav_path = Path(tempfile.mkdtemp()) / "TS000001.WAV"
        wav_path.touch()
        selected_item = Mock()
        selected_item.row.return_value = 0
        window = Mock()
        window.detail_table.selectedItems.return_value = [selected_item]
        window._detail_samples = [{"AudioPath": wav_path}]

        check_gui.CheckWindow._play_selected_detail_sample(window)

        window.media_player.stop.assert_called_once_with()
        window.audio_output.setVolume.assert_called_once_with(0.7)
        check_gui.QUrl.fromLocalFile.assert_called_once_with(str(wav_path))
        self.assertEqual(window.media_player.setSource.call_count, 2)
        window.media_player.play.assert_called_once_with()
        window.statusBar().showMessage.assert_called_once_with(
            f"🔊 Playing: {wav_path.name}", 3000)

    def test_detail_playback_reports_missing_audio(self):
        selected_item = Mock()
        selected_item.row.return_value = 0
        window = Mock()
        window.detail_table.selectedItems.return_value = [selected_item]
        window._detail_samples = [{"AudioPath": Path("missing.WAV")}]

        check_gui.CheckWindow._play_selected_detail_sample(window)

        window.media_player.stop.assert_called_once_with()
        window.media_player.setSource.assert_not_called()
        window.media_player.play.assert_not_called()
        window.statusBar().showMessage.assert_called_once_with(
            "⚠️ Audio file not found", 5000)


class TestCheckGuiCsv(unittest.TestCase):
    def test_export_csv_includes_duration_and_audio_path(self):
        tmp_dir = Path(tempfile.mkdtemp())
        wav_path = tmp_dir / "AERIE001.WAV"
        wav_path.touch()
        export_csv_path = tmp_dir / "exported.csv"

        window = Mock()
        window._all_npc_data = {
            "AERIE": {
                "npc": "AERIE",
                "samples": [
                    {
                        "NPC": "AERIE",
                        "StrRef": 101,
                        "AudioFile": "AERIE001.WAV",
                        "AudioPath": wav_path,
                        "SimilarityScore": 94.5,
                        "Duration": 3.75,
                        "CSVText": "Hello my friend.",
                        "TranscribedText": "Hello my friend.",
                    }
                ],
            }
        }

        with patch.object(
            check_gui.QFileDialog,
            "getSaveFileName",
            return_value=(str(export_csv_path), "CSV Files (*.csv)"),
        ):
            check_gui.CheckWindow._export_csv(window)

        self.assertTrue(export_csv_path.exists())
        import csv

        with open(export_csv_path, "r", encoding="utf-8-sig") as f:
            reader = list(csv.DictReader(f))

        self.assertEqual(len(reader), 1)
        row = reader[0]
        self.assertEqual(row["NPC"], "AERIE")
        self.assertEqual(row["StrRef"], "101")
        self.assertEqual(row["AudioFile"], "AERIE001.WAV")
        self.assertEqual(row["AudioPath"], str(wav_path))
        self.assertEqual(row["SimilarityScore"], "94.5")
        self.assertEqual(row["Duration"], "3.75")
        self.assertEqual(row["CSVText"], "Hello my friend.")
        self.assertEqual(row["TranscribedText"], "Hello my friend.")

    def test_parse_imported_sample_normalizes_data(self):
        tmp_dir = Path(tempfile.mkdtemp())
        wav_path = tmp_dir / "MINSC001.WAV"
        wav_path.touch()

        window = check_gui.CheckWindow.__new__(check_gui.CheckWindow)

        # Case 1: Standard exported row with existing AudioPath
        row1 = {
            "NPC": "MINSC",
            "StrRef": "205",
            "AudioFile": "MINSC001.WAV",
            "AudioPath": str(wav_path),
            "SimilarityScore": "88.0",
            "Duration": "2.5",
            "CSVText": "Go for the eyes, Boo!",
            "TranscribedText": "Go for the eyes Boo!",
        }
        npc1, sample1 = window._parse_imported_sample(row1)
        self.assertEqual(npc1, "MINSC")
        self.assertEqual(sample1["StrRef"], 205)
        self.assertEqual(sample1["SimilarityScore"], 88.0)
        self.assertEqual(sample1["Duration"], 2.5)
        self.assertEqual(sample1["AudioPath"], wav_path.resolve())

        # Case 2: Alternative column names and fallback path
        row2 = {
            "NPC Name": "IMOEN",
            "strref": "bad_int",
            "Audio File": "IMOEN001.WAV",
            "score": "91.2",
            "duration": "",
            "ExpectedText": "Heya!",
            "Transcribed": "Heya!",
        }
        npc2, sample2 = window._parse_imported_sample(row2)
        self.assertEqual(npc2, "IMOEN")
        self.assertEqual(sample2["StrRef"], 0)
        self.assertEqual(sample2["SimilarityScore"], 91.2)
        self.assertEqual(sample2["Duration"], 0.0)
        self.assertEqual(sample2["CSVText"], "Heya!")
        self.assertEqual(sample2["TranscribedText"], "Heya!")

    def test_import_csv_populates_data_and_recalculates_metrics(self):
        tmp_dir = Path(tempfile.mkdtemp())
        csv_file = tmp_dir / "test_import.csv"

        import csv

        with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "NPC",
                    "StrRef",
                    "AudioFile",
                    "SimilarityScore",
                    "Duration",
                    "CSVText",
                    "TranscribedText",
                ],
            )
            writer.writeheader()
            writer.writerow({
                "NPC": "JAHEIRA",
                "StrRef": 301,
                "AudioFile": "JAH01.WAV",
                "SimilarityScore": 80.0,
                "Duration": 2.0,
                "CSVText": "Nature's servant.",
                "TranscribedText": "Nature's servant.",
            })
            writer.writerow({
                "NPC": "JAHEIRA",
                "StrRef": 302,
                "AudioFile": "JAH02.WAV",
                "SimilarityScore": 100.0,
                "Duration": 4.0,
                "CSVText": "Yes?",
                "TranscribedText": "Yes?",
            })

        window = Mock()
        window._all_npc_data = {}
        window._npc_row_index = {}
        window._selected_npc = None
        window.npc_table = Mock()
        window.npc_table.rowCount.return_value = 1
        window.detail_table = Mock()
        window.detail_placeholder = Mock()
        window.overall_bar = Mock()
        window.overall_label = Mock()
        window.export_btn = Mock()
        window._parse_imported_sample = check_gui.CheckWindow._parse_imported_sample.__get__(
            window, check_gui.CheckWindow
        )

        with patch.object(
            check_gui.QFileDialog,
            "getOpenFileName",
            return_value=(str(csv_file), "CSV Files (*.csv)"),
        ):
            check_gui.CheckWindow._import_csv(window)

        self.assertIn("JAHEIRA", window._all_npc_data)
        jah_data = window._all_npc_data["JAHEIRA"]
        self.assertEqual(len(jah_data["samples"]), 2)
        self.assertEqual(jah_data["worst_score"], 80.0)
        self.assertEqual(jah_data["avg_score"], 90.0)
        self.assertEqual(jah_data["sum_duration"], 6.0)
        self.assertTrue(jah_data["done"])
        window.export_btn.setEnabled.assert_called_with(True)
        window.overall_bar.setValue.assert_called_with(10_000)
        window.npc_table.selectRow.assert_called_with(0)

    def test_import_csv_cancelled_or_empty(self):
        window = Mock()
        window._all_npc_data = {}
        with patch.object(
            check_gui.QFileDialog,
            "getOpenFileName",
            return_value=("", ""),
        ):
            check_gui.CheckWindow._import_csv(window)
        self.assertEqual(len(window._all_npc_data), 0)

        tmp_dir = Path(tempfile.mkdtemp())
        empty_csv = tmp_dir / "empty.csv"
        empty_csv.touch()

        with patch.object(
            check_gui.QFileDialog,
            "getOpenFileName",
            return_value=(str(empty_csv), "CSV Files (*.csv)"),
        ):
            check_gui.CheckWindow._import_csv(window)
        window.statusBar().showMessage.assert_called_with(
            "No data found in imported CSV.", 5000
        )


if __name__ == "__main__":
    unittest.main()
