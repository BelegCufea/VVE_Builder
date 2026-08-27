import csv
import json
import os

from appconfig import cfg

# ============================================================
# Main
# ============================================================

def main():

    # --------------------------------------------------------
    # 1. Build filename -> (voice, strref) lookup from CSV
    # --------------------------------------------------------

    print("Reading CSV...")

    filename_map = {}

    with open(cfg.CSV_PATH, "r", encoding="utf-8") as f:
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

    print(f"Loaded {len(filename_map)} entries from CSV.")

    # --------------------------------------------------------
    # 2. Scan output directory recursively
    # --------------------------------------------------------

    print(f"Scanning '{cfg.OUTPUT_DIR}'...")

    generated = {}
    found_files = 0
    matched_files = 0
    unmatched_files = []

    for root, dirs, files in os.walk(cfg.OUTPUT_DIR):

        for filename in files:

            if not filename.lower().endswith(".wav"):
                continue

            found_files += 1

            lookup_name = filename.lower()
            entry = filename_map.get(lookup_name)

            if entry is None:
                unmatched_files.append(
                    os.path.join(root, filename)
                )
                continue

            matched_files += 1

            voice = entry["voice"]
            strref = entry["strref"]

            if voice not in generated:
                generated[voice] = {}

            generated[voice][strref] = True

    # --------------------------------------------------------
    # 3. Sort voices and StrRefs for clean, deterministic JSON
    # --------------------------------------------------------

    sorted_generated = {}
    for voice in sorted(generated.keys(), key=lambda v: v.lower()):
        sorted_strrefs = sorted(
            generated[voice].keys(),
            key=lambda x: int(x) if x.isdigit() else x
        )
        sorted_generated[voice] = {s: True for s in sorted_strrefs}

    # --------------------------------------------------------
    # 4. Save memory file
    # --------------------------------------------------------

    with open(cfg.GENERATION_MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(
            sorted_generated,
            f,
            ensure_ascii=False,
            indent=4
        )

    # --------------------------------------------------------
    # 5. Report
    # --------------------------------------------------------

    total_strrefs = sum(len(strrefs) for strrefs in sorted_generated.values())

    print()
    print("=" * 60)
    print("RECONSTRUCTION COMPLETE")
    print("=" * 60)
    print(f"Files found:       {found_files}")
    print(f"Files matched:     {matched_files}")
    print(f"Files unmatched:   {len(unmatched_files)}")
    print(f"Voices:            {len(sorted_generated)}")
    print(f"Generated StrRefs: {total_strrefs}")
    print(f"Memory file:       {cfg.GENERATION_MEMORY_PATH}")

    if unmatched_files:
        print()
        print("WARNING: The following files were not found in the CSV:")
        for path in unmatched_files:
            print(f"  {path}")

    print("=" * 60)


if __name__ == "__main__":
    main()