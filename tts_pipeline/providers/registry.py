"""
Provider registry.

Deliberately the simple version: a name -> constructor mapping and one
config constant to flip. No settings-panel UI for provider selection yet
-- per current scope, editing TTS_PROVIDER (and the matching *_CONFIG
dict) directly in generate_gui.py's config section is enough. Revisit
this if/when a second person other than you needs to switch providers
without touching code.
"""

from __future__ import annotations

from .base import TtsProvider
from .omnivoice import OmniVoiceProvider
from .voicebox import VoiceboxProvider

_PROVIDERS = {
    "voicebox": VoiceboxProvider,
    "omnivoice": OmniVoiceProvider,
}


def create_provider(name: str, config: dict) -> TtsProvider:
    """
    Args:
        name: one of _PROVIDERS' keys ("voicebox", "omnivoice").
        config: kwargs forwarded straight to the provider's constructor,
            e.g. for voicebox: {"base_url": ..., "engine": ..., "model_size": ...}
            for omnivoice: {"voices_dir": ..., "profiles_dir": ..., "device_map": ...}

    Raises:
        ValueError: if name isn't a known provider.
    """
    try:
        provider_cls = _PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"Unknown TTS provider '{name}'. Available: {', '.join(_PROVIDERS)}"
        )
    return provider_cls(**config)
