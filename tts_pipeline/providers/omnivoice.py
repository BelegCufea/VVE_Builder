"""
OmniVoice-server provider.

Targets maemreyo/omnivoice-server (an OpenAI-compatible HTTP wrapper around
k2-fsa/OmniVoice): POST /v1/audio/speech for generation, /v1/voices/profiles
for profile CRUD. Confirmed against the project's published API reference.

Key differences from Voicebox that shaped this file (see planning notes):

1. Generation is a single blocking request -- POST /v1/audio/speech returns
   the finished audio directly. There's no gen_id, no polling, no mid-flight
   cancel. generate() therefore does the whole job itself and returns a
   GenerationJob with done=True already; wait() is a no-op; cancel() is
   unsupported (see capabilities).

2. A cloned profile is invoked by passing voice="clone:<profile_id>" to
   /v1/audio/speech, per GET /v1/voices' documented id format.

3. Profile creation takes exactly ONE ref_audio + ref_text pair, unlike
   Voicebox which composes a profile from many numbered samples. When
   VOICES_DIR has multiple samples for a voice (e.g. "Jaheira 1.wav",
   "Jaheira 2.wav"), ensure_profile() picks the longest one by actual audio
   duration (read via the stdlib wave module, cached by path+size+mtime so
   repeat syncs don't re-open every file), falling back to transcript
   length if a file can't be read as a plain PCM wav.

4. Since audio comes back as the direct response body, fetch_audio() just
   writes out bytes already held on the job -- no second network call.
"""

from __future__ import annotations

import json
import logging
import wave
from pathlib import Path
from typing import Optional

import requests

from .base import (
    GenerationJob,
    ProfileInfo,
    ProfileSource,
    ProviderCapabilities,
    TtsProvider,
)
from .util import CaseInsensitiveDict

logger = logging.getLogger("generate_gui")

DURATION_CACHE_FILENAME = ".omnivoice_duration_cache.json"


class _DurationCache:
    """On-disk cache of WAV durations, keyed by path+size+mtime so a
    changed/replaced sample file is picked up automatically without
    needing to be invalidated by hand."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._data: dict = {}
        if cache_path.exists():
            try:
                self._data = json.loads(cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}
        self._dirty = False

    def get_duration(self, wav_path: Path) -> Optional[float]:
        try:
            stat = wav_path.stat()
        except OSError:
            return None
        key = str(wav_path)
        fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"

        cached = self._data.get(key)
        if cached and cached.get("fingerprint") == fingerprint:
            return cached.get("duration")

        duration = _read_wav_duration(wav_path)
        if duration is not None:
            self._data[key] = {"fingerprint": fingerprint, "duration": duration}
            self._dirty = True
        return duration

    def save(self):
        if not self._dirty:
            return
        try:
            self.cache_path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            self._dirty = False
        except OSError as e:
            logger.warning(f"⚠️ Could not write duration cache to {self.cache_path}: {e}")


def _read_wav_duration(wav_path: Path) -> Optional[float]:
    """Reads duration via the stdlib wave module. Returns None (rather
    than raising) for anything wave can't parse, e.g. a WAV with an
    unsupported compression format -- callers fall back to transcript
    length in that case."""
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return None
            return frames / float(rate)
    except (wave.Error, OSError, EOFError):
        return None


def _pick_reference_sample(files: list, voices_dir: str) -> dict:
    """Chooses the sample to use as the single reference OmniVoice-server
    needs: longest by actual audio duration where readable, otherwise
    longest transcript as a fallback proxy."""
    cache = _DurationCache(Path(voices_dir) / DURATION_CACHE_FILENAME)

    scored = []
    any_duration_read = False
    for file_info in files:
        duration = cache.get_duration(file_info["wav_path"])
        if duration is not None:
            any_duration_read = True
        scored.append((file_info, duration))
    cache.save()

    if any_duration_read:
        # Files whose duration couldn't be read sort last, not first --
        # avoids a corrupt/odd sample "winning" by default.
        best = max(scored, key=lambda pair: pair[1] if pair[1] is not None else -1)
        return best[0]

    # Duration unreadable for every sample (e.g. non-PCM wavs) -- fall
    # back to transcript length, which is already in memory.
    return max(files, key=lambda f: len(f.get("transcript", "")))


class OmniVoiceServerProvider(TtsProvider):
    def __init__(
        self,
        base_url: str,
        voices_dir: str,
        api_key: str = "",
        response_format: str = "wav",
        extra_speech_params: Optional[dict] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.voices_dir = voices_dir
        self.api_key = api_key
        self.response_format = response_format
        # passthrough for num_step/guidance_scale/etc. if you ever want to
        # tune quality vs. speed later -- kept generic on purpose
        self.extra_speech_params = extra_speech_params or {}

    def _headers(self) -> dict:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_cancel=False,
            supports_mid_job_progress=False,
            supports_native_profiles=True,
            is_async=False,
        )

    # -- profiles ----------------------------------------------------

    def list_profiles(self) -> tuple[dict, dict]:
        """GET /v1/voices, filtered down to type == "clone" entries.
        There's no zero-sample state here the way Voicebox has -- a
        profile either exists (with its one reference) or it doesn't --
        so the second map is always empty. Kept in the return shape only
        so callers written against the interface don't need a special case."""
        resp = requests.get(f"{self.base_url}/v1/voices", headers=self._headers())
        resp.raise_for_status()
        data = resp.json()

        profile_map = CaseInsensitiveDict()
        for voice in data.get("voices", []):
            if voice.get("type") == "clone" and voice.get("profile_id"):
                profile_map[voice["profile_id"]] = voice["profile_id"]

        return profile_map, CaseInsensitiveDict()

    def ensure_profile(self, source: ProfileSource) -> Optional[ProfileInfo]:
        """POST /v1/voices/profiles with a single reference sample.

        Picks the longest sample from source.files as the reference -- see
        module docstring point 3. overwrite=true so this doubles as
        "create or update," matching Voicebox's ensure_profile semantics
        (safe to call whether or not the profile already exists).
        """
        if not source.files:
            logger.warning(f"⚠️ No local samples for {source.voice_name}, cannot provision on OmniVoice-server.")
            return None

        chosen = _pick_reference_sample(source.files, self.voices_dir)
        profile_id = _sanitize_profile_id(source.voice_name)

        try:
            with open(chosen["wav_path"], "rb") as f:
                resp = requests.post(
                    f"{self.base_url}/v1/voices/profiles",
                    headers=self._headers(),
                    data={
                        "profile_id": profile_id,
                        "ref_text": chosen["transcript"],
                        "overwrite": "true",
                    },
                    files={"ref_audio": f},
                )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Error creating OmniVoice profile for {source.voice_name}: {e}")
            return None

        if len(source.files) > 1:
            logger.info(
                f"ℹ️ {source.voice_name} has {len(source.files)} local samples; "
                f"OmniVoice-server profiles use a single reference, so the "
                f"longest one (#{chosen['number']}) was used."
            )

        return ProfileInfo(name=source.voice_name, provider_ref=profile_id, has_samples=True)

    def delete_profile(self, provider_ref: str) -> tuple[bool, str]:
        try:
            resp = requests.delete(f"{self.base_url}/v1/voices/profiles/{provider_ref}", headers=self._headers())
            if resp.status_code == 200:
                return True, "Profile deleted successfully"
            return False, f"Deletion returned: {resp.status_code}"
        except Exception as e:
            return False, f"Deletion error: {e}"

    # -- generation ----------------------------------------------------

    def generate(self, profile_ref: str, text: str) -> GenerationJob:
        """POST /v1/audio/speech. This is the whole job -- audio comes
        back in the response body, so the returned GenerationJob is
        already done=True. wait() has nothing left to do."""
        payload = {
            "model": "omnivoice",
            "input": text,
            "voice": f"clone:{profile_ref}",
            "response_format": self.response_format,
            **self.extra_speech_params,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/v1/audio/speech",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            job = GenerationJob(provider_ref=profile_ref, done=True, succeeded=False)
            job.error = str(e)
            return job

        job = GenerationJob(provider_ref=profile_ref, done=True, succeeded=True)
        job._audio_bytes = resp.content
        # OmniVoice-server doesn't report a generated-audio duration in the
        # response headers/body, unlike Voicebox's SSE "duration" field.
        job.audio_duration = 0.0
        return job

    def wait(self, job: GenerationJob) -> GenerationJob:
        """No-op: generate() already ran the request to completion."""
        return job

    def fetch_audio(self, job: GenerationJob, output_path: str) -> None:
        """Writes out the bytes already captured in generate() -- no
        second network round-trip needed, unlike Voicebox's separate
        download step."""
        audio_bytes = job._audio_bytes
        if audio_bytes is None:
            raise RuntimeError("No audio available on this job -- generate() may have failed.")
        with open(output_path, "wb") as f:
            f.write(audio_bytes)


def _sanitize_profile_id(voice_name: str) -> str:
    """omnivoice-server profile_id must be alphanumeric/dash/underscore
    per its API reference -- voice names from VOICES_DIR can have spaces
    or other characters, so normalize them the same way
    create_profile_package() already does for Voicebox's zip filenames."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in voice_name.strip().lower())
