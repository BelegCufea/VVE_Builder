# VVE Builder

**VVE Builder** is an automation pipeline designed to streamline the creation, management, and compilation of AI-generated voiceover mods (similar to Voices Voices Extravaganza) for Infinity Engine games using **WeiDU** and **VoiceBox**.

It consists of several Python scripts semi-automating the process. Included are sample JSON configuration files for Baldur's Gate 2 Enhanced Edition automation.

---

## Workflow Pipeline

1. **Dialog Processing**: Extract and structure game dialog sources into working tracking sheets.
2. **Profile Management**: Set up, format, and audit character voice profiles alongside text and speaker substitution rules.
3. **Gather & Compile Voice Samples**: Collect, process, and compile custom voice reference samples for NPCs that have no default in-game voiceover.
4. **Audio Generation**: Execute batch text-to-speech generation via **VoiceBox**, utilizing cache and memory management to prevent redundant audio processing.

---

## TODO

* **Compile Mod**: Implement and finalize the WeiDU packaging pipeline to bundle the generated audio assets for seamless in-game installation.