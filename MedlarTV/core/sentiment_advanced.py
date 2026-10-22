import os
DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"
if DEBUG:
    print("[DEBUG sentiment_advanced] Loaded sentiment_advanced.py")

"""
MedlarTV Enhanced Sentiment Analysis
Works with the advanced emotional system
"""

import re
from typing import Dict, Tuple

# --- Expanded Sentiment Lexicon ---
POSITIVE_WORDS = [
    "good", "great", "awesome", "amazing", "love", "wonderful", "fantastic",
    "excellent", "perfect", "best", "brilliant", "outstanding", "superb",
    "happy", "joy", "excited", "yay", "nice", "cool", "sweet", "fun",
    "pogchamp", "pog", "lit", "fire", "hype", "epic", "legendary"
]

NEGATIVE_WORDS = [
    "bad", "terrible", "awful", "horrible", "worst", "hate", "suck",
    "sad", "depressed", "cry", "hurt", "pain", "miss", "lonely",
    "angry", "mad", "annoyed", "frustrating", "ugh", "annoying",
    "tired", "exhausted", "stressed", "overwhelmed", "anxious", "worried",
    "scared", "afraid", "fear", "nervous", "disappointed", "lame"
]

INTENSIFIERS = [
    "very", "so", "super", "really", "extremely", "incredibly", "absolutely",
    "totally", "completely", "utterly", "exceptionally", "particularly"
]

NEGATIONS = ["not", "no", "never", "neither", "nobody", "nothing", "don't", "won't", "can't"]

# Emotion-specific word clusters
EMOTION_WORDS = {
    "happiness": ["happy", "joy", "cheerful", "glad", "pleased", "delighted", "smile", "laugh"],
    "sadness": ["sad", "unhappy", "depressed", "miserable", "gloomy", "down", "cry"],
    "anger": ["angry", "mad", "furious", "annoyed", "irritated", "pissed", "rage"],
    "fear": ["scared", "afraid", "fearful", "anxious", "worried", "nervous", "terrified"],
    "excitement": ["excited", "thrilled", "hyped", "pumped", "stoked", "omg", "wow"],
    "gratitude": ["thanks", "thank you", "grateful", "appreciate", "thankful"],
    "jealousy": ["jealous", "envy", "envious", "wish i had"],
    "pride": ["proud", "accomplished", "achievement", "nailed it", "crushed it"],
    "affection": ["love", "loving", "adore", "affection", "heart", "caring", "sweetheart"],
    "romance": ["romance", "romantic", "date", "valentine", "kiss"],
    "attraction": ["attractive", "hot", "cute", "handsome", "pretty", "sexy"],
    "arousal": ["arousal", "aroused", "horny", "turned on"],
}

# Emoji sentiment mapping
EMOJI_SENTIMENT = {
    "😊": 0.5, "😀": 0.6, "😁": 0.7, "🤣": 0.8, "❤️": 0.7, "💖": 0.7,
    "😢": -0.5, "😭": -0.7, "😞": -0.4, "😔": -0.4,
    "😠": -0.6, "😡": -0.8, "🤬": -0.9,
    "😱": -0.6, "😨": -0.5, "😰": -0.5,
    "🔥": 0.6, "⚡": 0.5, "✨": 0.4, "💯": 0.6,
    "🙄": -0.3, "😒": -0.4, "😤": -0.5,
}


def analyze_sentiment_advanced(message: str) -> Tuple[float, Dict[str, float]]:
    if DEBUG:
        print(f"[DEBUG sentiment_advanced] analyze_sentiment_advanced() message_length={len(message)}")

    text = message.lower()
    if DEBUG:
        print(f"[DEBUG sentiment_advanced] normalized_length={len(text)}")

    words = re.findall(r'\b\w+\b', text)
    if DEBUG:
        print(f"[DEBUG sentiment_advanced] token_count={len(words)}")

    score = 0.0
    word_count = 0

    negated = False

    for i, word in enumerate(words):
        if DEBUG:
            print(f"[DEBUG sentiment_advanced] scanning_word index={i}")

        if i > 0 and words[i - 1] in NEGATIONS:
            negated = True
            if DEBUG:
                print("[DEBUG sentiment_advanced] word_is_negated=True")
        else:
            negated = False

        if word in POSITIVE_WORDS:
            if DEBUG:
                print(f"[DEBUG sentiment_advanced] POSITIVE match negated={negated}")
            word_count += 1
            score += -1 if negated else 1

        elif word in NEGATIVE_WORDS:
            if DEBUG:
                print(f"[DEBUG sentiment_advanced] NEGATIVE match negated={negated}")
            word_count += 1
            score += 1 if negated else -1

        elif word in INTENSIFIERS and i < len(words) - 1:
            next_word = words[i + 1]
            if DEBUG:
                print("[DEBUG sentiment_advanced] INTENSIFIER match")
            if next_word in POSITIVE_WORDS:
                score += 0.5
            elif next_word in NEGATIVE_WORDS:
                score -= 0.5

    emoji_score = 0.0
    emoji_count = 0

    for emoji, value in EMOJI_SENTIMENT.items():
        if emoji in message:
            if DEBUG:
                print(f"[DEBUG sentiment_advanced] emoji_detected value={value}")
            emoji_score += value
            emoji_count += 1

    if DEBUG:
        print(f"[DEBUG sentiment_advanced] score_words={score} count_words={word_count}")
        print(f"[DEBUG sentiment_advanced] emoji_score={emoji_score} emoji_count={emoji_count}")

    total_count = word_count + emoji_count
    if total_count > 0:
        combined_score = (score + emoji_score) / total_count
    else:
        combined_score = 0.0

    overall_sentiment = max(-1.0, min(1.0, combined_score))

    if DEBUG:
        print(f"[DEBUG sentiment_advanced] overall_sentiment={overall_sentiment}")

    emotion_scores: dict[str, float] = {}

    for emotion, emotion_keywords in EMOTION_WORDS.items():
        matches = sum(1 for keyword in emotion_keywords if keyword in text)
        if matches > 0:
            if DEBUG:
                print(f"[DEBUG sentiment_advanced] emotion_detected={emotion} matches={matches}")
            strength = min(matches * 0.15, 0.5)

            if emotion in ["happiness", "excitement", "gratitude", "pride"]:
                if overall_sentiment > 0:
                    strength *= (1 + overall_sentiment * 0.5)

            elif emotion in ["sadness", "anger", "fear"]:
                if overall_sentiment < 0:
                    strength *= (1 + abs(overall_sentiment) * 0.5)

            strength = min(strength, 1.0)
            emotion_scores[emotion] = strength

            if DEBUG:
                print(f"[DEBUG sentiment_advanced] emotion_strength[{emotion}]={strength}")

    if DEBUG:
        print(f"[DEBUG sentiment_advanced] final_emotion_scores={emotion_scores}")

    return overall_sentiment, emotion_scores


def analyze_sentiment_simple(message: str) -> float:
    if DEBUG:
        print(f"[DEBUG sentiment_advanced] analyze_sentiment_simple() called")
    sentiment, _ = analyze_sentiment_advanced(message)
    if DEBUG:
        print(f"[DEBUG sentiment_advanced] simple_sentiment={sentiment}")
    return sentiment


def detect_emotional_keywords(message: str) -> Dict[str, int]:
    if DEBUG:
        print(f"[DEBUG sentiment_advanced] detect_emotional_keywords() message_length={len(message)}")

    text = message.lower()
    detected: dict[str, int] = {}

    for emotion, keywords in EMOTION_WORDS.items():
        matches = sum(1 for keyword in keywords if keyword in text)
        if matches > 0:
            detected[emotion] = matches
            if DEBUG:
                print(f"[DEBUG sentiment_advanced] keyword_detected emotion={emotion} matches={matches}")

    return detected


def get_sentiment_description(sentiment: float) -> str:
    if DEBUG:
        print(f"[DEBUG sentiment_advanced] get_sentiment_description() sentiment={sentiment}")

    if sentiment >= 0.7:
        return "very positive"
    elif sentiment >= 0.3:
        return "positive"
    elif sentiment >= -0.3:
        return "neutral"
    elif sentiment >= -0.7:
        return "negative"
    else:
        return "very negative"
