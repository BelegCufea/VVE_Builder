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
BASE_URL = "http://10.0.50.5:17600" # VoiceBox API - http://localhost:17493 for local server, or remote URL for remote server
ENGINE = "qwen"
MODEL_SIZE = "0.6B"

# Audio Conversion Configuration
CONVERT_TO_OGG = True               # convert WAV to Ogg Vorbis after download
OGG_QUALITY = 4                     # libvorbis quality

# File Paths
CSV_PATH = r"dialog-report.csv"
PATCHER_CONFIG_PATH = r"patcher-config.json"
OUTPUT_DIR = r"output"

# Generation Limits and filters
LIMIT = 0                           # set to 0 to process all
# Process only these voices
TARGET_VOICES = [                   
    # "Jaheira",
    # "Edwin",
    "Nym Khalazza"
]   
# NPC name -> Voicebox profile substitution.
# If an NPC is not listed here, its name is used as the voice profile name.
VOICE_SUBSTITUTIONS = {
    # "Drizzt Do'Urden": "Drizzt",
    "Nym Khalazza": "BG1 Narrator",
}
FILENAME_PATTERN = r"^TS"          # regex pattern for filename (column 6)

# STRREF Filtering
STRREF_FILTER_FILE = r"strrefs.json"  # JSON file with list of strrefs to process
USE_STRREF_FILTER = True              # If False, falls back to TARGET_VOICES/FILENAME_PATTERN

# Voice Fallback Configuration
USE_VOICE_FALLBACK = True
FALLBACK_VOICE_MALE = "BG1 Narrator"
FALLBACK_VOICE_FEMALE = "BG3 Narrator"
FALLBACK_VOICE_NEUTRAL = "Description Narrator"

# Filename Generation
FORCE_GENERATED_FILENAMES = False    # If True, always use generated; if False, use CSV fallback
RESREF_PREFIX = "TS"                 # 2-character prefix for generated resrefs

# Generation memory
SKIP_ALREADY_GENERATED = True
GENERATION_MEMORY_PATH = r"generation-memory.json"
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
        replacement = rule.get("replacement", "")

        try:
            text = re.sub(
                pattern,
                replacement,
                text,
                flags=re.IGNORECASE | re.MULTILINE
            )
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
def progress_worker(stop_event, job_idx, total_jobs, filename, estimated_sec, npc_name, chars):
    """
    Background thread function for updating the console progress bar.

    Runs in a separate thread to provide real-time progress updates while
    a TTS generation is in progress. The thread updates the console every
    0.5 seconds with the current status.

    The progress calculation estimates completion based on elapsed time
    versus estimated duration, showing a percentage bar and time remaining
    even before the generation completes.

    Args:
        stop_event (threading.Event): Event to signal when the thread
            should terminate (generation completed or failed).
        job_idx (int): Current job number (1-indexed) in the queue.
        total_jobs (int): Total number of jobs to process.
        filename (str): The filename being generated (for display).
        estimated_sec (float): Estimated duration for this job in seconds.
        npc_name (str): The NPC name being processed (for display).
        chars (int): The number of characters in the text (for display).

    Note:
        The thread is daemonized and will exit cleanly when the stop_event
        is set. The function writes directly to stdout and clears the line
        when done to avoid cluttering the console output.
    """
    start_time = time.time()
    voice = get_voice_profile_name(npc_name)

    while not stop_event.is_set():
        elapsed = time.time() - start_time

        if estimated_sec > 0:
            percent = min(100, (elapsed / estimated_sec) * 100)
        else:
            percent = 0

        bar = progress_bar(percent)
        time_str = f"{format_time(elapsed)} / {format_time(estimated_sec)}"

        # Overwrite the current line with updated progress
        sys.stdout.write(
            f"\r[{job_idx}/{total_jobs}] {filename}  {bar}  {time_str}  ({chars} chars)  {npc_name} ({voice})"
        )
        sys.stdout.flush()

        time.sleep(0.5)

    # Clear the line when the generation finishes to prevent visual clutter
    sys.stdout.write('\r' + ' ' * 120 + '\r')
    sys.stdout.flush()


def print_pregeneration_summary(npc_stats, profile_map):
    """
    Print a structured summary of files to generate per NPC/voice.

    Displays a formatted table showing for each NPC:
    - The voice profile that will be used (with validation)
    - Total lines to process
    - Lines skipped (already generated)
    - Lines remaining to generate
    - Total character count

    The summary helps users verify that all configured voices exist before
    starting the potentially long generation process.

    Args:
        npc_stats (dict): Statistics dictionary for each NPC, structured as:
            {
                "NPC Name": {
                    "voice_name": str,
                    "total": int,
                    "skipped": int,
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
        Missing profiles are marked with "❌ Missing" in the table.
    """   
    print("\n" + "=" * 100)
    print("📊 PRE-GENERATION VOICE SUMMARY")
    
    # Show fallback status
    if USE_VOICE_FALLBACK:
        print(f"   🔄 Voice fallback ENABLED: M->{FALLBACK_VOICE_MALE}, F->{FALLBACK_VOICE_FEMALE}, NEUTRAL->{FALLBACK_VOICE_NEUTRAL}")
    else:
        print("   ⛔ Voice fallback DISABLED")
    
    # Show strref filter status
    if USE_STRREF_FILTER:
        try:
            with open(STRREF_FILTER_FILE, "r") as f:
                count = len(json.load(f))
            print(f"   📋 STRREF filter ENABLED: {count} STRREFs from {STRREF_FILTER_FILE}")
        except:
            print(f"   📋 STRREF filter ENABLED (file: {STRREF_FILTER_FILE})")
    else:
        print(f"   📋 STRREF filter DISABLED")
    
    # Show filename generation status
    if FORCE_GENERATED_FILENAMES:
        print(f"   🔧 Filenames: FORCED generated (base36) with prefix: {RESREF_PREFIX}")
    else:
        print(f"   🔧 Filenames: CSV with base36 fallback (prefix: {RESREF_PREFIX})")
    
    print("=" * 100)

    header = f"{'NPC Name':<28} {'Profile':<30} {'Total':>7} {'Skipped':>9} {'To Gen':>8} {'Characters':>12}"
    print(header)
    print("-" * 100)

    grand_total = 0
    grand_skipped = 0
    grand_to_gen = 0
    grand_chars = 0

    valid_total = 0
    valid_skipped = 0
    valid_to_gen = 0
    valid_chars = 0

    for npc_name, stats in npc_stats.items():
        profile_name = get_voice_profile_name(npc_name)
        has_profile = profile_name in profile_map
        profile_str = f"✅ {profile_name}" if has_profile else "❌ Missing"
        total = stats["total"]
        skipped = stats["skipped"]
        to_gen = stats["to_generate"]
        chars = stats["chars"]

        grand_total += total
        grand_skipped += skipped
        grand_to_gen += to_gen
        grand_chars += chars

        if has_profile:
            valid_total += total
            valid_skipped += skipped
            valid_to_gen += to_gen
            valid_chars += chars

        print(
            f"{npc_name:<28} "
            f"{profile_str:<29} "
            f"{total:>7,} "
            f"{skipped:>9,} "
            f"{to_gen:>8,} "
            f"{chars:>12,}"
        )

    print("-" * 100)
    print(
        f"{'VALID TOTAL':<28} "
        f"{'':<30} "
        f"{valid_total:>7,} "
        f"{valid_skipped:>9,} "
        f"{valid_to_gen:>8,} "
        f"{valid_chars:>12,}"
    )
    print(
        f"{'TOTAL':<28} "
        f"{'':<30} "
        f"{grand_total:>7,} "
        f"{grand_skipped:>9,} "
        f"{grand_to_gen:>8,} "
        f"{grand_chars:>12,}"
    )
    print("=" * 100 + "\n")   

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
    
    # Pre-populate targeted NPCs (only if not using STRREF filter)
    if target_voices and not use_strref_filter:
        for npc_name in target_voices:
            npc_stats[npc_name] = {
                "voice_name": get_voice_profile_name(npc_name, profile_map=profile_map),
                "total": 0,
                "skipped": 0,
                "to_generate": 0,
                "chars": 0
            }
    
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

                # Skip rows where no valid voice was found
                if voice_name is None:
                    print(f"⚠️ STRREF {strref}: No valid voice found for '{npc_name or 'Descriptions'}' (gender: {gender or 'unknown'})")
                    continue

                # Also skip if voice_name doesn't exist in profile_map (shouldn't happen with proper fallback)
                if profile_map is not None and voice_name not in profile_map:
                    print(f"⚠️ STRREF {strref}: Voice '{voice_name}' not found on server for '{npc_name or 'Descriptions'}'")
                    continue
                
                # Use npc_name for stats, or "Descriptions" if empty
                display_name = npc_name if npc_name else "Descriptions"
                
                # Initialize NPC stats
                if display_name not in npc_stats:
                    npc_stats[display_name] = {
                        "voice_name": voice_name,
                        "total": 0,
                        "skipped": 0,
                        "to_generate": 0,
                        "chars": 0
                    }
                
                npc_stats[display_name]["total"] += 1
                
                # Skip already generated if enabled
                if skip_generated and is_already_generated(generation_memory, display_name, strref):
                    npc_stats[display_name]["skipped"] += 1
                    continue
                
                npc_stats[display_name]["to_generate"] += 1
                npc_stats[display_name]["chars"] += len(text)
                
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


def process_generation_job(idx, total_jobs, strref, npc_name, voice_name, filename, text, profile_id, regressor, generation_memory):
    """
    Execute a single TTS generation job.

    Handles the complete lifecycle of one generation: submitting the request,
    waiting for completion, downloading the audio, and converting to Ogg Vorbis.

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

    Returns:
        tuple: (success, elapsed_time, audio_duration, chars_processed)
            where success is bool indicating if generation succeeded.
    """
    chars = len(text)
    estimated_sec = estimate_generation_time(regressor, chars)
    stop_event = threading.Event()

    # Start progress bar thread (use npc_name for display)
    worker = threading.Thread(
        target=progress_worker,
        args=(stop_event, idx, total_jobs, filename, estimated_sec, npc_name, chars)
    )
    worker.daemon = True
    worker.start()

    start_time = time.time()
    success = False
    elapsed = 0
    audio_duration = 0

    try:
        gen_id = submit_generation(profile_id, text, ENGINE, MODEL_SIZE)
        final_event = wait_for_completion(gen_id) or {}
        elapsed = time.time() - start_time

        # Stop the progress thread
        stop_event.set()
        worker.join(timeout=0.5)
        sys.stdout.write('\r' + ' ' * 120 + '\r')
        sys.stdout.flush()

        if final_event.get("status") == "completed":
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

        else:
            error = final_event.get("error", "unknown")
            print(f"[{idx}/{total_jobs}] ❌ {filename} failed: {error}")

    except Exception as e:
        stop_event.set()
        worker.join(timeout=0.5)
        sys.stdout.write('\r' + ' ' * 80 + '\r')
        sys.stdout.flush()
        print(f"[{idx}/{total_jobs}] ❌ {filename} error: {e}")

    return success, elapsed, audio_duration, chars


def print_job_summary(idx, total_jobs, filename, chars, elapsed, audio_duration, npc_name, voice_name):
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
    """
    realtime_speed = (audio_duration / elapsed * 100 if elapsed > 0 else 0)
    voice_part = f" (voice: {voice_name})" if voice_name != npc_name else ""
    print(
        f"[{idx}/{total_jobs}] ✅ {filename}  "
        f"({chars} chars)  "
        f"Gen: {elapsed:.2f}s  "
        f"Audio: {audio_duration:.2f}s  "
        f"Realtime speed: {realtime_speed:.2f}%  "
        f"NPC: {npc_name}"
        f"{voice_part}"
    )


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

        print(
            f"Overall: "
            f"{progress_bar(overall_percent)}  "
            f"{total_chars_processed}/{total_chars_all} chars  "
            f"Elapsed: {format_time(elapsed_total)}  "
            f"ETA: {format_time(eta_seconds)}"
            f"@ {format_finish_time(eta_seconds)}"
        )
    else:
        print("Overall: processing...")


def print_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats):
    """
    Print the final summary after all jobs are processed.

    Args:
        total_jobs (int): Total number of jobs processed.
        total_chars_processed (int): Total characters processed.
        avg_time_per_char (float): Average time per character.
        npc_stats (dict): Statistics dictionary per NPC.
    """
    print("\n" + "=" * 70)
    print("📋 FINAL SUMMARY")
    print("=" * 70)

    print(f"Processed {total_jobs} files. Total characters: {total_chars_processed}")

    if avg_time_per_char:
        print(f"Average generation time per character: {avg_time_per_char:.4f}s")

    total_skipped = sum(s["skipped"] for s in npc_stats.values())
    if total_skipped:
        skipped_details = ", ".join(
            f"{voice}: {stats['skipped']}"
            for voice, stats in npc_stats.items()
            if stats["skipped"] > 0
        )
        print(f"Skipped already generated: {total_skipped} ({skipped_details})")

    print("=" * 70)
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
        profile_map,  # <-- Pass profile_map here
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
            print(f"⏭️ Skipping {filename}: Voice '{voice_name}' not found.")
            continue
        
        # Process the generation job
        success, elapsed, audio_duration, chars = process_generation_job(
            idx, total_jobs, strref, display_name, voice_name, filename, text,
            profile_id, regressor, generation_memory
        )
        
        if success:
            # Update statistics
            regressor.push(chars, elapsed)
            total_chars_processed += chars
            avg_time_per_char = (time.time() - total_start_time) / total_chars_processed
            overall_regressor.push(chars, elapsed)
            
            # Print job summary
            print_job_summary(idx, total_jobs, filename, chars, elapsed, audio_duration, display_name, voice_name)
        
        # Print overall progress
        elapsed_total = time.time() - total_start_time
        print_overall_progress(
            total_chars_processed, total_chars_all, total_jobs, idx,
            overall_regressor, avg_time_per_char, elapsed_total
        )

    # 8. Final summary
    print_final_summary(total_jobs, total_chars_processed, avg_time_per_char, npc_stats)

if __name__ == "__main__":
    main()