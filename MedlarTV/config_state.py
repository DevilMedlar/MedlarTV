"""Bounded last-valid feature state; memory never uses cached enabled state."""
from os import PathLike
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import threading
from .config_contract import ConfigFormatError, FEATURE_DEFAULTS, parse_features

log = logging.getLogger('settings')


@dataclass(frozen=True)
class SettingsStatus:
    healthy: bool
    ready: bool
    reason: str
    using_previous: bool


class FeatureState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._previous: dict[str, bool] | None = None
        self._revision = 'unavailable'
        self.status = SettingsStatus(False, False, 'unloaded', False)

    def read(self, path: str | PathLike[str]) -> tuple[dict[str, object], str, SettingsStatus]:
        path = Path(path).resolve()
        with self._lock:
            # Never carry an old checkout's settings into a newly selected path.
            if path != self._path:
                self._path, self._previous = path, None
                self._revision = 'unavailable'
            # Both healthy parsing and failure recovery replace this local value.
            values: dict[str, object] = {}
            try:
                raw = path.read_bytes()
                data, features = parse_features(raw)
                self._previous = dict(features)
                values = {**data, **features}
                self._revision = hashlib.sha256(raw).hexdigest()
                status = SettingsStatus(True, True, 'valid', False)
            except FileNotFoundError:
                status = SettingsStatus(False, self._previous is not None, 'missing', self._previous is not None)
            except (OSError, UnicodeError):
                status = SettingsStatus(False, self._previous is not None, 'unavailable', self._previous is not None)
            except ConfigFormatError:
                status = SettingsStatus(False, self._previous is not None, 'invalid', self._previous is not None)
            if not status.healthy:
                values = dict(self._previous) if self._previous is not None else {key: False for key in FEATURE_DEFAULTS}
                # Fresh failure suspends actions; restrictive guards stay on.
                if self._previous is None:
                    values.update(content_filter=True, ignore_viewer_pokecatch=True)
                values['remember_users'] = False
            # Fixed transition logs make failure visible without exposing YAML.
            if status != self.status:
                if status.healthy:
                    log.info('Configuration status=healthy')
                else:
                    log.warning('Configuration status=unhealthy reason=%s using_previous=%s chat_ready=%s', status.reason, status.using_previous, status.ready)
            self.status = status
            return dict(values), self._revision, status


FEATURE_STATE = FeatureState()
