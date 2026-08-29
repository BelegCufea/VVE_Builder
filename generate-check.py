import csv
import json
import random
import re
from pathlib import Path
from difflib import SequenceMatcher

import requests

from appconfig import cfg


BASE36_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def from_base36(s: str) -> int:
    """Convert a base36 string back to an integer StrRef."""
    s = s.upper()

    value = 0
    for ch in s:
        value = value * 36 + BASE36_ALPHABET.index(ch)

    return value


def filename_re() -> re.Pattern:
    """
    Matches filenames like:
        TS000ABC.WAV

    where TS is cfg.FILENAME_PREFIX and 000ABC is the
    base36-encoded StrRef.
    """
    return re.compile(
        re.escape(cfg.FILENAME_PREFIX) + r"([0-9A-Za-z]{6})\.WAV$",
        re.IGNORECASE,
    )


def load_text_lookup(csv_path: str | Path) -> dict[int, str]:
    """Load StrRef -> Text lookup from CSV."""
    lookup: dict[int, str] = {}

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            try:
                strref = int(row["StrRef"])
            except (KeyError, ValueError):
                continue

            lookup[strref] = row.get("Text", "")

    return lookup


def load_patcher_config(config_path: str) -> dict:
    """
    Load the patcher configuration from a JSON file.

    Args:
        config_path: Path to the JSON configuration file.

    Returns:
        The loaded configuration object.

    Raises:
        FileNotFoundError: If the configuration file doesn't exist.
        json.JSONDecodeError: If the file contains invalid JSON.
    """
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def preprocess_text(text: str, patcher_config: dict) -> str:
    """
    Apply comprehensive text transformations for TTS preparation.

    Processes the input text through several transformation stages:
        1. Identity tokens: Replace <CHARNAME>, <GABBER>, <RACE>, <PRO_RACE>
        2. Gender tokens: Replace <HE>, <SHE>, <HIS>, <HER>, <HIM>, etc.
        3. Phonetic rules: Apply regex-based substitutions
        4. Token cleanup: Remove any remaining <...> tokens

    Args:
        text: The raw input text from the CSV file.
        patcher_config: Loaded patcher configuration.

    Returns:
        The preprocessed text, ready for TTS generation.
    """
    pc_name = patcher_config.get("pcName", "CHARNAME")
    pc_race = patcher_config.get("pcRace", "RACE")
    identity_tokens = patcher_config.get("identityTokens", [])

    token_map = {}
    for token in identity_tokens:
        if token == "CHARNAME" or token == "GABBER":
            token_map[token] = pc_name
        elif token == "PRO_RACE" or token == "RACE":
            token_map[token] = pc_race
    for token, replacement in token_map.items():
        text = text.replace(f"<{token}>", replacement)

    pc_gender = patcher_config.get("pcGender", "neutral")
    gender_tokens = patcher_config.get("genderTokens", {})
    for token, forms in gender_tokens.items():
        replacement = forms.get(pc_gender, "")
        if replacement:
            text = text.replace(f"<{token}>", replacement)

    phonetic_rules = patcher_config.get("phoneticRules", [])
    for rule in phonetic_rules:
        pattern = rule.get("pattern")
        replacement_template = rule.get("replacement", "")
        try:
            compiled_pattern = re.compile(pattern, flags=re.IGNORECASE | re.MULTILINE)
            if replacement_template and '$' in replacement_template:
                def repl_func(match, _template=replacement_template):
                    """Expand $1, $2, ... placeholders using the regex match groups."""
                    result = _template
                    for i in range(1, match.lastindex + 1 if match.lastindex else 0):
                        group_value = match.group(i) or ''
                        result = result.replace(f'${i}', group_value)
                    return result
                text = compiled_pattern.sub(repl_func, text)
            else:
                text = compiled_pattern.sub(replacement_template, text)
        except re.error:
            continue

    text = re.sub(r'<[^>]+>', '', text)
    return text


def transcribe_audio(audio_path: Path) -> str:
    """Send audio to VoiceBox and return transcription."""

    url = (
        cfg.BASE_URL.rstrip("/")
        + "/"
        + cfg.TRANSCRIBE_ENDPOINT.lstrip("/")
    )

    with open(audio_path, "rb") as f:
        response = requests.post(
            url,
            files={
                "file": (
                    audio_path.name,
                    f,
                    "audio/wav",
                )
            },
            timeout=300,
        )

    response.raise_for_status()

    data = response.json()
    return data.get("text", "")

def similarity_score(a: str, b: str) -> float:
    """
    Returns similarity as percentage (0-100).
    """
    return round(
        SequenceMatcher(
            None,
            a.strip().lower(),
            b.strip().lower(),
        ).ratio() * 100,
        2,
    )


def collect_samples() -> list:
    output_dir = Path(cfg.OUTPUT_DIR)

    text_lookup = load_text_lookup(cfg.CSV_PATH)

    # Load patcher config for text preprocessing (mirrors generate_gui.py behavior)
    patcher_config = None
    try:
        patcher_config = load_patcher_config(cfg.PATCHER_CONFIG_PATH)
    except Exception:
        pass  # gracefully fall back to None → text used raw

    pattern = filename_re()

    results = []

    npc_dirs = sorted(
        p for p in output_dir.iterdir()
        if p.is_dir()
    )

    for npc_dir in npc_dirs:

        wav_files = list(npc_dir.glob("*.wav"))
        wav_files += list(npc_dir.glob("*.WAV"))

        if not wav_files:
            continue

        sample_files = random.sample(
            wav_files,
            min(5, len(wav_files)),
        )

        print(f"\n=== {npc_dir.name} ===")

        for wav_file in sample_files:

            match = pattern.match(wav_file.name)

            if not match:
                print(f"Skipping invalid filename: {wav_file.name}")
                continue

            strref = from_base36(match.group(1))

            csv_text = text_lookup.get(strref, "")
            
            # Apply text preprocessing (same as generate_gui.py does before generation)
            if patcher_config:
                csv_text = preprocess_text(csv_text, patcher_config)

            try:
                transcribed_text = transcribe_audio(wav_file)
            except Exception as ex:
                transcribed_text = f"<ERROR: {ex}>"

            score = similarity_score(
                csv_text,
                transcribed_text,
            )                

            row = {
                "NPC": npc_dir.name,
                "StrRef": strref,
                "AudioFile": wav_file.name,
                "CSVText": csv_text,
                "TranscribedText": transcribed_text,
                "SimilarityScore": score,
            }

            results.append(row)

            print(f"StrRef: {strref}")
            print(f"File   : {wav_file.name}")

    return results


def print_report(rows: list[dict]) -> None:
    for row in rows:

        print("\n" + "=" * 120)

        print(f"NPC      : {row['NPC']}")
        print(f"StrRef   : {row['StrRef']}")
        print(f"File     : {row['AudioFile']}")
        print(f"Similarity: {row['SimilarityScore']}%")

        print("\nCSV TEXT:")
        print(row["CSVText"])

        print("\nTRANSCRIBED:")
        print(row["TranscribedText"])


def save_csv(rows: list[dict], output_file: str = "transcription_samples.csv") -> None:
    with open(output_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "NPC",
                "StrRef",
                "AudioFile",
                "SimilarityScore",
                "CSVText",
                "TranscribedText",
            ]
        )

        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved: {output_file}")


def main() -> int:
    rows = collect_samples()

    print_report(rows)

    save_csv(rows)

    print(f"\nProcessed {len(rows)} samples.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())