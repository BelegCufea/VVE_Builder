"""
Voicebox provider.

This is a behind-the-interface port of the Voicebox-specific functions
that used to live directly in generate_gui.py: submit_generation,
wait_for_completion, cancel_generation, download_audio, get_all_profiles,
create_profile_package, import_profile_zip, delete_profile. The logic
itself is unchanged -- only the seams moved, so this should behave
identically to before.

Config (base_url, engine, model_size, endpoints) is passed in at
construction instead of read from module globals, so multiple providers
(or a provider + settings-panel edits) can coexist without fighting over
shared state.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
import zipfile
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
from .util import CaseInsensitiveDict, get_canonical_key

logger = logging.getLogger("generate_gui")


class VoiceboxProvider(TtsProvider):
    def __init__(
        self,
        base_url: str,
        engine: str,
        model_size: str,
        profile_packages_dir: str = "profiles",
        profiles_endpoint: str = "/profiles",
        profiles_import_endpoint: str = "/profiles/import",
        generate_endpoint: str = "/generate",
        generate_status_endpoint: str = "/generate/{gen_id}/status",
        generate_cancel_endpoint: str = "/generate/{gen_id}/cancel",
        audio_endpoint: str = "/audio/{gen_id}",
    ):
        self.base_url = base_url
        self.engine = engine
        self.model_size = model_size
        self.profile_packages_dir = profile_packages_dir
        self.profiles_endpoint = profiles_endpoint
        self.profiles_import_endpoint = profiles_import_endpoint
        self.generate_endpoint = generate_endpoint
        self.generate_status_endpoint = generate_status_endpoint
        self.generate_cancel_endpoint = generate_cancel_endpoint
        self.audio_endpoint = audio_endpoint

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_cancel=True,
            supports_mid_job_progress=True,
            supports_native_profiles=True,
            is_async=True,
        )

    # -- profiles ----------------------------------------------------

    def list_profiles(self) -> tuple[dict, dict]:
        """Ported from get_all_profiles(). Filters out zero-sample profiles
        into a separate map since they're unusable for generation."""
        resp = requests.get(f"{self.base_url}{self.profiles_endpoint}")
        resp.raise_for_status()

        profile_map = CaseInsensitiveDict()
        zero_sample_profiles = CaseInsensitiveDict()
        total_profiles = 0

        for p in resp.json():
            total_profiles += 1
            profile_id = p.get("id")
            profile_name = p.get("name")
            sample_count = p.get("sample_count", 0)

            if not profile_name or not profile_id:
                continue
            if sample_count == 0:
                zero_sample_profiles[profile_name] = profile_id
                continue
            profile_map[profile_name] = profile_id

        if zero_sample_profiles:
            logger.warning(
                f"⚠️ Found {len(zero_sample_profiles)} zero-sample profile(s) "
                f"out of {total_profiles} total: {', '.join(list(zero_sample_profiles.keys())[:5])}"
                + (f" and {len(zero_sample_profiles) - 5} more" if len(zero_sample_profiles) > 5 else "")
            )
        else:
            logger.info(f"Loaded {len(profile_map)} voice profiles (all with samples).")

        return profile_map, zero_sample_profiles

    def _create_profile_package(self, voice_name: str, files: list, output_dir: str) -> Optional[Path]:
        """Ported unchanged from create_profile_package()."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        safe_name = voice_name.lower().replace(' ', '-')
        temp_dir = output_path / f"profile-{safe_name}.voicebox"
        temp_dir.mkdir(exist_ok=True)

        try:
            samples_dir = temp_dir / "samples"
            samples_dir.mkdir(exist_ok=True)

            manifest = {
                "version": "1.0",
                "profile": {"name": voice_name, "description": "", "language": "en"},
                "has_avatar": False,
            }
            with open(temp_dir / "manifest.json", "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

            samples_data = {}
            for file_info in sorted(files, key=lambda x: x["number"]):
                sample_uuid = str(uuid.uuid4())
                wav_filename = f"{sample_uuid}.wav"
                shutil.copy2(file_info["wav_path"], samples_dir / wav_filename)
                samples_data[wav_filename] = file_info["transcript"]

            with open(temp_dir / "samples.json", "w", encoding="utf-8") as f:
                json.dump(samples_data, f, indent=2)

            zip_path = output_path / f"profile-{safe_name}.voicebox.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for root, _, files_in_dir in os.walk(temp_dir):
                    for file in files_in_dir:
                        file_path = Path(root) / file
                        zipf.write(file_path, file_path.relative_to(temp_dir))

            return zip_path
        except Exception as e:
            logger.error(f"❌ Error creating profile package for {voice_name}: {e}")
            return None
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

    def _import_profile_zip(self, zip_path: Path) -> Optional[dict]:
        """Ported unchanged from import_profile_zip()."""
        try:
            with open(zip_path, "rb") as f:
                files = {"file": (zip_path.name, f, "application/zip")}
                resp = requests.post(f"{self.base_url}{self.profiles_import_endpoint}", files=files)
                resp.raise_for_status()
                return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Error importing {zip_path.name}: {e}")
            return None

    def ensure_profile(self, source: ProfileSource) -> Optional[ProfileInfo]:
        """Compose a .voicebox.zip from local samples and import it.
        Note: this only creates/imports -- it does not delete a stale
        zero-sample profile first. The shared reconciliation helper
        (sync_profiles-equivalent) is responsible for calling
        delete_profile() before this when rebuilding, same two-step
        sequence sync_profiles() already used."""
        zip_path = self._create_profile_package(source.voice_name, source.files, self.profile_packages_dir)
        if not zip_path:
            return None
        result = self._import_profile_zip(zip_path)
        if not result:
            return None
        provider_ref = result.get("id") or result.get("profile", {}).get("id")
        return ProfileInfo(name=source.voice_name, provider_ref=provider_ref, has_samples=True)

    def delete_profile(self, provider_ref: str) -> tuple[bool, str]:
        """Ported unchanged from delete_profile()."""
        try:
            delete_url = f"{self.base_url}{self.profiles_endpoint}/{provider_ref}"
            resp = requests.delete(delete_url)
            if resp.status_code == 200:
                return True, "Profile deleted successfully"
            return False, f"Deletion returned: {resp.status_code}"
        except Exception as e:
            return False, f"Deletion error: {e}"

    # -- generation ----------------------------------------------------

    def generate(self, profile_ref: str, text: str) -> GenerationJob:
        """Ported from submit_generation(). Returns immediately with
        done=False -- caller must still call wait()."""
        payload = {
            "text": text, "profile_id": profile_ref, "language": "en",
            "engine": self.engine, "model_size": self.model_size,
        }
        resp = requests.post(f"{self.base_url}{self.generate_endpoint}", json=payload)
        resp.raise_for_status()
        data = resp.json()
        gen_id = data.get("id")
        if not gen_id:
            raise RuntimeError(f"Response missing 'id': {data}")
        return GenerationJob(provider_ref=gen_id, done=False)

    def wait(self, job: GenerationJob) -> GenerationJob:
        """Ported from wait_for_completion(): streams SSE until a terminal
        status arrives, then fills in the job handle."""
        url = f"{self.base_url}{self.generate_status_endpoint.format(gen_id=job.provider_ref)}"
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

        job.done = True
        if final_event and final_event.get("status") == "completed":
            job.succeeded = True
            job.audio_duration = final_event.get("duration", 0.0)
        else:
            job.succeeded = False
            job.error = (final_event or {}).get("error", "Generation failed or connection lost")
        return job

    def cancel(self, job: GenerationJob) -> tuple[bool, str]:
        """Ported unchanged from cancel_generation()."""
        try:
            cancel_url = f"{self.base_url}{self.generate_cancel_endpoint.format(gen_id=job.provider_ref)}"
            resp = requests.post(cancel_url)
            if resp.status_code == 200:
                return True, "Cancellation successful"
            return False, f"Cancellation returned: {resp.status_code}"
        except Exception as e:
            return False, f"Cancellation error: {e}"

    def fetch_audio(self, job: GenerationJob, output_path: str) -> None:
        """Ported unchanged from download_audio()."""
        url = f"{self.base_url}{self.audio_endpoint.format(gen_id=job.provider_ref)}"
        resp = requests.get(url)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(resp.content)
