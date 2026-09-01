"""
build_mod.py

Scans OUTPUT_DIR (subdirectories per NPC) for WAV files named:
    TSXXXXXX.WAV
where XXXXXX = base36(strref).

Filenames are left EXACTLY as-is (TSXXXXXX.WAV, 8-char resref "TSXXXXXX")
and simply copied/flattened into the mod's WAV folder. Alongside that,
this script writes a 2DA-style lookup table (strref -> resref) that the
.tp2 reads at install time with WeiDU's built-in COUNT_2DA_ROWS /
READ_2DA_ENTRY_FORMER - so WeiDU never has to know about base36 at all,
it just looks up "which resref goes with this strref" from the table.

The tp2 source (MOD_TP2) is copied alongside the WAV folder and mapping
table, renamed to f"setup-{MOD_NAME}.tp2" as WeiDU convention expects.

Before staging, every candidate file's strref is checked against a 
dialog.tlk (+ dialogf.tlk).
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from appconfig import cfg
from utils import from_base36, filename_re, load_valid_strrefs

# =========================================================================

@dataclass
class Entry:
    strref: int
    resref: str          # e.g. "TS12345" (filename without .WAV, original case)
    source_path: Path
    npc_subdir: str


def scan(output_dir: Path) -> tuple[list[Entry], list[Path]]:
    entries: list[Entry] = []
    skipped: list[Path] = []
    pattern = filename_re()

    for wav_path in output_dir.rglob("*"):
        if not wav_path.is_file():
            continue
        if wav_path.suffix.lower() != ".wav":
            continue
        m = pattern.match(wav_path.name)
        if not m:
            continue
        try:
            strref = from_base36(m.group(1))
        except ValueError:
            skipped.append(wav_path)
            continue

        resref = wav_path.stem  # "TS12345", preserves original casing/length
        npc_subdir = wav_path.parent.relative_to(output_dir).as_posix()
        entries.append(Entry(strref=strref, resref=resref, source_path=wav_path, npc_subdir=npc_subdir))

    return entries, skipped


def filter_to_valid_strrefs(
    entries: list[Entry], valid_strrefs: set[int]
) -> tuple[list[Entry], list[Entry]]:
    """
    Split entries into (kept, dropped) based on whether their strref exists
    in the game's dialog.tlk/dialogf.tlk.
    """
    kept: list[Entry] = []
    dropped: list[Entry] = []
    for e in entries:
        (kept if e.strref in valid_strrefs else dropped).append(e)
    return kept, dropped


def write_2da(entries: list[Entry], mapping_path: Path) -> None:
    """
    Minimal 2DA: WeiDU's COUNT_2DA_ROWS/READ_2DA_ENTRY_FORMER treat line 1
    as a free-form signature and line 2 as a default value; data rows start
    at line 3. Columns are whitespace-separated.
    """
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    with mapping_path.open("w", encoding="ascii", newline="\n") as f:
        f.write("2DA V1.0\n")
        f.write("0\n")
        for e in sorted(entries, key=lambda x: x.strref):
            f.write(f"{e.strref} {e.resref}\n")


def main() -> int:
    mod_root = Path("mod") / cfg.MOD_NAME
    output_dir = Path(cfg.OUTPUT_DIR)
    mod_dir = Path(mod_root / "WAV")
    mapping_path = Path(mod_root / "mapping.2da")
    tp2_src = Path(cfg.MOD_TP2)
    tp2_dest = Path(mod_root / f"setup-{cfg.MOD_NAME}.tp2")

    if not output_dir.is_dir():
        print(f"ERROR: output dir not found: {output_dir}", file=sys.stderr)
        return 1

    if not tp2_src.is_file():
        print(f"ERROR: tp2 source not found: {tp2_src}", file=sys.stderr)
        return 1

    game_dir = Path(cfg.GAME_DIRECTORY)
    if not game_dir.is_dir():
        print(f"ERROR: game dir not found: {game_dir}", file=sys.stderr)
        return 1

    entries, skipped = scan(output_dir)

    if skipped:
        print(f"WARNING: {len(skipped)} file(s) matched the {cfg.FILENAME_PREFIX} prefix "
              f"but had an invalid base36 body and were skipped:")
        for p in skipped:
            print(f"  - {p}")

    print(f"Found {len(entries)} candidate voiceover file(s) across "
          f"{len({e.npc_subdir for e in entries})} NPC folder(s).")

    print(f"Loading strrefs from game install: {game_dir}")
    valid_strrefs = load_valid_strrefs(game_dir)
    print(f"Clean install has {len(valid_strrefs)} valid strref(s).")

    entries, dropped = filter_to_valid_strrefs(entries, valid_strrefs)

    if dropped:
        print(f"WARNING: {len(dropped)} file(s) reference strrefs not present in the "
              f"install's dialog.tlk and were dropped (these would make WeiDU "
              f"fail on a install):")
        for e in sorted(dropped, key=lambda x: x.strref):
            print(f"  - {e.strref} ({e.resref}) [{e.source_path}]")

    print(f"{len(entries)} voiceover file(s) will be staged after strref validation.")

    mod_dir.mkdir(parents=True, exist_ok=True)
    for e in entries:
        dest = mod_dir / e.source_path.name  # keep original filename as-is
        shutil.copy2(e.source_path, dest)

    write_2da(entries, mapping_path)

    tp2_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tp2_src, tp2_dest)

    print(f"Staged {len(entries)} WAV file(s) to: {mod_dir}")
    print(f"Lookup table written to: {mapping_path}")
    print(f"tp2 copied to: {tp2_dest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
