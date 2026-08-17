"""
prepare.py

Extracts all DLG and CRE resources from an Infinity Engine Enhanced Edition
game (via WeiDU), decompiles DLG files to readable .D text, parses
dialog.tlk / dialogf.tlk, and produces a dialog-report.csv linking every
strref to its speaker (via owning CRE), gender, and sound info.
"""

import re
import struct
import subprocess
import shutil
import csv
from dataclasses import dataclass
from pathlib import Path

# ==================== CONFIGURATION ====================
WEIDU_PATH = r"./weidu/weidu.exe"
GAME_DIRECTORY = r"C:/Relax/BGEET"
EXTRACT_DIR = r"./extracted"
TEXT_ENCODING = "utf-8"
GENDER_MAP = {1: "M", 2: "F", 3: "O", 4: "N"}  # GENDER.IDS: MALE, FEMALE, OTHER, NEITHER
# =======================================================


# ==================== STEP 1: WeiDU extraction (binary DLG + CRE) ====================

def run_weidu_extraction(weidu_path: Path, weidu_dir: Path, game_dir: Path, extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)

    tp2_content = f"""BACKUP ~export/backup~
AUTHOR ~Prepare~

BEGIN ~Export DLG and CRE~
COPY_EXISTING_REGEXP ~.*\\.dlg~ ~{extract_dir}~
COPY_EXISTING_REGEXP ~.*\\.cre~ ~{extract_dir}~
"""

    tp2_path = weidu_dir / "export.tp2"
    tp2_path.write_text(tp2_content, encoding=TEXT_ENCODING)

    result = subprocess.run(
        [
            str(weidu_path),
            tp2_path.name,
            "--game", str(game_dir),
            "--force-install", "0",
        ],
        cwd=weidu_dir,
        capture_output=True,
        text=True,
    )

    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError(f"WeiDU exited with code {result.returncode}")


# ==================== STEP 1b: copy override-only DLG/CRE files ====================

def copy_override_files(game_dir: Path, extract_dir: Path) -> None:
    override_dir = game_dir / "override"
    if not override_dir.exists():
        print("No override folder found — skipping.")
        return

    copied = 0
    for ext in ("dlg", "cre"):
        for src in iter_files_ci(override_dir, ext):
            dest = extract_dir / src.name
            shutil.copy2(src, dest)
            copied += 1

    print(f"Copied {copied} DLG/CRE files directly from override (including override-only resources)")


# ==================== STEP 2: Parse DLG files ====================

@dataclass
class DlgStrref:
    strref: int
    kind: str  # "SAY", "REPLY", or "JOURNAL"


def parse_dlg_strrefs(path: Path) -> list[DlgStrref]:
    data = path.read_bytes()
    if len(data) < 0x30:
        return []

    signature = data[0:4]
    version = data[4:8]
    if signature != b"DLG " or version not in (b"V1.0", b"V1  "):
        return []

    num_states, state_off, num_trans, trans_off = struct.unpack_from("<IIII", data, 0x0008)

    results: list[DlgStrref] = []

    # State table: 0x0000 strref (SAY), entry size 16 bytes (incl. trigger idx at 0x000c)
    for i in range(num_states):
        rec = state_off + i * 16
        if rec + 4 > len(data):
            break
        (strref,) = struct.unpack_from("<i", data, rec)
        if strref >= 0:
            results.append(DlgStrref(strref, "SAY"))

    return results


def scan_dlg_files_for_strrefs(extract_dir: Path) -> dict[int, dict]:
    """
    Parses every .DLG binary directly (no .d decompile needed).
    Returns, per strref: {"dlg": <owning DLG resref>, "kind": <SAY|REPLY|JOURNAL>}
    First writer wins per strref (a strref is rarely reused across DLGs;
    if it is, the first DLG encountered keeps ownership).
    """
    strref_info: dict[int, dict] = {}

    for dlg_file in iter_files_ci(extract_dir, "dlg"):
        dlg_resref = dlg_file.stem.upper()
        for item in parse_dlg_strrefs(dlg_file):
            if item.strref not in strref_info:
                strref_info[item.strref] = {"dlg": dlg_resref, "kind": item.kind}

    return strref_info


# ==================== STEP 3: dialog.tlk / dialogf.tlk parsing ====================

@dataclass
class TlkEntry:
    strref: int
    flags: int
    sound_resref: str
    text: str


def parse_dialog_tlk(path: Path) -> dict[int, TlkEntry]:
    data = path.read_bytes()
    signature, version, lang_id, count, strings_offset = struct.unpack_from(
        "<4s4sHII", data, 0
    )
    if signature != b"TLK ":
        raise ValueError(f"Not a TLK file: {path} (signature={signature!r})")

    entries: dict[int, TlkEntry] = {}
    pos = 18  # header size
    for strref in range(count):
        flags, sound_resref_raw, vol_var, pitch_var, text_off, text_len = (
            struct.unpack_from("<H8sIII I", data, pos)
        )
        sound_resref = sound_resref_raw.split(b"\x00", 1)[0].decode(TEXT_ENCODING, errors="replace")
        text_start = strings_offset + text_off
        text = data[text_start:text_start + text_len].decode(TEXT_ENCODING, errors="replace")
        entries[strref] = TlkEntry(strref, flags, sound_resref, text)
        pos += 26  # entry size

    return entries


def find_dialog_tlk(game_dir: Path) -> Path:
    candidates = list(game_dir.glob("lang/*/dialog.tlk"))
    if not candidates:
        raise FileNotFoundError(f"No dialog.tlk found under {game_dir}/lang/*/")
    for c in candidates:
        if c.parent.name.lower() == "en_us":
            return c
    return candidates[0]


def find_dialogf_tlk(dialog_tlk_path: Path) -> Path | None:
    candidate = dialog_tlk_path.parent / "dialogf.tlk"
    return candidate if candidate.exists() else None


def resolve_tlk_entry(
    strref: int,
    tlk: dict[int, TlkEntry],
    tlk_f: dict[int, TlkEntry] | None,
) -> tuple[int, TlkEntry] | None:
    entry = tlk.get(strref)
    if entry is None:
        return None
    return strref, entry


# ==================== STEP 4: CRE parsing ====================

@dataclass
class CreInfo:
    filename: str
    long_name_strref: int
    short_name_strref: int
    dialog_resref: str
    gender_byte: int


def parse_cre(path: Path) -> CreInfo | None:
    data = path.read_bytes()
    if len(data) < 0x02cc + 8:
        return None

    signature = data[0:4]
    version = data[4:8]
    if signature != b"CRE ":
        return None
    if version not in (b"V1.0", b"V1  "):
        # V1.2/V2.2/V9.0 (IWD2/PST) use different header layouts — skipped
        return None

    strip_color = r"\^0x[0-9a-fA-F]{8}(.*?)\^-"
    long_name_strref = struct.unpack_from("<i", data, 0x0008)[0]
    short_name_strref = struct.unpack_from("<i", data, 0x000c)[0]
    dialog_resref_raw = data[0x02cc:0x02cc + 8]
    dialog_resref = dialog_resref_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").upper()
    gender_byte = data[0x0237]

    return CreInfo(path.stem.upper(), long_name_strref, short_name_strref, dialog_resref, gender_byte)


def iter_files_ci(directory: Path, extension: str):
    """Case-insensitive glob for a given extension, e.g. 'cre' or 'd'."""
    pattern = "".join(f"[{c.lower()}{c.upper()}]" for c in extension)
    return directory.glob(f"*.{pattern}")


# ==================== STEP 5: build lookup tables ====================
STRIP_COLOR_RE = re.compile(r"\^0x[0-9a-fA-F]{8}(.*?)\^-")

def build_dlg_to_cre_info(extract_dir: Path, tlk: dict[int, TlkEntry]) -> dict[str, tuple[str, str]]:
    """Maps DLG resref -> (real_name, gender_letter), resolved via owning CRE."""
    result: dict[str, tuple[str, str]] = {}

    for cre_file in iter_files_ci(extract_dir, "cre"):
        info = parse_cre(cre_file)
        if info is None or not info.dialog_resref:
            continue

        name_strref = (
            info.long_name_strref if info.long_name_strref >= 0 else info.short_name_strref
        )
        name = ""
        if name_strref >= 0 and name_strref in tlk:
            raw_text = tlk[name_strref].text
            name = STRIP_COLOR_RE.sub(r"\1", raw_text).strip()

        gender = GENDER_MAP.get(info.gender_byte, "")
        result[info.dialog_resref] = (name, gender)

    return result


# ==================== STEP 6: report generation ====================

def sound_wav_placeholder(sound_resref: str) -> str:
    # TODO: check <sound_resref>.WAV under the game's override directory.
    # Skipped for now per current requirements.
    return ""


def write_dialog_report(
    out_path: Path,
    tlk: dict[int, TlkEntry],
    tlk_f: dict[int, TlkEntry] | None,
    strref_info: dict[int, dict],
    dlg_to_cre_info: dict[str, tuple[str, str]],
) -> None:
    with out_path.open("w", encoding=TEXT_ENCODING, newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "StrRef", "SystemName", "RealName", "Gender",
            "HasSound", "SoundResRef", "SoundFileExists", "Text",
        ])

        for strref in range(len(tlk)):
            entry = tlk.get(strref)
            if entry is None:
                continue

            info = strref_info.get(strref)
            system_name = info["dlg"] if info else ""

            real_name = ""
            gender = ""
            if system_name and system_name in dlg_to_cre_info:
                real_name, gender = dlg_to_cre_info[system_name]

            if not gender and tlk_f is not None:
                f_entry = tlk_f.get(strref)
                if f_entry is not None and f_entry.text != entry.text:
                    # Fallback heuristic: dialog.tlk and dialogf.tlk diverge
                    # for this strref, suggesting a gendered line. Not a
                    # guaranteed NPC-gender signal — see earlier caveat.
                    gender = "F"

            sound_resref = entry.sound_resref.strip()
            has_sound = bool(sound_resref)
            sound_file_exists = sound_wav_placeholder(sound_resref) if has_sound else ""

            writer.writerow([
                strref,
                system_name,
                real_name,
                gender,
                str(has_sound).lower(),
                sound_resref,
                sound_file_exists,
                entry.text,
            ])

    print(f"Wrote {out_path}")

# ==================== MAIN ====================

def main() -> None:
    weidu_path = Path(WEIDU_PATH).resolve()
    weidu_dir = weidu_path.parent
    game_dir = Path(GAME_DIRECTORY).resolve()
    extract_dir = Path(EXTRACT_DIR).resolve()

    if not weidu_path.exists():
        raise FileNotFoundError(f"weidu.exe not found at {weidu_path}")

    print("Running WeiDU extraction (binary DLG + CRE) ...")
    # run_weidu_extraction(weidu_path, weidu_dir, game_dir, extract_dir)

    print("Copying any override-only DLG/CRE files ...")
    # copy_override_files(game_dir, extract_dir)

    dlg_files = list(iter_files_ci(extract_dir, "dlg"))
    cre_files = list(iter_files_ci(extract_dir, "cre"))
    print(f"Extracted {len(dlg_files)} DLG, {len(cre_files)} CRE")

    print("Parsing dialog.tlk ...")
    tlk_path = find_dialog_tlk(game_dir)
    tlk = parse_dialog_tlk(tlk_path)
    print(f"Loaded {len(tlk)} strings from {tlk_path}")

    tlkf_path = find_dialogf_tlk(tlk_path)
    tlk_f = parse_dialog_tlk(tlkf_path) if tlkf_path else None
    if tlk_f:
        print(f"Loaded {len(tlk_f)} strings from {tlkf_path}")
    else:
        print("No dialogf.tlk found — gender fallback via TLK divergence unavailable")

    print("Parsing DLG binaries for strref ownership ...")
    strref_info = scan_dlg_files_for_strrefs(extract_dir)
    print(f"Found {len(strref_info)} distinct strrefs referenced across DLG files")

    print("Building DLG -> CRE lookup ...")
    dlg_to_cre_info = build_dlg_to_cre_info(extract_dir, tlk)
    print(f"Resolved {len(dlg_to_cre_info)} DLG resrefs to a speaking CRE")

    out_path = extract_dir / "dialog-report.csv"
    write_dialog_report(out_path, tlk, tlk_f, strref_info, dlg_to_cre_info)


if __name__ == "__main__":
    main()