"""Read-only, validated viewer-memory control. No directory/file creation."""
from typing import cast
from dataclasses import dataclass
from pathlib import Path
from os import PathLike
import re

import yaml

MEMORY_KEY = 'remember_users'
REVISION_KEY = '_memory_revision'


@dataclass(frozen=True)
class MemoryControl:
    enabled: bool
    revision: str


def control_from_mapping(data: object) -> MemoryControl:
    if not isinstance(data, dict):
        raise ValueError('Feature configuration must be a mapping')
    mapping = cast(dict[object, object], data)
    enabled = mapping.get(MEMORY_KEY, True)
    revision = mapping.get(REVISION_KEY, 'initial')
    if type(enabled) is not bool or not isinstance(revision, str) \
            or not re.fullmatch(r'initial|[0-9a-f]{64}', revision):
        raise ValueError('Invalid viewer-memory control')
    return MemoryControl(enabled, revision)


def read_memory_control(path: str | PathLike[str]) -> MemoryControl:
    try:
        raw = Path(path).read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        # No valid control file can authorize intake or recall, even at startup.
        return MemoryControl(False, 'missing')
    except (OSError, UnicodeError):
        return MemoryControl(False, 'unavailable')
    try:
        from MedlarTV.config_contract import parse_features
        data, _ = parse_features(raw)
        return control_from_mapping(data)
    except (ValueError, TypeError, yaml.YAMLError):
        return MemoryControl(False, 'invalid')
