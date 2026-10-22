import os
DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"
if DEBUG:
    print("[DEBUG response_templates] Loaded response_templates.py")

"""
MedlarTV Response Templates
Templates for LLM output depending on severity levels
"""

import random


def get_severity_response(severity: int, username: str) -> str:
    if DEBUG:
        print(f"[DEBUG response_templates] get_severity_response(severity={severity}, username={username!r}) CALLED")

    templates = {
        1: [
            "Hey {user}, I hear you. Want to talk about it a bit more?",
            "I'm listening, {user}. Tell me what's going on.",
            "{user}, I got you. What happened?",
        ],
        2: [
            "{user}, that sounds really rough. You’re not alone here.",
            "I’m sorry you’re dealing with that, {user}. Want to talk it out?",
            "{user}, that’s tough… I’m here with you.",
        ],
        3: [
            "{user}… that’s a lot to carry. I’m really sorry.",
            "That’s seriously painful, {user}. I'm here — you’re not alone.",
            "{user}, that sounds overwhelming. It makes sense you’re feeling that way.",
        ],
        4: [
            "{user}… I’m so, so sorry. That’s heartbreaking.",
            "I’m here with you, {user}. I wish I could give you a real hug.",
            "{user}, I’m genuinely sorry you're going through something this heavy.",
        ],
    }

    if DEBUG:
        print(f"[DEBUG response_templates] severity={severity} checking range 1–4")

    if severity not in templates:
        if DEBUG:
            print(f"[DEBUG response_templates] Invalid severity={severity}, returning fallback")
        return f"{username}, I'm here for you."

    chosen_list = templates[severity]
    if DEBUG:
        print(f"[DEBUG response_templates] Template list size={len(chosen_list)}")

    chosen = random.choice(chosen_list)
    if DEBUG:
        print(f"[DEBUG response_templates] Selected template={chosen!r}")

    final = chosen.format(user=username)
    if DEBUG:
        print(f"[DEBUG response_templates] Final output={final!r}")

    return final


def get_supportive_message(username: str) -> str:
    if DEBUG:
        print(f"[DEBUG response_templates] get_supportive_message(username={username!r}) CALLED")

    responses = [
        "You're doing your best, {user}. That's enough.",
        "I'm proud of you for hanging in there, {user}.",
        "{user}, things will get better — one step at a time.",
    ]

    if DEBUG:
        print(f"[DEBUG response_templates] total_responses={len(responses)}")
    chosen = random.choice(responses)
    if DEBUG:
        print(f"[DEBUG response_templates] chosen_template={chosen!r}")

    final = chosen.format(user=username)
    if DEBUG:
        print(f"[DEBUG response_templates] output={final!r}")

    return final


def get_hype_message(username: str) -> str:
    if DEBUG:
        print(f"[DEBUG response_templates] get_hype_message(username={username!r}) CALLED")

    options = [
        "LET'S GO {user}!!!",
        "{user} showing UP today!! 🔥",
        "Ayyy {user}!! Big energy!!!",
    ]

    if DEBUG:
        print(f"[DEBUG response_templates] hype_list_size={len(options)}")
    chosen = random.choice(options)
    if DEBUG:
        print(f"[DEBUG response_templates] chosen_hype={chosen!r}")

    final = chosen.format(user=username)
    if DEBUG:
        print(f"[DEBUG response_templates] output={final!r}")

    return final
 
