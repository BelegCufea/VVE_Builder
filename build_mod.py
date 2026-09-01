"""
build_mod.py

Stages generated voiceover WAV files into a WeiDU-installable mod folder:
copies the WAVs, writes a strref->resref lookup table, and brings in the
tp2 and the WeiDU executable, renamed to WeiDU's setup-<modname> convention.

Files whose strref doesn't exist in the target game's dialog.tlk or whose
audio files are missing/empty/strref <= 0 are dropped before staging,
so a modded generation source can be reduced to something a clean install
can actually accept.

Run with --help for the full description and available options (the
--help text is built at runtime so it reflects your actual configuration).
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import textwrap
from dataclasses import dataclass
from pathlib import Path

from appconfig import cfg
from utils import from_base36, filename_re, load_valid_strrefs, setup_logging

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

        if strref <= 0 or wav_path.stat().st_size == 0:
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
    in the game's dialog.tlk/dialogf.tlk and audio file physically exists.
    """
    kept: list[Entry] = []
    dropped: list[Entry] = []
    for e in entries:
        is_valid = (
            e.strref > 0
            and e.strref in valid_strrefs
            and e.source_path.is_file()
            and e.source_path.stat().st_size > 0
        )
        (kept if is_valid else dropped).append(e)
    return kept, dropped


def _rmtree_onerror(func, path, exc_info) -> None:
    """
    shutil.rmtree error handler: if the failure is permissions-related
    (e.g. a read-only file/dir on Windows), clear the read-only bit and
    retry once. Otherwise leave it - the caller decides whether that's fatal.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def clean_mod_root(mod_root: Path, logger) -> None:
    """
    Remove an existing mod_root folder before staging, best-effort.

    Read-only files/dirs are unlocked and retried automatically. Anything
    that still can't be removed (e.g. a dir held read-only in a way Windows
    won't budge on) is logged as a warning and left in place - the WAV/2DA
    files it may contain get overwritten anyway on the next steps.
    """
    if not mod_root.exists():
        return
    shutil.rmtree(mod_root, onerror=_rmtree_onerror)
    if mod_root.exists():
        logger.warning(f"Could not fully clean {mod_root} (permission denied on some "
                       f"files/dirs) - continuing, existing files will be overwritten.")


def write_mapping(entries: list[Entry], mapping_path: Path) -> None:
    """
    Writes a raw space-delimited text mapping file (strref resref) with no headers.
    """
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    with mapping_path.open("w", encoding="ascii", newline="\n") as f:
        for e in sorted(entries, key=lambda x: x.strref):
            f.write(f"{e.strref} {e.resref}\n")


def build_description() -> str:
    """
    Full --help description, built at runtime so it reflects the actual
    configured cfg values rather than placeholder names.
    """
    fill = lambda text: textwrap.fill(text, width=78, break_on_hyphens=False)

    paragraphs = [
        fill(f"Scans {cfg.OUTPUT_DIR} (subdirectories per NPC) for WAV "
             f"files named:")
        + f"\n    {cfg.FILENAME_PREFIX}XXXXXX.WAV\n"
        + fill("where XXXXXX = base36(strref)."),

        fill("Filenames are copied/flattened into the mod's WAV folder. "
             "Alongside that, this script writes a text lookup table "
             "(mapping.txt) that the .tp2 reads at install time, so "
             "WeiDU never has to know about base36 at all."),

        fill(f"The tp2 source ({cfg.MOD_TP2}) is copied alongside the WAV "
             f"folder and mapping table, renamed to setup-{cfg.MOD_NAME}.tp2 "
             f"as WeiDU convention expects. The tra file ({cfg.MOD_TRA}) "
             f"is copied to the TRA subdirectory. The WeiDU executable "
             f"({cfg.WEIDU_PATH}) is likewise copied in and renamed to "
             f"setup-{cfg.MOD_NAME}.exe, so the mod folder is ready to "
             f"install standalone."),

        fill(f"Before staging, every candidate file's strref is checked "
             f"against {cfg.GAME_DIRECTORY}'s dialog.tlk (+ dialogf.tlk)."),
    ]
    return "\n\n".join(paragraphs)


ARG_SPECS = [
    {
        "flags": ["--game-dir"],
        "type": Path,
        "metavar": "PATH",
        "help": (
            f"Game install to validate strrefs against (its dialog.tlk / "
            f"dialogf.tlk). Defaults to {cfg.GAME_DIRECTORY}. Point this "
            f"at a clean game install to strip strrefs that don't exist "
            f"there - useful if VO generation was done against a modded "
            f"install."
        ),
    },
    {
        "flags": ["--mod-name"],
        "type": str,
        "metavar": "NAME",
        "help": (
            f"Mod name to use for this build (folder name under "
            f"{cfg.MOD_ROOT}, and the setup-<mod-name>.tp2/.exe "
            f"filenames). Defaults to {cfg.MOD_NAME}."
        ),
    },
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=build_description(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    for spec in ARG_SPECS:
        parser.add_argument(
            *spec["flags"],
            type=spec["type"],
            default=None,
            metavar=spec["metavar"],
            help=spec["help"],
        )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def log_optional_parameters(logger) -> None:
    """Log a short summary of ARG_SPECS, reusing the same help text and
    already-resolved cfg values shown by --help - nothing hand-duplicated."""
    lines = ["Optional parameters (run with --help for the full description):"]
    for spec in ARG_SPECS:
        flag = f"{spec['flags'][0]} <{spec['metavar']}>"
        lines.append(f"  {flag}")
        lines.extend(
            f"      {line}"
            for line in textwrap.wrap(spec["help"], width=70, break_on_hyphens=False)
        )
    logger.info("\n".join(lines))


def main() -> int:
    args = parse_args()

    logger = setup_logging( Path(__file__).stem)

    log_optional_parameters(logger)

    mod_name = args.mod_name if args.mod_name is not None else cfg.MOD_NAME
    mod_root = Path(cfg.MOD_ROOT) / mod_name
    output_dir = Path(cfg.OUTPUT_DIR)
    mod_dir = Path(mod_root / "WAV")
    mapping_path = Path(mod_root / "mapping.txt")
    tp2_src = Path(cfg.MOD_TP2)
    tp2_dest = Path(mod_root / f"setup-{mod_name}.tp2")
    tra_src = Path(cfg.MOD_TRA)
    tra_dest = Path(mod_root / "TRA" / "english" / cfg.MOD_TRA)
    weidu_src = Path(cfg.WEIDU_PATH)
    weidu_dest = Path(cfg.MOD_ROOT) / f"setup-{mod_name}.exe"

    if not output_dir.is_dir():
        logger.error(f"output dir not found: {output_dir}")
        return 1

    if not tp2_src.is_file():
        logger.error(f"tp2 source not found: {tp2_src}")
        return 1

    if not tra_src.is_file():
        logger.error(f"tra source not found: {tra_src}")
        return 1    

    if not weidu_src.is_file():
        logger.error(f"WeiDU executable not found: {weidu_src}")
        return 1

    game_dir = args.game_dir if args.game_dir is not None else Path(cfg.GAME_DIRECTORY)
    if not game_dir.is_dir():
        logger.error(f"game dir not found: {game_dir}")
        return 1

    if mod_root.exists():
        logger.info(f"Cleaning existing mod folder: {mod_root}")
        clean_mod_root(mod_root, logger)

    entries, skipped = scan(output_dir)

    if skipped:
        logger.warning(f"{len(skipped)} file(s) matched the {cfg.FILENAME_PREFIX} prefix "
                       f"but had an invalid base36 body and were skipped:")
        for p in skipped:
            logger.warning(f"  - {p}")

    logger.info(f"Found {len(entries)} candidate voiceover file(s) across "
                f"{len({e.npc_subdir for e in entries})} NPC folder(s).")

    logger.info(f"Loading strrefs from game install: {game_dir}")
    valid_strrefs = load_valid_strrefs(game_dir)
    logger.info(f"Clean install has {len(valid_strrefs)} valid strref(s).")

    entries, dropped = filter_to_valid_strrefs(entries, valid_strrefs)

    if dropped:
        logger.warning(f"WARNING: {len(dropped)} file(s) reference strrefs not present in the "
                       f"install's dialog.tlk and were dropped (these would make WeiDU "
                       f"fail on a install):")
        for e in sorted(dropped, key=lambda x: x.strref):
            logger.warning(f"  - {e.strref} ({e.resref}) [{e.source_path}]")

    logger.info(f"{len(entries)} voiceover file(s) will be staged after strref validation.")

    mod_dir.mkdir(parents=True, exist_ok=True)
    for e in entries:
        dest = mod_dir / e.source_path.name
        shutil.copy2(e.source_path, dest)

    write_mapping(entries, mapping_path)

    tp2_content = tp2_src.read_text(encoding="utf-8")
    tp2_content = tp2_content.replace("VVEBG2", mod_name).replace("%MOD_NAME%", mod_name)
    tp2_dest.parent.mkdir(parents=True, exist_ok=True)
    tp2_dest.write_text(tp2_content, encoding="utf-8")

    tra_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tra_src, tra_dest)

    weidu_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(weidu_src, weidu_dest)

    logger.info(f"Staged {len(entries)} WAV file(s) to: {mod_dir}")
    logger.info(f"Lookup table written to: {mapping_path}")
    logger.info(f"tp2 copied to: {tp2_dest}")
    logger.info(f"tra copied to: {tra_dest}")
    logger.info(f"WeiDU copied to: {weidu_dest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())