"""
MedlarTV Emotion Type Definitions
Centralized emotion constants and types
"""
import logging
log = logging.getLogger("emotion_types")
log.setLevel(logging.DEBUG)
log.debug("[DEBUG] emotion_types.py loaded successfully")

from typing import Literal, Dict
from enum import Enum

# ===== Emotion Types =====
class Emotion(str, Enum):
    """Core emotion types"""
    HAPPINESS = "happiness"
    SADNESS = "sadness"
    ANGER = "anger"
    FEAR = "fear"
    EXCITEMENT = "excitement"
    GRATITUDE = "gratitude"
    JEALOUSY = "jealousy"
    PRIDE = "pride"
    NEUTRAL = "neutral"


# ===== Mood Types =====
class Mood(str, Enum):
    """Bot mood/personality modes"""
    HYPE = "hype"
    CHILL = "chill"
    SNARKY = "snarky"
    SUPPORTIVE = "supportive"


# ===== Type Aliases =====
EmotionType = Literal["happiness", "sadness", "anger", "fear", "excitement", "gratitude", "jealousy", "pride", "neutral"]
MoodType = Literal["hype", "chill", "snarky", "supportive"]
EmotionState = Dict[str, float]
MoodState = Dict[str, float]


# ===== Constants =====
DEFAULT_EMOTIONS: EmotionState = {
    "happiness": 0.5,
    "sadness": 0.0,
    "anger": 0.0,
    "fear": 0.0,
    "excitement": 0.3,
    "gratitude": 0.2,
    "jealousy": 0.0,
    "pride": 0.0,
}
log.debug(f"[DEBUG] DEFAULT_EMOTIONS loaded: {DEFAULT_EMOTIONS}")

DEFAULT_MOODS: MoodState = {
    "hype": 0.25,
    "chill": 0.25,
    "snarky": 0.25,
    "supportive": 0.25,
}
log.debug(f"[DEBUG] DEFAULT_MOODS loaded: {DEFAULT_MOODS}")

# Emotion intensity thresholds
EMOTION_THRESHOLD_LOW = 0.3
EMOTION_THRESHOLD_MEDIUM = 0.6
EMOTION_THRESHOLD_HIGH = 0.8
log.debug(
    f"[DEBUG] Emotion thresholds: low={EMOTION_THRESHOLD_LOW}, "
    f"medium={EMOTION_THRESHOLD_MEDIUM}, high={EMOTION_THRESHOLD_HIGH}"
)

# Mood transition rates
MOOD_DECAY_RATE = 0.1
MOOD_TRANSITION_SPEED = 0.3
log.debug(
    f"[DEBUG] Mood constants: decay={MOOD_DECAY_RATE}, transition_speed={MOOD_TRANSITION_SPEED}"
)

