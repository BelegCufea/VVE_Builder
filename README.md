# VVE Builder

**VVE Builder** is an automation pipeline designed to streamline the creation, management, and compilation of AI-generated voiceover mods (similar to Voices Voices Extravaganza) for Infinity Engine games using **WeiDU** and **VoiceBox**.

It consists of several Python scripts semi-automating the process. Included are sample JSON configuration files for Baldur's Gate 2 Enhanced Edition automation.

---

## Workflow Pipeline

1. **Dialog Processing**: Extract and structure game dialog sources into working tracking sheets.
2. **Profile Preparation**: Extract and assemble suitable source samples for NPCs already voiced in the game.
3. **Profile Management**: Audit extracted voice samples, create new ones and assign them to NPCs.
4. **Audio Generation**: Execute batch text-to-speech generation via **VoiceBox**, utilizing cache and memory management to prevent redundant audio processing.
5. **Compile Mod**: Build the WeiDU package with bundled generated audio assets for seamless in-game installation.