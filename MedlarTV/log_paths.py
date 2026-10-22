"""Shared Core/status log paths; relative directories belong to the selected root."""
from collections.abc import Mapping
from os import PathLike
import os
from pathlib import Path

def resolve_log_directory(root: str | PathLike[str], environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    return Path(root) / values.get("LOG_DIRECTORY", "logs")
