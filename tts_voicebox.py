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
    """Build the base URL for the /transcribe endpoint."""
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.TRANSCRIBE_ENDPOINT.strip('/')}"


def _build_generate_url() -> str:
    """Build the base URL for the /generate endpoint."""
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.GENERATE_ENDPOINT.strip('/')}"


def _build_audio_url(gen_id: str) -> str:
    """Build the URL for downloading generated audio."""
    endpoint = cfg.AUDIO_ENDPOINT.strip('/').format(gen_id=gen_id)
    return f"{cfg.BASE_URL.rstrip('/')}/{endpoint}"


def _build_status_url(gen_id: str) -> str:
    """Build the URL for checking generation status."""
    endpoint = cfg.GENERATE_STATUS_ENDPOINT.strip('/').format(gen_id=gen_id)
    return f"{cfg.BASE_URL.rstrip('/')}/{endpoint}"


def _build_cancel_url(gen_id: str) -> str:
    """Build the URL for cancelling a generation."""
    endpoint = cfg.GENERATE_CANCEL_ENDPOINT.strip('/').format(gen_id=gen_id)
    return f"{cfg.BASE_URL.rstrip('/')}/{endpoint}"



def _build_profile_url(profile_id: str) -> str:
    """Build the URL for a specific voice profile."""
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.PROFILES_ENDPOINT.strip('/')}/{profile_id}"


def _build_delete_url(profile_id: str) -> str:
    """Build the URL for deleting a voice profile."""
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.PROFILES_ENDPOINT.strip('/')}/{profile_id}"


def _build_profiles_url() -> str:
    """Build the URL for listing all profiles."""
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.PROFILES_ENDPOINT.strip('/')}"


def _build_import_url() -> str:
    """Build the URL for importing a profile."""
    return f"{cfg.BASE_URL.rstrip('/')}/{cfg.PROFILES_IMPORT_ENDPOINT.strip('/')}"



def transcribe_wav(
    wav_path: Union[str, Path],
    timeout: Optional[float] = None,
    retry_count: Optional[int] = None,
    retry_delay: Optional[float] = None,
) -> Tuple[str, bool, float]:
    """
    Transcribe a WAV file using the VoiceBox /transcribe endpoint.

    Args:
        wav_path: Path to the .wav file to transcribe.
        timeout: Per-attempt request timeout in seconds (defaults to cfg.SAMPLE_TIMEOUT_SECONDS).
        retry_count: Number of retries after the first attempt (defaults to cfg.SAMPLE_RETRY_COUNT).
        retry_delay: Seconds to wait between retries (defaults to cfg.SAMPLE_RETRY_DELAY).

    Returns:
        A tuple of (transcribed_text, success, duration).
        On failure, text is "<ERROR: ...>" and success is False.

    Example:
        >>> text, success, duration = transcribe_wav("npc_voice.wav")
        >>> print(text[:100])
    """
    url = _build_transcribe_url()
    timeout = timeout if timeout is not None else cfg.SAMPLE_TIMEOUT_SECONDS
    retry_count_val = retry_count if retry_count is not None else cfg.SAMPLE_RETRY_COUNT
    retry_delay = retry_delay if retry_delay is not None else cfg.SAMPLE_RETRY_DELAY

    wav_path = Path(wav_path)
    last_error = ""

    for attempt in range(retry_count_val + 1):
        try:
            with open(wav_path, "rb") as f:
                resp = requests.post(
                    url,
                    files={"file": (wav_path.name, f, "audio/wav")},
                    data={"language": cfg.TRANSCRIPTION_LANGUAGE},
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
    engine: str | None = None,
    model_size: str | None = None,
    language: str | None = None,
) -> str:
    """
    Submit text-to-speech generation to the VoiceBox /generate endpoint.

    Args:
        text: The text to generate speech for.
        profile_id: The ID of the voice profile to use.
        engine: The TTS engine to use (default: cfg.ENGINE).
        model_size: The model size to use (default: cfg.MODEL_SIZE).
        language: The language for generation (default: cfg.LANGUAGE).

    Returns:
        The generation ID returned by the VoiceBox server.

    Raises:
        requests.exceptions.RequestException: If the API request fails.
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

    Args:
        gen_id: The generation ID to check.

    Returns:
        A dictionary containing the generation status and details.

    Raises:
        requests.exceptions.RequestException: If the API request fails.
    """
    url = _build_status_url(gen_id)
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json()


def cancel_generation(gen_id: str) -> Tuple[bool, str]:
    """
    Cancel a pending or running generation.

    Args:
        gen_id: The generation ID to cancel.

    Returns:
        A tuple of (success, message) indicating whether the cancellation succeeded.
    """
    url = _build_cancel_url(gen_id)
    resp = requests.post(url)
    resp.raise_for_status()
    return True, "Cancellation successful"


def download_generated_audio(gen_id: str, output_path: str) -> None:
    """
    Download the audio file for a completed generation.

    Args:
        gen_id: The generation ID whose audio to download.
        output_path: The local path where the audio should be saved.

    Raises:
        requests.exceptions.RequestException: If the download fails.
        OSError: If the output file cannot be written.
    """
    url = _build_audio_url(gen_id)
    resp = requests.get(url)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(resp.content)


def delete_voice_profile(profile_id: str) -> Tuple[bool, str]:
    """
    Delete a voice profile from the Voicebox service.

    Args:
        profile_id: The ID of the profile to delete.

    Returns:
        A tuple of (success, message) indicating whether the deletion succeeded.
    """
    url = _build_delete_url(profile_id)
    resp = requests.delete(url)
    resp.raise_for_status()
    return True, "Profile deleted successfully"


def list_profiles() -> List[Dict[str, Any]]:
    """
    List all available voice profiles.

    Returns:
        A list of dictionaries, each representing a profile with its id and name.
    """
    url = _build_profiles_url()
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json()


def import_profile(zip_path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    """
    Import a composed .voicebox.zip package into Voicebox.

    Args:
        zip_path: Path to the .voicebox.zip file.

    Returns:
        Parsed JSON response on success, None on failure.
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

    Args:
        base_url: The base URL to check (defaults to cfg.BASE_URL).

    Returns:
        A tuple of (success, payload). On success, payload contains the JSON response.
        On failure, payload contains an "error" key with the error message.
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
    """Wait for a generation job to complete by streaming Server-Sent Events.

    Connects to the Voicebox API's status endpoint and listens for SSE events
    until the generation reaches either "completed" or "failed" status.

    Args:
        gen_id: The generation ID returned by submit_generation().

    Returns:
        The final event data containing at least "status" and
        potentially "duration" (for completed jobs) or "error"
        (for failed jobs), or None if no final event was received.

    Raises:
        requests.exceptions.RequestException: If the SSE connection fails.
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

