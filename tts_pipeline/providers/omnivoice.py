"""
OmniVoice direct provider.

Runs k2-fsa/omnivoice's Python model in-process (torch/CUDA loaded inside
this app), instead of going through maemreyo/omnivoice-server. That HTTP
wrapper project last committed 3 months ago while omnivoice itself ships
every few days -- staying on it meant being permanently stuck behind
whatever omnivoice version the wrapper happened to pin, with real
abandonment risk. Talking to omnivoice directly means `pip install -U
omnivoice` alone gets you the latest model; there's no second project's
maintenance to depend on. See TtsProvider's docstring for why this
swap only touched this one file.

Key differences from both Voicebox and the old server-based approach:

1. There is no server and no HTTP call at all -- generate() runs
   inference directly in this process. Still a single blocking call
   from the caller's perspective (same as the server was), so
   capabilities are unchanged: no cancel, no mid-job progress.

2. "Profile" = a saved VoiceClonePrompt (omnivoice's own mechanism for
   "encode a reference once, reuse across sessions" -- see the
   Python API docs). ensure_profile() encodes the chosen reference
   sample once and writes a `<profile_id>.pt` file to profiles_dir;
   generate() loads that instead of touching the raw wav again. This
   is a better fit for "profile" than the server's per-call ref_audio
   upload was, and it's entirely local -- no server-side state to
   drift out of sync with.

3. The model itself is loaded lazily, once, on first use (not in
   __init__) -- constructing this provider is cheap; the first
   generate()/ensure_profile() call is the one that pays GPU/model
   load time.

4. Same single-reference-sample constraint as before: when VOICES_DIR
   has multiple samples for a voice, ensure_profile() still picks the
   longest one by actual audio duration (unchanged logic, moved over
   verbatim from the server-based provider).
"""

from __future__ import annotations

import json
import logging
import wave
from pathlib import Path
from typing import Optional

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
PROMPT_FILE_SUFFIX = ".pt"


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

    def get_duration(self, wav_path) -> Optional[float]:
        wav_path = Path(wav_path)
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
    """Chooses the sample to use as the single reference omnivoice's
    voice-cloning API needs: longest by actual audio duration where
    readable, otherwise longest transcript as a fallback proxy."""
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


def _sanitize_profile_id(voice_name: str) -> str:
    """Voice names from VOICES_DIR can have spaces/other characters --
    normalize to something safe as a filename stem."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in voice_name.strip().lower())


class OmniVoiceProvider(TtsProvider):
    def __init__(
        self,
        voices_dir: str,
        profiles_dir: str = "omnivoice-profiles",
        model_name: str = "k2-fsa/OmniVoice",
        device_map: str = "cuda:0",
        dtype: str = "float16",
        extra_generate_params: Optional[dict] = None,
    ):
        self.voices_dir = voices_dir
        self.profiles_dir = Path(profiles_dir)
        self.model_name = model_name
        self.device_map = device_map
        self.dtype = dtype
        # passthrough for num_step/speed/duration/etc. if you ever want to
        # tune quality vs. speed later -- kept generic on purpose
        self.extra_generate_params = extra_generate_params or {}

        self._model = None
        # Loaded VoiceClonePrompt objects, keyed by profile_ref (the .pt
        # path) -- avoids re-loading the same prompt from disk for every
        # line of dialogue a voice has in a batch run.
        self._prompt_cache: dict = {}

    def _ensure_model_loaded(self):
        """Lazy singleton load -- constructing this provider is cheap;
        the first real call pays GPU/model load time, not __init__."""
        if self._model is not None:
            return
        import torch
        from omnivoice import OmniVoice

        logger.info(f"Loading OmniVoice model '{self.model_name}' onto {self.device_map} ({self.dtype})...")
        torch_dtype = getattr(torch, self.dtype)
        self._model = OmniVoice.from_pretrained(
            self.model_name, device_map=self.device_map, dtype=torch_dtype,
        )
        logger.info("✓ OmniVoice model loaded.")

    def _profile_path(self, profile_id: str) -> Path:
        return self.profiles_dir / f"{profile_id}{PROMPT_FILE_SUFFIX}"

    def _load_prompt(self, provider_ref: str):
        cached = self._prompt_cache.get(provider_ref)
        if cached is not None:
            return cached
        from omnivoice import VoiceClonePrompt
        prompt = VoiceClonePrompt.load(provider_ref)
        self._prompt_cache[provider_ref] = prompt
        return prompt

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
        """Scans profiles_dir for `<name>.pt` files. There's no
        zero-sample state the way Voicebox has -- a profile either
        exists on disk or it doesn't -- so the second map is always
        empty, kept only so callers don't need a special case."""
        profile_map = CaseInsensitiveDict()
        if self.profiles_dir.exists():
            for pt_file in self.profiles_dir.glob(f"*{PROMPT_FILE_SUFFIX}"):
                profile_map[pt_file.stem] = str(pt_file)
        return profile_map, CaseInsensitiveDict()

    def ensure_profile(self, source: ProfileSource) -> Optional[ProfileInfo]:
        """Encodes the longest local sample into a VoiceClonePrompt and
        saves it to profiles_dir. Safe to call whether or not the
        profile already exists (overwrites), matching Voicebox's
        ensure_profile semantics."""
        if not source.files:
            logger.warning(f"⚠️ No local samples for {source.voice_name}, cannot provision an OmniVoice profile.")
            return None

        self._ensure_model_loaded()
        chosen = _pick_reference_sample(source.files, self.voices_dir)
        profile_id = _sanitize_profile_id(source.voice_name)
        profile_path = self._profile_path(profile_id)

        try:
            self.profiles_dir.mkdir(parents=True, exist_ok=True)
            prompt = self._model.create_voice_clone_prompt(
                ref_audio=str(chosen["wav_path"]), ref_text=chosen["transcript"],
            )
            prompt.save(str(profile_path))
        except Exception as e:
            logger.error(f"❌ Error creating OmniVoice profile for {source.voice_name}: {e}")
            return None

        self._prompt_cache.pop(str(profile_path), None)  # drop any stale cached copy

        if len(source.files) > 1:
            logger.info(
                f"ℹ️ {source.voice_name} has {len(source.files)} local samples; "
                f"OmniVoice profiles use a single reference, so the "
                f"longest one (#{chosen['number']}) was used."
            )

        return ProfileInfo(name=source.voice_name, provider_ref=str(profile_path), has_samples=True)

    def delete_profile(self, provider_ref: str) -> tuple[bool, str]:
        try:
            Path(provider_ref).unlink(missing_ok=True)
            self._prompt_cache.pop(provider_ref, None)
            return True, "Profile deleted successfully"
        except OSError as e:
            return False, f"Deletion error: {e}"

    # -- generation ----------------------------------------------------

    def generate(self, profile_ref: str, text: str) -> GenerationJob:
        """Runs inference directly. This is the whole job -- the audio
        array is ready the moment this returns, so the GenerationJob is
        already done=True. wait() has nothing left to do."""
        try:
            self._ensure_model_loaded()
            prompt = self._load_prompt(profile_ref)
            audio_list = self._model.generate(
                text=text, voice_clone_prompt=prompt, **self.extra_generate_params,
            )
        except Exception as e:
            job = GenerationJob(provider_ref=profile_ref, done=True, succeeded=False)
            job.error = str(e)
            return job

        audio_array = audio_list[0]  # np.ndarray, shape (T,), 24 kHz
        job = GenerationJob(provider_ref=profile_ref, done=True, succeeded=True)
        job._audio_array = audio_array
        job.audio_duration = len(audio_array) / 24000.0
        return job

    def wait(self, job: GenerationJob) -> GenerationJob:
        """No-op: generate() already ran inference to completion."""
        return job

    def fetch_audio(self, job: GenerationJob, output_path: str) -> None:
        """Writes out the audio array already held on the job -- no
        network involved at all for this provider."""
        audio_array = job._audio_array
        if audio_array is None:
            raise RuntimeError("No audio available on this job -- generate() may have failed.")
        import soundfile as sf
        sf.write(output_path, audio_array, 24000)
