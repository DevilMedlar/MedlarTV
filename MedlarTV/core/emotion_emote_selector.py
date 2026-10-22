"""
MedlarTV Emotion-Aware Emote Selector
Automatically chooses emotes based on current emotional state
"""

import random
from typing import List, Optional
from collections.abc import Mapping
import logging
log = logging.getLogger("emotion_emotes")
log.setLevel(logging.DEBUG)
log.debug("[DEBUG] emotion_emote_selector loaded")

# Emotion-to-Emote mapping
EMOTION_EMOTE_MAP = {
    # Core Emotions
    "happiness": {
        "twitch": ["PogChamp", "VoHiYo", "CoolCat"],
        "unicode": ["😊", "😀", "😁", "💖", "✨"]
    },
    "sadness": {
        "twitch": ["BibleThump", "NotLikeThis"],
        "unicode": ["😢", "😔", "😞", "💔"]
    },
    "anger": {
        "twitch": ["DansGame", "ResidentSleeper"],
        "unicode": ["😠", "😡", "💢", "🤬"]
    },
    "fear": {
        "twitch": ["NotLikeThis", "BibleThump"],
        "unicode": ["😨", "😰", "😱", "🙀"]
    },
    
    # Social Emotions
    "excitement": {
        "twitch": ["PogChamp", "Kreygasm", "PogChamp"],
        "unicode": ["🔥", "⚡", "🚀", "💥", "✨"]
    },
    "gratitude": {
        "twitch": ["VoHiYo", "CoolCat"],
        "unicode": ["💖", "💕", "🙏", "✨", "💝"]
    },
    "jealousy": {
        "twitch": ["DansGame", "ResidentSleeper"],
        "unicode": ["😒", "🙄", "😤"]
    },
    "pride": {
        "twitch": ["PogChamp", "CoolCat"],
        "unicode": ["🏆", "👑", "💪", "⭐", "🌟"]
    },
    
    # Mood States
    "chill": {
        "twitch": ["CoolCat", "Kappa"],
        "unicode": ["😌", "🌙", "💫", "🫶", "✌️"]
    },
    "supportive": {
        "twitch": ["VoHiYo", "CoolCat"],
        "unicode": ["💖", "🌟", "✨", "💪", "🤗"]
    },
    "snarky": {
        "twitch": ["Kappa", "4Head", "LUL"],
        "unicode": ["😏", "🙃", "😉", "😈", "🤨"]
    },
    "affection": {
        "twitch": ["VoHiYo", "CoolCat"],
        "unicode": ["❤️", "💖", "💕", "💓", "💞"]
    },
    "romance": {
        "twitch": ["VoHiYo", "CoolCat"],
        "unicode": ["💘", "💝", "💌", "💞", "💗"]
    },
    "attraction": {
        "twitch": ["PogChamp", "Kreygasm"],
        "unicode": ["😍", "🥰", "✨", "💫", "⭐"]
    },
    "arousal": {
        "twitch": ["PogChamp", "Kreygasm"],
        "unicode": ["🔥", "⚡", "💥", "🚀", "✨"]
    },
    
    # Energy States
    "energetic": {
        "twitch": ["PogChamp", "Kreygasm"],
        "unicode": ["⚡", "🔥", "💪", "🚀", "✨"]
    },
    "tired": {
        "twitch": ["ResidentSleeper", "NotLikeThis"],
        "unicode": ["😴", "💤", "😪", "🥱"]
    },
    "stressed": {
        "twitch": ["NotLikeThis", "DansGame"],
        "unicode": ["😰", "😓", "😤", "😫"]
    },
    
    # Connection States
    "lonely": {
        "twitch": ["BibleThump", "NotLikeThis"],
        "unicode": ["😔", "💔", "😢", "🥺"]
    },
    "connected": {
        "twitch": ["VoHiYo", "CoolCat"],
        "unicode": ["🤝", "💖", "✨", "🌟", "💕"]
    }
}

# Allowed Twitch emotes (explicit whitelist to avoid 7TV/BTTV/FFZ)
ALLOWED_TWITCH_EMOTES: List[str] = [
    "PogChamp", "Kappa", "LUL", "NotLikeThis", "BibleThump",
    "VoHiYo", "CoolCat", "DansGame", "ResidentSleeper", "4Head",
    "Kreygasm", "KappaPride", "Keepo"
]

# Fallback emotes for unknown emotions
DEFAULT_EMOTES = {
    "twitch": ["PogChamp", "Kappa", "CoolCat"],
    "unicode": ["💬", "✨", "🎮", "🎯"]
}


def get_emotion_emote(emotion: str | None, emote_type: str = "twitch", available_emotes: Optional[List[str]] = None) -> str:
    log.debug(f"[EMOTE] get_emotion_emote(emotion='{emotion}', type='{emote_type}')")

    """
    Get an appropriate emote for the given emotion.
    
    Args:
        emotion: Current emotional state (e.g., "happiness", "excitement")
        emote_type: "twitch" for Twitch emotes, "unicode" for emoji
        available_emotes: List of emotes available in the channel (optional)

    Returns:
        A single emote string
    """
    # Get emote pool for this emotion
    if emotion in EMOTION_EMOTE_MAP:
        emote_pool = EMOTION_EMOTE_MAP[emotion].get(emote_type, [])
    else:
        emote_pool = DEFAULT_EMOTES.get(emote_type, [])
    
    # If available_emotes provided, filter strictly to allowed Twitch emotes
    if available_emotes and emote_type == "twitch":
        allowed_set = set(ALLOWED_TWITCH_EMOTES)
        available_allowed = [e for e in available_emotes if e in allowed_set]
        emote_pool = [e for e in emote_pool if e in available_allowed]

        # If none from the emotion map are available, fall back to allowed emotes only
        if not emote_pool and available_allowed:
            emote_pool = available_allowed

    # Return random emote from pool
    if emote_pool:
        chosen = random.choice(emote_pool)
        log.debug(f"[EMOTE] Selected emote='{chosen}' from pool={emote_pool}")
        return chosen
    
    # Absolute fallback
    return "PogChamp" if emote_type == "twitch" else "💬"


def add_emotion_emote(message: str, emotion: str | None, available_emotes: Optional[List[str]] = None,
                      use_unicode: bool = False) -> str:
    log.debug("[ADD_EMOTE] emotion=%s message_length=%d", emotion, len(message))

    """
    Add an emotion-appropriate emote to a message.
    
    Args:
        message: The message text
        emotion: Current emotional state
        available_emotes: List of available Twitch emotes (optional)
        use_unicode: If True, use unicode emoji instead of Twitch emotes
    
    Returns:
        Message with emote appended
    """
    emote_type = "unicode" if use_unicode else "twitch"
    emote = get_emotion_emote(emotion, emote_type, available_emotes)
    return f"{message} {emote}"


def get_multiple_emotion_emotes(emotions: Mapping[str, float], count: int = 2, emote_type: str = "twitch",
                                available_emotes: Optional[List[str]] = None) -> List[str]:
    log.debug(f"[MULTI] Building multi-emote for emotions={emotions} count={count}")

    """
    Get multiple emotes representing a complex emotional state.
    
    Args:
        emotions: Dict of emotions with their values (e.g., {"happiness": 0.7, "excitement": 0.5})
        count: Number of emotes to return
        emote_type: "twitch" or "unicode"
        available_emotes: List of available Twitch emotes

    Returns:
        List of emote strings
    """
    # Sort emotions by value (highest first)
    sorted_emotions = sorted(emotions.items(), key=lambda x: x[1], reverse=True)
    
    # Get emotes for top emotions
    selected_emotes: list[str] = []
    for emotion, value in sorted_emotions[:count]:
        log.debug(f"[MULTI] Loop: considering emotion='{emotion}', value={value}")
        if value > 0.3:  # Only include emotions above 30%
            emote = get_emotion_emote(emotion, emote_type, available_emotes)
            if emote not in selected_emotes:  # Avoid duplicates
                selected_emotes.append(emote)
    
    # Fill up to count if needed
    while len(selected_emotes) < count:
        fallback = get_emotion_emote("chill", emote_type, available_emotes)
        if fallback not in selected_emotes:
            selected_emotes.append(fallback)
        else:
            break
    log.debug(f"[MULTI] Selected emotes={selected_emotes}")

    return selected_emotes[:count]


def format_message_with_emotions(message: str, emotion_state: Mapping[str, float],
                                 available_emotes: Optional[List[str]] = None,
                                 max_emotes: int = 2) -> str:
    log.debug("[FORMAT] message_length=%d emotion_count=%d", len(message), len(emotion_state))

    """
    Format a message with emotion-appropriate emotes.
    
    Args:
        message: The message text
        emotion_state: Dict of all emotion values
        available_emotes: List of available Twitch emotes
        max_emotes: Maximum number of emotes to add
    
    Returns:
        Formatted message with emotes
    """
    # Get top emotions
    top_emotions = dict(sorted(emotion_state.items(), key=lambda x: x[1], reverse=True)[:3])
    
    # Get appropriate emotes
    emotes = get_multiple_emotion_emotes(top_emotions, max_emotes, "twitch", available_emotes)
    
    # Add emotes to message
    if emotes:
        emote_str = " ".join(emotes)
        return f"{message} {emote_str}"

    log.debug(f"[FORMAT] No-emote case: returning original message")
    return message

# Integration example
def integrate_with_emotional_system():
    """
    Example of how to integrate with the emotional system
    """
    from MedlarTV.core.emotional_system import get_emotion_state, get_current_emotion
    from MedlarTV.core.twitch_events import load_channel_emotes as load_channel_emotes, load_global_emotes as load_global_emotes
    
    # Get current emotional state
    emotion_state = get_emotion_state()
    dominant_emotion = get_current_emotion()
    
    # Load available emotes (you'd do this once at startup)
    # token = os.getenv("TWITCH_TOKEN")
    # broadcaster_id = "your_broadcaster_id"
    # available_emotes = load_global_emotes(token) + load_channel_emotes(token, broadcaster_id)
    available_emotes = ["PogChamp", "Kappa", "LUL", "CoolCat"]
    
    # Generate response
    message = "Thanks for being here!"
    
    # Add emotion-appropriate emote
    formatted_message = add_emotion_emote(message, dominant_emotion, available_emotes)
    print(f"Simple: {formatted_message}")
    
    # Or use full emotion state for richer expression
    rich_message = format_message_with_emotions(message, emotion_state, available_emotes, max_emotes=2)
    print(f"Rich: {rich_message}")


# Example usage
if __name__ == "__main__":
    print("=" * 60)
    print("Emotion-Aware Emote Selector - Demo")
    print("=" * 60)
    
    # Simulate different emotional states
    test_emotions = [
        ("happiness", {"happiness": 0.8, "excitement": 0.5}),
        ("excitement", {"excitement": 0.9, "happiness": 0.6, "energetic": 0.7}),
        ("sadness", {"sadness": 0.6, "lonely": 0.4}),
        ("chill", {"chill": 0.7, "connected": 0.5}),
        ("snarky", {"snarky": 0.6, "anger": 0.3}),
    ]
    
    available = ["PogChamp", "Kappa", "LUL", "CoolCat", "BibleThump"]
    
    for emotion, state in test_emotions:
        print(f"\n--- {emotion.upper()} ---")
        
        # Simple emote selection
        emote = get_emotion_emote(emotion, "twitch", available)
        print(f"Selected Twitch emote: {emote}")
        
        # Unicode emoji
        emoji = get_emotion_emote(emotion, "unicode")
        print(f"Selected Unicode emoji: {emoji}")
        
        # Add to message
        message = "Great stream today!"
        formatted = add_emotion_emote(message, emotion, available)
        print(f"Formatted: {formatted}")
        
        # Rich multi-emote
        rich = format_message_with_emotions(message, state, available, max_emotes=2)
        print(f"Rich format: {rich}")
