import os
import subprocess
import sys
import tempfile
import time

# ============================================================
# Configuration
# ============================================================

OUTPUT_DIR = r"output"
VORBIS_QUALITY = 2  # -qscale:a 2 (~96-112 kbps, good default for speech)

# ============================================================
# Conversion Logic
# ============================================================

def convert_in_place(wav_path, quality=2):
    """
    Re-encodes an existing audio file to Ogg Vorbis in-place 
    forcing the Ogg container (-f ogg) while keeping the .wav filename.
    """
    temp_ogg_path = wav_path + ".oggtmp"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", wav_path,
        "-c:a", "libvorbis",
        "-qscale:a", str(quality),
        "-f", "ogg",
        temp_ogg_path
    ]

    try:
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        # Replace original file atomically / in-place
        os.replace(temp_ogg_path, wav_path)
    except subprocess.CalledProcessError as e:
        if os.path.exists(temp_ogg_path):
            os.remove(temp_ogg_path)
        raise RuntimeError(f"FFmpeg error: {e.stderr.strip()}")
    except Exception as e:
        if os.path.exists(temp_ogg_path):
            os.remove(temp_ogg_path)
        raise e


def main():
    if not os.path.exists(OUTPUT_DIR):
        print(f"❌ Output directory '{OUTPUT_DIR}' not found.")
        sys.exit(1)

    # 1. Collect all .wav files
    wav_files = []
    for root, _, files in os.walk(OUTPUT_DIR):
        for f in files:
            if f.lower().endswith(".wav"):
                wav_files.append(os.path.join(root, f))

    total_files = len(wav_files)
    if total_files == 0:
        print("No .wav files found to convert.")
        return

    print(f"Found {total_files} .wav files in '{OUTPUT_DIR}'.")
    print(f"Converting to Ogg Vorbis (quality: {VORBIS_QUALITY})...\n")

    start_time = time.time()
    success_count = 0
    failed_files = []

    # 2. Batch process
    for idx, wav_path in enumerate(wav_files, start=1):
        rel_path = os.path.relpath(wav_path, OUTPUT_DIR)
        print(f"[{idx}/{total_files}] Converting: {rel_path}...", end="\r", flush=True)

        try:
            convert_in_place(wav_path, VORBIS_QUALITY)
            success_count += 1
        except Exception as e:
            failed_files.append((rel_path, str(e)))

    elapsed = time.time() - start_time
    print(" " * 80 + "\r", end="")

    # 3. Summary
    print("=" * 60)
    print("RE-ENCODING COMPLETE")
    print("=" * 60)
    print(f"Total processed:   {total_files}")
    print(f"Successfully converted: {success_count}")
    print(f"Failed:            {len(failed_files)}")
    print(f"Elapsed time:      {elapsed:.1f}s")
    print("=" * 60)

    if failed_files:
        print("\nFailed files:")
        for path, err in failed_files:
            print(f"  ❌ {path} -> {err}")


if __name__ == "__main__":
    main()