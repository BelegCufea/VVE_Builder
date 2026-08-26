"""
Small shared utilities used across providers and pipeline code.

Moved verbatim out of generate_gui.py -- no behavior change, just a new
home so tts_pipeline/providers/*.py don't have to import back into the GUI module.
"""

from __future__ import annotations


class CaseInsensitiveDict(dict):
    """
    A dictionary with case-insensitive string keys while preserving original key casing.

    Stores key-value pairs where string keys can be accessed, retrieved, checked,
    or deleted with any letter casing. Tracks the canonical (original) casing
    of keys for iteration and representation.
    """

    def __init__(self, *args, **kwargs):
        self._keys = {}
        super().__init__()
        if args or kwargs:
            self.update(*args, **kwargs)

    def __setitem__(self, key, value):
        if isinstance(key, str):
            lower = key.lower()
            old_canonical = self._keys.get(lower)
            if old_canonical is not None and old_canonical != key:
                super().pop(old_canonical, None)
            self._keys[lower] = key
            super().__setitem__(key, value)
        else:
            super().__setitem__(key, value)

    def __getitem__(self, key):
        if isinstance(key, str):
            lower = key.lower()
            if lower in self._keys:
                return super().__getitem__(self._keys[lower])
        return super().__getitem__(key)

    def __contains__(self, key):
        if isinstance(key, str):
            return key.lower() in self._keys
        return super().__contains__(key)

    def get(self, key, default=None):
        if isinstance(key, str):
            lower = key.lower()
            if lower in self._keys:
                return super().get(self._keys[lower], default)
        return super().get(key, default)

    def pop(self, key, *args):
        if isinstance(key, str):
            lower = key.lower()
            if lower in self._keys:
                canonical = self._keys.pop(lower)
                return super().pop(canonical, *args)
        return super().pop(key, *args)

    def get_canonical_key(self, key):
        if isinstance(key, str):
            return self._keys.get(key.lower(), key)
        return key

    def update(self, *args, **kwargs):
        if args:
            if hasattr(args[0], "items"):
                for k, v in args[0].items():
                    self[k] = v
            elif hasattr(args[0], "keys"):
                for k in args[0]:
                    self[k] = args[0][k]
            else:
                for k, v in args[0]:
                    self[k] = v
        for k, v in kwargs.items():
            self[k] = v


def get_canonical_key(mapping, key):
    """
    Retrieve the canonical key from a mapping or return the key as-is.
    """
    if hasattr(mapping, "get_canonical_key"):
        return mapping.get_canonical_key(key)
    return key
