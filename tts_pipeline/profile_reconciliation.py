"""
Provider-agnostic profile reconciliation.

This replaces sync_profiles()/sync_missing_profiles() from the old
Voicebox-only code. The diff/decide logic (what's missing, what's a
zero-sample profile that needs rebuilding, what's already fine) is
identical regardless of backend -- only the leaf operations
(ensure_profile/delete_profile) are provider-specific, and those already
live behind the TtsProvider interface.

Kept deliberately separate from tts_pipeline/providers/ itself: this is pipeline
logic (it knows about VOICES_DIR scanning and CSV-needed-voice sets),
not a provider implementation. Providers stay ignorant of where "needed"
and "available" come from.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from tts_pipeline.providers.base import ProfileSource, TtsProvider
from tts_pipeline.providers.util import get_canonical_key

logger = logging.getLogger("generate_gui")


def reconcile_profiles(
    provider: TtsProvider,
    available: dict,
    needed: Optional[set] = None,
    sync_all: bool = False,
    max_attempts: int = 10,
    retry_delay: float = 3.0,
) -> dict:
    """
    Reconcile a provider's profile list against local VOICES_DIR samples.

    Args:
        provider: the active TtsProvider.
        available: name -> list[sample dict], as produced by
            scan_available_voice_dirs() (unchanged, still lives in
            generate_gui.py -- this function doesn't do its own scanning).
        needed: set of voice names the current CSV run requires. Ignored
            when sync_all=True. Required (non-None) when sync_all=False.
        sync_all: if True, reconcile every voice found in `available`
            rather than just what the CSV needs.
        max_attempts / retry_delay: how long to wait for a provider's
            profile list to reflect a just-created/rebuilt profile before
            giving up. Meaningless for a synchronous provider (the first
            list_profiles() call will already show it) but harmless.

    Returns:
        dict: freshest profile name -> provider_ref map available.
    """
    profile_map, zero_sample_profiles = provider.list_profiles()

    if not provider.capabilities.supports_native_profiles:
        # A provider with no profile management at all -- nothing to
        # reconcile, just hand back whatever it reports.
        return profile_map

    if sync_all:
        logger.info(f"🔄 Starting full voice profile sync from local samples...")
        target_names = list(available.keys())
        zero_sample_targets = [name for name in zero_sample_profiles if name in available]
        missing_targets = [name for name in target_names if name not in profile_map and name not in zero_sample_profiles]
        already_up_to_date = [name for name in target_names if name in profile_map and name not in zero_sample_profiles]
    else:
        if needed is None:
            raise ValueError("`needed` is required when sync_all=False")
        zero_sample_targets = [name for name in needed if name in zero_sample_profiles and name in available]
        missing_targets = [name for name in needed if name not in profile_map and name not in zero_sample_profiles and name in available]
        already_up_to_date = [name for name in needed if name in profile_map]

        truly_missing = [name for name in needed if name not in profile_map and name not in zero_sample_profiles and name not in available]
        unfixable_zero = [name for name in needed if name in zero_sample_profiles and name not in available]
        if truly_missing or unfixable_zero:
            logger.warning(
                f"⚠️ {len(truly_missing)} needed voice(s) missing and not found locally, "
                f"and {len(unfixable_zero)} zero-sample profile(s) cannot be rebuilt."
            )

    rebuildable = sorted(zero_sample_targets, key=str.lower)
    composable = sorted(missing_targets, key=str.lower)

    if not rebuildable and not composable:
        logger.info(f"✅ All {len(already_up_to_date)} profile(s) are already up to date.")
        return profile_map

    created, rebuilt, failed = [], [], []

    if rebuildable:
        logger.info(f"♻️ Rebuilding {len(rebuildable)} zero-sample profile(s)...")
        for voice_name in rebuildable:
            provider_ref = zero_sample_profiles[voice_name]
            canonical_name = get_canonical_key(available, voice_name)
            logger.info(f"  Deleting zero-sample profile: {voice_name}...")
            success, message = provider.delete_profile(provider_ref)
            if not success:
                logger.warning(f"  ✗ Failed to delete {voice_name}: {message}")
                failed.append(voice_name)
                continue
            logger.info(f"  ✓ Deleted: {voice_name}")
            time.sleep(retry_delay)

            logger.info(f"  Rebuilding profile: {canonical_name}...")
            result = provider.ensure_profile(ProfileSource(voice_name=canonical_name, files=available[voice_name]))
            if result:
                rebuilt.append(canonical_name)
                logger.info(f"  ✓ Re-created: {canonical_name}")
            else:
                logger.warning(f"  ✗ Failed to re-create: {canonical_name}")
                failed.append(voice_name)

    if composable:
        logger.info(f"🧩 Creating {len(composable)} new profile(s)...")
        for voice_name in composable:
            canonical_name = get_canonical_key(available, voice_name)
            result = provider.ensure_profile(ProfileSource(voice_name=canonical_name, files=available[voice_name]))
            if result:
                created.append(canonical_name)
                logger.info(f"  ✓ Created: {canonical_name}")
            else:
                logger.warning(f"  ✗ Failed to create: {canonical_name}")
                failed.append(voice_name)

    all_done = created + rebuilt
    if not all_done:
        logger.warning("⚠️ Could not create any profiles.")
        return profile_map

    still_missing = set(all_done)
    for attempt in range(1, max_attempts + 1):
        profile_map, _ = provider.list_profiles()
        still_missing = {name for name in still_missing if name not in profile_map}
        if not still_missing:
            break
        time.sleep(retry_delay)

    if still_missing:
        logger.warning(
            f"⚠️ {len(still_missing)} created/rebuilt profile(s) not yet visible after "
            f"{max_attempts} attempts: {', '.join(sorted(still_missing))}"
        )

    logger.info("=" * 60)
    logger.info("VOICE PROFILE SYNC SUMMARY")
    logger.info("=" * 60)
    if sync_all:
        logger.info(f"  Total local voices:                {len(available)}")
    logger.info(f"  New profiles created:              {len(created)}")
    logger.info(f"  Zero-sample profiles rebuilt:       {len(rebuilt)}")
    logger.info(f"  Already up to date:                 {len(already_up_to_date)}")
    if failed:
        logger.warning(f"  Failed:                            {len(failed)} ({', '.join(failed)})")
    logger.info("=" * 60)

    return profile_map
