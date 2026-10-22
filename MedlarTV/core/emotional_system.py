"""
MedlarTV Emotional System (Full Rework, Session-Scoped Only)
------------------------------------------------------------

- Emotions are numeric weights in [0.0, 1.0]
- Validated wrapped/scalar overrides are loaded; flat nested values stay inactive
- State lives ONLY in memory – no persistence between runs
- Each message:
    * decays emotions toward baseline
    * uses advanced sentiment analysis for multi-emotion scores
    * estimates severity level from sentiment strength
    * applies personality multipliers (from personality.yaml if defined)
    * ripples via an influence graph to keep emotions interconnected
- Public API is stable and backwards-compatible:

    get_emotional_system()
    process_chat_emotion(message: str, username: str | None = None)
    get_current_emotion() -> str | None
    get_emotion_state() -> dict[str, float]
    boost_emotion(name: str, amount: float = 0.1)
    reset_emotions()

Any other module should continue to work with these functions.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from MedlarTV.core.interaction_logger import log_mood_change

import yaml  # type: ignore

log = logging.getLogger("emotions")
log.setLevel(logging.DEBUG)

DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"
if DEBUG:
    print("[DEBUG emotions] emotional_system.py loaded")

# ---------------------------------------------------------------------------
# Paths & Config Helpers
# ---------------------------------------------------------------------------

def _find_config_path(filename: str) -> Optional[Path]:
    from MedlarTV.config_contract import config_path
    path = config_path(filename, data_fallback=True)
    return path if path.is_file() else None


EMOTIONS_CONFIG_PATH = _find_config_path("emotions.yaml")
PERSONALITY_CONFIG_PATH = _find_config_path("personality.yaml")

# Expression level for how strongly the bot should show emotions in text.
EXPRESSION_LEVEL: int = 10  # 0–10, we run on maximum expressiveness by design


# ---------------------------------------------------------------------------
# Default Emotion Configuration
# ---------------------------------------------------------------------------

# These are built-in defaults in case emotions.yaml is missing or incomplete.
# Approved D10 preserves these when YAML uses previously ignored flat nested entries.
DEFAULT_EMOTIONS: Dict[str, Dict[str, Any]] = {
    "happiness":   {"baseline": 0.4, "decay": 0.92},
    "excitement":  {"baseline": 0.3, "decay": 0.95},
    "supportive":  {"baseline": 0.6, "decay": 0.96},
    "chill":       {"baseline": 0.7, "decay": 0.99},
    "sadness":     {"baseline": 0.1, "decay": 0.95},
    "anger":       {"baseline": 0.05, "decay": 0.95},
    "fear":        {"baseline": 0.05, "decay": 0.98},
    "snarky":      {"baseline": 0.2, "decay": 0.96},
    "energetic":   {"baseline": 0.3, "decay": 0.94},
    "tired":       {"baseline": 0.2, "decay": 0.96},
    "stressed":    {"baseline": 0.1, "decay": 0.95},
    "lonely":      {"baseline": 0.1, "decay": 0.98},
    "connected":   {"baseline": 0.5, "decay": 0.97},
    "pride":       {"baseline": 0.2, "decay": 0.95},
    "jealousy":    {"baseline": 0.05, "decay": 0.96},
    "affection":   {"baseline": 0.3, "decay": 0.97},
    "romance":     {"baseline": 0.2, "decay": 0.96},
    "attraction":  {"baseline": 0.2, "decay": 0.95},
    "arousal":     {"baseline": 0.05, "decay": 0.90},
}

# Fallback decay if not specified anywhere
DEFAULT_DECAY: float = 0.97

# Severity → base magnitude
SEVERITY_SCALE = {
    1: 0.05,   # mild
    2: 0.15,   # medium
    3: 0.30,   # strong
    4: 0.50,   # extreme
}

# Influence graph: source emotion → {target emotion: weight}
# Weight is scaled internally so this stays gentle, not chaotic.
EMOTION_INFLUENCES: Dict[str, Dict[str, float]] = {
    "sadness": {
        "lonely": 0.3,
        "happiness": -0.15,
        "excitement": -0.15,
    },
    "happiness": {
        "sadness": -0.15,
        "fear": -0.1,
        "connected": 0.2,
    },
    "excitement": {
        "energetic": 0.4,
        "tired": -0.3,
        "chill": -0.1,
    },
    "supportive": {
        "connected": 0.3,
        "lonely": -0.2,
        "happiness": 0.1,
    },
    "anger": {
        "snarky": 0.3,
        "stressed": 0.3,
        "chill": -0.2,
    },
    "fear": {
        "stressed": 0.3,
        "tired": 0.2,
    },
    "lonely": {
        "connected": -0.3,
        "sadness": 0.2,
    },
    "pride": {
        "happiness": 0.2,
        "excitement": 0.2,
    },
    "affection": {
        "connected": 0.3,
        "happiness": 0.2,
        "excitement": 0.1,
    },
    "romance": {
        "connected": 0.3,
        "happiness": 0.2,
    },
    "attraction": {
        "connected": 0.2,
        "excitement": 0.2,
        "pride": 0.1,
    },
    "arousal": {
        "excitement": 0.4,
        "energetic": 0.3,
        "chill": -0.2,
    },
}


# ---------------------------------------------------------------------------
# Personality Trait Loading
# ---------------------------------------------------------------------------

def _load_personality_traits() -> Dict[str, float]:
    """
    Optional: load per-emotion trait multipliers from personality.yaml.

    Expected shape (example):

    traits:
      supportive: 1.2
      energetic: 1.3
      snarky: 1.1

    If the file or 'traits' key is missing, we just return an empty dict,
    and all emotions use a factor of 1.0 (no modification).
    """
    if not PERSONALITY_CONFIG_PATH:
        return {}

    try:
        with PERSONALITY_CONFIG_PATH.open("r", encoding="utf-8") as f:
            from MedlarTV.config_contract import parse_mapping
            data = parse_mapping(f.read())
    except Exception as e:
        log.warning("Failed to load personality.yaml: %s", type(e).__name__)
        return {}

    traits = data.get("traits") or {}
    if not isinstance(traits, dict):
        return {}

    cleaned: Dict[str, float] = {}
    for key, value in traits.items():
        try:
            cleaned[str(key)] = float(value)
        except Exception:
            continue
    return cleaned


PERSONALITY_TRAITS: Dict[str, float] = _load_personality_traits()


# ---------------------------------------------------------------------------
# Emotion Config Loading
# ---------------------------------------------------------------------------

def _load_emotion_config() -> Dict[str, Dict[str, Any]]:
    """Validate compatible YAML while preserving Medlar's effective tone.

    Wrapped/scalar overrides retain their existing meaning. Flat nested values
    were previously ignored; D10 keeps those inactive until separately approved.
    """
    from MedlarTV.config_contract import parse_emotions
    config = {key: value.copy() for key, value in DEFAULT_EMOTIONS.items()}
    if not EMOTIONS_CONFIG_PATH:
        log.warning("Emotion configuration unavailable; effective defaults retained")
        return config
    try:
        intended, inactive = parse_emotions(EMOTIONS_CONFIG_PATH.read_bytes())
    except (OSError, ValueError):
        log.warning("Emotion configuration invalid or unreadable; effective defaults retained")
        return config
    if inactive:
        log.warning("Flat nested emotion values remain inactive under approved D10; effective defaults retained")
    for name, info in intended.items():
        if name in inactive:
            continue
        config.setdefault(name, {"baseline": 0.0, "decay": DEFAULT_DECAY}).update(info)
    return config


EMOTION_CONFIG: Dict[str, Dict[str, Any]] = _load_emotion_config()


# ---------------------------------------------------------------------------
# Sentiment Integration
# ---------------------------------------------------------------------------

def _analyze_message(message: str) -> Tuple[float, Dict[str, float]]:
    """
    Wrap sentiment_advanced.analyze_sentiment_advanced if available.

    Returns:
        sentiment: float in [-1, 1] (negative to positive)
        emotion_scores: mapping emotion_name -> score in [0, 1]
    """
    try:
        from MedlarTV.core.sentiment_advanced import analyze_sentiment_advanced  # type: ignore
    except Exception:
        # Fallback: neutral sentiment, no specific emotion scores
        return 0.0, {}

    try:
        sentiment, emotion_scores = analyze_sentiment_advanced(message)
        if not isinstance(emotion_scores, dict):
            emotion_scores = {}
        # clamp scores to [0, 1]
        cleaned = {}
        for k, v in emotion_scores.items():
            try:
                fv = float(v)
            except Exception:
                continue
            cleaned[k] = max(0.0, min(1.0, fv))
        return float(sentiment), cleaned
    except Exception as e:
        log.error("Failed to analyze sentiment: %s", type(e).__name__)
        return 0.0, {}


def _estimate_severity(sentiment: float, emotion_scores: Dict[str, float], msg: str) -> int:
    """
    Estimate a discrete severity level (1–4) from overall sentiment
    magnitude and any strong emotion scores.

    This is intentionally simple and heuristic.
    """
    mag = abs(sentiment)

    # If any emotion score is very strong, bump severity.
    max_score = max(emotion_scores.values()) if emotion_scores else 0.0

    # Keyword-based bump for extremely strong phrases
    lower = msg.lower()
    strong_keywords = [
        "my cat died",
        "my dog died",
        "they died",
        "funeral",
        "i want to die",
        "i'm devastated",
    ]
    extreme_hit = any(k in lower for k in strong_keywords)

    if extreme_hit or max_score > 0.85 or mag > 0.85:
        return 4
    if max_score > 0.6 or mag > 0.6:
        return 3
    if max_score > 0.35 or mag > 0.35:
        return 2
    return 1


# ---------------------------------------------------------------------------
# Emotional System Core
# ---------------------------------------------------------------------------

class EmotionalSystem:
    """
    Core runtime-only emotional model.
    """

    def __init__(self) -> None:
        self._config = {k: v.copy() for k, v in EMOTION_CONFIG.items()}
        self._emotions: Dict[str, float] = {}
        self._baselines: Dict[str, float] = {}
        self.last_update: float = time.time()

        self._init_from_config()

        log.debug("[Emotions] __init__ complete. Baselines=%s", self._baselines)
        if DEBUG:
            print(f"[DEBUG emotions] EmotionalSystem.__init__ baselines={self._baselines}")

    # --------------------------- Setup -----------------------------

    def _init_from_config(self) -> None:
        for name, cfg in self._config.items():
            baseline = float(cfg.get("baseline", 0.0))
            decay = float(cfg.get("decay", DEFAULT_DECAY))
            cfg["baseline"] = max(0.0, min(1.0, baseline))
            cfg["decay"] = min(0.999, max(0.5, decay))  # clamp to sensible range
            self._baselines[name] = cfg["baseline"]
            self._emotions[name] = cfg["baseline"]

        log.info("[Emotions] Initialized with %d emotions", len(self._emotions))

    # --------------------------- Properties -----------------------------

    @property
    def emotions(self) -> Dict[str, float]:
        return dict(self._emotions)

    @property
    def baselines(self) -> Dict[str, float]:
        return dict(self._baselines)

    # --------------------------- Core Logic -----------------------------

    def apply_decay(self) -> None:
        """
        Decay each emotion toward its baseline using its specific decay rate.
        """
        for name, value in list(self._emotions.items()):
            cfg = self._config.get(name, {})
            baseline = cfg.get("baseline", self._baselines.get(name, 0.0))
            decay = cfg.get("decay", DEFAULT_DECAY)
            try:
                baseline_f = float(baseline)
                decay_f = float(decay)
            except Exception:
                baseline_f = 0.0
                decay_f = DEFAULT_DECAY

            new_val = baseline_f + (value - baseline_f) * decay_f
            self._emotions[name] = self._clamp(new_val)

    def _apply_deltas(self, deltas: Dict[str, float]) -> None:
        for name, delta in deltas.items():
            if name not in self._emotions:
                # Allow new emotions on the fly with neutral baseline.
                self._emotions[name] = self._clamp(delta)
                self._baselines.setdefault(name, 0.0)
                continue
            self._emotions[name] = self._clamp(self._emotions[name] + delta)

    def _clamp(self, value: float) -> float:
        return max(0.0, min(1.0, value))

    def apply_influences(self) -> None:
        """
        Apply cross-emotion influence in a gentle way to avoid chaotic swings.
        """
        updates: Dict[str, float] = {}

        for src, targets in EMOTION_INFLUENCES.items():
            src_val = self._emotions.get(src)
            if src_val is None:
                continue
            for target, weight in targets.items():
                # Small factor to keep it subtle.
                influence = src_val * weight * 0.1
                updates[target] = updates.get(target, 0.0) + influence

        if updates:
            for name, delta in updates.items():
                if name not in self._emotions:
                    # if unknown, create with baseline 0
                    self._emotions[name] = self._clamp(delta)
                    self._baselines.setdefault(name, 0.0)
                else:
                    self._emotions[name] = self._clamp(self._emotions[name] + delta)

    def _compute_deltas_from_scores(
        self,
        sentiment: float,
        emotion_scores: Dict[str, float],
        severity: int,
    ) -> Dict[str, float]:
        """
        Turn sentiment + per-emotion scores into actual numeric deltas.
        """
        base_mag = SEVERITY_SCALE.get(severity, SEVERITY_SCALE[1])
        deltas: Dict[str, float] = {}

        # If we have specific emotion scores, use them.
        if emotion_scores:
            # Normalize scores so max is 1 (if necessary).
            max_score = max(emotion_scores.values())
            norm_factor = 1.0 / max_score if max_score > 1.0 else 1.0

            for name, raw_score in emotion_scores.items():
                score = raw_score * norm_factor
                # Positive sentiment: focus on positive-leaning emotions.
                # Negative sentiment: focus on negative-leaning emotions.
                # Neutral: let specific emotion scores drive the changes.
                if sentiment >= 0.25:
                    if name in ("happiness", "excitement", "supportive", "connected", "pride", "energetic", "affection", "romance", "attraction", "arousal"):
                        delta = base_mag * score
                    elif name in ("sadness", "anger", "fear", "lonely", "stressed", "tired", "jealousy"):
                        delta = -base_mag * score * 0.5
                    else:
                        delta = base_mag * (score - 0.4)
                elif sentiment <= -0.25:
                    if name in ("sadness", "anger", "fear", "lonely", "stressed", "jealousy"):
                        delta = base_mag * score
                    elif name in ("happiness", "excitement", "supportive", "connected", "pride", "energetic", "affection", "romance", "attraction", "arousal"):
                        delta = -base_mag * score * 0.5
                    else:
                        delta = base_mag * (score - 0.4)
                else:
                    # Low overall sentiment magnitude → rely mostly on emotion_scores.
                    delta = base_mag * (score - 0.5)

                if abs(delta) < 1e-4:
                    continue
                deltas[name] = deltas.get(name, 0.0) + delta

        else:
            # No detailed emotion scores: fall back to general sentiment.
            if sentiment > 0.1:
                for name in ("happiness", "excitement", "supportive", "connected", "pride", "energetic"):
                    deltas[name] = deltas.get(name, 0.0) + base_mag * float(sentiment)
                for name in ("sadness", "anger", "fear", "lonely", "stressed", "tired"):
                    deltas[name] = deltas.get(name, 0.0) - base_mag * float(sentiment) * 0.5
            elif sentiment < -0.1:
                for name in ("sadness", "anger", "fear", "lonely", "stressed", "tired"):
                    deltas[name] = deltas.get(name, 0.0) + base_mag * float(-sentiment)
                for name in ("happiness", "excitement", "supportive", "connected", "pride", "energetic"):
                    deltas[name] = deltas.get(name, 0.0) - base_mag * float(-sentiment) * 0.5

        return deltas

    def _apply_personality(self, deltas: Dict[str, float]) -> Dict[str, float]:
        """
        Apply personality multipliers from PERSONALITY_TRAITS.

        Example traits mapping:

            traits:
              supportive: 1.2
              energetic: 1.3
              snarky: 1.15
        """
        if not PERSONALITY_TRAITS:
            return deltas

        adjusted: Dict[str, float] = {}
        for name, delta in deltas.items():
            factor = PERSONALITY_TRAITS.get(name, 1.0)
            adjusted[name] = delta * factor
        return adjusted

    # --------------------------- Public Ops -----------------------------

    def process_message(self, message: str, username: Optional[str] = None) -> Dict[str, Any]:
        """
        Main entry: call this once per chat message to update emotional state.

        Returns a diagnostic dict with sentiment, severity, and new emotion snapshot.
        """
        if DEBUG:
            print(f"[DEBUG emotions] process_message() message_length={len(message)}")

        prev_dom = self.get_dominant_emotion()
        # 1) Decay toward baseline first
        self.apply_decay()
        if DEBUG:
            print(f"[DEBUG emotions] After decay: {self._emotions}")

        # 2) Analyze sentiment & per-emotion scores
        sentiment, emotion_scores = _analyze_message(message)
        if DEBUG:
            print(f"[DEBUG emotions] Sentiment={sentiment}, emotion_scores={emotion_scores}")

        # 3) Estimate severity 1–4
        severity = _estimate_severity(sentiment, emotion_scores, message)
        if DEBUG:
            print(f"[DEBUG emotions] Severity estimated={severity}")

        # 4) Compute raw deltas
        deltas = self._compute_deltas_from_scores(sentiment, emotion_scores, severity)
        if DEBUG:
            print(f"[DEBUG emotions] Raw deltas={deltas}")

        # 5) Apply personality multipliers
        deltas = self._apply_personality(deltas)
        if DEBUG:
            print(f"[DEBUG emotions] Personality-adjusted deltas={deltas}")

        # 6) Apply the deltas
        self._apply_deltas(deltas)
        if DEBUG:
            print(f"[DEBUG emotions] After applying deltas: {self._emotions}")

        # 7) Ripple through influence graph
        self.apply_influences()
        if DEBUG:
            print(f"[DEBUG emotions] After influences: {self._emotions}")

        self.last_update = time.time()

        snapshot = self.emotions
        if DEBUG:
            print(f"[DEBUG emotions] Final snapshot returned: {snapshot}")

        try:
            new_dom = self.get_dominant_emotion()
            if new_dom and prev_dom and new_dom != prev_dom:
                log_mood_change(prev_dom, new_dom, "chat", {"severity": severity})
        except Exception:
            pass

        return {
            "sentiment": sentiment,
            "severity": severity,
            "deltas": deltas,
            "emotions": snapshot,
        }

    # ------------------------------------------------------------------

    def reset_to_baseline(self) -> None:
        """
        Reset all emotions to their configured baselines.
        Called at startup and should be used on shutdown if needed.
        """
        for name, baseline in self._baselines.items():
            self._emotions[name] = baseline
        self.last_update = time.time()
        log.info("[Emotions] Reset to baseline")

    def boost(self, name: str, amount: float = 0.1) -> None:
        """
        Manual boost of a specific emotion (e.g., from commands).
        """
        if name not in self._emotions:
            # create a new emotion track if needed
            self._emotions[name] = self._clamp(amount)
            self._baselines.setdefault(name, 0.0)
        else:
            self._emotions[name] = self._clamp(self._emotions[name] + amount)

    # ------------------------------------------------------------------

    def get_dominant_emotion(self) -> Optional[str]:
        if not self._emotions:
            return None
        return max(self._emotions.items(), key=lambda kv: kv[1])[0]

    def get_top_emotions(self, n: int = 3) -> Dict[str, float]:
        items = sorted(self._emotions.items(), key=lambda kv: kv[1], reverse=True)
        return dict(items[:n])

    def get_mood_description(self) -> str:
        """
        Lightweight human-readable mood summary using all emotions.

        This is intentionally simple and can later be delegated to mood_system.
        """
        if not self._emotions:
            return "Neutral"

        dom = self.get_dominant_emotion()
        if not dom:
            return "Neutral"

        # High-level groupings
        pos = sum(self._emotions.get(e, 0.0) for e in ("happiness", "excitement", "supportive", "connected", "pride"))
        neg = sum(self._emotions.get(e, 0.0) for e in ("sadness", "anger", "fear", "lonely", "stressed", "jealousy"))
        energy = self._emotions.get("energetic", 0.0) - self._emotions.get("tired", 0.0)
        snark = self._emotions.get("snarky", 0.0)
        chill = self._emotions.get("chill", 0.0)

        if neg > pos and self._emotions.get("sadness", 0.0) > 0.5:
            if self._emotions.get("lonely", 0.0) > 0.4:
                return "Feeling fragile and a bit isolated."
            return "Feeling pretty down and heavy."
        if pos > neg and self._emotions.get("excitement", 0.0) > 0.6:
            return "Hyped and in a really good mood."
        if pos > neg and chill > 0.6:
            return "Warm, calm, and relaxed."
        if snark > 0.6 and self._emotions.get("anger", 0.0) > 0.3:
            return "Spicy, annoyed, but still present."
        if energy < -0.3:
            return "Low energy, a bit drained."
        if energy > 0.5:
            return "Energetic and ready to engage."

        # Fallback: describe by dominant emotion
        return f"Mostly feeling {dom}."

    # Convenience snapshots ------------------------------------------------

    def snapshot(self) -> Dict[str, float]:
        return self.emotions


# ---------------------------------------------------------------------------
# Module-level Singleton & API
# ---------------------------------------------------------------------------

_emotional_system: Optional[EmotionalSystem] = None


def get_emotional_system() -> EmotionalSystem:
    global _emotional_system
    if DEBUG:
        print("[DEBUG emotions] get_emotional_system() called")
    if _emotional_system is None:
        if DEBUG:
            print("[DEBUG emotions] No existing EmotionalSystem, creating new one...")
        _emotional_system = EmotionalSystem()
        log.info("[Emotions] Emotional system created")
    else:
        if DEBUG:
            print("[DEBUG emotions] Reusing existing EmotionalSystem instance")
    return _emotional_system

def process_chat_emotion(message: str, username: Optional[str] = None) -> Dict[str, Any]:
    """
    Backwards-compatible wrapper used by other parts of the system.

    Returns the same diagnostic dict as EmotionalSystem.process_message().
    """
    system = get_emotional_system()
    return system.process_message(message, username=username)


def get_current_emotion() -> Optional[str]:
    system = get_emotional_system()
    return system.get_dominant_emotion()


def get_emotion_state() -> Dict[str, float]:
    system = get_emotional_system()
    return system.snapshot()


def boost_emotion(name: str, amount: float = 0.1) -> None:
    system = get_emotional_system()
    system.boost(name, amount)


def reset_emotions() -> None:
    system = get_emotional_system()
    system.reset_to_baseline()
