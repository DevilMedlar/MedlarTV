"""
MedlarTV Fuzzy Trigger Detection
Detects mentions of MedlarTV even with typos, case variations, and misspellings.
Now reads trigger words from personality.yaml!
"""

import re
import yaml
import os
from collections.abc import Collection
from pathlib import Path
from difflib import SequenceMatcher
if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
    print("[DEBUG fuzzy_trigger] Loaded fuzzy_trigger.py")

def load_trigger_keywords() -> Collection[str]:
    """Load trigger keywords from personality.yaml"""
    from typing import cast
    from collections.abc import Mapping

    config_path = Path("MedlarTV/config/personality.yaml")
    
    if not config_path.exists():
        # Fallback to defaults if file doesn't exist
        return ["medlartv", "medlar"]
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = cast(Mapping[str, object], yaml.safe_load(f) or {})
        
        personality = cast(Mapping[str, object], config.get("personality", {}))
        triggers = cast(Collection[str], personality.get("trigger_keywords", []))
        
        # Return triggers if found, otherwise use defaults
        if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
            print(f"[DEBUG fuzzy_trigger] Trigger keyword count={len(triggers)}")
        return triggers if triggers else ["medlartv", "medlar"]

    except Exception as e:
        print(f"[Fuzzy Trigger] Error loading personality.yaml: {type(e).__name__}")
        return ["medlartv", "medlar"]

def calculate_similarity(a: str, b: str) -> float:
    """Calculate similarity ratio between two strings (0.0 to 1.0)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def normalize_text(text: str) -> str:
    """Normalize text by removing special characters and extra spaces."""
    # Remove special characters except spaces
    text = re.sub(r'[^a-z0-9\s]', '', text.lower())
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def check_keyboard_distance(word: str, target: str, max_distance: int = 1) -> bool:
    """
    Check if word is close to target considering keyboard layout.
    Helps detect accidental key presses (e.g., nedlar instead of medlar).
    """
    # Keyboard adjacency map (QWERTY layout)
    keyboard = {
        'q': ['w', 'a'], 'w': ['q', 'e', 's'], 'e': ['w', 'r', 'd'],
        'r': ['e', 't', 'f'], 't': ['r', 'y', 'g'], 'y': ['t', 'u', 'h'],
        'u': ['y', 'i', 'j'], 'i': ['u', 'o', 'k'], 'o': ['i', 'p', 'l'],
        'p': ['o', 'l'],
        'a': ['q', 's', 'z'], 's': ['a', 'w', 'd', 'x'], 'd': ['s', 'e', 'f', 'c'],
        'f': ['d', 'r', 'g', 'v'], 'g': ['f', 't', 'h', 'b'], 'h': ['g', 'y', 'j', 'n'],
        'j': ['h', 'u', 'k', 'm'], 'k': ['j', 'i', 'l'], 'l': ['k', 'o', 'p'],
        'z': ['a', 'x'], 'x': ['z', 's', 'c'], 'c': ['x', 'd', 'v'],
        'v': ['c', 'f', 'b'], 'b': ['v', 'g', 'n'], 'n': ['b', 'h', 'm'],
        'm': ['n', 'j']
    }
    
    if len(word) != len(target):
        return False
    
    differences = 0
    for w_char, t_char in zip(word.lower(), target.lower()):
        if w_char != t_char:
            differences += 1
            if differences > max_distance:
                return False
            # Every changed position must independently be an adjacent key.
            if t_char not in keyboard.get(w_char, []):
                return False
    
    return differences <= max_distance

def check_missing_letters(word: str, target: str, max_missing: int = 2) -> bool:
    """
    Check if word is target with missing letters.
    Example: medltv -> medlartv (missing 'ar')
    """
    if len(word) >= len(target):
        return False
    
    word_lower = word.lower()
    target_lower = target.lower()
    
    # Try to match word as subsequence of target
    target_idx = 0
    for char in word_lower:
        while target_idx < len(target_lower) and target_lower[target_idx] != char:
            target_idx += 1
        if target_idx >= len(target_lower):
            return False
        target_idx += 1
    
    missing = len(target) - len(word)
    return missing <= max_missing

def check_extra_letters(word: str, target: str, max_extra: int = 2) -> bool:
    """
    Check if word is target with extra letters.
    Example: meedlartv -> medlartv (extra 'e')
    """
    if len(word) <= len(target):
        return False
    
    return check_missing_letters(target, word, max_extra)

def check_swapped_letters(word: str, target: str) -> bool:
    """
    Check if word has adjacent letters swapped.
    Example: mdelartv -> medlartv (swapped 'ed')
    """
    if len(word) != len(target):
        return False
    
    word_lower = word.lower()
    target_lower = target.lower()
    
    swaps = 0
    i = 0
    while i < len(word_lower):
        if word_lower[i] != target_lower[i]:
            # Check if next chars are swapped
            if (i + 1 < len(word_lower) and 
                word_lower[i] == target_lower[i + 1] and 
                word_lower[i + 1] == target_lower[i]):
                swaps += 1
                i += 2  # Skip both swapped chars
                continue
            else:
                return False
        i += 1
    
    return swaps <= 2

def is_trigger_word(word: str, strict: bool = False) -> bool:
    """
    Main function to check if a word is a trigger.
    
    Args:
        word: The word to check
        strict: If True, only check exact matches and close variations
    
    Returns:
        True if word should trigger MedlarTV
    """
    normalized = normalize_text(word)
    
    # Empty or too short
    if not normalized or len(normalized) < 3:
        return False
    
    # Load triggers from personality.yaml
    triggers = load_trigger_keywords()
    
    # --- EXACT MATCHES ---
    # Check triggers (exact match, case-insensitive)
    for trigger in triggers:
        if normalized == normalize_text(trigger):
            return True
    
    # --- FUZZY MATCHES ---
    if strict:
        return False
    
    # Check each trigger for fuzzy matches
    for trigger in triggers:
        trigger_norm = normalize_text(trigger)
        
        # High similarity (>= 85%)
        if calculate_similarity(normalized, trigger_norm) >= 0.85:
            print("[DEBUG fuzzy_trigger] TRIGGER: similarity match")
            return True
        
        # Keyboard typos (1 key away)
        if check_keyboard_distance(normalized, trigger_norm, max_distance=1):
            print("[DEBUG fuzzy_trigger] TRIGGER: keyboard distance")
            return True
        
        # Missing letters (up to 2)
        if check_missing_letters(normalized, trigger_norm, max_missing=2):
            print("[DEBUG fuzzy_trigger] TRIGGER: missing letters")
            return True
        
        # Extra letters (up to 2)
        if check_extra_letters(normalized, trigger_norm, max_extra=2):
            print("[DEBUG fuzzy_trigger] TRIGGER: extra letters")
            return True
        
        # Swapped adjacent letters
        if check_swapped_letters(normalized, trigger_norm):
            print("[DEBUG fuzzy_trigger] TRIGGER: swapped letters")
            return True
    
    return False

def find_triggers_in_message(message: str, strict: bool = False) -> list[str]:
    """
    Find all trigger words in a message.
    
    Args:
        message: The chat message to check
        strict: If True, only find exact/close matches
    
    Returns:
        List of detected trigger words
    """
    # Split message into words
    words = re.findall(r'\b\w+\b', message)
    print(f"[DEBUG fuzzy_trigger] Scanning message_length={len(message)}")
    
    # Also check multi-word combinations
    message_normalized = normalize_text(message)
    
    triggers_found: list[str] = []
    
    # Check individual words
    for word in words:
        if is_trigger_word(word, strict):
            print("[DEBUG fuzzy_trigger] FOUND trigger word")
            triggers_found.append(word)
    
    # Check multi-word triggers from personality.yaml
    trigger_keywords = load_trigger_keywords()
    for trigger in trigger_keywords:
        if ' ' in trigger:  # Multi-word trigger
            trigger_norm = normalize_text(trigger)
            if trigger_norm in message_normalized:
                triggers_found.append(trigger)
    
    return triggers_found

def should_respond(message: str, strict: bool = False) -> bool:
    """
    Determine if MedlarTV should respond to this message.
    """
    print(f"[DEBUG fuzzy_trigger] should_respond() called with message_length={len(message)} (strict={strict})")

    # Run the actual detection
    found = find_triggers_in_message(message, strict)

    print(f"[DEBUG fuzzy_trigger] should_respond → {len(found) > 0} (count={len(found)})")

    return len(found) > 0

# --- TESTING / DEMO ---
def test_fuzzy_trigger() -> None:
    """Test cases for fuzzy trigger detection."""
    print("=" * 60)
    print("MedlarTV Fuzzy Trigger Detection - Test Results")
    print("=" * 60)
    
    # Load triggers from personality.yaml
    triggers = load_trigger_keywords()
    print(f"\nLoaded triggers from personality.yaml: {triggers}\n")
    
    test_cases = [
        # (input, should_trigger, description)
        ("medlartv", True, "Exact match"),
        ("MEDLARTV", True, "All uppercase"),
        ("MeDlArTv", True, "Mixed case"),
        ("medlar", True, "Short form"),
        ("@medlartv", True, "With @ symbol"),
        ("medlartv!", True, "With punctuation"),
        ("medlrtv", True, "Missing 'a'"),
        ("medltv", True, "Missing 'ar'"),
        ("meedlartv", True, "Extra 'e'"),
        ("medlarttvv", True, "Extra letters at end"),
        ("mdelartv", True, "Swapped 'de'"),
        ("nedlartv", True, "Keyboard typo (n instead of m)"),
        ("medkartv", True, "Keyboard typo (k instead of l)"),
        ("medlarvt", True, "Swapped 'tv'"),
        ("medlar tv", True, "With space"),
        ("hey medlartv!", True, "In sentence"),
        ("medlartv is awesome", True, "Start of sentence"),
        ("call medlartv", True, "Middle of sentence"),
        ("random text", False, "No trigger"),
        ("medical", False, "Similar but wrong word"),
        ("me", False, "Too short"),
    ]
    
    passed = 0
    failed = 0
    
    for text, expected, description in test_cases:
        result = should_respond(text)
        status = "✓" if result == expected else "✗"
        
        if result == expected:
            passed += 1
        else:
            failed += 1
        
        print(f"{status} {description:30s} | '{text:20s}' → {result}")
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed ({passed}/{len(test_cases)})")
    print("=" * 60)

if __name__ == "__main__":
    test_fuzzy_trigger()
