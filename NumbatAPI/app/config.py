r"""Application configuration for the unified Numbat API.

This module no longer owns its own defaults for the data root. Instead it
reuses Kelpie's project-wide configuration (``Kelpie/config.py`` backed by
``kelpie_config.json``) as the single source of truth, so NumbatAPI writes
profile folders under the same ``data_root`` the rest of Kelpie uses (the task
runner, the logger, etc.).

Values can still be overridden per-run via environment variables:

    ``NUMBAT_DATA_ROOT``      -> data root (handled by Kelpie's config)
    ``NUMBAT_ALLOWED_DOMAIN`` -> allowed email domain

The allowed domain may also be set in ``kelpie_config.json`` under the
``allowed_domain`` key; the environment variable takes precedence.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the Kelpie project root importable so we can reuse its central config.
# NumbatAPI/app/config.py -> NumbatAPI/app -> NumbatAPI -> Kelpie (root).
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config as kelpie_config  # noqa: E402  (path set up above)

# Fallback used only when neither the env var nor kelpie_config.json define it.
_DEFAULT_ALLOWED_DOMAIN = "teamglobalexp.com"


class Settings:
    """Settings backed by Kelpie's central configuration.

    Attributes:
        data_root: Root folder under which profile folders are created. Comes
            from Kelpie's ``get_data_root()`` (env ``NUMBAT_DATA_ROOT`` wins).
        allowed_domain: Email domain permitted to register. Read from the
            ``NUMBAT_ALLOWED_DOMAIN`` env var, then ``allowed_domain`` in
            ``kelpie_config.json``, then a built-in default.
    """

    @property
    def data_root(self) -> Path:
        """Profile data root, shared with the rest of Kelpie."""
        return Path(kelpie_config.get_data_root())

    @property
    def allowed_domain(self) -> str:
        """Email domain allowed to register."""
        env_value = os.getenv("NUMBAT_ALLOWED_DOMAIN")
        if env_value:
            return env_value
        return kelpie_config.get("allowed_domain", _DEFAULT_ALLOWED_DOMAIN)


settings = Settings()
