# VVE Builder

**VVE Builder** is an automation pipeline for creating, managing, and compiling AI-generated voiceover mods for Infinity Engine games using **WeiDU** and **VoiceBox**. It is inspired by the workflow used by projects like Voices Voices Extravaganza.

The project consists of several Python scripts that semi-automate the process, along with sample JSON configuration files for Baldur's Gate II: Enhanced Edition.

> 🤖 **Vibe-coded disclosure:** this project was built in close collaboration with a small committee of AI assistants who never quite agreed on code style. Bugs are their fault; the good parts are obviously mine.

## Example mod: Voices Voices Extravaganza: Baldur's Gate 2

**[Voices Voices Extravaganza: Baldur's Gate 2](https://github.com/BelegCufea/VoicesVoicesExtravaganzaBG2)** is a full AI-generated NPC voiceover mod for Baldur's Gate II: Enhanced Edition, built end-to-end with this toolchain — a spiritual successor to ColossusChang's original *Voices Voices Extravaganza* for Baldur's Gate 1. Its README includes a full no-command-line-experience walkthrough for regenerating or fixing a single NPC's voice using VVE Builder.

---

## Workflow Pipeline

1. **Dialog Processing**: Extract and structure game dialog sources into working tracking sheets.
2. **Profile Preparation**: Extract and assemble suitable source samples for NPCs already voiced in the game.
3. **Profile Management**: Audit extracted voice samples, create new ones and assign them to NPCs.
4. **Audio Generation**: Execute batch text-to-speech generation via **VoiceBox**, utilizing cache and memory management to prevent redundant audio processing.
5. **Quality Verification**: Scan generated NPC audio samples, run speech-to-text via **VoiceBox**, and score transcriptions against source dialog to identify problematic audio.
6. **Compile Mod**: Build the WeiDU package with bundled generated audio assets for seamless in-game installation.

## Requirements

### Python requirements
Install the project dependencies listed in [requirements.txt](requirements.txt):

- requests
- runstats
- jiwer
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
- [config_gui.py](config_gui.py) — configure game settings (directory, language, text encoding) and Voicebox API defaults (URL, health check endpoint, engine, model size, and transcription language) before anything else. This is the required setup step for the entire pipeline.

### 1) Dialog processing
- [dialog-report-prepare.py](dialog-report-prepare.py) — extracts DLG/CRE data, parses dialog TLK files, and builds the main CSV report used downstream.

### 2) Profile preparation
- [profiles-prepare.py](profiles-prepare.py) — reads the dialog report, finds reusable voice samples, and prepares voice sample candidates for NPCs.

### 3) Profile management
- [profiles-manage_gui.py](profiles-manage_gui.py) — manages profile assignments, reviews sample quality, approves samples, and creates/edits voice profiles.

### 4) Audio generation
- [generate_gui.py](generate_gui.py) — runs the main VoiceBox generation workflow, including batch TTS generation and progress reporting.

### 5) Quality verification
- [check_gui.py](check_gui.py) — scans generated NPC audio, runs Voicebox speech-to-text transcription, and scores results against expected CSV dialog to flag low-quality or incorrect audio.

### 6) Compile mod
- [build_mod.py](build_mod.py) — scans generated WAV files, creates the WeiDU mapping tables, and stages the final mod package.

The compiled mod installs as three separate WeiDU components, so players can choose how much of the cast gets voiced:

- **Core/Essential NPC voiceovers** — main characters, companions, and other NPCs with a substantial number of generated lines. This component is **mandatory**: it's the one that actually copies the WAV files into the game's `override` folder, so the other two components depend on it being installed.
- **Common NPC voiceovers** — repeat-visit service/ambient NPCs (vendors, tavern staff, temple clergy, generic guards/soldiers, generic townsfolk) that the player is likely to talk to over and over across a playthrough. Curated by name in [common_NPCs.json](common_NPCs.json), regardless of how many lines they have.
- **Minor NPC voiceovers** — everyone else: NPCs not listed in `common_NPCs.json` whose folder has fewer than `CORE_NPC_THRESHOLD` (default `10`, configurable in [appconfig.py](libs/appconfig.py)) generated files.

An NPC's folder under `output/` is checked against `common_NPCs.json` first (forced into Common if listed), then against `CORE_NPC_THRESHOLD` (Minor if below it), and otherwise falls into Core/Essential. Players who'd rather skip repetitive vendor/guard barks can simply not install the Common and/or Minor components.

### Troubleshooting / recovery only
- [generation-memory-regenerate.py](generation-memory-regenerate.py) — rebuilds the generation-memory cache from generated output files if the cache is missing or stale.
- [generation-memory-merge.py](generation-memory-merge.py) — merges multiple generation-memory JSON files into a single cache file when recovering from split or stale cache data.

### Shared / support scripts
- [appconfig.py](appconfig.py) — central configuration store used by the entire toolchain.

> The main end-to-end flow is: [config_gui.py](config_gui.py) → [dialog-report-prepare.py](dialog-report-prepare.py) → [profiles-prepare.py](profiles-prepare.py) → [profiles-manage_gui.py](profiles-manage_gui.py) → [generate_gui.py](generate_gui.py) → [check_gui.py](check_gui.py) → [build_mod.py](build_mod.py).
>
> [generation-memory-regenerate.py](generation-memory-regenerate.py) and [generation-memory-merge.py](generation-memory-merge.py) are only for recovery/debugging and are not part of the normal pipeline.