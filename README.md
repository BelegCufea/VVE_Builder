# VVE Builder

**VVE Builder** is an automation pipeline for creating, managing, and compiling AI-generated voiceover mods for Infinity Engine games using **WeiDU** and **VoiceBox**. It is inspired by the workflow used by projects like Voices Voices Extravaganza.

The project consists of several Python scripts that semi-automate the process, along with sample JSON configuration files for Baldur's Gate II: Enhanced Edition.

---

## Workflow Pipeline

1. **Dialog Processing**: Extract and structure game dialog sources into working tracking sheets.
2. **Profile Preparation**: Extract and assemble suitable source samples for NPCs already voiced in the game.
3. **Profile Management**: Audit extracted voice samples, create new ones and assign them to NPCs.
4. **Audio Generation**: Execute batch text-to-speech generation via **VoiceBox**, utilizing cache and memory management to prevent redundant audio processing.
5. **Compile Mod**: Build the WeiDU package with bundled generated audio assets for seamless in-game installation.

## Requirements

### Python requirements
Install the project dependencies listed in [requirements.txt](requirements.txt):

- requests
- runstats
- pandas>=2.0
- PySide6>=6.5

```bash
pip install -r requirements.txt
```

### External tools
- A working Infinity Engine game installation for dialog extraction.
- WeiDU: install it from [WeiDU.org: Infinity Engine Utilities and Mods](https://weidu.org/) and keep the local WeiDU files available for the extraction step. This project also includes a bundled local copy in the [weidu](weidu) folder on Windows.
- ffmpeg: required for audio conversion and validation. On Windows, for example:

```powershell
winget install Gyan.Dev.FFmpeg
```

- VoiceBox: this toolchain expects a running VoiceBox instance at the configured API URL. Use the service at https://voicebox.sh/ and prefer the Qwen 1.7B model.

## Script-to-Step Mapping

### MUST RUN FIRST
- [config_gui.py](config_gui.py) — configure the game directory, language, and text encoding before anything else. This is the required setup step for the entire pipeline.

### 1) Dialog processing
- [dialog-report-prepare.py](dialog-report-prepare.py) — extracts DLG/CRE data, parses dialog TLK files, and builds the main CSV report used downstream.

### 2) Profile preparation
- [profiles-prepare.py](profiles-prepare.py) — reads the dialog report, finds reusable voice samples, and prepares voice sample candidates for NPCs.

### 3) Profile management
- [profiles-manage_gui.py](profiles-manage_gui.py) — manages profile assignments, reviews sample quality, approves samples, and creates/edits voice profiles.

### 4) Audio generation
- [generate_gui.py](generate_gui.py) — runs the main VoiceBox generation workflow, including batch TTS generation and progress reporting.

### 5) Compile mod
- [build_mod.py](build_mod.py) — scans generated WAV files, creates the WeiDU mapping table, and stages the final mod package.

### Troubleshooting / recovery only
- [generation-memory-regenerate.py](generation-memory-regenerate.py) — rebuilds the generation-memory cache from generated output files if the cache is missing or stale.
- [generation-memory-merge.py](generation-memory-merge.py) — merges multiple generation-memory JSON files into a single cache file when recovering from split or stale cache data.

### Shared / support scripts
- [appconfig.py](appconfig.py) — central configuration store used by the entire toolchain.

> The main end-to-end flow is: [config_gui.py](config_gui.py) → [dialog-report-prepare.py](dialog-report-prepare.py) → [profiles-prepare.py](profiles-prepare.py) → [profiles-manage_gui.py](profiles-manage_gui.py) → [generate_gui.py](generate_gui.py) → [build_mod.py](build_mod.py).
>
> [generation-memory-regenerate.py](generation-memory-regenerate.py) and [generation-memory-merge.py](generation-memory-merge.py) are only for recovery/debugging and are not part of the normal pipeline.