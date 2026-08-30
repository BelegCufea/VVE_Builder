"""Small Qt configuration utility for the game-specific settings."""

from __future__ import annotations

import codecs
import sys
from pathlib import Path

import requests
from appconfig import cfg, set_many
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
    """Runs a single Voicebox API health check off the UI thread."""

    finished = Signal(bool, dict)

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._base_url = base_url

    def run(self) -> None:
        try:
            resp = requests.get(f"{self._base_url}/health", timeout=5)
            resp.raise_for_status()
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            self.finished.emit(True, payload)
        except Exception as exc:  # noqa: BLE001 - surface any failure reason to the UI
            self.finished.emit(False, {"error": str(exc)})


class ConfigWindow(QMainWindow):
    """Edit and validate the settings required by the game-data workflows."""

    def __init__(self) -> None:
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

        api_tab = self._build_api_tab()

        tabs = QTabWidget()
        tabs.addTab(settings_group, "Game data")
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
        self._health_thread: QThread | None = None
        self._health_worker: HealthCheckWorker | None = None
        self.refresh_languages()

    def _build_api_tab(self) -> QWidget:
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
        """Kick off a single async health check against the current URL field."""
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
        if self._health_worker is not None:
            self._health_worker.deleteLater()
        if self._health_thread is not None:
            self._health_thread.deleteLater()
        self._health_worker = None
        self._health_thread = None
        self.check_health_button.setEnabled(True)

    def on_health_checked(self, ok: bool, payload: dict) -> None:
        if ok:
            self.health_dot.setStyleSheet("background-color: #28a745; border-radius: 7px;")
            self.health_dot.setToolTip("Voicebox API is reachable")
        else:
            self.health_dot.setStyleSheet("background-color: #a33b2f; border-radius: 7px;")
            error = payload.get("error", "Unknown error")
            self.health_dot.setToolTip(f"Voicebox API check failed: {error}")

    def browse_game_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select game directory",
            self.game_directory_edit.text(),
        )
        if selected:
            self.game_directory_edit.setText(selected)

    def available_languages(self) -> list[str]:
        lang_directory = Path(self.game_directory_edit.text()).expanduser() / "lang"
        if not lang_directory.is_dir():
            return []
        return sorted(
            (entry.name for entry in lang_directory.iterdir() if entry.is_dir()),
            key=str.casefold,
        )

    def on_language_changed(self, locale_name: str) -> None:
        if locale_name:
            loc = QLocale(locale_name)
            if loc.language() != QLocale.Language.C:
                english_name = QLocale.languageToString(loc.language())
                self.transcription_language_edit.setText(english_name.lower())
        self.validate()    

    def refresh_languages(self) -> None:
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

    def validation_errors(self) -> list[str]:
        game_directory = Path(self.game_directory_edit.text()).expanduser()
        languages = self.available_languages()
        transcription_lang = self.transcription_language_edit.text().strip()
        encoding = self.encoding_edit.text().strip()
        errors: list[str] = []

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

        return errors

    def validate(self) -> None:
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
        QMessageBox.information(self, "Configuration saved", "The settings were saved successfully.")
        self.close()


def main() -> int:
    app = QApplication(sys.argv)
    window = ConfigWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())