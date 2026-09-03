"""
Small Qt configuration utility for the game-specific settings.

Provides a graphical interface for editing and validating configuration
settings used by the game-data workflows, including game directory paths,
language selection, text encoding, and Voicebox API configuration.
"""

from __future__ import annotations

import codecs
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any

import requests
from libs.appconfig import cfg, set_many
from libs.utils import load_patcher_config, update_patcher_config
from PySide6.QtCore import Qt, QLocale, QObject, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class HealthCheckWorker(QObject):
    """
    Runs a single Voicebox API health check off the UI thread.

    Performs an asynchronous GET request to the /health endpoint of the
    Voicebox API and emits the result via the finished signal.

    Signals:
        finished: Emitted with (success_bool, payload_dict) when the
            health check completes.
    """

    finished = Signal(bool, dict)

    def __init__(self, base_url: str) -> None:
        """
        Initialize the health check worker.

        Args:
            base_url: Base URL of the Voicebox API (e.g., http://localhost:17493).
        """
        super().__init__()
        self._base_url = base_url

    def run(self) -> None:
        """
        Execute the health check request.

        Sends a GET request to /health endpoint with a 5-second timeout.
        Emits finished signal with success status and response payload
        or error information.
        """
        try:
            resp = requests.get(f"{self._base_url}/health", timeout=5)
            resp.raise_for_status()
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            self.finished.emit(True, payload)
        except Exception as exc:
            # Surface any failure reason to the UI
            self.finished.emit(False, {"error": str(exc)})


class ConfigWindow(QMainWindow):
    """
    Edit and validate the settings required by the game-data workflows.

    Provides a tabbed interface for configuring:
        - Game directory path
        - Language selection (auto-detected from game's lang directory)
        - Text encoding
        - Voicebox API URL, engine, model size
        - Transcription language

    Features real-time validation and health checking for the API endpoint.
    """

    def __init__(self) -> None:
        """Initialize the configuration window and build the UI."""
        super().__init__()
        self.setWindowTitle("VVE Builder - Configuration")
        self.setMinimumWidth(620)

        self.game_directory_edit = QLineEdit(str(cfg.GAME_DIRECTORY))
        self.game_directory_edit.setPlaceholderText("Select the game installation directory")
        self.game_directory_edit.textChanged.connect(self.refresh_languages)

        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.browse_game_directory)

        directory_row = QHBoxLayout()
        directory_row.addWidget(self.game_directory_edit)
        directory_row.addWidget(browse_button)

        self.language_combo = QComboBox()
        self.language_combo.setToolTip("Languages found in the game's lang directory")
        self.language_combo.currentTextChanged.connect(self.on_language_changed)

        self.encoding_edit = QLineEdit(str(cfg.TEXT_ENCODING))
        self.encoding_edit.setPlaceholderText("For example: utf-8")
        self.encoding_edit.textChanged.connect(self.validate)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.addRow("Game directory", directory_row)
        form.addRow("Language", self.language_combo)
        form.addRow("Text encoding", self.encoding_edit)

        settings_group = QGroupBox("Game data")
        settings_group.setLayout(form)

        pc_group = self._build_player_character_group()

        game_data_container = QWidget()
        game_data_layout = QVBoxLayout(game_data_container)
        game_data_layout.setContentsMargins(0, 0, 0, 0)
        game_data_layout.addWidget(settings_group)
        game_data_layout.addWidget(pc_group)
        game_data_layout.addStretch()

        api_tab = self._build_api_tab()

        tabs = QTabWidget()
        tabs.addTab(game_data_container, "Game data")
        tabs.addTab(api_tab, "API defaults")

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        save_button = QPushButton("Save")
        save_button.setDefault(True)
        save_button.clicked.connect(self.save)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.close)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(QLabel("Review the settings used to read and extract game dialog data."))
        layout.addWidget(tabs)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)
        self.setCentralWidget(central_widget)

        self.save_button = save_button
        self._health_thread: Optional[QThread] = None
        self._health_worker: Optional[HealthCheckWorker] = None
        self.refresh_languages()

    def _build_player_character_group(self) -> QWidget:
        """
        Build the "Player character" settings group.

        Exposes pcName, pcRace, and pcGender from patcher-config.json,
        used to fill the <CHARNAME>/<GABBER>, <PRO_RACE>/<RACE>, and
        gendered tokens during TTS text preprocessing.

        Returns:
            QGroupBox containing the player character settings form.
        """
        try:
            patcher_config = load_patcher_config(cfg.PATCHER_CONFIG_PATH)
        except (FileNotFoundError, ValueError):
            patcher_config = {}

        self.pc_name_edit = QLineEdit(str(patcher_config.get("pcName", "adventurer")))
        self.pc_name_edit.textChanged.connect(self.validate)

        self.pc_race_edit = QLineEdit(str(patcher_config.get("pcRace", "adventurer")))
        self.pc_race_edit.textChanged.connect(self.validate)

        self.pc_gender_combo = QComboBox()
        self.pc_gender_combo.addItems(["male", "female", "neutral"])
        pc_gender = str(patcher_config.get("pcGender", "neutral"))
        if pc_gender in ("male", "female", "neutral"):
            self.pc_gender_combo.setCurrentText(pc_gender)
        self.pc_gender_combo.currentTextChanged.connect(self.validate)

        pc_form = QFormLayout()
        pc_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        pc_form.addRow("Player name", self.pc_name_edit)
        pc_form.addRow("Player race", self.pc_race_edit)
        pc_form.addRow("Player gender", self.pc_gender_combo)

        pc_group = QGroupBox("Player character")
        pc_group.setLayout(pc_form)
        return pc_group

    def _build_api_tab(self) -> QWidget:
        """
        Build the API configuration tab.

        Returns:
            QWidget containing the API settings form.
        """
        self.base_url_edit = QLineEdit(str(cfg.BASE_URL))
        self.base_url_edit.setPlaceholderText("http://localhost:8000")
        self.base_url_edit.textChanged.connect(self.validate)

        self.health_dot = QLabel()
        self.health_dot.setFixedSize(14, 14)
        self.health_dot.setStyleSheet("background-color: gray; border-radius: 7px;")
        self.health_dot.setToolTip("Health not checked yet")

        check_button = QPushButton("Check now")
        check_button.clicked.connect(self.check_health)
        self.check_health_button = check_button

        base_url_row = QHBoxLayout()
        base_url_row.addWidget(self.base_url_edit)
        base_url_row.addWidget(self.health_dot)
        base_url_row.addWidget(check_button)

        self.engine_edit = QLineEdit(str(cfg.ENGINE))
        self.engine_edit.textChanged.connect(self.validate)

        self.model_size_edit = QLineEdit(str(cfg.MODEL_SIZE))
        self.model_size_edit.textChanged.connect(self.validate)

        self.transcription_language_edit = QLineEdit(str(cfg.TRANSCRIPTION_LANGUAGE))
        self.transcription_language_edit.setPlaceholderText("For example: English")
        self.transcription_language_edit.textChanged.connect(self.validate)

        api_form = QFormLayout()
        api_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        api_form.addRow("Voicebox URL", base_url_row)
        api_form.addRow("Engine", self.engine_edit)
        api_form.addRow("Model size", self.model_size_edit)
        api_form.addRow("Transcription language", self.transcription_language_edit)

        api_group = QGroupBox("Voicebox API")
        api_group.setLayout(api_form)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addWidget(api_group)
        container_layout.addStretch()
        return container

    def check_health(self) -> None:
        """
        Kick off a single async health check against the current URL field.

        Starts a background thread to query the /health endpoint and
        updates the status indicator when complete. Prevents multiple
        concurrent health checks.
        """
        if self._health_thread is not None:
            return  # a check is already in flight

        base_url = self.base_url_edit.text().strip()
        if not base_url:
            self.health_dot.setStyleSheet("background-color: gray; border-radius: 7px;")
            self.health_dot.setToolTip("No URL to check")
            return

        self.health_dot.setStyleSheet("background-color: #d9a63b; border-radius: 7px;")
        self.health_dot.setToolTip("Checking Voicebox API health...")
        self.check_health_button.setEnabled(False)

        thread = QThread(self)
        worker = HealthCheckWorker(base_url)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self.on_health_checked)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._clear_health_thread)

        self._health_thread = thread
        self._health_worker = worker
        thread.start()

    def _clear_health_thread(self) -> None:
        """Clean up the health check thread and worker after completion."""
        if self._health_worker is not None:
            self._health_worker.deleteLater()
        if self._health_thread is not None:
            self._health_thread.deleteLater()
        self._health_worker = None
        self._health_thread = None
        self.check_health_button.setEnabled(True)

    def on_health_checked(self, ok: bool, payload: Dict[str, Any]) -> None:
        """
        Handle the health check result.

        Updates the status indicator color and tooltip based on success/failure.

        Args:
            ok: True if the health check succeeded, False otherwise.
            payload: Response payload from the API or error information.
        """
        if ok:
            self.health_dot.setStyleSheet("background-color: #28a745; border-radius: 7px;")
            self.health_dot.setToolTip("Voicebox API is reachable")
        else:
            self.health_dot.setStyleSheet("background-color: #a33b2f; border-radius: 7px;")
            error = payload.get("error", "Unknown error")
            self.health_dot.setToolTip(f"Voicebox API check failed: {error}")

    def browse_game_directory(self) -> None:
        """
        Open a directory selection dialog for the game installation.

        Updates the game directory field with the selected path.
        """
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select game directory",
            self.game_directory_edit.text(),
        )
        if selected:
            self.game_directory_edit.setText(selected)

    def available_languages(self) -> List[str]:
        """
        Detect available language directories in the game's lang directory.

        Returns:
            Sorted list of language directory names found in the game's
            lang directory, or empty list if the directory doesn't exist.
        """
        lang_directory = Path(self.game_directory_edit.text()).expanduser() / "lang"
        if not lang_directory.is_dir():
            return []
        return sorted(
            (entry.name for entry in lang_directory.iterdir() if entry.is_dir()),
            key=str.casefold,
        )

    def on_language_changed(self, locale_name: str) -> None:
        """
        Handle language selection change.

        Automatically sets the transcription language to the English name
        of the selected locale when possible.

        Args:
            locale_name: The selected locale name (e.g., "en_US").
        """
        if locale_name:
            loc = QLocale(locale_name)
            if loc.language() != QLocale.Language.C:
                english_name = QLocale.languageToString(loc.language())
                self.transcription_language_edit.setText(english_name.lower())
        self.validate()

    def refresh_languages(self) -> None:
        """
        Refresh the language combo box with available languages.

        Preserves the previous selection if still available, otherwise
        selects the first available language.
        """
        previous = self.language_combo.currentText() or str(cfg.LANGUAGE)
        languages = self.available_languages()
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        self.language_combo.addItems(languages)
        if previous in languages:
            self.language_combo.setCurrentText(previous)
        elif languages:
            self.language_combo.setCurrentIndex(0)
        self.language_combo.blockSignals(False)
        self.validate()

    def validation_errors(self) -> List[str]:
        """
        Validate all configuration fields and collect errors.

        Returns:
            List of error messages describing validation failures.
            Empty list if all settings are valid.
        """
        game_directory = Path(self.game_directory_edit.text()).expanduser()
        languages = self.available_languages()
        transcription_lang = self.transcription_language_edit.text().strip()
        encoding = self.encoding_edit.text().strip()
        errors: List[str] = []

        if not game_directory.is_dir():
            errors.append("Game directory does not exist or is not a directory.")
        elif not (game_directory / "lang").is_dir():
            errors.append("The game directory does not contain a lang directory.")

        if not languages:
            errors.append("No language directories were found in the game's lang directory.")
        elif self.language_combo.currentText() not in languages:
            errors.append("Select one of the languages found in the lang directory.")

        if not transcription_lang:
            errors.append("Transcription language cannot be empty.")

        if not encoding:
            errors.append("Text encoding cannot be empty.")
        else:
            try:
                codecs.lookup(encoding)
            except LookupError:
                errors.append(f"Unknown text encoding: {encoding}")

        if not self.base_url_edit.text().strip():
            errors.append("Voicebox URL cannot be empty.")

        if not self.engine_edit.text().strip():
            errors.append("Engine cannot be empty.")

        if not self.model_size_edit.text().strip():
            errors.append("Model size cannot be empty.")

        if not self.pc_name_edit.text().strip():
            errors.append("Player name cannot be empty.")

        if not self.pc_race_edit.text().strip():
            errors.append("Player race cannot be empty.")

        return errors

    def validate(self) -> None:
        """
        Perform validation and update the status display accordingly.

        Enables/disables the Save button based on validation results.
        """
        errors = self.validation_errors()
        if errors:
            self.status_label.setText("<b>Needs attention</b><br>" + "<br>".join(errors))
            self.status_label.setStyleSheet("color: #a33b2f;")
            self.save_button.setEnabled(False)
        else:
            self.status_label.setText("Settings are valid and ready to save.")
            self.status_label.setStyleSheet("color: #28784a;")
            self.save_button.setEnabled(True)

    def save(self) -> None:
        """
        Save the current configuration to appconfig.json.

        Validates settings before saving and displays success/failure
        message dialogs. Closes the window on success.
        """
        errors = self.validation_errors()
        if errors:
            QMessageBox.warning(self, "Invalid configuration", "\n".join(errors))
            return

        set_many(
            {
                "GAME_DIRECTORY": str(Path(self.game_directory_edit.text()).expanduser()),
                "LANGUAGE": self.language_combo.currentText(),
                "TRANSCRIPTION_LANGUAGE": self.transcription_language_edit.text().strip(),
                "TEXT_ENCODING": self.encoding_edit.text().strip(),
                "BASE_URL": self.base_url_edit.text().strip(),
                "ENGINE": self.engine_edit.text().strip(),
                "MODEL_SIZE": self.model_size_edit.text().strip(),
            }
        )
        update_patcher_config(
            cfg.PATCHER_CONFIG_PATH,
            {
                "pcName": self.pc_name_edit.text().strip(),
                "pcRace": self.pc_race_edit.text().strip(),
                "pcGender": self.pc_gender_combo.currentText(),
            },
        )
        QMessageBox.information(self, "Configuration saved", "The settings were saved successfully.")
        self.close()


def main() -> int:
    """
    Application entry point.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    app = QApplication(sys.argv)
    window = ConfigWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())