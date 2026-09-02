"""
Regenerate generation memory from existing audio files and CSV data.

This script scans the output directory for all generated WAV files,
matches them against the dialog CSV to determine which STRREFs and voices
they belong to, and rebuilds the generation-memory.json file from scratch.

This is useful when:
    - The generation memory file has been lost or corrupted
    - You want to synchronize the memory with the actual files on disk
    - You've moved or renamed files and need to update the memory

Usage:
    python generation-memory-regenerate.py

The script will:
    1. Load the dialog CSV to build a filename -> (voice, strref) lookup
    2. Scan the output directory recursively for all WAV files
    3. Match each file against the CSV lookup
    4. Build a new generation memory dictionary
    5. Save the memory to generation-memory.json
    6. Report statistics including any unmatched files
"""

import csv
import json
import os
from typing import Dict, List, Tuple

from libs.appconfig import cfg


def load_csv_lookup(csv_path: str) -> Dict[str, Dict[str, str]]:
    """
    Build a filename -> (voice, strref) lookup from the dialog CSV.

    Reads the CSV file and creates a mapping from filename (lowercase with
    .wav extension) to a dictionary containing the voice name and STRREF.

    Args:
        csv_path: Path to the dialog CSV file.

    Returns:
        Dictionary mapping filename (lowercase) to {'voice': str, 'strref': str}.
        Files without a voice name default to "Descriptions".

    Note:
        Rows with fewer than 8 columns, empty strref, or empty filename
        are skipped.
    """
    filename_map: Dict[str, Dict[str, str]] = {}

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)

        for row in reader:
            if len(row) < 8:
                continue

            strref = row[0].strip()
            voice = row[2].strip() if len(row) > 2 else ""
            filename = row[5].strip()

            if not strref or not filename:
                continue

            # If voice is empty, use "Descriptions" as the voice name
            if not voice:
                voice = "Descriptions"

            filename_map[f"{filename}.wav".lower()] = {
                "voice": voice,
                "strref": strref
            }

    return filename_map


def scan_output_directory(output_dir: str, filename_map: Dict[str, Dict[str, str]]) -> Tuple[Dict[str, Dict[str, bool]], int, int, List[str]]:
    """
    Scan the output directory for WAV files and match them against the CSV lookup.

    Recursively traverses the output directory, identifying WAV files and
    matching them against the filename lookup table. Files that don't match
    are collected for reporting.

    Args:
        output_dir: Root directory to scan for WAV files.
        filename_map: Mapping from filename to (voice, strref) data.

    Returns:
        A tuple containing:
            - generated: Dictionary mapping voice name -> {strref: True}
            - found_files: Total number of WAV files found
            - matched_files: Number of files that matched the CSV lookup
            - unmatched_files: List of paths to unmatched files
    """
    generated: Dict[str, Dict[str, bool]] = {}
    found_files = 0
    matched_files = 0
    unmatched_files: List[str] = []

    for root, dirs, files in os.walk(output_dir):
        for filename in files:
            if not filename.lower().endswith(".wav"):
                continue

            found_files += 1

            lookup_name = filename.lower()
            entry = filename_map.get(lookup_name)

            if entry is None:
                unmatched_files.append(os.path.join(root, filename))
                continue

            matched_files += 1

            voice = entry["voice"]
            strref = entry["strref"]

            if voice not in generated:
                generated[voice] = {}

            generated[voice][strref] = True

    return generated, found_files, matched_files, unmatched_files


def sort_generated_memory(generated: Dict[str, Dict[str, bool]]) -> Dict[str, Dict[str, bool]]:
    """
    Sort the generated memory dictionary for deterministic output.

    Sorts voices alphabetically (case-insensitive) and STRREFs numerically.

    Args:
        generated: Unsorted dictionary mapping voice -> {strref: True}.

    Returns:
        Sorted dictionary with voices in alphabetical order and STRREFs
        in numerical order.
    """
    sorted_generated: Dict[str, Dict[str, bool]] = {}
    for voice in sorted(generated.keys(), key=lambda v: v.lower()):
        sorted_strrefs = sorted(
            generated[voice].keys(),
            key=lambda x: int(x) if x.isdigit() else x
        )
        sorted_generated[voice] = {s: True for s in sorted_strrefs}
    return sorted_generated


def save_generation_memory(memory: Dict[str, Dict[str, bool]], memory_path: str) -> None:
    """
    Save the generation memory dictionary to a JSON file.

    Args:
        memory: The generation memory dictionary to save.
        memory_path: Path where the JSON file should be written.

    Raises:
        OSError: If the file cannot be written due to permissions or
            filesystem errors.
        TypeError: If the memory contains data that is not JSON-serializable.
    """
    with open(memory_path, "w", encoding="utf-8") as f:
        json.dump(
            memory,
            f,
            ensure_ascii=False,
            indent=4
        )


def print_report(found_files: int, matched_files: int, unmatched_files: List[str],
                 total_voices: int, total_strrefs: int, memory_path: str) -> None:
    """
    Print a summary report of the memory regeneration process.

    Args:
        found_files: Total number of WAV files found.
        matched_files: Number of files that matched the CSV lookup.
        unmatched_files: List of paths to unmatched files.
        total_voices: Number of distinct voice profiles.
        total_strrefs: Total number of STRREF entries in the memory.
        memory_path: Path where the memory was saved.
    """
    print()
    print("=" * 60)
    print("RECONSTRUCTION COMPLETE")
    print("=" * 60)
    print(f"Files found:       {found_files}")
    print(f"Files matched:     {matched_files}")
    print(f"Files unmatched:   {len(unmatched_files)}")
    print(f"Voices:            {total_voices}")
    print(f"Generated StrRefs: {total_strrefs}")
    print(f"Memory file:       {memory_path}")

    if unmatched_files:
        print()
        print("WARNING: The following files were not found in the CSV:")
        for path in unmatched_files:
            print(f"  {path}")

    print("=" * 60)


def main() -> None:
    """
    Main entry point for the generation memory regeneration script.

    Orchestrates the complete workflow:
        1. Loads the CSV file to build a filename lookup
        2. Scans the output directory for WAV files
        3. Matches files against the CSV lookup
        4. Builds a new generation memory dictionary
        5. Sorts the memory for deterministic output
        6. Saves the memory to generation-memory.json
        7. Displays a summary report including unmatched files

    The script will print a warning if any files in the output directory
    cannot be matched to the CSV.
    """
    # Build filename -> (voice, strref) lookup from CSV
    print("Reading CSV...")
    filename_map = load_csv_lookup(cfg.CSV_PATH)
    print(f"Loaded {len(filename_map)} entries from CSV.")

    # Scan output directory recursively
    print(f"Scanning '{cfg.OUTPUT_DIR}'...")
    generated, found_files, matched_files, unmatched_files = scan_output_directory(
        cfg.OUTPUT_DIR, filename_map
    )

    # Sort voices and StrRefs for clean, deterministic JSON
    sorted_generated = sort_generated_memory(generated)

    # Save memory file
    save_generation_memory(sorted_generated, cfg.GENERATION_MEMORY_PATH)

    # Report
    total_strrefs = sum(len(strrefs) for strrefs in sorted_generated.values())
    total_voices = len(sorted_generated)

    print_report(
        found_files=found_files,
        matched_files=matched_files,
        unmatched_files=unmatched_files,
        total_voices=total_voices,
        total_strrefs=total_strrefs,
        memory_path=cfg.GENERATION_MEMORY_PATH
    )


if __name__ == "__main__":
    main()