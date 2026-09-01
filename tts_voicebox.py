"""
VoiceBox TTS Library

A lightweight wrapper around the VoiceBox API for transcription,
text-to-speech generation, and profile management.

This library provides high-level methods for interacting with the
VoiceBox service, building on top of the existing appconfig system.

Library Design: Silent and Clean
- No internal logging - host script controls all logging
- Pure data layer focused on API calls
- Structured error handling for caller convenience
- Type-safe with full documentation
"""

import json
import time
from typing import Optional, Dict, Any, List, Tuple, Union
from pathlib import Path

import requests

from appconfig import cfg


def _build_transcribe_url() -> str:
    """
    Build the base URL for the /transcribe endpoint.

    Returns:
        The fully constructed URL for the transcription endpoint.
    """
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.TRANSCRIBE_ENDPOINT.strip('/')}"


def _build_generate_url() -> str:
    """
    Build the base URL for the /generate endpoint.

    Returns:
        The fully constructed URL for the generation endpoint.
    """
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.GENERATE_ENDPOINT.strip('/')}"


def _build_audio_url(gen_id: str) -> str:
    """
    Build the URL for downloading generated audio.

    Args:
        gen_id: The generation ID to construct the URL for.

    Returns:
        The fully constructed URL for the audio download endpoint.
    """
    endpoint = cfg.AUDIO_ENDPOINT.strip('/').format(gen_id=gen_id)
    return f"{cfg.BASE_URL.rstrip('/')}/{endpoint}"


def _build_status_url(gen_id: str) -> str:
    """
    Build the URL for checking generation status.

    Args:
        gen_id: The generation ID to construct the URL for.

    Returns:
        The fully constructed URL for the status endpoint.
    """
    endpoint = cfg.GENERATE_STATUS_ENDPOINT.strip('/').format(gen_id=gen_id)
    return f"{cfg.BASE_URL.rstrip('/')}/{endpoint}"


def _build_cancel_url(gen_id: str) -> str:
    """
    Build the URL for cancelling a generation.

    Args:
        gen_id: The generation ID to construct the URL for.

    Returns:
        The fully constructed URL for the cancellation endpoint.
    """
    endpoint = cfg.GENERATE_CANCEL_ENDPOINT.strip('/').format(gen_id=gen_id)
    return f"{cfg.BASE_URL.rstrip('/')}/{endpoint}"


def _build_profile_url(profile_id: str) -> str:
    """
    Build the URL for a specific voice profile.

    Args:
        profile_id: The profile ID to construct the URL for.

    Returns:
        The fully constructed URL for the profile endpoint.
    """
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.PROFILES_ENDPOINT.strip('/')}/{profile_id}"


def _build_delete_url(profile_id: str) -> str:
    """
    Build the URL for deleting a voice profile.

    Args:
        profile_id: The profile ID to construct the URL for.

    Returns:
        The fully constructed URL for the profile deletion endpoint.
    """
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.PROFILES_ENDPOINT.strip('/')}/{profile_id}"


def _build_profiles_url() -> str:
    """
    Build the URL for listing all profiles.

    Returns:
        The fully constructed URL for the profiles listing endpoint.
    """
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.PROFILES_ENDPOINT.strip('/')}"


def _build_import_url() -> str:
    """
    Build the URL for importing a profile.

    Returns:
        The fully constructed URL for the profile import endpoint.
    """
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.PROFILES_IMPORT_ENDPOINT.strip('/')}"


def transcribe_wav(
    wav_path: Union[str, Path],
    timeout: Optional[float] = None,
    retry_count: Optional[int] = None,
    retry_delay: Optional[float] = None,
    language: Optional[str] = None,
) -> Tuple[str, bool, float]:
    """
    Transcribe a WAV file using the VoiceBox /transcribe endpoint.

    Sends the audio file to the VoiceBox API for transcription using the
    configured transcription language. Includes automatic retry logic with
    exponential backoff for transient failures.

    Args:
        wav_path: Path to the .wav file to transcribe.
        timeout: Per-attempt request timeout in seconds.
            Defaults to cfg.SAMPLE_TIMEOUT_SECONDS.
        retry_count: Number of retries after the first attempt.
            Defaults to cfg.SAMPLE_RETRY_COUNT.
        retry_delay: Seconds to wait between retries.
            Defaults to cfg.SAMPLE_RETRY_DELAY.
        language: Language to transcribe to (english, german etc.).
            Defaults to cfg.TRANSCRIPTION_LANGUAGE.

    Returns:
        A tuple of (transcribed_text, success, duration).
        On success, transcribed_text contains the transcription result.
        On failure, transcribed_text is "<ERROR: ...>" and success is False.
        duration is the audio duration in seconds (0.0 on failure).

    Example:
        >>> text, success, duration = transcribe_wav("npc_voice.wav")
        >>> if success:
        ...     print(f"Transcribed: {text[:100]}...")
    """
    url = _build_transcribe_url()
    timeout = timeout if timeout is not None else cfg.SAMPLE_TIMEOUT_SECONDS
    retry_count_val = retry_count if retry_count is not None else cfg.SAMPLE_RETRY_COUNT
    retry_delay = retry_delay if retry_delay is not None else cfg.SAMPLE_RETRY_DELAY
    language = language if language is not None else cfg.TRANSCRIPTION_LANGUAGE

    wav_path = Path(wav_path)
    last_error = ""

    for attempt in range(retry_count_val + 1):
        try:
            with open(wav_path, "rb") as f:
                resp = requests.post(
                    url,
                    files={"file": (wav_path.name, f, "audio/wav")},
                    data={"language": language},
                    timeout=timeout,
                )
            resp.raise_for_status()
            data = resp.json()
            return data.get("text", ""), True, data.get("duration", 0.0)

        except Exception as ex:
            last_error = str(ex)
            if attempt < retry_count_val:
                time.sleep(retry_delay or 0.0)

    return f"<ERROR: {last_error}>", False, 0.0


def submit_generation(
    text: str,
    profile_id: str,
    engine: Optional[str] = None,
    model_size: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    """
    Submit text-to-speech generation to the VoiceBox /generate endpoint.

    Sends a generation request to the VoiceBox API with the specified text
    and voice profile. The generation runs asynchronously on the server;
    the returned generation ID can be used with wait_for_completion() to
    monitor progress.

    Args:
        text: The text to generate speech for.
        profile_id: The ID of the voice profile to use.
        engine: The TTS engine to use. Defaults to cfg.ENGINE.
        model_size: The model size to use. Defaults to cfg.MODEL_SIZE.
        language: The language for generation. Defaults to cfg.LANGUAGE
            (converted to a two-letter ISO code).

    Returns:
        The generation ID assigned by the VoiceBox server.

    Raises:
        requests.exceptions.RequestException: If the API request fails.

    Example:
        >>> gen_id = submit_generation("Hello world", profile_id="123")
        >>> print(f"Generation started: {gen_id}")
    """
    if engine is None:
        engine = cfg.ENGINE
    if model_size is None:
        model_size = cfg.MODEL_SIZE
    if language is None:
        language = cfg.LANGUAGE.replace("-", "_").split("_")[0]

    url = _build_generate_url()
    payload = {
        "text": text,
        "profile_id": profile_id,
        "engine": engine,
        "model_size": model_size,
        "language": language,
    }

    resp = requests.post(url, json=payload)
    resp.raise_for_status()
    data = resp.json()
    return data.get("id", "")


def get_generation_status(gen_id: str) -> Dict[str, Any]:
    """
    Get the status of a generation job.

    Queries the VoiceBox API for the current status of a generation job.
    The returned dictionary includes the status ("pending", "running",
    "completed", or "failed") and additional metadata.

    Args:
        gen_id: The generation ID to check.

    Returns:
        A dictionary containing the generation status and details.
        Common keys include:
            - status: "pending", "running", "completed", or "failed"
            - duration: Audio duration in seconds (if completed)
            - error: Error message (if failed)

    Raises:
        requests.exceptions.RequestException: If the API request fails.

    Example:
        >>> status = get_generation_status(gen_id)
        >>> if status["status"] == "completed":
        ...     print(f"Duration: {status['duration']}s")
    """
    url = _build_status_url(gen_id)
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json()


def cancel_generation(gen_id: str) -> Tuple[bool, str]:
    """
    Cancel a pending or running generation.

    Sends a cancellation request to the VoiceBox API for the specified
    generation. The generation will be stopped as soon as possible.

    Args:
        gen_id: The generation ID to cancel.

    Returns:
        A tuple of (success, message) indicating whether the cancellation
        succeeded. On success, message is "Cancellation successful".
        On failure, an exception is raised.

    Raises:
        requests.exceptions.RequestException: If the API request fails.

    Example:
        >>> success, msg = cancel_generation(gen_id)
        >>> if success:
        ...     print("Generation cancelled")
    """
    url = _build_cancel_url(gen_id)
    resp = requests.post(url)
    resp.raise_for_status()
    return True, "Cancellation successful"


def download_generated_audio(gen_id: str, output_path: str) -> None:
    """
    Download the audio file for a completed generation.

    Retrieves the generated audio from the VoiceBox API and saves it to
    the specified local path. The audio is downloaded as raw WAV data.

    Args:
        gen_id: The generation ID whose audio to download.
        output_path: The local path where the audio should be saved.

    Raises:
        requests.exceptions.RequestException: If the download fails.
        OSError: If the output file cannot be written.

    Example:
        >>> download_generated_audio(gen_id, "output.wav")
        >>> print("Audio downloaded successfully")
    """
    url = _build_audio_url(gen_id)
    resp = requests.get(url)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(resp.content)


def delete_voice_profile(profile_id: str) -> Tuple[bool, str]:
    """
    Delete a voice profile from the Voicebox service.

    Removes the specified voice profile and all its associated samples
    from the VoiceBox server. This operation cannot be undone.

    Args:
        profile_id: The ID of the profile to delete.

    Returns:
        A tuple of (success, message) indicating whether the deletion
        succeeded. On success, message is "Profile deleted successfully".
        On failure, an exception is raised.

    Raises:
        requests.exceptions.RequestException: If the API request fails.

    Example:
        >>> success, msg = delete_voice_profile("profile_123")
        >>> if success:
        ...     print("Profile deleted")
    """
    url = _build_delete_url(profile_id)
    resp = requests.delete(url)
    resp.raise_for_status()
    return True, "Profile deleted successfully"


def list_profiles() -> List[Dict[str, Any]]:
    """
    List all available voice profiles.

    Retrieves a list of all voice profiles from the VoiceBox server.
    Each profile entry includes the profile ID, name, sample count,
    and other metadata.

    Returns:
        A list of dictionaries, each representing a profile with keys
        including "id", "name", "sample_count", and others.

    Raises:
        requests.exceptions.RequestException: If the API request fails.

    Example:
        >>> profiles = list_profiles()
        >>> for p in profiles:
        ...     print(f"{p['name']}: {p['id']} ({p['sample_count']} samples)")
    """
    url = _build_profiles_url()
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json()


def import_profile(zip_path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    """
    Import a composed .voicebox.zip package into Voicebox.

    Uploads a voice profile package (created by create_profile_package())
    to the VoiceBox server. The package must contain a manifest.json,
    samples.json, and the sample audio files.

    Args:
        zip_path: Path to the .voicebox.zip file.

    Returns:
        Parsed JSON response on success (containing the new profile ID
        and name), None on failure.

    Example:
        >>> result = import_profile("profile-boy.voicebox.zip")
        >>> if result:
        ...     print(f"Imported profile: {result['name']} (ID: {result['id']})")
    """
    url = _build_import_url()
    try:
        with open(zip_path, "rb") as f:
            files = {"file": (Path(zip_path).name, f, "application/zip")}
            resp = requests.post(url, files=files)
            resp.raise_for_status()
            return resp.json()
    except requests.exceptions.RequestException:
        return None


def check_health(base_url: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
    """
    Check the health of the VoiceBox API server.

    Sends a GET request to the /health endpoint of the VoiceBox server
    to verify it's running and responsive.

    Args:
        base_url: The base URL to check. Defaults to cfg.BASE_URL.

    Returns:
        A tuple of (success, payload).
        On success, payload contains the JSON health response.
        On failure, payload contains an "error" key with the error message.

    Example:
        >>> success, info = check_health()
        >>> if success:
        ...     print("VoiceBox is healthy:", info)
        ... else:
        ...     print("VoiceBox unreachable:", info["error"])
    """
    url = (base_url or cfg.BASE_URL).rstrip("/") + "/health"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return True, payload
    except Exception as exc:
        return False, {"error": str(exc)}


def wait_for_completion(gen_id: str) -> Optional[Dict[str, Any]]:
    """
    Wait for a generation job to complete by streaming Server-Sent Events.

    Connects to the Voicebox API's status endpoint and listens for SSE events
    until the generation reaches either "completed" or "failed" status.
    This is a blocking call that will return only when the generation
    finishes or the connection is interrupted.

    Args:
        gen_id: The generation ID returned by submit_generation().

    Returns:
        The final event data containing at least:
            - status: "completed" or "failed"
            - duration: Audio duration in seconds (if completed)
            - error: Error message (if failed)
        Returns None if no final event was received (e.g., connection closed).

    Raises:
        requests.exceptions.RequestException: If the SSE connection fails.

    Example:
        >>> result = wait_for_completion(gen_id)
        >>> if result and result.get("status") == "completed":
        ...     print(f"Generation completed in {result['duration']}s")
        ... else:
        ...     print(f"Generation failed: {result.get('error', 'Unknown error')}")
    """
    url = _build_status_url(gen_id)
    headers = {"Accept": "text/event-stream"}
    final_event = None

    with requests.get(url, headers=headers, stream=True) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            if not line or not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event.get("status") in ("completed", "failed"):
                final_event = event
                break

    return final_event