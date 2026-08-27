"""
Merge all generation-memory*.json files in the current directory.

This script automatically finds all generation-memory*.json files in the
current directory and merges them into a single generation-memory.json file.

Usage:
    python merge_memory.py

This will:
    1. Find all generation-memory*.json files (e.g., generation-memory.json,
       generation-memory-pc1.json, generation-memory-backup.json, etc.)
    2. Merge all STRREFs from each file
    3. Save the merged result to generation-memory.json
    4. Create a backup of the existing generation-memory.json (if it exists)
"""

import json
import os
import glob
import shutil

from appconfig import cfg

# ============================================================
# Configuration
# ============================================================

_BACKUP_SUFFIX = ".backup"

# ============================================================
# Functions
# ============================================================

def find_memory_files():
    """
    Find all generation-memory*.json files in the current directory.

    Returns:
        list: List of file paths, sorted alphabetically.
    """
    pattern = "generation-memory*.json"
    files = glob.glob(pattern)
    
    # Sort for consistent order
    return sorted(files)


def merge_memory_files(file_list, verbose=True):
    """
    Merge multiple generation-memory.json files into one.

    Args:
        file_list (list): List of file paths to merge.
        verbose (bool): Whether to print progress information.

    Returns:
        dict: The merged memory dictionary.
    """
    merged = {}
    loaded_count = 0
    total_strrefs = 0

    for file_path in file_list:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            loaded_count += 1
            file_strrefs = 0

            # Merge the data
            for npc, strrefs in data.items():
                if npc not in merged:
                    merged[npc] = {}

                for strref in strrefs:
                    merged[npc][strref] = True
                    file_strrefs += 1

            total_strrefs += file_strrefs

            if verbose:
                print(f"  ✅ {file_path} ({file_strrefs} STRREFs, {len(data)} NPCs)")

        except Exception as e:
            if verbose:
                print(f"  ❌ Error loading {file_path}: {e}")

    # Sort the merged data
    sorted_merged = {}
    for npc in sorted(merged.keys(), key=lambda v: v.lower()):
        sorted_strrefs = sorted(
            merged[npc].keys(),
            key=lambda x: int(x) if x.isdigit() else x
        )
        sorted_merged[npc] = {s: True for s in sorted_strrefs}

    return sorted_merged, loaded_count, total_strrefs


def backup_file(file_path):
    """
    Create a backup copy of the file if it exists.

    Creates a copy of the file with BACKUP_SUFFIX appended to the filename.
    The original file remains unchanged.

    Args:
        file_path (str): Path to the file to backup.

    Returns:
        bool: True if backup was created successfully, False otherwise.
    """
    if not os.path.exists(file_path):
        return False
    
    backup_path = file_path + _BACKUP_SUFFIX
    try:
        shutil.copy2(file_path, backup_path)  # copy2 preserves metadata
        return True
    except Exception:
        return False


def main():
    """Main entry point."""
    print("=" * 60)
    print("🗂️  GENERATION MEMORY MERGER")
    print("=" * 60)

    # Find all memory files
    files = find_memory_files()
    
    if not files:
        print("❌ No generation-memory*.json files found!")
        print("   Make sure you have at least one memory file in this directory.")
        print("   Looking for: generation-memory*.json")
        sys.exit(1)

    # Check if we have only one file
    if len(files) == 1:
        print(f"ℹ️  Only one file found: {files[0]}")
        print("   Nothing to merge. Exiting.")
        return

    print(f"Found {len(files)} file(s) to merge:")
    for f in files:
        print(f"  - {f}")
    print()

    # Backup existing output file if it exists (COPY, don't rename)
    if os.path.exists(cfg.GENERATION_MEMORY_PATH):
        print(f"📦 Creating backup: {cfg.GENERATION_MEMORY_PATH}{_BACKUP_SUFFIX}")
        if backup_file(cfg.GENERATION_MEMORY_PATH):
            print(f"   ✅ Backup created (original preserved)")
        else:
            print(f"   ⚠️ Could not create backup")
        print()

    # Merge the files
    print("📊 Merging files...")
    merged, loaded_count, total_strrefs = merge_memory_files(files)

    # Save the merged file (overwrites the original)
    print(f"💾 Saving merged data to: {cfg.GENERATION_MEMORY_PATH}")
    with open(cfg.GENERATION_MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=4)

    # Report
    total_npcs = len(merged)
    
    print()
    print("=" * 60)
    print("MERGE COMPLETE")
    print("=" * 60)
    print(f"Files merged:      {loaded_count}")
    print(f"NPCs in merged:    {total_npcs}")
    print(f"Total STRREFs:     {total_strrefs}")
    print(f"Output file:       {cfg.GENERATION_MEMORY_PATH}")
    
    # Show which NPCs were found
    if total_npcs > 0:
        print()
        print("NPCs:")
        for npc in sorted(merged.keys(), key=lambda v: v.lower()):
            count = len(merged[npc])
            print(f"  - {npc}: {count} STRREFs")
    
    print("=" * 60)
    print("✅ Done!")

if __name__ == "__main__":
    import sys
    main()