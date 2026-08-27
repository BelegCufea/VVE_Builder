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
import json
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from appconfig import cfg

# ==================== CONFIGURATION ====================

# CRE binary layout constants
_CRE_DIALOG_RESREF_OFFSET = 0x02CC       # byte offset of the 8-byte dialog resref field
_CRE_MIN_SIZE             = _CRE_DIALOG_RESREF_OFFSET + 8   # = 0x02D4
_CRE_SUPPORTED_VERSIONS   = frozenset({b"V1.0", b"V1  "})   # V1.2/V2.2/V9.0 (IWD2/PST) excluded
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
    tp2_path.write_text(tp2_content, encoding=cfg.TEXT_ENCODING)

    result = subprocess.run(
        [
            str(weidu_path),
            tp2_path.name,
            "--game", str(game_dir),
            "--force-install", "0",
            "--use-lang", str(cfg.LANGUAGE)
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
        sound_resref = sound_resref_raw.split(b"\x00", 1)[0].decode(cfg.TEXT_ENCODING, errors="replace")
        text_start = strings_offset + text_off
        text = data[text_start:text_start + text_len].decode(cfg.TEXT_ENCODING, errors="replace")
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
    if len(data) < _CRE_MIN_SIZE:
        return None
    if data[0:4] != b"CRE ":
        return None
    if data[4:8] not in _CRE_SUPPORTED_VERSIONS:
        # V1.2/V2.2/V9.0 (IWD2/PST) use different header layouts — skipped
        return None

    long_name_strref, short_name_strref = struct.unpack_from("<ii", data, 0x0008)
    dialog_resref = (
        data[_CRE_DIALOG_RESREF_OFFSET : _CRE_DIALOG_RESREF_OFFSET + 8]
        .split(b"\x00", 1)[0]
        .decode("ascii", errors="replace")
        .upper()
    )
    gender_byte = data[0x0237]

    return CreInfo(path.stem.upper(), long_name_strref, short_name_strref, dialog_resref, gender_byte)


def iter_files_ci(directory: Path, extension: str):
    """Case-insensitive glob for a given extension, e.g. 'cre' or 'd'."""
    pattern = "".join(f"[{c.lower()}{c.upper()}]" for c in extension)
    return directory.glob(f"*.{pattern}")


# ==================== STEP 5: build lookup tables ====================
STRIP_COLOR_RE = re.compile(r"\^0x[0-9a-fA-F]{8}(.*?)\^-")

def load_patcher_config(config_path: Path) -> dict:
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def build_dlg_to_cre_info(
    extract_dir: Path, 
    tlk: dict[int, TlkEntry],
    config: dict
) -> dict[str, tuple[str, str]]:
    """Maps DLG resref -> (real_name, gender_letter) with robust fallback resolution."""
    result: dict[str, tuple[str, str]] = {}
    
    # Get the name replacements and gender overrides from config
    name_replacements = config.get("creNameReplacements", {})
    gender_overrides = config.get("genderOverrides", {})
    
    # Build list of all CRE basenames for indexing
    cre_file_index = {file.stem.upper() for file in iter_files_ci(extract_dir, "cre")}
    
    # Single pass: build dialog_resref → cre_basename map and cache all CreInfo objects
    cre_dialog_map: dict[str, str] = {}      # dialog_resref → cre_basename
    cre_info_cache: dict[str, CreInfo] = {}  # cre_basename  → CreInfo

    for cre_file in iter_files_ci(extract_dir, "cre"):
        info = parse_cre(cre_file)
        if info is None or not info.dialog_resref:
            continue
        cre_basename = cre_file.stem.upper()
        cre_dialog_map[info.dialog_resref] = cre_basename
        cre_info_cache[cre_basename] = info
    
    # ITERATE THROUGH DLG FILES, not CRE files
    for dlg_file in iter_files_ci(extract_dir, "dlg"):
        dlg_resref = dlg_file.stem.upper()
        
        # Find the CRE that owns this DLG
        cre_basename = find_cre_file(
            dlg_resref,  # Pass the DLG name, not the CRE's dialog_resref
            cre_dialog_map,
            cre_file_index,
            name_replacements
        )
        
        if cre_basename is None:
            continue
            
        # Get the CRE info from cache — no second file read
        cre_info = cre_info_cache.get(cre_basename)
        if cre_info is None:
            continue
            
        # Apply gender override if exists
        gender = cfg.GENDER_MAP.get(cre_info.gender_byte, "")
        if cre_info.filename in gender_overrides:
            gender = gender_overrides[cre_info.filename]
        
        # Get name from TLK
        name_strref = cre_info.long_name_strref if cre_info.long_name_strref >= 0 else cre_info.short_name_strref
        name = ""
        if name_strref >= 0 and name_strref in tlk:
            raw_text = tlk[name_strref].text
            name = STRIP_COLOR_RE.sub(r"\1", raw_text).strip()
        
        # Map the DLG to the CRE info
        result[dlg_resref] = (name, gender)
    
    return result

def find_cre_file(
    dialog_resref: str,
    dlg_to_cre_index: dict[str, str],
    cre_file_index: set[str],
    name_replacements: dict[str, str]
) -> str | None:
    """
    Find the CRE file that owns this dialog using the same fallback logic as the C# code.
    """
    base_name = dialog_resref.upper()
    
    # Highest priority: authoritative match via CRE's embedded dialog ref
    if base_name in dlg_to_cre_index:
        return dlg_to_cre_index[base_name]
    
    # Apply name replacements from config before any other stripping (using regex!)
    for pattern, replacement in name_replacements.items():
        base_name = re.sub(pattern, replacement, base_name, flags=re.IGNORECASE)
    
    # Helper to try resolving cascade
    def try_resolve_cascade(current: str) -> str | None:
        # 1. Try direct match
        if current in cre_file_index:
            return current

        if current in dlg_to_cre_index:
            return dlg_to_cre_index[current]
        
        # 2. Strip trailing digits and try
        no_digits = re.sub(r'\d+$', '', current)
        if no_digits in cre_file_index:
            return no_digits
        
        # 3. Strip trailing 'A' or 'E' and try
        if no_digits and no_digits[-1] in ('A', 'E'):
            no_digits_no_suffix = no_digits[:-1]
            if no_digits_no_suffix in cre_file_index:
                return no_digits_no_suffix
        
        # 4. Wildcard Fallback
        for cre_name in sorted(cre_file_index):
            if cre_name.startswith(no_digits):
                return cre_name
        
        return None
    
    # Try original name after replacements
    match = try_resolve_cascade(base_name)
    if match:
        return match
    
    # Strip trailing underscore
    if base_name and base_name[-1] == '_':
        base_name = base_name[:-1]
        match = try_resolve_cascade(base_name)
        if match:
            return match
    
    # Strip "BD" or "TB" prefix
    if base_name.startswith(("BD", "TB")):
        base_name = base_name[2:]
        match = try_resolve_cascade(base_name)
        if match:
            return match
    
    # Strip "B" prefix
    if base_name and base_name[0] == 'B':
        base_name = base_name[1:]
        match = try_resolve_cascade(base_name)
        if match:
            return match
    
    # Strip trailing 'J', 'P', 'B', 'S', or 'D' suffix
    if base_name and base_name[-1] in ('J', 'P', 'B', 'S', 'D'):
        base_name = base_name[:-1]
        match = try_resolve_cascade(base_name)
        if match:
            return match
    
    # Strip first digit and everything after it
    stripped_digits = re.sub(r'\d.*$', '', base_name)
    if stripped_digits != base_name:
        match = try_resolve_cascade(stripped_digits)
        if match:
            return match
    
    return None

# ==================== STEP 6: report generation ====================

def sound_wav_placeholder(sound_resref: str) -> str:
    override_path = Path(cfg.GAME_DIRECTORY) / "override" / f"{sound_resref}.wav"
    return str(override_path.exists())

def write_dialog_report(
    out_path: Path,
    tlk: dict[int, TlkEntry],
    tlk_f: dict[int, TlkEntry] | None,
    strref_info: dict[int, dict],
    dlg_to_cre_info: dict[str, tuple[str, str]],
) -> None:
    # Backup existing file if it exists
    if out_path.exists():
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_path = out_path.parent / f"{out_path.stem}_{timestamp}{out_path.suffix}.bak"
        shutil.copy2(out_path, backup_path)
        print(f"Backed up existing report to: {backup_path}")
    
    # Write new report
    with out_path.open("w", encoding=cfg.TEXT_ENCODING, newline="") as f:
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
    weidu_path = Path(cfg.WEIDU_PATH).resolve()
    weidu_dir = Path(cfg.WEIDU_PATH).resolve().parent
    game_dir = Path(cfg.GAME_DIRECTORY).resolve()
    extract_dir = Path(cfg.EXTRACT_DIR).resolve()

    if not weidu_path.exists():
        raise FileNotFoundError(f"weidu.exe not found at {weidu_path}")

    print("Running WeiDU extraction (binary DLG + CRE) ...")
    run_weidu_extraction(weidu_path, weidu_dir, game_dir, extract_dir)

    print("Copying any override-only DLG/CRE files ...")
    copy_override_files(game_dir, extract_dir)

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

    config_path = Path(cfg.PATCHER_CONFIG_PATH)
    if config_path.exists():
        config = load_patcher_config(config_path)
        print(f"Loaded patcher config from {config_path}")
    else:
        print("Warning: patcher-config.json not found, using defaults")
        config = {"creNameReplacements": {}, "genderOverrides": {}}        

    print("Parsing DLG binaries for strref ownership ...")
    strref_info = scan_dlg_files_for_strrefs(extract_dir)
    print(f"Found {len(strref_info)} distinct strrefs referenced across DLG files")

    print("Building DLG -> CRE lookup ...")
    dlg_to_cre_info = build_dlg_to_cre_info(extract_dir, tlk, config)
    print(f"Resolved {len(dlg_to_cre_info)} DLG resrefs to a speaking CRE")

    out_path = Path(cfg.CSV_PATH)
    write_dialog_report(out_path, tlk, tlk_f, strref_info, dlg_to_cre_info)


if __name__ == "__main__":
    main()