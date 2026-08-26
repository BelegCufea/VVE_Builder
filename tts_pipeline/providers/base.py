"""
Provider-agnostic TTS interface.

Every backend (Voicebox, OmniVoice-server, future engines) implements
TtsProvider. Pipeline code (CSV parsing, dialog.tlk memory tracking, ogg
conversion, GUI progress reporting) must only ever call methods on this
interface -- never reach into a provider's own HTTP/config details.

Design notes (from planning):
- Voicebox is async/job-based: submit -> poll -> cancel -> download.
- OmniVoice-server's /v1/audio/speech is a single blocking call: no gen_id,
  no polling, no mid-flight cancel.
  Rather than forcing OmniVoice to fake a job lifecycle, GenerationJob
  below is a *handle* that a provider may resolve immediately (is_done()
  already True the moment generate() returns) or asynchronously (Voicebox
  fills it in as SSE events arrive). Callers always go through
  wait(), cancel(), fetch_audio() and don't need to know which.
- capabilities let the orchestrator adapt (e.g. skip the "cancel" button
  state, skip mid-job percent ticking) instead of assuming every provider
  can do everything Voicebox can.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a given provider backend can and can't do.

    The orchestrator (GenerationWorker) reads these instead of assuming
    every provider behaves like Voicebox.
    """
    supports_cancel: bool = False          # can abort a job already in flight
    supports_mid_job_progress: bool = False  # meaningful % while a single job runs
    supports_native_profiles: bool = True  # server has real profile CRUD
    is_async: bool = False                 # job may still be running after generate() returns


@dataclass
class ProfileInfo:
    """A named, provider-managed voice. Same meaning across all providers,
    even though how each provider gets one into existence differs."""
    name: str
    provider_ref: str  # opaque provider-specific id/handle (Voicebox profile id,
                        # OmniVoice-server profile id, a local folder path, etc.)
    has_samples: bool = True


@dataclass
class ProfileSource:
    """One local sample group as scanned from VOICES_DIR, handed to a
    provider so it can provision a profile from it. Mirrors what
    scan_available_voice_dirs() already produces today."""
    voice_name: str
    files: list  # sample dicts: {"number", "wav_path", "txt_path", "transcript"}


@dataclass
class GenerationJob:
    """Handle returned by generate(). May already be finished (sync
    providers) or still running (async providers) -- callers should not
    branch on this; use wait()/is_done()."""
    provider_ref: str                 # opaque id, e.g. Voicebox gen_id
    done: bool = False
    succeeded: Optional[bool] = None
    audio_duration: float = 0.0
    error: Optional[str] = None
    # local path to raw audio once fetch_audio() has been called, if the
    # provider chooses to cache it there
    _audio_path: Optional[Path] = field(default=None, repr=False)
    # raw audio bytes, for providers (e.g. OmniVoice-server) whose
    # generate() call returns the finished audio directly rather than a
    # separate downloadable resource
    _audio_bytes: Optional[bytes] = field(default=None, repr=False)


class TtsProvider(ABC):
    """Interface every TTS backend must implement.

    Method names deliberately mirror the existing Voicebox functions
    (submit_generation -> generate, wait_for_completion -> wait,
    cancel_generation -> cancel, download_audio -> fetch_audio,
    get_all_profiles -> list_profiles) so porting Voicebox's current
    logic into VoiceboxProvider is close to a rename, not a rewrite.
    """

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        ...

    # -- profiles ----------------------------------------------------

    @abstractmethod
    def list_profiles(self) -> tuple[dict, dict]:
        """Returns (profile_map, zero_sample_profiles), both name -> provider_ref.
        Matches get_all_profiles()'s existing return shape."""
        ...

    @abstractmethod
    def ensure_profile(self, source: ProfileSource) -> Optional[ProfileInfo]:
        """Make sure a profile for source.voice_name exists on/for this
        provider, creating or rebuilding it from source.files if needed.
        Returns the resulting ProfileInfo, or None on failure."""
        ...

    @abstractmethod
    def delete_profile(self, provider_ref: str) -> tuple[bool, str]:
        ...

    # -- generation ----------------------------------------------------

    @abstractmethod
    def generate(self, profile_ref: str, text: str) -> GenerationJob:
        """Start (and for sync providers, finish) a generation. Never
        blocks longer than the provider's own request/response time --
        long-running wait is wait()'s job, not this one, so async
        providers can return immediately with done=False."""
        ...

    @abstractmethod
    def wait(self, job: GenerationJob) -> GenerationJob:
        """Block until job is finished, filling in succeeded/audio_duration/
        error. For sync providers this is a no-op (job is already done)."""
        ...

    def cancel(self, job: GenerationJob) -> tuple[bool, str]:
        """Best-effort abort. Providers that can't cancel (capabilities.
        supports_cancel is False) may leave this as a no-op."""
        return False, "Cancellation not supported by this provider"

    @abstractmethod
    def fetch_audio(self, job: GenerationJob, output_path: str) -> None:
        """Write the finished job's raw audio to output_path."""
        ...
