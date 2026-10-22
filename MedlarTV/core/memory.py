import os
DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"
if DEBUG:
    print("[DEBUG memory] Loaded memory.py")
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

MEMORY_FILE = Path("memory.json")


# --------------------------------------------------------------
# LOAD MEMORY (diagnostics contain types/stages only)
# --------------------------------------------------------------

def load_memory() -> Dict[str, Any]:
    if DEBUG:
        print("[DEBUG memory] load_memory() called")

    if not MEMORY_FILE.exists():
        if DEBUG:
            print("[DEBUG memory] memory.json does NOT exist. Returning empty memory.")
        return {"moods": [], "last_reset": datetime.utcnow().isoformat()}

    try:
        if DEBUG:
            print("[DEBUG memory] Opening memory.json for read...")
        with MEMORY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if DEBUG:
            print(f"[DEBUG memory] load_memory() root_type={type(data).__name__}")
        return data
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG memory] ERROR while reading memory.json: {type(e).__name__}")
        return {"moods": [], "last_reset": datetime.utcnow().isoformat()}


# --------------------------------------------------------------
# SAVE MEMORY
# --------------------------------------------------------------

def save_memory(data: Dict[str, Any]) -> None:
    if DEBUG:
        print(f"[DEBUG memory] save_memory() root_type={type(data).__name__}")

    try:
        if DEBUG:
            print("[DEBUG memory] Opening memory.json for WRITE...")
        with MEMORY_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if DEBUG:
            print("[DEBUG memory] save_memory() write completed.")
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG memory] ERROR saving memory.json: {type(e).__name__}")


# --------------------------------------------------------------
# RECORD MOOD ENTRY
# --------------------------------------------------------------

def record_mood(mood: str, source: str) -> None:
    if DEBUG:
        print("[DEBUG memory] record_mood() called")

    data = load_memory()
    if DEBUG:
        print("[DEBUG memory] record_mood() stage=loaded")

    entry = {
        "mood": mood,
        "source": source,
        "timestamp": datetime.utcnow().isoformat()
    }
    if DEBUG:
        print("[DEBUG memory] record_mood() stage=entry_ready")

    try:
        data.setdefault("moods", []).append(entry)
        if DEBUG:
            print("[DEBUG memory] record_mood() stage=updated")
        save_memory(data)
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG memory] ERROR updating mood memory: {type(e).__name__}")


# --------------------------------------------------------------
# GET DOMINANT MOOD (SIMPLE COUNT)
# --------------------------------------------------------------

def get_dominant_mood() -> Optional[str]:
    if DEBUG:
        print("[DEBUG memory] get_dominant_mood() called")

    data = load_memory()
    moods = data.get("moods", [])

    if DEBUG:
        print('[DEBUG memory] get_dominant_mood stage=observed')

    if not moods:
        if DEBUG:
            print("[DEBUG memory] dominant_mood → None (no moods)")
        return None

    counts: Dict[str, int] = {}
    for entry in moods:
        m = entry.get("mood")
        counts[m] = counts.get(m, 0) + 1

    if DEBUG:
        print('[DEBUG memory] get_dominant_mood stage=observed')

    dominant = max(counts, key=counts.get)
    if DEBUG:
        print('[DEBUG memory] get_dominant_mood stage=observed')

    return dominant


# --------------------------------------------------------------
# GET DOMINANT MOOD (WEIGHTED BY RECENCY)
# --------------------------------------------------------------

def get_dominant_weighted_mood() -> Optional[str]:
    if DEBUG:
        print("[DEBUG memory] get_dominant_weighted_mood() called")

    data = load_memory()
    moods = data.get("moods", [])

    if DEBUG:
        print('[DEBUG memory] get_dominant_weighted_mood stage=observed')

    if not moods:
        if DEBUG:
            print("[DEBUG memory] weighted_mood → None (no moods)")
        return None

    weights: Dict[str, float] = {}
    now = datetime.utcnow()

    for entry in moods:
        mood = entry.get("mood")
        ts = entry.get("timestamp")

        try:
            dt = datetime.fromisoformat(ts.replace("Z", ""))
            age_seconds = max(1, (now - dt).total_seconds())
            weight = 1 / age_seconds
        except Exception as e:
            if DEBUG:
                print('[DEBUG memory] get_dominant_weighted_mood stage=observed')
            weight = 0.000001

        if DEBUG:
            print('[DEBUG memory] get_dominant_weighted_mood stage=observed')

        weights[mood] = weights.get(mood, 0) + weight

    if DEBUG:
        print('[DEBUG memory] get_dominant_weighted_mood stage=observed')

    dominant = max(weights, key=weights.get)
    if DEBUG:
        print('[DEBUG memory] get_dominant_weighted_mood stage=observed')

    return dominant


# --------------------------------------------------------------
# RESET MEMORY
# --------------------------------------------------------------

def reset_memory_on_shutdown() -> None:
    if DEBUG:
        print("[DEBUG memory] reset_memory_on_shutdown() called")

    data = {
        "moods": [],
        "last_reset": datetime.utcnow().isoformat()
    }

    if DEBUG:
        print('[DEBUG memory] reset_memory_on_shutdown stage=observed')

    try:
        save_memory(data)
        if DEBUG:
            print("[DEBUG memory] reset_memory_on_shutdown() save complete")
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG memory] ERROR in reset_memory_on_shutdown(): {type(e).__name__}")
