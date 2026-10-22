from __future__ import annotations

import os
DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"
if DEBUG:
    print("[DEBUG mood_system] Loaded mood_system.py")

import logging
from collections.abc import Callable, Iterable
from typing import Dict, Any

log = logging.getLogger("mood")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _avg(values: Iterable[float | None]) -> float:
    vals = [
        v
        for v in values
        if v is not None
    ]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


# This is the structure we use everywhere for moods
MoodVector = Dict[str, float]


def compute_mood(emotions: Dict[str, float]) -> Dict[str, Any]:
    """
    Take all emotion intensities and compress them into a single vector
    with:
        - valence  (-1..1)  → how positive/negative overall
        - energy   (-1..1)  → how hyped vs drained
        - warmth   (0..1)   → how supportive / caring
        - snark    (0..1)   → how sarcastic / spicy

    The caller (MedlarTV brain) turns this into a label + style.
    """
    if DEBUG:
        print(f"[DEBUG mood_system] compute_mood() called with emotions={emotions}")

    # Pull all known emotions with safe defaults
    g: Callable[[str], float] = lambda k: float(emotions.get(k, 0.0))

    # Core valence-related emotions
    happiness = g("happiness")
    gratitude = g("gratitude")
    pride = g("pride")
    excitement = g("excitement")

    sadness = g("sadness")
    loneliness = g("lonely")
    grief = g("grief")
    disappointment = g("disappointment")

    # Anger / frustration cluster
    anger = g("anger")
    annoyance = g("annoyance")
    jealousy = g("jealousy")
    resentment = g("resentment")
    betrayal = g("betrayal")

    # Fear / anxiety cluster
    fear = g("fear")
    anxiety = g("anxiety")
    stress = g("stressed")
    insecurity = g("insecure")

    # Energy-related emotions
    energetic = g("energetic")
    hype = g("hype") if "hype" in emotions else energetic
    tired = g("tired")
    drained = g("drained")
    bored = g("bored")
    arousal = g("arousal")

    # Warmth / support cluster
    supportive = g("supportive")
    compassion = g("compassion")
    affection = g("affection")
    connected = g("connected")
    calm = g("calm") if "calm" in emotions else 0.0

    # Snark / chaos cluster
    snarky = g("snarky")
    playful = g("playful")
    chaotic = g("chaotic")
    mischief = g("mischief")

    # ------------------------------------------------------------------
    # VALENCE
    # ------------------------------------------------------------------
    positive_valence = _avg([
        happiness,
        gratitude,
        pride,
        excitement,
        connected,
        supportive,
        affection,
        compassion,
        calm,
    ])

    negative_valence = _avg([
        sadness,
        loneliness,
        grief,
        disappointment,
        anger,
        annoyance,
        jealousy,
        resentment,
        betrayal,
        fear,
        anxiety,
        stress,
        insecurity,
    ])

    valence_raw = positive_valence - negative_valence
    valence = _clamp(valence_raw, -1.0, 1.0)

    # ------------------------------------------------------------------
    # ENERGY
    # ------------------------------------------------------------------
    high_energy = _avg([
        energetic,
        hype,
        excitement,
        anger,
        anxiety,
        stress,
        chaotic,
        mischief,
        arousal,
    ])

    low_energy = _avg([
        tired,
        drained,
        bored,
        sadness,
        loneliness,
    ])

    energy_raw = high_energy - low_energy
    energy = _clamp(energy_raw, -1.0, 1.0)

    # ------------------------------------------------------------------
    # WARMTH
    # ------------------------------------------------------------------
    warmth_positive = _avg([
        supportive,
        compassion,
        affection,
        connected,
        gratitude,
        calm,
        g("romance"),
        g("attraction"),
    ])

    warmth_negative = _avg([
        jealousy,
        resentment,
        betrayal,
        annoyance,
        anger,
    ])

    warmth_raw = warmth_positive - (0.5 * warmth_negative)
    warmth = _clamp(warmth_raw, 0.0, 1.0)

    # ------------------------------------------------------------------
    # SNARK
    # ------------------------------------------------------------------
    snark_sources = _avg([
        snarky,
        playful,
        chaotic,
        mischief,
        annoyance,
        jealousy,
    ])

    # Warmth tempers snark: very warm → less biting
    snark_raw = snark_sources * (1.0 - 0.4 * warmth)
    snark = _clamp(snark_raw, 0.0, 1.0)

    mood_vector: MoodVector = {
        "valence": valence,
        "energy": energy,
        "warmth": warmth,
        "snark": snark,
    }

    log.debug(
        "[Mood] compute_mood → "
        f"valence={valence:.2f}, energy={energy:.2f}, "
        f"warmth={warmth:.2f}, snark={snark:.2f}"
    )
    if DEBUG:
        print(f"[DEBUG mood_system] compute_mood() mood_vector before labeling: {mood_vector}")

    # Also derive a coarse label for convenience
    label = get_mood_label(mood_vector, emotions)
    mood_vector_with_label: Dict[str, Any] = dict(mood_vector)
    mood_vector_with_label["label"] = label

    log.info(
        f"[Mood] Final mood: label={label}, "
        f"valence={valence:.2f}, energy={energy:.2f}, "
        f"warmth={warmth:.2f}, snark={snark:.2f}"
    )
    if DEBUG:
        print(f"[DEBUG mood_system] compute_mood() returning mood_vector: {mood_vector_with_label}")

    return mood_vector_with_label


def get_mood_label(mood: Dict[str, float], emotions: Dict[str, float]) -> str:
    """
    Turn the numeric mood vector + some raw emotions into a human label
    like "Chill & Supportive", "Hyped & Chaotic", etc.
    """
    v = float(mood.get("valence", 0.0))
    e = float(mood.get("energy", 0.0))
    w = float(mood.get("warmth", 0.0))
    s = float(mood.get("snark", 0.0))

    if DEBUG:
        print(f"[DEBUG mood_system] get_mood_label() called with valence={v:.2f}, energy={e:.2f}, warmth={w:.2f}, snark={s:.2f}")

    # For some special cases, look directly at raw emotions
    g: Callable[[str], float] = lambda k: float(emotions.get(k, 0.0))

    # --------------------------------
    # Sadness / grief cases
    # --------------------------------
    grief = g("grief")
    sadness = g("sadness")
    loneliness = g("lonely")

    if grief > 0.6 or (sadness > 0.6 and loneliness > 0.4):
        # Very heavy stuff → grief label
        if DEBUG:
            print('[DEBUG mood_system] get_mood_label(): returning "Grief / Overwhelmed"')
        return "Grief / Overwhelmed"

    # --------------------------------
    # Highly positive + hyped
    # --------------------------------
    if v > 0.4 and e > 0.4:
        if s > 0.5:
            if DEBUG:
                print('[DEBUG mood_system] get_mood_label(): returning "Hyper & Chaotic"')
            return "Hyper & Chaotic"
        if DEBUG:
            print('[DEBUG mood_system] get_mood_label(): returning "Hyped & Excited"')
        return "Hyped & Excited"

    # --------------------------------
    # Positive but low energy
    # --------------------------------
    if v > 0.3 and e < 0.1:
        if w > 0.6:
            if DEBUG:
                print('[DEBUG mood_system] get_mood_label(): returning "Cozy & Supportive"')
            return "Cozy & Supportive"
        if DEBUG:
            print('[DEBUG mood_system] get_mood_label(): returning "Chill & Content"')
        return "Chill & Content"

    # --------------------------------
    # Negative valence, high energy
    # --------------------------------
    anger = g("anger")
    annoyance = g("annoyance")
    stress = g("stressed")
    anxiety = g("anxiety")

    if v < -0.3 and e > 0.2:
        if anger > 0.5 or annoyance > 0.5:
            if s > 0.5:
                if DEBUG:
                    print('[DEBUG mood_system] get_mood_label(): returning "Spicy / Ranty"')
                return "Spicy / Ranty"
            if DEBUG:
                print('[DEBUG mood_system] get_mood_label(): returning "Frustrated"')
            return "Frustrated"
        if stress > 0.5 or anxiety > 0.5:
            if DEBUG:
                print('[DEBUG mood_system] get_mood_label(): returning "Stressed / Overloaded"')
            return "Stressed / Overloaded"

    # --------------------------------
    # Negative valence, low energy
    # --------------------------------
    if v < -0.3 and e < 0.0:
        if sadness > 0.5 or loneliness > 0.5:
            if DEBUG:
                print('[DEBUG mood_system] get_mood_label(): returning "Low & Sad"')
            return "Low & Sad"
        if DEBUG:
            print('[DEBUG mood_system] get_mood_label(): returning "Drained / Meh"')
        return "Drained / Meh"

    # --------------------------------
    # Neutral valence but spicy snark
    # --------------------------------
    if abs(v) < 0.2 and s > 0.6:
        if DEBUG:
            print('[DEBUG mood_system] get_mood_label(): returning "Snarky & Playful"')
        return "Snarky & Playful"

    # --------------------------------
    # High warmth & moderate energy
    # --------------------------------
    if w > 0.6 and e > 0.1:
        if DEBUG:
            print('[DEBUG mood_system] get_mood_label(): returning "Encouraging & Pumped"')
        return "Encouraging & Pumped"

    # --------------------------------
    # Warmth dominant, low energy
    # --------------------------------
    if w > 0.6 and e < 0.1 and v >= 0.0:
        if DEBUG:
            print('[DEBUG mood_system] get_mood_label(): returning "Calm & Supportive"')
        return "Calm & Supportive"

    # --------------------------------
    # Mixed/ambiguous defaults
    # --------------------------------
    if abs(v) < 0.2 and abs(e) < 0.2:
        if w > 0.5:
            if DEBUG:
                print('[DEBUG mood_system] get_mood_label(): returning "Calm & Supportive"')
            return "Calm & Supportive"
        if DEBUG:
            print('[DEBUG mood_system] get_mood_label(): returning "Neutral"')
        return "Neutral"

    # Last-resort safety net
    if DEBUG:
        print('[DEBUG mood_system] get_mood_label(): returning "Mixed"')
    return "Mixed"
