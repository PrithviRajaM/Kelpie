"""Application configuration.

Values can be overridden via environment variables. The data root is where
per-user profile folders get created.
"""
from __future__ import annotations

import os
from pathlib import Path


class Settings:
    """Simple settings holder.

    Attributes:
        data_root: Root folder under which profile folders are created.
        allowed_domain: Email domain that is permitted to register.
    """

    def __init__(self) -> None:
        self.data_root: Path = Path(
            os.getenv("NUMBAT_DATA_ROOT", r"D:\Data\Numbat")
        )
        self.allowed_domain: str = os.getenv(
            "NUMBAT_ALLOWED_DOMAIN", "teamglobalexp.com"
        )


settings = Settings()
