"""
Voice Profile Manager (PySide6 desktop version)

A native desktop GUI for managing voice profile assignments across
three levels: NPC Name, NPC+Gender, and System Name.
"""

import sys
import json
import time
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem, QLineEdit, QLabel, QPushButton, QComboBox,
    QScrollArea, QSplitter, QGroupBox, QFrame, QMessageBox, QStatusBar,
)

# ============================================================================
# Configuration
# ============================================================================

DEBUG = True


def debug_print(*args, **kwargs):
    if DEBUG:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] DEBUG:", *args, **kwargs, file=sys.stderr)


CSV_PATH = "dialog-report.csv"
VOICES_DIR = "voices"
VOICE_SUBSTITUTIONS_FILE = "voice-substitutions.json"
VOICE_SUBSTITUTIONS_GENDER_FILE = "voice-substitutions-gender.json"
VOICE_SUBSTITUTIONS_SYSNAME_FILE = "voice-substitutions-sysname.json"


# ============================================================================
# Data loading / saving
# ============================================================================

def load_csv(csv_path: str) -> pd.DataFrame:
    debug_print(f"Loading CSV from: {csv_path}")
    try:
        df = pd.read_csv(csv_path)
        debug_print(f"CSV loaded: {len(df)} rows, {len(df.columns)} columns")
        return df
    except Exception as e:
        debug_print(f"ERROR loading CSV: {e}")
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
                debug_print(f"Loaded {len(data)} entries from {path_str}")
            except Exception as e:
                debug_print(f"ERROR loading {path_str}: {e}")
    return substitutions, gender_substitutions, sys_substitutions


def save_json_file(file_path: str, data: Dict) -> bool:
    try:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        debug_print(f"Saved {file_path} ({len(data)} entries)")
        return True
    except Exception as e:
        debug_print(f"ERROR saving {file_path}: {e}")
        return False


def get_available_voice_profiles() -> List[str]:
    """Get unique voice profile names (grouping "Boy 2.wav" as "Boy")."""
    voices_dir = Path(VOICES_DIR)
    if not voices_dir.exists():
        return []
    
    profiles = set()
    for wav_path in list(voices_dir.glob("*.WAV")) + list(voices_dir.glob("*.wav")):
        base_name = re.sub(r'\s+\d+$', '', wav_path.stem)
        profiles.add(base_name)
    
    debug_print(f"Found {len(profiles)} unique voice profiles")
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
    debug_print("Building NPC hierarchy...")
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

    debug_print(f"Built hierarchy with {len(hierarchy)} NPCs in {time.time() - start_time:.2f}s")
    return hierarchy


# ============================================================================
# Main window
# ============================================================================

class VoiceProfileManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎯 Voice Profile Manager")
        self.resize(1400, 900)

        # --- Load everything once at startup ---
        self.df = load_csv(CSV_PATH)
        self.substitutions, self.gender_substitutions, self.sys_substitutions = load_json_files()
        self.available_voices = get_available_voice_profiles()
        self.existing_voices = get_existing_voice_files()

        if self.df.empty:
            QMessageBox.critical(self, "Error", f"Could not load CSV file: {CSV_PATH}")

        self.hierarchy = build_hierarchy(
            self.df, self.substitutions, self.gender_substitutions,
            self.sys_substitutions, self.existing_voices,
        )

        self.npc_names = sorted(self.hierarchy.keys())
        self.selected_npc: Optional[str] = self.npc_names[0] if self.npc_names else None

        self._build_ui()
        self._populate_npc_list()
        if self.selected_npc:
            self._select_npc_in_list(self.selected_npc)

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

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("🔍 Type to filter...")
        self.search_box.textChanged.connect(self._on_search_changed)
        left_layout.addWidget(self.search_box)

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
    def _npc_icon(self, npc_name: str) -> str:
        data = self.hierarchy[npc_name]
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
            return "🟢"
        elif has_assignments:
            return "✅"
        return "🔴"

    def _populate_npc_list(self, filter_text: str = ""):
        self.npc_list.blockSignals(True)
        self.npc_list.clear()
        filter_lower = filter_text.lower()
        shown = 0
        for name in self.npc_names:
            if filter_lower and filter_lower not in name.lower():
                continue
            item = QListWidgetItem(f"{self._npc_icon(name)}  {name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.npc_list.addItem(item)
            shown += 1
        self.npc_count_label.setText(f"Showing {shown} of {len(self.npc_names)} NPCs")
        self.npc_list.blockSignals(False)

    def _refresh_all_list_icons(self):
        """Update all list item icons to reflect current state."""
        self.npc_list.blockSignals(True)
        for i in range(self.npc_list.count()):
            item = self.npc_list.item(i)
            name = item.data(Qt.ItemDataRole.UserRole)
            if name in self.hierarchy:
                item.setText(f"{self._npc_icon(name)}  {name}")
        self.npc_list.blockSignals(False)

    def _select_npc_in_list(self, npc_name: str):
        for i in range(self.npc_list.count()):
            item = self.npc_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == npc_name:
                self.npc_list.setCurrentItem(item)
                return
        self.selected_npc = npc_name
        self._render_detail_panel()

    def _on_search_changed(self, text: str):
        self._populate_npc_list(text)

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

    def _make_voice_combo(self, current_voice: Optional[str], on_change) -> QComboBox:
        combo = QComboBox()
        options = [""] + self.available_voices
        combo.addItems(options)
        current_voice = current_voice or ""
        combo.blockSignals(True)
        if current_voice in options:
            combo.setCurrentIndex(options.index(current_voice))
        combo.blockSignals(False)
        combo.currentTextChanged.connect(on_change)
        return combo

    def _render_detail_panel(self):
        self._clear_layout(self.detail_layout)

        if not self.selected_npc:
            self.detail_layout.addWidget(QLabel("ℹ️ No NPC selected."))
            return

        npc_name = self.selected_npc
        npc_data = self.hierarchy[npc_name]
        has_existing = npc_data.get("has_existing_voice", False)
        has_assignments = (
            npc_data["assigned_voice"] is not None
            or any(g["assigned_voice"] is not None for g in npc_data["genders"].values())
            or any(
                s["assigned_voice"] is not None
                for g in npc_data["genders"].values()
                for s in g["sysnames"]
            )
        )
        icon = "🟢" if has_existing else ("✅" if has_assignments else "🔴")

        # --- Header ---
        header_frame = QFrame()
        header_frame.setFrameShape(QFrame.Shape.StyledPanel)
        header_layout = QHBoxLayout(header_frame)

        title_box = QVBoxLayout()
        title_label = QLabel(f"<h2>{icon} {npc_name}</h2>")
        title_box.addWidget(title_label)
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
        npc_group = QGroupBox("📌 NPC Level Assignment")
        npc_form = QFormLayout(npc_group)
        npc_combo = self._make_voice_combo(
            npc_data["assigned_voice"],
            lambda text: self._on_npc_voice_changed(npc_name, text),
        )
        npc_form.addRow("Voice:", npc_combo)
        self.detail_layout.addWidget(npc_group)

        # --- Gender level (shown if genders exist) ---
        if npc_data["genders"]:
            gender_group = QGroupBox("🚻 Gender Overrides")
            gender_layout = QVBoxLayout(gender_group)

            # First, render all gender comboboxes
            for gender, gender_data in npc_data["genders"].items():
                gform = QFormLayout()
                gcombo = self._make_voice_combo(
                    gender_data["assigned_voice"],
                    lambda text, g=gender: self._on_gender_voice_changed(npc_name, g, text),
                )
                gform.addRow(f"Voice for {gender}:", gcombo)
                gender_layout.addLayout(gform)

            # Then, render all system name groups (one per gender)
            for gender, gender_data in npc_data["genders"].items():
                if gender_data["sysnames"]:
                    sys_group = QGroupBox(f"📋 System Name Overrides ({gender})")
                    sys_form = QFormLayout(sys_group)
                    for sys in gender_data["sysnames"]:
                        sysname = sys["name"]
                        scombo = self._make_voice_combo(
                            sys["assigned_voice"],
                            lambda text, s=sysname: self._on_sys_voice_changed(s, text),
                        )
                        sys_form.addRow(f"{sysname}:", scombo)
                    gender_layout.addWidget(sys_group)

                # Add separator between gender sections if both have systems
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
        self.hierarchy[npc_name] = build_hierarchy_for_npc(
            self.df, npc_name, self.substitutions, self.gender_substitutions,
            self.sys_substitutions, self.existing_voices,
        )

    def _on_npc_voice_changed(self, npc_name: str, new_voice: str):
        current = self.substitutions.get(npc_name) or ""
        if new_voice == current:
            return
        debug_print(f"Changing NPC voice: {npc_name} -> {new_voice or 'unassigned'}")
        if new_voice:
            self.substitutions[npc_name] = new_voice
        else:
            self.substitutions.pop(npc_name, None)
        if save_json_file(VOICE_SUBSTITUTIONS_FILE, self.substitutions):
            self._refresh_npc_entry(npc_name)
            self.statusBar().showMessage(f"✅ Updated {npc_name} → {new_voice or 'unassigned'}", 3000)
            self._update_stats()
            self._refresh_all_list_icons()
            self._render_detail_panel()

    def _on_gender_voice_changed(self, npc_name: str, gender: str, new_voice: str):
        gender_key = f"{npc_name}|{gender}"
        current = self.gender_substitutions.get(gender_key) or ""
        if new_voice == current:
            return
        debug_print(f"Changing gender voice: {gender_key} -> {new_voice or 'unassigned'}")
        if new_voice:
            self.gender_substitutions[gender_key] = new_voice
        else:
            self.gender_substitutions.pop(gender_key, None)
        if save_json_file(VOICE_SUBSTITUTIONS_GENDER_FILE, self.gender_substitutions):
            self._refresh_npc_entry(npc_name)
            self.statusBar().showMessage(f"✅ Updated {npc_name}|{gender} → {new_voice or 'unassigned'}", 3000)
            self._update_stats()
            self._refresh_all_list_icons()
            self._render_detail_panel()

    def _on_sys_voice_changed(self, sysname: str, new_voice: str):
        current = self.sys_substitutions.get(sysname) or ""
        if new_voice == current:
            return
        debug_print(f"Changing system voice: {sysname} -> {new_voice or 'unassigned'}")
        if new_voice:
            self.sys_substitutions[sysname] = new_voice
        else:
            self.sys_substitutions.pop(sysname, None)
        if save_json_file(VOICE_SUBSTITUTIONS_SYSNAME_FILE, self.sys_substitutions):
            if self.selected_npc is not None:
                self._refresh_npc_entry(self.selected_npc)
            self.statusBar().showMessage(f"✅ Updated {sysname} → {new_voice or 'unassigned'}", 3000)
            self._update_stats()
            self._refresh_all_list_icons()
            self._render_detail_panel()

    def _update_stats(self):
        npcs_with_voice = sum(1 for d in self.hierarchy.values() if d.get("has_existing_voice", False))
        self.stats_total_label.setText(str(len(self.hierarchy)))
        self.stats_voices_label.setText(str(len(self.available_voices)))
        self.stats_existing_label.setText(str(npcs_with_voice))
        self.stats_npc_level_label.setText(str(len(self.substitutions)))
        self.stats_gender_level_label.setText(str(len(self.gender_substitutions)))
        self.stats_sys_level_label.setText(str(len(self.sys_substitutions)))


def main():
    app = QApplication(sys.argv)
    window = VoiceProfileManager()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()