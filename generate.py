import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import requests
from runstats import Regression
from datetime import datetime, timedelta

#region Configuration
# Voicebox API Configuration
BASE_URL = "http://10.0.50.5:17600"    # VoiceBox API - http://localhost:17493 for local server, or remote URL for remote server
ENGINE = "qwen"
MODEL_SIZE = "0.6B"

# Generation Timeout Safeguards
ENABLE_TIMEOUT_SAFEGUARD = True        # Enable/disable timeout protection
TIMEOUT_MAX_SECONDS = 600              # Hard maximum: 10 minutes (600 seconds)
TIMEOUT_MULTIPLIER = 3.0               # Cancel if actual time > estimated * multiplier
TIMEOUT_MIN_ESTIMATES = 10             # Minimum jobs before using estimated time

# Audio Conversion Configuration
CONVERT_TO_OGG = True                  # convert WAV to Ogg Vorbis after download
OGG_QUALITY = 4                        # libvorbis quality

# File Paths
CSV_PATH = r"dialog-report.csv"
PATCHER_CONFIG_PATH = r"patcher-config.json"
OUTPUT_DIR = r"output"

# Generation Limits and filters
LIMIT = 0                              # set to 0 to process all
# Process only these voices, leaving empty will process all voices.
TARGET_VOICES = [                   
    # "Jaheira",
    # "Edwin",
    # "Neera",
    # "Bodhi",
    "Torsin de Lancie"
]   
# NPC name -> Voicebox profile substitution.
# If an NPC is not listed here, its name is used as the voice profile name.
VOICE_SUBSTITUTIONS = {
    # "Drizzt Do'Urden": "Drizzt",
    "Nym Khalazza": "BG1 Narrator",
    "Armored Figure": "Sarevok"
}

# Filter for which lines to process based on the CSV sound filename (column 6).
FILENAME_PATTERN = r"^TS"              # regex pattern for filename (column 6)

# STRREF Filtering
USE_STRREF_FILTER = True              # If False, falls back to TARGET_VOICES/FILENAME_PATTERN
STRREF_FILTER_FILE = r"strrefs.json"   # JSON file with list of strrefs to process

# Voice Fallback Configuration
USE_VOICE_FALLBACK = False
FALLBACK_VOICE_MALE = "BG1 Narrator"
FALLBACK_VOICE_FEMALE = "BG3 Narrator"
FALLBACK_VOICE_NEUTRAL = "Description Narrator"

# Filename Generation
FORCE_GENERATED_FILENAMES = False      # If True, always use generated; if False, use CSV fallback
RESREF_PREFIX = "TS"                   # 2-character prefix for generated resrefs

# Generation memory
SKIP_ALREADY_GENERATED = True          # If True, skip lines already generated (based on generation-memory.json)
GENERATION_MEMORY_PATH = r"generation-memory.json"

# Logging
LOG_ENABLED = True
LOG_FILE_PATH = r"logs/generation.log"

# Pre-generation Summary Options
COMPACT_SUMMARY = True                 # If True, only show NPCs with valid voices; if False, show all
#endregion Configuration

#region Utility Functions
def format_time(seconds):
    """
    Convert a time duration in seconds to a human-readable string format.

    The function automatically selects the most appropriate time unit based on
    the duration, providing a compact but readable representation suitable for
    progress displays and logging.

    Examples:
        45.6 seconds -> "45.6s"
        125 seconds -> "2m5s"
        3720 seconds -> "1h2m"
        172800 seconds -> "2d0h"

    Args:
        seconds (float): Time duration in seconds. Can be fractional.

    Returns:
        str: Human-readable time string with appropriate units (s, m, h, or d).
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m{secs}s"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    if hours < 24:
        return f"{hours}h{mins}m"
    days = int(hours // 24)
    hrs = int(hours % 24)
    return f"{days}d{hrs}h"


def format_finish_time(eta_seconds):
    """
    Format an estimated time remaining into a human-readable finish time string.

    Converts the ETA (in seconds) to an absolute date/time that the process is
    expected to complete. The format automatically adapts based on how far
    in the future the finish time is:
    - Same day: Shows time only (e.g., "14:30:45")
    - Future day: Shows date and time using the system's locale settings

    This provides users with a clear, at-a-glance understanding of when their
    long-running generation job will complete, making it easier to plan
    around the process.

    Args:
        eta_seconds (float): Estimated time remaining in seconds.
            Can be fractional (e.g., 125.5 seconds).

    Returns:
        str: Formatted finish time string.
            - If eta_seconds > 0: Returns formatted date/time.
            - If eta_seconds <= 0: Returns "..." (indicates ETA is being
              calculated or process is nearly complete).

    Examples:
        >>> # Assuming current time is 2026-08-12 14:00:00 in US locale
        >>> format_finish_time(2745)  # 45 minutes 45 seconds
        "14:45:45"
        >>> format_finish_time(86400)  # 24 hours from now
        "08/13/2026 14:00"  # US locale
        "13.08.2026 14:00"  # German locale

    Note:
        The function uses the system's local time and locale settings for
        date formatting. This means the output will automatically adapt to
        the user's regional preferences (e.g., MM/DD/YYYY in US, DD.MM.YYYY
        in Europe). For multi-day runs, this is generally accurate enough
        for practical purposes.
    """
    if eta_seconds > 0:
        finish_time = datetime.now() + timedelta(seconds=eta_seconds)
        # If finishing today, show time only
        if finish_time.date() == datetime.now().date():
            finish_time_str = finish_time.strftime("%H:%M:%S")
        else:
            # Use locale-aware date format with time
            # %x = locale's appropriate date representation
            # %X = locale's appropriate time representation
            finish_time_str = finish_time.strftime("%x %X")
    else:
        finish_time_str = "..."
    return finish_time_str


def progress_bar(percent, width=30):
    """
    Generate a visual progress bar string for console output.

    Creates a horizontal bar filled with block characters (█) proportional to
    the completion percentage, with unfilled portions shown as dots (░).
    The percentage is displayed numerically at the end.

    Args:
        percent (float): Completion percentage, expected to be between 0 and 100.
        width (int, optional): Total character width of the progress bar.
            Defaults to 30 characters.

    Returns:
        str: Formatted progress bar string, e.g., "[████████░░░░] 66.7%".

    Note:
        The function does not clamp percent values; values outside 0-100 will
        produce visually incorrect bars but won't crash.
    """
    filled = int(width * percent / 100)
    bar = '█' * filled + '░' * (width - filled)
    return f"[{bar}] {percent:.1f}%"


def sanitize_filename(name):
    r"""
    Clean a string to be safe for use as a Windows file or directory name.

    This function ensures the resulting name is valid on Windows systems by:
    1. Replacing invalid characters (<>:"/\|?*) with underscores
    2. Removing trailing spaces and dots (which Windows doesn't allow)
    3. Handling reserved device names (CON, PRN, AUX, COM1-9, LPT1-9)
    4. Providing a fallback if the result would be empty

    Args:
        name (str): The original string to sanitize.

    Returns:
        str: A safe filename/directory name string.

    Note:
        The function is Windows-focused but produces safe names for most
        modern filesystems. It does not enforce length limits.
    """
    # Replace Windows-invalid characters with underscore
    name = re.sub(r'[<>:"/\\|?*]', '_', name)

    # Windows does not allow names ending in a space or dot
    name = name.rstrip(' .')

    # Windows reserved device names
    reserved_names = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5",
        "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
        "LPT6", "LPT7", "LPT8", "LPT9",
    }

    if name.upper() in reserved_names:
        name = f"_{name}_"

    # Just in case the result became empty
    if not name:
        name = "_unnamed_"

    return name


def to_base36(value):
    """
    Convert an integer to its base36 representation.
    
    Args:
        value (int): Non-negative integer to convert.
        
    Returns:
        str: Base36 representation of the value.
        
    Raises:
        ValueError: If value is negative.
    """
    if value < 0:
        raise ValueError(f"Value must be non-negative, got {value}")
    
    if value == 0:
        return "0"
    
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = ""
    while value > 0:
        result = alphabet[value % 36] + result
        value //= 36
    return result


def generate_resref(strref, prefix="TS"):
    """
    Generate an 8-character resref from a StrRef number.
    
    Format: 2-character prefix + 6-character base36 number.
    Example: prefix "TS" + StrRef 12345 -> "TS0009IX"
    
    Args:
        strref (int or str): The StrRef number (can be int or string).
        prefix (str): 2-character prefix. Defaults to "TS".
        
    Returns:
        str: 8-character resref in uppercase.
        
    Raises:
        ValueError: If prefix is not exactly 2 characters or strref is invalid.
    """
    # Validate prefix
    if len(prefix) != 2:
        raise ValueError(f"Prefix must be exactly 2 characters, got '{prefix}'")
    
    # Convert strref to int if it's a string
    if isinstance(strref, str):
        try:
            strref_int = int(strref)
        except ValueError:
            raise ValueError(f"StrRef must be a valid integer, got '{strref}'")
    else:
        strref_int = strref
    
    # Validate strref is non-negative
    if strref_int < 0:
        raise ValueError(f"StrRef must be non-negative, got {strref_int}")
    
    # Convert to base36 and pad to 6 characters
    suffix = to_base36(strref_int).rjust(6, '0')
    
    # Return as uppercase
    return (prefix + suffix).upper()
#endregion Utility Functions

#region Logging
def init_log_file(log_path, total_jobs, total_chars_all):
    """
    Initialize the log file with a header and job summary.

    If the log file doesn't exist, creates a new one with a header.
    If it already exists, appends a separator and new batch header
    to clearly distinguish this run from previous ones.

    This allows multiple batch runs to be logged in the same file
    with clear separation between sessions.

    Args:
        log_path (str): Filesystem path to the log file.
        total_jobs (int): Total number of jobs to process.
        total_chars_all (int): Total characters across all jobs.

    Returns:
        bool: True if the log file was initialized successfully,
            False if an error occurred.

    Note:
        The function will create the directory structure if it doesn't exist.
        If the file cannot be created, it returns False and the caller
        should consider disabling logging.
    """
    try:
        # Ensure the directory exists
        log_dir = os.path.dirname(log_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"# TTS Generation Log - Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# Total jobs: {total_jobs}, Total chars: {total_chars_all}\n")
            f.write("#" + "=" * 70 + "\n\n")
        return True
    except Exception:
        return False


def write_pregeneration_summary_to_log(log_path, npc_stats, profile_map):
    """
    Write the pre-generation summary to the log file.

    Args:
        log_path (str): Filesystem path to the log file.
        npc_stats (dict): Statistics dictionary for each NPC.
        profile_map (dict): Voice profile map from get_all_profiles().

    Returns:
        None: This function has no return value.
    """
    try:
        summary = format_pregeneration_summary(npc_stats, profile_map)
        summary = strip_icons(summary)  # <-- Strip icons here
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(summary)
    except Exception:
        pass    


def write_log_entry(log_path, idx, total_jobs, strref, filename, npc_name, voice_name, 
                    chars, elapsed, audio_duration, success, error_msg=None):
    """
    Write a formatted log entry for a completed generation job to a log file.

    Appends a single line to the specified log file containing all relevant
    information about a TTS generation job. The log line includes a timestamp,
    job status, STRREF, filename, NPC name, character count, generation time,
    and audio duration.

    The log format is designed to be both human-readable and easily parsable
    for post-processing or analysis. Each log entry is a single line with
    space-separated fields.

    Args:
        log_path (str): Filesystem path to the log file. If the file doesn't
            exist, it will be created.
        idx (int): Current job index (1-based) in the processing queue.
        total_jobs (int): Total number of jobs to process.
        strref (str): The STRREF identifier being generated.
        filename (str): The output filename (without extension).
        npc_name (str): The NPC name associated with this voice line.
        voice_name (str): The voice profile name used for generation.
        chars (int): Number of characters in the generated text.
        elapsed (float): Time taken for generation in seconds.
        audio_duration (float): Duration of the generated audio in seconds.
        success (bool): True if generation succeeded, False otherwise.
        error_msg (str, optional): If success is False, an error message
            describing what went wrong. Defaults to None.

    Returns:
        None: This function has no return value.

    Note:
        The function silently handles write errors (e.g., permission denied,
        disk full) to prevent logging failures from crashing the main
        generation process. Any errors are suppressed and ignored.

    Example log line:
        [2026-08-13 14:30:10] [  1/114] SUCCESS  STRREF: 47566  File: TS001GY6
        NPC: Aerie                Chars:   85  Gen:  10.15s  Audio:  1.20s
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    line = format_job_summary(idx, total_jobs, strref, filename, chars, elapsed, 
                             audio_duration, npc_name, voice_name, success, error_msg)
    
    # Strip icons for log file
    line = strip_icons(line)
    log_line = f"[{timestamp}] {line}"
    
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception:
        pass

def write_final_log_summary(log_path, total_jobs, total_chars_processed, avg_time_per_char, npc_stats):
    """
    Write a final summary to the log file after all jobs are processed.

    Appends a summary section to the log file with overall statistics
    including total files processed, total characters, average time per
    character, and skipped files per NPC. This provides a complete
    picture of the entire generation run.

    Args:
        log_path (str): Filesystem path to the log file.
        total_jobs (int): Total number of jobs processed.
        total_chars_processed (int): Total characters processed.
        avg_time_per_char (float): Average generation time per character.
        npc_stats (dict): Statistics dictionary per NPC, containing
            "done" and "skipped" counts.

    Returns:
        None: This function has no return value.

    Note:
        The function silently handles write errors to prevent crashes.
    """
    try:
        summary = format_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats)
        summary = strip_icons(summary)  # <-- Strip icons here
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(summary)
    except Exception:
        pass
#endregion Logging

#region Voice Profile Management
def get_voice_profile_name(npc_name, gender=None, profile_map=None):
    r"""
    Resolve an NPC name to the corresponding Voicebox profile name.

    Uses the VOICE_SUBSTITUTIONS mapping to translate NPC names to specific
    voice profiles. This allows multiple NPCs to share a voice profile or
    to use a profile name that differs from the NPC's display name.

    If no substitution exists for the NPC, the NPC name itself is used
    as the profile name.

    If voice fallback is enabled and the profile doesn't exist, falls back
    to gender-based voices.

    Priority order:
    1. Explicit substitution (VOICE_SUBSTITUTIONS)
    2. NPC name as profile name (if it exists in profile_map)
    3. Gender-based fallback (if USE_VOICE_FALLBACK is True)
    4. Neutral/unknown fallback

    Args:
        npc_name (str): The name of the NPC as it appears in the CSV data.
            Can be empty for descriptions/lore entries.
        gender (str, optional): Gender from CSV ("M", "F", or empty).
        profile_map (dict, optional): Map of available voice profiles for fallback checking.

    Returns:
        str: The Voicebox profile name to use for generating speech, or None
            if no valid voice could be resolved.

    Example:
        >>> get_voice_profile_name("Nym Khalazza")
        "BG1 Narrator"
        >>> get_voice_profile_name("Jaheira", "F")
        "Jaheira"  # or fallback to FALLBACK_VOICE_FEMALE if not found
        >>> get_voice_profile_name("", "M")  # Empty NPC name
        "BG1 Narrator"  # Uses FALLBACK_VOICE_MALE
        >>> get_voice_profile_name("Unknown NPC", "", None)
        "Unknown NPC"  # No fallback, returns the name as-is
    """
    # 1. Check explicit substitutions first (only if npc_name exists)
    if npc_name and npc_name in VOICE_SUBSTITUTIONS:
        return VOICE_SUBSTITUTIONS[npc_name]
    
    # 2. Check if the NPC name exists as a profile (only if npc_name exists)
    if npc_name and profile_map is not None and npc_name in profile_map:
        return npc_name
    
    # 3. Fallback if enabled
    if USE_VOICE_FALLBACK:
        if gender == "M":
            return FALLBACK_VOICE_MALE
        elif gender == "F":
            return FALLBACK_VOICE_FEMALE
        else:
            return FALLBACK_VOICE_NEUTRAL
    
    # 4. No fallback and no valid voice found - return None
    return None 


def get_all_profiles():
    """
    Fetch all available voice profiles from the Voicebox TTS API.

    Sends a GET request to the /profiles endpoint of the Voicebox server
    and returns a mapping of profile names to their numeric IDs.

    Returns:
        dict: A dictionary mapping profile names (str) to profile IDs (int).

    Raises:
        requests.exceptions.RequestException: If the API request fails
            due to network issues, server errors, or invalid responses.

    Note:
        The API is expected to return a JSON array of profile objects,
        each containing at least "name" and "id" fields. Profiles without
        a name or ID will be silently skipped.
    """
    resp = requests.get(f"{BASE_URL}/profiles")
    resp.raise_for_status()
    return {p["name"]: p["id"] for p in resp.json()}
#endregion Voice Profile Management

#region Generation Memory
def load_generation_memory(memory_path):
    """
    Load the generation history from a JSON file.

    The generation memory tracks which voice lines have already been successfully
    generated, organized by NPC name and STRREF. This enables the script to
    skip already-completed generations on subsequent runs.

    Memory structure:
        {
            "NPC Name 1": {
                "12345": true,
                "12346": true
            },
            "NPC Name 2": {
                "78901": true
            }
        }

    Args:
        memory_path (str): Filesystem path to the JSON memory file.

    Returns:
        dict: The loaded memory dictionary, or an empty dict if the file
            doesn't exist or is corrupted.

    Note:
        If the file exists but contains invalid JSON or is not a dict,
        the function logs a warning and returns an empty dict to allow
        the script to continue with a fresh memory.
    """
    if not os.path.exists(memory_path):
        return {}

    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            print("⚠️ Generation memory is not a JSON object. Starting with empty memory.")
            return {}

        return data

    except Exception as e:
        print(f"⚠️ Could not load generation memory: {e}")
        print("   Starting with empty memory.")
        return {}


def save_generation_memory(memory, memory_path):
    """
    Save the generation history to a JSON file.

    Writes the memory dictionary to disk with pretty-printing (4-space indent)
    for human readability. The file is overwritten completely on each save.

    Args:
        memory (dict): The generation memory dictionary to save.
        memory_path (str): Filesystem path where the JSON file should be written.

    Raises:
        OSError: If the file cannot be written due to permissions or
            filesystem errors.
        TypeError: If the memory contains data that is not JSON-serializable.

    Note:
        The function does not create parent directories; they should exist
        before calling this function.
    """
    with open(memory_path, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=4, ensure_ascii=False)


def is_already_generated(memory, npc_name, strref):
    """
    Check if a specific voice line has already been generated.

    Queries the generation memory to determine if a given NPC/STRREF
    combination has been previously successfully processed.

    Args:
        memory (dict): The generation memory dictionary.
        npc_name (str): The name of the NPC.
        strref (str): The STRREF identifier for the voice line.

    Returns:
        bool: True if the combination exists in memory, False otherwise.

    Note:
        STRREF values are converted to strings for dictionary lookup
        to ensure consistent matching regardless of input type.
    """
    return str(strref) in memory.get(npc_name, {})


def mark_as_generated(memory, npc_name, strref):
    """
    Record a successfully generated voice line in memory.

    Updates the generation memory to mark a specific NPC/STRREF
    combination as completed. Creates the necessary nested dictionary
    structure if it doesn't already exist.

    Args:
        memory (dict): The generation memory dictionary (modified in place).
        npc_name (str): The name of the NPC.
        strref (str): The STRREF identifier for the voice line.

    Note:
        STRREF is stored as a string key with value True to indicate
        successful generation. The function modifies the memory dict
        in place and does not automatically save to disk.
    """
    voice_memory = memory.setdefault(npc_name, {})
    voice_memory[str(strref)] = True
#endregion Generation Memory

#region TTS API Client
def submit_generation(profile_id, text, engine, model_size):
    """
    Submit a text-to-speech generation request to the Voicebox API.

    Sends the provided text to the Voicebox server for processing with the
    specified voice profile, engine, and model size. The server responds
    with a generation ID that can be used to track the job's progress.

    Args:
        profile_id (int): The numeric ID of the voice profile to use.
        text (str): The text content to convert to speech.
        engine (str): The TTS engine to use (e.g., "qwen").
        model_size (str): The model size (e.g., "0.6B", "1.5B").

    Returns:
        str: The generation ID assigned by the Voicebox server.

    Raises:
        requests.exceptions.RequestException: If the API request fails.
        RuntimeError: If the response does not contain an "id" field.

    Note:
        The generation ID is required for subsequent status polling
        and audio download operations.
    """
    payload = {
        "text": text,
        "profile_id": profile_id,
        "language": "en",
        "engine": engine,
        "model_size": model_size,
    }
    resp = requests.post(f"{BASE_URL}/generate", json=payload)
    resp.raise_for_status()
    data = resp.json()
    gen_id = data.get("id")
    if not gen_id:
        raise RuntimeError(f"Response missing 'id': {data}")
    return gen_id


def wait_for_completion(gen_id):
    """
    Wait for a generation job to complete by streaming Server-Sent Events.

    Connects to the Voicebox API's status endpoint and listens for SSE events
    until the generation reaches either "completed" or "failed" status.

    The function streams events in real-time, allowing the server to push
    progress updates, though this implementation only cares about the final
    status event.

    Args:
        gen_id (str): The generation ID returned by submit_generation().

    Returns:
        dict: The final event data containing at least "status" and
            potentially "duration" (for completed jobs) or "error"
            (for failed jobs).

    Raises:
        requests.exceptions.RequestException: If the SSE connection fails.

    Note:
        The function blocks until the generation completes or fails.
        For very slow generations, this may take a long time.
    """
    url = f"{BASE_URL}/generate/{gen_id}/status"
    headers = {"Accept": "text/event-stream"}
    final_event = None

    with requests.get(url, headers=headers, stream=True) as response:
        response.raise_for_status()

        for line in response.iter_lines(decode_unicode=True):
            if isinstance(line, bytes):
                line = line.decode("utf-8")

            if not line:
                continue

            if line.startswith("data: "):
                json_str = line[6:]

                try:
                    event = json.loads(json_str)
                except json.JSONDecodeError:
                    # Malformed JSON in SSE stream - skip this event
                    continue

                status = event.get("status")

                # The API sends a final event when generation ends
                if status in ("completed", "failed"):
                    final_event = event
                    break

    return final_event


def cancel_generation(gen_id):
    """
    Cancel a queued or running generation on the Voicebox server.

    Sends a POST request to the /generate/{generation_id}/cancel endpoint
    to stop the generation if it's still running.

    Args:
        gen_id (str): The generation ID to cancel.

    Returns:
        tuple: (success, message)
            - success (bool): True if cancellation was successful, False otherwise.
            - message (str): Status message describing the result.
    """
    try:
        cancel_url = f"{BASE_URL}/generate/{gen_id}/cancel"
        resp = requests.post(cancel_url)
        
        if resp.status_code == 200:
            return True, "Cancellation successful"
        else:
            return False, f"Cancellation returned: {resp.status_code}"
    except Exception as e:
        return False, f"Cancellation error: {e}"


def download_audio(gen_id, output_path):
    """
    Download the generated audio file from the Voicebox API.

    Retrieves the audio for a completed generation job and saves it to
    the specified output path. The audio is downloaded as raw WAV data
    (or whatever format the server provides).

    Args:
        gen_id (str): The generation ID to download.
        output_path (str): Filesystem path where the audio should be saved.

    Raises:
        requests.exceptions.RequestException: If the download request fails.
        OSError: If the output file cannot be written.

    Note:
        The function does not perform any audio conversion; it saves the
        raw audio data exactly as received from the server.
    """
    url = f"{BASE_URL}/audio/{gen_id}"
    resp = requests.get(url)
    resp.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(resp.content)
#endregion TTS API Client

#region Text Processing
def strip_icons(text):
    """
    Remove emojis and special Unicode characters from a string for log output.

    Replaces common emojis with plain text equivalents to ensure clean
    log files that display properly in any text viewer.

    Args:
        text (str): Text containing emojis/special characters.

    Returns:
        str: Text with emojis replaced by plain text equivalents.
    """
    replacements = {
        "✅": "OK",
        "❌": "--",
        "📊": "",
        "📋": "",
        "🔧": "",
        "🔄": "",
        "⛔": "",
        "⚠️": "",
        "🎙️": "",
        "📝": "",
        "🗂️": "",
        "💾": "",
        "⌛": "",
    }
    
    for emoji, replacement in replacements.items():
        text = text.replace(emoji, replacement)
    
    return text

def load_patcher_config(config_path):
    """
    Load the patcher configuration from a JSON file.

    The patcher config contains text transformation rules including:
    - Identity token mappings (e.g., <CHARNAME> -> actual character name)
    - Gender token variations (e.g., <HE> -> "he", "she", or "they")
    - Phonetic substitution rules for improved TTS pronunciation

    Args:
        config_path (str): Filesystem path to the JSON configuration file.

    Returns:
        dict: The loaded configuration object.

    Raises:
        FileNotFoundError: If the configuration file doesn't exist.
        json.JSONDecodeError: If the file contains invalid JSON.

    Note:
        The configuration structure must match what the patcher system expects.
        Missing optional fields will default to empty lists/dicts.
    """
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def preprocess_text(text, patcher_config):
    """
    Apply comprehensive text transformations for TTS preparation.

    Processes the input text through several transformation stages to
    convert placeholder tokens and apply pronunciation rules before
    sending to the TTS engine.

    Transformation stages (executed in order):
        1. Identity tokens: Replace <CHARNAME>, <GABBER>, <RACE>, <PRO_RACE>
           with the actual PC name/race from the patcher config.

        2. Gender tokens: Replace <HE>, <SHE>, <HIS>, <HER>, <HIM>, etc.
           with appropriate forms based on the PC's gender (male/female/neutral).

        3. Phonetic rules: Apply regex-based substitutions to improve
           TTS pronunciation (e.g., expanding "Mr." to "Mister").

        4. Token cleanup: Remove any remaining <...> tokens that weren't
           processed (treats them as formatting artifacts).

    Args:
        text (str): The raw input text from the CSV file.
        patcher_config (dict): Loaded patcher configuration containing
            identity tokens, gender tokens, and phonetic rules.

    Returns:
        str: The preprocessed text, ready for TTS generation.

    Note:
        The function gracefully handles missing configuration keys by
        skipping that transformation stage. Regex errors in phonetic rules
        are caught and ignored to prevent total failure.
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
            
            # If replacement contains C#-style backreferences ($1, $2, etc.)
            if replacement_template and '$' in replacement_template:
                def repl_func(match):
                    result = replacement_template
                    # Replace $1, $2, etc. with match groups
                    for i in range(1, match.lastindex + 1 if match.lastindex else 0):
                        group_value = match.group(i) or ''
                        result = result.replace(f'${i}', group_value)
                    return result
                
                text = compiled_pattern.sub(repl_func, text)
            else:
                text = compiled_pattern.sub(replacement_template, text)
                
        except re.error:
            # Skip malformed regex patterns rather than crashing
            continue

    # Remove any leftover <...> tokens that weren't processed
    text = re.sub(r'<[^>]+>', '', text)

    return text
#endregion Text Processing

#region Audio Processing
def convert_to_ogg(input_path, output_path=None, quality=2):
    """
    Convert an audio file to Ogg Vorbis format using ffmpeg.

    Uses ffmpeg to convert audio to the Ogg Vorbis codec with libvorbis.
    The output file can be either specified or overwrite the input file.

    Args:
        input_path (str): Path to the source audio file (typically WAV).
        output_path (str, optional): Path for the output file. If None,
            overwrites the input file.
        quality (int, optional): libvorbis quality scale from 0 (lowest)
            to 10 (highest). Defaults to 2, which provides reasonable
            quality-to-size ratio for dialog.

    Returns:
        None: The converted audio is written to the output file.

    Raises:
        subprocess.CalledProcessError: If ffmpeg fails to convert the file.
        FileNotFoundError: If ffmpeg is not installed or not in PATH.

    Note:
        The function forces the Ogg container format regardless of file
        extension using `-f ogg`. This means a file named "example.wav"
        will still be properly formatted as Ogg Vorbis despite the .wav
        extension (which is often required by game engines for compatibility).
    """
    if output_path is None:
        output_path = input_path

    cmd = [
        'ffmpeg',
        '-y',                      # Overwrite output files
        '-i', input_path,          # Input file
        '-c:a', 'libvorbis',       # Use libvorbis codec
        '-qscale:a', str(quality), # Quality setting
        '-f', 'ogg',               # Force Ogg container format
        output_path
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"ffmpeg stderr:\n{e.stderr.decode('utf-8', errors='ignore') if isinstance(e.stderr, bytes) else e.stderr}")
        raise
#endregion Audio Processing

#region UI/Progress Display
def format_overall_line(total_chars_processed, total_chars_all, total_jobs, idx,
                        overall_regressor, avg_time_per_char, elapsed_total):
    """
    Build the Overall progress line string without printing it.

    Constructs a single-line status summary that shows global generation
    progress across all jobs. The returned string is designed to be kept
    as the permanent last line of the console while individual jobs run,
    giving the user a continuous view of overall completion, elapsed time,
    and estimated finish time.

    Calculation logic:
        - Percentage is based on characters processed versus total characters.
        - ETA is predicted with linear regression when enough historical data
          exists (len(overall_regressor) > 1). Otherwise it falls back to a
          simple average-time-per-character estimate.
        - When insufficient data is available the function returns a static
          "Overall: processing..." placeholder.

    Args:
        total_chars_processed (int): Characters successfully generated so far.
        total_chars_all (int): Total characters across all selected jobs.
        total_jobs (int): Total number of jobs in the current run.
        idx (int): Number of jobs already completed (0-based for remaining
            calculation). Used to weight the intercept term of the regression.
        overall_regressor (Regression): Accumulated (chars, time) samples used
            for slope/intercept prediction of remaining work.
        avg_time_per_char (float or None): Running average seconds per character.
            None on the first job before any data exists.
        elapsed_total (float): Wall-clock seconds since the start of the whole
            generation batch.

    Returns:
        str: A fully formatted Overall line ready to be written to stdout,
            or the placeholder "Overall: processing..." when statistics are
            not yet available.
    """
    if avg_time_per_char is not None and total_chars_all > 0:
        remaining_chars = total_chars_all - total_chars_processed

        if len(overall_regressor) > 1:
            eta_seconds = (overall_regressor.slope() * remaining_chars
                           + overall_regressor.intercept() * (total_jobs - idx))
        else:
            eta_seconds = remaining_chars * avg_time_per_char if remaining_chars > 0 else 0

        overall_percent = (total_chars_processed / total_chars_all) * 100
        chars_processed_str = f"{total_chars_processed:,}"
        chars_total_str = f"{total_chars_all:,}"

        return (
            f"Overall: "
            f"{progress_bar(overall_percent)}  "
            f"{chars_processed_str:>8}/{chars_total_str:<8} chars  "
            f"Elapsed: {format_time(elapsed_total):>9}  "
            f"ETA: {format_time(eta_seconds):>9}  "
            f"@ {format_finish_time(eta_seconds)}"
        )
    return "Overall: processing..."


def progress_worker(stop_event, job_idx, total_jobs, filename, estimated_sec, timeout_sec,
                    npc_name, voice_name, chars, overall_line, strref):
    r"""
    Background thread that maintains a two-line live progress display.

    While a single TTS generation is running this worker continuously updates
    two console lines:

        [job progress bar]          <-- rewritten every 0.5 s
        Overall: ...                <-- always kept as the LAST line

    The job line shows the current file, a percentage bar based on elapsed
    versus estimated time, character count, and NPC name. The Overall line
    (supplied by the caller) remains permanently at the bottom so the user
    can always see global progress and ETA without scrolling.

    Implementation notes:
        - On the first iteration both lines are printed normally so the
          Overall line becomes the final visible line.
        - Subsequent iterations use ANSI cursor movement (\033[2A, \033[K)
          to rewrite the two lines in place without scrolling the terminal.
        - When stop_event is set the worker clears both lines and repositions
          the cursor so the main thread can print the permanent ✅ summary
          cleanly on the same screen real-estate.

    Args:
        stop_event (threading.Event): Event that signals the thread to exit
            (generation completed or failed).
        job_idx (int): Current job number (1-indexed) in the queue.
        total_jobs (int): Total number of jobs to process.
        filename (str): The filename being generated (for display).
        estimated_sec (float): Estimated duration for this job in seconds.
        timeout_sec (float): Maximum allowed duration for this job in seconds.
        npc_name (str): The NPC name being processed (for display).
        voice_name (str): Voice profile name; shown in parentheses only when
            it differs from npc_name.
        chars (int): Number of characters in the text being generated.
        overall_line (str): Pre-formatted Overall progress string that must
            stay as the last line of the display.
        strref (str): The STRREF identifier being generated.

    Note:
        The thread is daemonized and will exit cleanly when stop_event is set.
        It assumes a terminal that understands basic ANSI escape sequences
        for cursor movement and line clearing.
    """
    start_time = time.time()
    first = True
    job_width = len(str(total_jobs))

    while not stop_event.is_set():
        elapsed = time.time() - start_time

        if estimated_sec > 0:
            percent = min(100, (elapsed / estimated_sec) * 100)
        else:
            percent = 0

        bar = progress_bar(percent)
        time_str = f"{format_time(elapsed)} / {format_time(estimated_sec)} (max: {format_time(timeout_sec)})"

        # Job progress line with STRREF - keep Grok's formatting
        job_msg = (
            f"[{job_idx:>{job_width}}/{total_jobs:>{job_width}}] "
            f"{strref}/{filename}  "
            f"{bar}  "
            f"{time_str}  "
            f"({chars} chars)  "
            f"{npc_name}"
        )
        if voice_name != npc_name:
            job_msg += f" ({voice_name})"

        if first:
            # First draw: print both lines so Overall becomes the last line
            sys.stdout.write(job_msg + "\n")
            sys.stdout.write(overall_line + "\n")
            sys.stdout.flush()
            first = False
        else:
            # Subsequent draws: move up 2 lines, rewrite both, leave cursor
            # after the Overall line so it stays last.
            sys.stdout.write("\033[2A")              # up to job line
            sys.stdout.write("\r\033[K" + job_msg)  # clear + rewrite job
            sys.stdout.write("\n")
            sys.stdout.write("\r\033[K" + overall_line)  # clear + rewrite Overall
            sys.stdout.write("\n")
            sys.stdout.flush()

        time.sleep(0.5)

    # Job finished: clear the two progress lines so the caller can print the ✅ summary
    if not first:
        sys.stdout.write("\033[2A")      # up to job line
        sys.stdout.write("\r\033[K")     # clear job line
        sys.stdout.write("\n")
        sys.stdout.write("\r\033[K")     # clear Overall line
        sys.stdout.write("\n")
        sys.stdout.write("\033[2A")      # go back up so next print starts on the cleared job line
        sys.stdout.flush()


def format_pregeneration_summary(npc_stats, profile_map, for_log=False):
    """
    Format the pre-generation summary as a string for both console and log output.

    Builds a complete summary table showing:
    - Voice profile status (valid or missing)
    - Total lines per NPC
    - Already generated (Done)
    - Missing voices (Missing)
    - Remaining to generate (To Gen)
    - Total character count

    The summary helps users verify that all configured voices exist before
    starting the potentially long generation process.

    If COMPACT_SUMMARY is True, only NPCs with valid voices are shown in the
    detailed table, and missing voices are summarized in a single line at the end.
    If COMPACT_SUMMARY is False, all NPCs are shown including those with
    missing voices (marked with "❌ Missing").

    Column definitions:
        - Total: Total number of rows for this NPC
        - Done: Rows already generated (from generation-memory.json)
        - Missing: Rows skipped because no valid voice profile exists
        - To Gen: Rows that will be generated now
        - Characters: Total characters across all rows for this NPC

    Args:
        npc_stats (dict): Statistics dictionary for each NPC, structured as:
            {
                "NPC Name": {
                    "voice_name": str,
                    "total": int,
                    "done": int,
                    "missing": int,
                    "to_generate": int,
                    "chars": int
                }
            }
        profile_map (dict): Voice profile map from get_all_profiles(),
            mapping profile names to their IDs.

    Note:
        The table includes a "VALID TOTAL" row showing only NPCs with
        existing voice profiles, and a "TOTAL" row showing all NPCs
        (including those with missing profiles that will be skipped).
        Missing profiles are marked with "❌ Missing" in the table when
        COMPACT_SUMMARY is False, or summarized in a single line when True.
    """
    line_length = 108
    lines = []
    
    lines.append("\n" + "=" * line_length)
    lines.append("📊 PRE-GENERATION VOICE SUMMARY")
    
    # Show fallback status
    if USE_VOICE_FALLBACK:
        lines.append(f"   🔄 Voice fallback ENABLED: M->{FALLBACK_VOICE_MALE}, F->{FALLBACK_VOICE_FEMALE}, NEUTRAL->{FALLBACK_VOICE_NEUTRAL}")
    else:
        lines.append("   ⛔ Voice fallback DISABLED")
    
    # Show strref filter status
    if USE_STRREF_FILTER:
        try:
            with open(STRREF_FILTER_FILE, "r") as f:
                count = len(json.load(f))
            lines.append(f"   📋 STRREF filter ENABLED: {count} STRREFs from {STRREF_FILTER_FILE}")
        except:
            lines.append(f"   📋 STRREF filter ENABLED (file: {STRREF_FILTER_FILE})")
    else:
        lines.append("   📋 STRREF filter DISABLED")
    
    # Show filename generation status
    if FORCE_GENERATED_FILENAMES:
        lines.append(f"   🔧 Filenames: FORCED generated (base36) with prefix: {RESREF_PREFIX}")
    else:
        lines.append(f"   🔧 Filenames: CSV with base36 fallback (prefix: {RESREF_PREFIX})")
    
    lines.append("=" * line_length)

    header = f"{'NPC Name':<28} {'Profile':<30} {'Total':>7} {'Done':>8} {'Missing':>9} {'To Gen':>8} {'Chars':>12}"
    lines.append(header)
    lines.append("-" * line_length)

    grand_total = 0
    grand_done = 0
    grand_skipped = 0
    grand_to_gen = 0
    grand_chars = 0

    valid_total = 0
    valid_done = 0
    valid_skipped = 0
    valid_to_gen = 0
    valid_chars = 0
    
    missing_npcs = []
    missing_chars_total = 0
    missing_done_total = 0
    missing_skipped_total = 0

    for npc_name, stats in npc_stats.items():
        profile_name = stats.get("voice_name", npc_name)
        has_profile = profile_name in profile_map
        total = stats["total"]
        done = stats["done"]
        skipped = stats["skipped"]
        to_gen = stats["to_generate"]
        chars = stats["chars"]

        grand_total += total
        grand_done += done
        grand_skipped += skipped
        grand_to_gen += to_gen
        grand_chars += chars

        if has_profile:
            valid_total += total
            valid_done += done
            valid_skipped += skipped
            valid_to_gen += to_gen
            valid_chars += chars
            
            # Format valid NPCs (always show these)
            profile_str = f"✅ {profile_name}"
            lines.append(
                f"{npc_name:<28} "
                f"{profile_str:<29} "
                f"{total:>7,} "
                f"{done:>8,} "
                f"{skipped:>9,} "
                f"{to_gen:>8,} "
                f"{chars:>12,}"
            )
        else:
            # Track missing NPCs for summary
            missing_npcs.append(npc_name)
            missing_chars_total += chars
            missing_done_total += done
            missing_skipped_total += skipped
            
            # Only print missing NPCs if COMPACT_SUMMARY is False
            if not COMPACT_SUMMARY:
                profile_str = "❌ Missing"
                lines.append(
                    f"{npc_name:<28} "
                    f"{profile_str:<29} "
                    f"{total:>7,} "
                    f"{done:>8,} "
                    f"{skipped:>9,} "
                    f"{to_gen:>8,} "
                    f"{chars:>12,}"
                )

    lines.append("-" * line_length)
    
    # Print summary for missing voices if there are any
    if missing_npcs and COMPACT_SUMMARY:
        missing_total = grand_total - valid_total
        missing_done = grand_done - valid_done
        missing_skipped = grand_skipped - valid_skipped
        
        lines.append(
            f"{'❌ MISSING VOICES':<27} "
            f"{'(summary)':<30} "
            f"{missing_total:>7,} "
            f"{missing_done:>8,} "
            f"{missing_skipped:>9,} "
            f"{0:>8,} "
            f"{missing_chars_total:>12,}"
        )
        lines.append("-" * line_length)

    # Print totals
    lines.append(
        f"{'VALID TOTAL':<28} "
        f"{'':<30} "
        f"{valid_total:>7,} "
        f"{valid_done:>8,} "
        f"{valid_skipped:>9,} "
        f"{valid_to_gen:>8,} "
        f"{valid_chars:>12,}"
    )
    lines.append(
        f"{'TOTAL':<28} "
        f"{'':<30} "
        f"{grand_total:>7,} "
        f"{grand_done:>8,} "
        f"{grand_skipped:>9,} "
        f"{grand_to_gen:>8,} "
        f"{grand_chars:>12,}"
    )
    lines.append("=" * line_length + "\n")
    
    return "\n".join(lines)


def print_pregeneration_summary(npc_stats, profile_map):
    """
    Print a structured summary of files to generate per NPC/voice.

    Displays a formatted table showing for each NPC:
    - The voice profile that will be used (with validation)
    - Total lines to process
    - Lines already generated (Done)
    - Lines with missing voices (Missing)
    - Lines remaining to generate (To Gen)
    - Total character count

    The summary helps users verify that all configured voices exist before
    starting the potentially long generation process.

    If COMPACT_SUMMARY is True, only NPCs with valid voices are shown in the
    detailed table, and missing voices are summarized in a single line at the end.
    If COMPACT_SUMMARY is False, all NPCs are shown including those with
    missing voices (marked with "❌ Missing").

    Column definitions:
        - Total: Total number of rows for this NPC
        - Done: Rows already generated (from generation-memory.json)
        - Missing: Rows skipped because no valid voice profile exists
        - To Gen: Rows that will be generated now
        - Characters: Total characters across all rows for this NPC

    Args:
        npc_stats (dict): Statistics dictionary for each NPC, structured as:
            {
                "NPC Name": {
                    "voice_name": str,
                    "total": int,
                    "done": int,
                    "missing": int,
                    "to_generate": int,
                    "chars": int
                }
            }
        profile_map (dict): Voice profile map from get_all_profiles(),
            mapping profile names to their IDs.

    Note:
        The table includes a "VALID TOTAL" row showing only NPCs with
        existing voice profiles, and a "TOTAL" row showing all NPCs
        (including those with missing profiles that will be skipped).
        Missing profiles are marked with "❌ Missing" in the table when
        COMPACT_SUMMARY is False, or summarized in a single line when True.
    """
    summary = format_pregeneration_summary(npc_stats, profile_map, for_log=False)
    print(summary)
#endregion UI/Progress Display

#region CSV Processing
def filter_and_sort_rows(selected_rows, profile_map):
    """
    Filter out rows with missing voice profiles and sort for optimal processing.

    Removes rows where the voice profile doesn't exist on the server, then sorts
    by voice name to group similar voices together for better caching and
    performance.

    Args:
        selected_rows (list): List of (strref, display_name, voice_name, filename, text)
        profile_map (dict): Map of profile names to IDs.

    Returns:
        list: Filtered and sorted rows.
    """
    # Filter out rows where the voice profile does not exist
    valid_rows = [row for row in selected_rows if row[2] in profile_map]  # voice_name is at index 2
    # Sort by voice name (case-insensitive) to group files by voice
    valid_rows.sort(key=lambda row: row[2].lower())  # voice_name is at index 2
    return valid_rows


def load_strref_filter(filter_file):
    """
    Load the STRREF filter list from a JSON file.
    
    Args:
        filter_file (str): Path to the JSON file containing strref list.
        
    Returns:
        set: Set of strref strings to process, or empty set if file not found.
    """
    if not os.path.exists(filter_file):
        print(f"⚠️ STRREF filter file not found: {filter_file}")
        print("   Processing all rows (no filter).")
        return set()
    
    try:
        with open(filter_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        if not isinstance(data, list):
            print(f"⚠️ STRREF filter file must contain a JSON array, got {type(data)}")
            return set()
        
        # Convert to set of strings for fast lookup
        return {str(item) for item in data}
    
    except Exception as e:
        print(f"⚠️ Could not load STRREF filter: {e}")
        print("   Processing all rows (no filter).")
        return set()


def load_and_filter_csv(csv_path, target_voices, filename_pattern, patcher_config, 
                       generation_memory, skip_generated, limit, profile_map=None,
                       use_strref_filter=False, strref_filter_file="strrefs.json", 
                       force_generated_filenames=False):
    """
    Load CSV data, apply filters, and prepare rows for generation.
    
    Args:
        csv_path (str): Path to the CSV file.
        target_voices (list): List of NPC names to process (empty = all).
        filename_pattern (str): Regex pattern for filename filtering.
        patcher_config (dict): Patcher configuration for text preprocessing.
        generation_memory (dict): Generation memory for skip checking.
        skip_generated (bool): Whether to skip already generated files.
        limit (int): Maximum number of rows to process (0 = all).
        profile_map (dict, optional): Map of available voice profiles for validation.
        use_strref_filter (bool): Whether to use STRREF filter instead of voice/filename filters.
        strref_filter_file (str): Path to STRREF filter JSON file.
        force_generated_filenames (bool): If True, always use generated; if False, use CSV with fallback.
        
    Returns:
        tuple: (selected_rows, npc_stats)
    """
    selected_rows = []
    npc_stats = {}
    strref_filter = set()
    
    # Load STRREF filter if enabled
    if use_strref_filter:
        strref_filter = load_strref_filter(strref_filter_file)
        if strref_filter:
            print(f"Loaded {len(strref_filter)} STRREFs from filter file.")
        else:
            print("⚠️ No STRREFs loaded from filter file. Processing all rows.")
    
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            
            for row in reader:
                if len(row) < 8:
                    continue
                
                strref = row[0].strip()
                npc_name = row[2].strip() if len(row) > 2 else ""
                gender = row[3].strip() if len(row) > 3 else ""
                csv_filename = row[5].strip() if len(row) > 5 else ""
                text = row[7].strip() if len(row) > 7 else ""
                
                # Apply STRREF filter if enabled
                if use_strref_filter and strref_filter:
                    if strref not in strref_filter:
                        continue
                
                # Apply voice filter (only if not using STRREF filter)
                if not use_strref_filter and target_voices and npc_name not in target_voices:
                    continue
                
                # Apply filename pattern filter (only if not using STRREF filter)
                if not use_strref_filter and filename_pattern and not re.match(filename_pattern, csv_filename):
                    continue
                
                # Skip rows without text
                if not text:
                    continue
                
                # Determine filename with fallback
                if force_generated_filenames:
                    # Always use generated filename
                    filename = generate_resref(strref, RESREF_PREFIX)
                else:
                    # Use CSV filename if it exists and is valid, otherwise fallback to generated
                    if csv_filename and csv_filename.strip():
                        filename = csv_filename
                    else:
                        # CSV filename is empty or invalid - generate one
                        filename = generate_resref(strref, RESREF_PREFIX)
                        print(f"⚠️ STRREF {strref}: CSV filename empty or invalid, using generated: {filename}")
                
                # Get voice profile with fallback - pass profile_map for validation
                voice_name = get_voice_profile_name(npc_name, gender, profile_map)

                # Use npc_name for stats, or "Descriptions" if empty
                display_name = npc_name if npc_name else "Descriptions"
                
                # Initialize NPC stats (ALWAYS, even if voice is missing)
                if display_name not in npc_stats:
                    npc_stats[display_name] = {
                        "voice_name": voice_name if voice_name else "MISSING",
                        "total": 0,
                        "done": 0,              # Already generated
                        "skipped": 0,           # Missing voices
                        "to_generate": 0,
                        "chars": 0
                    }
                
                npc_stats[display_name]["total"] += 1
                npc_stats[display_name]["chars"] += len(text)
                
                # Check if voice is missing
                if voice_name is None:
                    # Count as skipped (missing voice)
                    npc_stats[display_name]["skipped"] += 1
                    continue

                # Check if voice_name exists in profile_map
                if profile_map is not None and voice_name not in profile_map:
                    # Count as skipped (voice not on server)
                    npc_stats[display_name]["skipped"] += 1
                    continue
                
                # Skip already generated if enabled
                if skip_generated and is_already_generated(generation_memory, display_name, strref):
                    npc_stats[display_name]["done"] += 1
                    continue
                
                # This row passed all filters - add to generation queue
                npc_stats[display_name]["to_generate"] += 1
                
                # Preprocess text
                text = preprocess_text(text, patcher_config) if patcher_config else text
                
                # Store the row with display_name for folder structure
                selected_rows.append((strref, display_name, voice_name, filename, text))
                
                if limit and len(selected_rows) >= limit:
                    break
    
    except Exception as e:
        print(f"❌ Error reading CSV: {e}")
        sys.exit(1)
    
    return selected_rows, npc_stats
#endregion CSV Processing

#region Generation Execution
def estimate_generation_time(regressor, chars):
    """
    Estimate generation time based on historical data or fallback.

    Uses linear regression from previous jobs to predict time for the current
    text length. Falls back to a conservative estimate if insufficient data.

    Args:
        regressor (Regression): Regression object with historical (chars, time) data.
        chars (int): Number of characters in the current text.

    Returns:
        float: Estimated time in seconds.
    """
    if len(regressor) > 1:
        estimated_sec = regressor.slope() * chars + regressor.intercept()
        return max(estimated_sec, 2.0)
    return 10.0  # Initial guess when no historical data


def timeout_monitor(stop_event, gen_id, timeout_sec, idx, start_time):
    """
    Monitor thread that checks for timeout and cancels the generation if exceeded.
    
    Runs in a separate thread and periodically checks if the generation has
    exceeded its time limit. If it has, it sends a cancellation request.
    
    Args:
        stop_event (threading.Event): Event to signal when generation completes.
        gen_id (str): The generation ID to cancel.
        timeout_sec (float): Timeout in seconds.
        idx (int): Job index for logging.
        start_time (float): Timestamp when the job started.
    """
    elapsed = 0
    while not stop_event.is_set():
        elapsed = time.time() - start_time
        if elapsed > timeout_sec:
            success, message = cancel_generation(gen_id)
            break
        time.sleep(1.0)


def process_generation_job(idx, total_jobs, strref, npc_name, voice_name, filename, text,
                           profile_id, regressor, generation_memory, overall_line):
    """
    Execute a single TTS generation job.

    Handles the complete lifecycle of one generation: submitting the request,
    waiting for completion, downloading the audio, and converting to Ogg Vorbis.

    The progress_worker keeps the job progress line and the Overall line
    (passed in as overall_line) visible, with Overall always last.

    If ENABLE_TIMEOUT_SAFEGUARD is True, a separate monitor thread watches
    the elapsed time and cancels the generation if it exceeds the threshold.

    Args:
        idx (int): Current job index (1-based).
        total_jobs (int): Total number of jobs.
        strref (str): STRREF identifier.
        npc_name (str): NPC name (for memory and folder).
        voice_name (str): Voice profile name.
        filename (str): Output filename base.
        text (str): Preprocessed text to generate.
        profile_id (int): Voice profile ID.
        regressor (Regression): Regression for time estimation.
        generation_memory (dict): Generation memory for recording completion.
        overall_line (str): Pre-formatted Overall progress string to keep as last line.

    Returns:
        tuple: (success, elapsed_time, audio_duration, chars_processed)
            where success is bool indicating if generation succeeded.
    """
    chars = len(text)
    estimated_sec = estimate_generation_time(regressor, chars)
    stop_event = threading.Event()
    cancel_event = threading.Event()  # Signals that generation is done (for monitor)

    # Calculate timeout threshold
    timeout_sec = None
    if ENABLE_TIMEOUT_SAFEGUARD:
        # Always use hard maximum
        timeout_sec = TIMEOUT_MAX_SECONDS
        
        # Use estimated time if we have enough data
        if len(regressor) >= TIMEOUT_MIN_ESTIMATES:
            estimated_timeout = estimated_sec * TIMEOUT_MULTIPLIER
            # Use the more conservative (smaller) timeout
            timeout_sec = min(timeout_sec, estimated_timeout)

    # Start progress bar thread – it will draw job line + Overall line
    worker = threading.Thread(
        target=progress_worker,
        args=(stop_event, idx, total_jobs, filename, estimated_sec, timeout_sec,
              npc_name, voice_name, chars, overall_line, strref)
    )
    worker.daemon = True
    worker.start()

    start_time = time.time()
    success = False
    elapsed = 0
    audio_duration = 0
    gen_id = None
    final_event = None
    monitor_thread = None

    try:
        gen_id = submit_generation(profile_id, text, ENGINE, MODEL_SIZE)
        
        # Start timeout monitor thread if timeout is enabled
        monitor_thread = None
        if timeout_sec is not None:
            monitor_thread = threading.Thread(
                target=timeout_monitor,
                args=(cancel_event, gen_id, timeout_sec, idx, start_time)
            )
            monitor_thread.daemon = True
            monitor_thread.start()

        # Wait for completion (blocking - uses SSE)
        final_event = wait_for_completion(gen_id)
        
        # Signal that generation is done (stop timeout monitor)
        cancel_event.set()
        if monitor_thread:
            monitor_thread.join(timeout=0.5)

        elapsed = time.time() - start_time

        # Stop the progress thread (it clears the two lines for us)
        stop_event.set()
        worker.join(timeout=1.0)

        if final_event and final_event.get("status") == "completed":
            audio_duration = final_event.get("duration", 0.0)

            # Create output directory if it doesn't exist
            safe_npc = sanitize_filename(npc_name)
            npc_output_dir = os.path.join(OUTPUT_DIR, safe_npc)
            os.makedirs(npc_output_dir, exist_ok=True)
            output_path = os.path.join(npc_output_dir, f"{filename}.wav")

            # Download and convert audio
            temp_path = output_path + ".tmp"
            try:
                download_audio(gen_id, temp_path)

                if CONVERT_TO_OGG:
                    convert_to_ogg(temp_path, output_path, OGG_QUALITY)
                    os.remove(temp_path)
                else:
                    os.rename(temp_path, output_path)

            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise e

            # Record success in memory
            mark_as_generated(generation_memory, npc_name, strref)
            save_generation_memory(generation_memory, GENERATION_MEMORY_PATH)

            success = True

    except Exception as e:
        # If there was an error and we had a gen_id, try to cancel it
        if gen_id:
            try:
                cancel_generation(gen_id)
            except Exception:
                pass  # Ignore cancellation errors during exception handling
        
        cancel_event.set()
        if monitor_thread:
            monitor_thread.join(timeout=0.5)
        
        stop_event.set()
        worker.join(timeout=1.0)

    return success, elapsed, audio_duration, chars


def format_job_summary(idx, total_jobs, strref, filename, chars, elapsed, audio_duration, npc_name, voice_name, success=True, error_msg=None):
    """
    Format a job summary line for both console output and logging.

    Creates a consistent formatted string containing all job information.
    The same format is used for both the console print and the log file,
    ensuring consistency between output streams.

    Args:
        idx (int): Current job index (1-based).
        total_jobs (int): Total number of jobs.
        strref (str): STRREF identifier.
        filename (str): Output filename.
        chars (int): Number of characters in the text.
        elapsed (float): Generation time in seconds.
        audio_duration (float): Duration of generated audio in seconds.
        npc_name (str): NPC name.
        voice_name (str): Voice profile name used.
        success (bool, optional): Whether generation succeeded. Defaults to True.
        error_msg (str, optional): Error message if failed. Defaults to None.

    Returns:
        str: Formatted job summary line.
    """
    realtime_speed = (audio_duration / elapsed * 100 if elapsed > 0 else 0)
    voice_part = f" (voice: {voice_name})" if voice_name != npc_name else ""
    status = "✅ " if success else "❌ "
    job_width = len(str(total_jobs))
    
    line = (
        f"[{idx:>{job_width}}/{total_jobs:>{job_width}}] "
        f"{status}"
        f"Strref: {strref:>6}  "
        f"File: {filename:<10}  "
        f"Chars: {chars:>4}  "
        f"Gen: {elapsed:>6.2f}s  "
        f"Audio: {audio_duration:>6.2f}s  "
        f"Speed: {realtime_speed:>5.1f}%  "
        f"Npc: {npc_name:<20}"
        f"{voice_part}"
    )
    
    if not success and error_msg:
        line += f"  Error: {error_msg}"
    
    return line


def print_job_summary(idx, total_jobs, strref, filename, chars, elapsed, audio_duration, npc_name, voice_name, success=True, error_msg=None):
    """
    Print a formatted summary for a completed generation job.

    Args:
        idx (int): Current job index (1-based).
        total_jobs (int): Total number of jobs.
        filename (str): Filename of the generated file.
        chars (int): Number of characters in the text.
        elapsed (float): Generation time in seconds.
        audio_duration (float): Duration of generated audio in seconds.
        npc_name (str): NPC name.
        voice_name (str): Voice profile name used.
        success (bool, optional): Whether generation succeeded. Defaults to True.
        error_msg (str, optional): Error message if failed. Defaults to None.
    """
    line = format_job_summary(idx, total_jobs, strref, filename, chars, elapsed, 
                             audio_duration, npc_name, voice_name, success, error_msg)
    print(line)


def print_overall_progress(total_chars_processed, total_chars_all, total_jobs, idx, overall_regressor, avg_time_per_char, elapsed_total):   
    """
    Print the overall progress bar and ETA.

    Args:
        total_chars_processed (int): Characters processed so far.
        total_chars_all (int): Total characters to process.
        total_jobs (int): Total number of jobs.
        idx (int): Current job index (1-based).
        overall_regressor (Regression): Regression for overall time estimation.
        avg_time_per_char (float): Average time per character.
    """
    if avg_time_per_char is not None:
        remaining_chars = (total_chars_all - total_chars_processed)
        
        if len(overall_regressor) > 1:
            eta_seconds = overall_regressor.slope() * remaining_chars + overall_regressor.intercept() * (total_jobs - idx)
        else:
            eta_seconds = remaining_chars * avg_time_per_char if remaining_chars > 0 else 0

        overall_percent = (total_chars_processed / total_chars_all) * 100

        # Format with thousands separators and fixed widths
        chars_processed_str = f"{total_chars_processed:,}"
        chars_total_str = f"{total_chars_all:,}"
        
        print(
            f"Overall: "
            f"{progress_bar(overall_percent)}  "
            f"{chars_processed_str:>8}/{chars_total_str:<8} chars  "
            f"Elapsed: {format_time(elapsed_total):>9}  "
            f"ETA: {format_time(eta_seconds):>9}  "
            f"@ {format_finish_time(eta_seconds)}"
        )
    else:
        print("Overall: processing...")


def format_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats):
    """
    Format the final summary as a string for both console and log output.

    Builds a complete summary string including:
    - Total files processed
    - Total characters processed
    - Average time per character
    - Already generated files (if any)
    - Missing voices (if any)

    Args:
        total_jobs (int): Total number of jobs processed.
        total_chars_processed (int): Total characters processed.
        avg_time_per_char (float): Average generation time per character.
        npc_stats (dict): Statistics dictionary per NPC.

    Returns:
        str: Formatted summary string ready for console or log output.
    """
    # Extract statistics
    total_done = 0
    total_skipped = 0
    done_summary = {}
    skipped_summary = {}

    for voice, stats in npc_stats.items():
        done = stats.get("done", 0)
        skipped = stats.get("skipped", 0)
        
        if done > 0:
            total_done += done
            done_summary[voice] = done
        
        if skipped > 0:
            total_skipped += skipped
            skipped_summary[voice] = skipped

    # Build the summary string (single content for both console and log)
    lines = []
    lines.append("")
    lines.append("=" * 70)
    lines.append("FINAL SUMMARY")
    lines.append("=" * 70)
    lines.append(f"Processed: {total_jobs:,} files")
    lines.append(f"Total characters: {total_chars_processed:,}")
    
    if avg_time_per_char:
        lines.append(f"Average time per character: {avg_time_per_char:.4f}s")

    if total_done:
        done_details = ", ".join(f"{voice}: {count:,}" for voice, count in done_summary.items())
        lines.append(f"Skipped already generated: {total_done:,} ({done_details})")

    if total_skipped:
        skipped_details = ", ".join(f"{voice}: {count:,}" for voice, count in skipped_summary.items())
        lines.append(f"Skipped missing voices: {total_skipped:,} ({skipped_details})")

    lines.append(f"# Finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append("")

    return "\n".join(lines)


def print_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats):
    """
    Print the final summary after all jobs are processed.

    Args:
        total_jobs (int): Total number of jobs processed.
        total_chars_processed (int): Total characters processed.
        avg_time_per_char (float): Average time per character.
        npc_stats (dict): Statistics dictionary per NPC.
    """
    summary = format_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats)
    print(summary)
    print("✅ Done.")
#endregion Generation Execution

# ---------- Main ----------
def main():
    """
    Main entry point for the TTS generation script.

    Orchestrates the entire generation workflow:
    1. Load voice profiles from Voicebox API
    2. Load patcher configuration for text preprocessing
    3. Load generation memory to skip already processed files
    4. Read and filter CSV data
    5. Display pre-generation summary
    6. Process each generation job with progress feedback
    7. Display final summary
    """
    # 1. Load profiles
    try:
        profile_map = get_all_profiles()
        print(f"Loaded {len(profile_map)} voice profiles.")
    except Exception as e:
        print(f"❌ Failed to fetch profiles: {e}")
        sys.exit(1)

    # 2. Load patcher config (optional - generation continues without it)
    try:
        patcher_config = load_patcher_config(PATCHER_CONFIG_PATH)
        print("Loaded patcher config.")
    except Exception as e:
        patcher_config = None
        print(f"⚠️ Could not load patcher config: {e}")

    # 3. Load generation memory to skip already processed files
    generation_memory = load_generation_memory(GENERATION_MEMORY_PATH)
    if SKIP_ALREADY_GENERATED:
        print("Loaded generation memory. Already generated files will be skipped.")
    else:
        print("Generation memory loaded. Skipping already generated files is disabled.")

    # 4. Read CSV, filter, and select rows
    selected_rows, npc_stats = load_and_filter_csv(
        CSV_PATH,
        TARGET_VOICES,
        FILENAME_PATTERN,
        patcher_config,
        generation_memory,
        SKIP_ALREADY_GENERATED,
        LIMIT,
        profile_map,
        USE_STRREF_FILTER,
        STRREF_FILTER_FILE,
        FORCE_GENERATED_FILENAMES
    )

    # 5. Display pre-generation summary
    print_pregeneration_summary(npc_stats, profile_map)

    # 6. Filter out rows with missing profiles and sort for processing
    selected_rows = filter_and_sort_rows(selected_rows, profile_map)
    total_jobs = len(selected_rows)

    total_chars_all = sum(len(text) for _, _, _, _, text in selected_rows)  # text is now at index 4

    # Show filename mode in output
    if FORCE_GENERATED_FILENAMES:
        filename_mode = "FORCED generated (base36)"
    else:
        filename_mode = "CSV with base36 fallback"

    print(f"Selected {total_jobs} rows. Total characters: {total_chars_all}")
    print(f"Filename mode: {filename_mode}")

    if total_jobs == 0:
        print("No jobs to process. Exiting.")
        return

    # Initialize logging if enabled
    global LOG_ENABLED

    if LOG_ENABLED:
        if init_log_file(LOG_FILE_PATH, total_jobs, total_chars_all):
            print(f"📝 Logging enabled: {LOG_FILE_PATH}")
        else:
            print(f"⚠️ Could not initialize log file: {LOG_FILE_PATH}")
            LOG_ENABLED = False

    # Write pre-generation summary to log if logging is enabled
    if LOG_ENABLED:
        write_pregeneration_summary_to_log(LOG_FILE_PATH, npc_stats, profile_map)            

    print("\nStarting generation...\n")

    # 7. Process all generation jobs
    total_chars_processed = 0
    total_start_time = time.time()
    avg_time_per_char = None
    overall_regressor = Regression()
    regressor = Regression()

    for idx, (strref, display_name, voice_name, filename, text) in enumerate(selected_rows, start=1):
        profile_id = profile_map.get(voice_name)
        if not profile_id:
            print(f"⏭️ Skipping {strref}/{filename}: Voice '{voice_name}' not found.")
            continue

        # Snapshot Overall line (static for the duration of this job)
        elapsed_total = time.time() - total_start_time
        overall_line = format_overall_line(
            total_chars_processed, total_chars_all, total_jobs, idx - 1,
            overall_regressor, avg_time_per_char, elapsed_total
        )

        # Process the generation job
        success, elapsed, audio_duration, chars = process_generation_job(
            idx, total_jobs, strref, display_name, voice_name, filename, text,
            profile_id, regressor, generation_memory, overall_line
        )

        if success:
            # Update statistics
            regressor.push(chars, elapsed)
            total_chars_processed += chars
            avg_time_per_char = (time.time() - total_start_time) / total_chars_processed
            overall_regressor.push(chars, elapsed)

            # Print job summary
            print_job_summary(idx, total_jobs, strref, filename, chars, elapsed, 
                            audio_duration, display_name, voice_name, success=True)
            
            # Write to log
            if LOG_ENABLED:
                write_log_entry(LOG_FILE_PATH, idx, total_jobs, strref, filename, 
                              display_name, voice_name, chars, elapsed, audio_duration, success=True)

        else:
            # Handle failure
            error_msg = "Generation failed"
            print_job_summary(idx, total_jobs, strref, filename, chars, elapsed, 
                            audio_duration, display_name, voice_name, success=False, error_msg=error_msg)
            
            if LOG_ENABLED:
                write_log_entry(LOG_FILE_PATH, idx, total_jobs, strref, filename, 
                              display_name, voice_name, chars, elapsed, audio_duration, 
                              success=False, error_msg=error_msg)           

    # 8. Final summary
    print_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats)

    # Save final log summary if logging is enabled
    if LOG_ENABLED:
        write_final_log_summary(LOG_FILE_PATH, total_jobs, total_chars_processed, avg_time_per_char, npc_stats)
        print(f"📝 Log saved to: {LOG_FILE_PATH}")

if __name__ == "__main__":
    main()