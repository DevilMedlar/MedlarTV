"""Runtime settings and dormant validated writer; no file creation or hosting."""
from __future__ import annotations
import threading
from MedlarTV.config_contract import config_path
from MedlarTV.config_state import FEATURE_STATE, SettingsStatus
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .memory_control import MemoryControl

# Medlar's launcher-selected root is authoritative, with this checkout as fallback.
APP_SETTINGS_FILE = config_path('app_settings.yaml')
_lock = threading.Lock()


def get_settings() -> dict[str, object]:
    values, _, _ = FEATURE_STATE.read(APP_SETTINGS_FILE)
    return values


def settings_status() -> SettingsStatus:
    _, _, status = FEATURE_STATE.read(APP_SETTINGS_FILE)
    return status


# Dormant HTTP callers use the same validated, revision-aware editor as the GUI.
def update_settings(updates: dict[str, bool]) -> dict[str, object]:
    from MedlarTV.app.config_editor_store import load_features, save_features
    with _lock:
        _, revision = load_features(APP_SETTINGS_FILE)
        save_features(APP_SETTINGS_FILE, updates, revision)
        return get_settings()


# Memory reads remain fresh: a last-valid chat setting cannot authorize memory.
def get_memory_control() -> MemoryControl:
    from .memory_control import read_memory_control
    return read_memory_control(APP_SETTINGS_FILE)


def is_enabled(key: str) -> bool:
    if key == 'remember_users':
        return get_memory_control().enabled
    return get_settings().get(key, False) is True


def settings_path() -> str:
    return str(APP_SETTINGS_FILE)
