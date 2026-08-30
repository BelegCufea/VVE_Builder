"""Small Qt configuration utility for the game-specific settings."""

from __future__ import annotations

import codecs
import sys
from pathlib import Path

from appconfig import cfg, set_many
from PySide6.QtCore import Qt, QLocale
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
    QVBoxLayout,
    QWidget,
)


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

        self.transcription_language_edit = QLineEdit(str(cfg.TRANSCRIPTION_LANGUAGE))
        self.transcription_language_edit.setPlaceholderText("For example: English")
        self.transcription_language_edit.textChanged.connect(self.validate)        

        self.encoding_edit = QLineEdit(str(cfg.TEXT_ENCODING))
        self.encoding_edit.setPlaceholderText("For example: utf-8")
        self.encoding_edit.textChanged.connect(self.validate)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.addRow("Game directory", directory_row)
        form.addRow("Language", self.language_combo)
        form.addRow("Transcription language", self.transcription_language_edit)
        form.addRow("Text encoding", self.encoding_edit)

        settings_group = QGroupBox("Game data")
        settings_group.setLayout(form)

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
        layout.addWidget(settings_group)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)
        self.setCentralWidget(central_widget)

        self.save_button = save_button
        self.refresh_languages()

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