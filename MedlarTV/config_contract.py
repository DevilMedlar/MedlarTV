"""Pure configuration contracts shared by Medlar's editors and consumers."""
from collections.abc import Hashable
from os import PathLike
from typing import TYPE_CHECKING, Protocol, cast
from yaml.nodes import MappingNode
from pathlib import Path
import math
import os
import re
import yaml


if TYPE_CHECKING:
    from yaml.nodes import Node

    class _YamlConstructor(Protocol):
        # Existing PyYAML method; its bundled stub leaves inputs/results unknown.
        def construct_object(self, node: Node, deep: bool = False) -> object: ...


class ConfigFormatError(ValueError):
    """Invalid configuration; messages never contain input values."""


# Omitted switches in a valid mapping keep Medlar's established defaults.
FEATURE_DEFAULTS = {
    'remember_users': True, 'llm_brain': True, 'pcg_auto_catch': True,
    'ignore_viewer_pokecatch': True, 'content_filter': True,
    'timers': True, 'fuzzy_trigger': True,
}


class StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, object]:
        result: dict[Hashable, object] = {}
        for key_node, value_node in node.value:
            key = cast("_YamlConstructor", self).construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConfigFormatError('Configuration keys must be text')
            if key in result:
                raise ConfigFormatError('Configuration contains duplicate keys')
            result[key] = cast("_YamlConstructor", self).construct_object(value_node, deep=deep)
        return result


def parse_mapping(raw: object) -> dict[str, object]:
    if not isinstance(raw, (str, bytes)):
        raise ConfigFormatError('Configuration input must be UTF-8 bytes or text')
    try:
        text = raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw
        data = yaml.load(text, Loader=StrictLoader)
    except (UnicodeError, yaml.YAMLError):
        raise ConfigFormatError('Configuration is not valid UTF-8 YAML') from None
    if not isinstance(data, dict):
        raise ConfigFormatError('Configuration must be a mapping')
    # StrictLoader validates every mapping key before this boundary.
    return cast(dict[str, object], data)


def feature_values(data: object) -> dict[str, bool]:
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in cast(dict[object, object], data)):
        raise ConfigFormatError('Feature configuration must be a mapping with text keys')
    mapping = cast(dict[str, object], data)
    values = dict(FEATURE_DEFAULTS)
    for key in values:
        if key in mapping:
            if type(mapping[key]) is not bool:
                raise ConfigFormatError('Feature switches must be YAML booleans')
            values[key] = cast(bool, mapping[key])
    revision = mapping.get('_memory_revision', 'initial')
    if not isinstance(revision, str) or not re.fullmatch(r'initial|[0-9a-f]{64}', revision):
        raise ConfigFormatError('Invalid viewer-memory revision')
    return values


def parse_features(raw: object) -> tuple[dict[str, object], dict[str, bool]]:
    data = parse_mapping(raw)
    return data, feature_values(data)


# Resolve only the selected checkout; reading configuration must not create it.
def selected_root(root: str | PathLike[str] | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    configured = os.getenv('MEDLARTV_ROOT', '').strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[1]


def config_path(filename: str, root: str | PathLike[str] | None = None, *, data_fallback: bool = False) -> Path:
    base = selected_root(root) / 'MedlarTV'
    preferred = base / 'config' / filename
    if data_fallback and not preferred.is_file():
        return base / 'data' / filename
    return preferred


def _weight(value: object) -> float:
    if type(value) not in (int, float) or not 0 <= cast(int | float, value) <= 1 or not math.isfinite(cast(int | float, value)):
        raise ConfigFormatError('Emotion weights must be finite numbers between zero and one')
    return float(cast(int | float, value))


# Parse intended emotion values separately from the decision to activate them.
def parse_emotions(raw: object) -> tuple[dict[str, dict[str, float]], tuple[str, ...]]:
    data = parse_mapping(raw)
    wrapped = 'emotions' in data
    section = data['emotions'] if wrapped else data
    if not isinstance(section, dict):
        raise ConfigFormatError('Emotions must be a mapping')
    section = cast(dict[str, object], section)
    normalized: dict[str, dict[str, float]] = {}
    ignored: list[str] = []
    for name, info in section.items():
        if name in {'last_update', 'timestamp'}:
            continue
        if isinstance(info, dict):
            info = cast(dict[object, object], info)
            if 'baseline' not in info:
                raise ConfigFormatError('Emotion entries require a baseline')
            entry = {'baseline': _weight(info['baseline'])}
            if 'decay' in info:
                entry['decay'] = _weight(info['decay'])
            if not wrapped:
                ignored.append(name)
        else:
            entry = {'baseline': _weight(info)}
        normalized[name] = entry
    if not normalized:
        raise ConfigFormatError('Emotion configuration must contain entries')
    return normalized, tuple(ignored)
