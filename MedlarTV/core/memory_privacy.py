"""Conservative intake checks shared by Twitch session and profile memory.

These checks decide what memory may retain. They do not change moderation or
the live chat message itself.
"""

from __future__ import annotations

import re


_RUN_ID_IN_CONTEXT = re.compile(
    r"((?<!\S)run\s+)"
    r"(MM-20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-[A-Z])"
    r"(?=$|[\s,;:!?)]|\.(?:\s|$))",
    re.IGNORECASE,
)

_CONTACT_DETAIL = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"(?:https?://|www\.)\S+|"
    r"(?<![\w@])(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z]{2,24}(?!\w)|"
    r"\b[A-Z0-9-]+\s+(?:dot|\[dot\]|\(dot\))\s+"
    r"[A-Z]{2,24}\b|"
    r"\b(?:contact\s+(?:number|details?|info)|mobile(?:\s+(?:phone|number))?|"
    r"telephone(?:\s+number)?|cell(?:phone|\s+phone)(?:\s+number)?|"
    r"phone(?:\s+number)?|email(?:\s+address)?|"
    r"(?:mailing|postal)\s+address|p\.?o\.?\s*box)\s*(?::|\bis\b|\d)|"
    r"(?<!\w)(?:\+?\d{1,3}[-. ]?)?(?:\(\d{3}\)|\d{3})"
    r"[-. ]?\d{3}[-. ]?\d{4}(?!\w)|"
    r"(?<!\w)\d{3}[-. ]\d{4}(?!\w)|(?<!\w)\d{7,}(?!\w)|"
    r"\b(?:at|on|near|from|to|by|located\s+at|lives?\s+at)\s+"
    r"\d{1,5}\s+(?-i:[A-Z])[\w.-]*"
    r"(?:\s+(?-i:[A-Z])[\w.-]*){0,3}\b|"
    r"\b\d{1,5}\s+(?:[\w.-]+\s+){0,4}"
    r"(?:st|street|rd|road|ln|lane|ave|avenue|dr|drive|"
    r"way|blvd|boulevard|ct|court|pl|place|cir|circle|"
    r"ter|terrace|pkwy|parkway|hwy|highway|trl|trail|sq|square|"
    r"row|mews|loop|alley|aly|path|walk|close|crescent|cres|"
    r"parade|pde|grove|grv|apt|apartment)\b",
    re.IGNORECASE,
)

_HEALTH_DETAIL = re.compile(
    r"\b(?:asthma|diabet(?:es|ic)|celiac|epilepsy|arthritis|lupus|hiv|bipolar|"
    r"(?:high|low)\s+blood pressure|hypertension|hypotension|"
    r"(?:heart|cardiac|lung|respiratory|neurologic(?:al)?|mental|"
    r"medical|health|chronic|autoimmune)\s+"
    r"(?:condition|disease|disorder|syndrome|problem|issue)|"
    r"i\s+(?:have|had|live with|suffer from)\s+(?:a|an)\s+"
    r"(?:medical\s+|health\s+|chronic\s+)?condition|"
    r"multiple sclerosis|cancer|allerg(?:y|ies)|migraine|headache|"
    r"nausea|vomit(?:ing)?|fever|flu|infection|chronic pain|"
    r"vertigo|dizziness|dizzy|chronic fatigue|cough|rash|sore throat|"
    r"surgery|surgical|fractur(?:e|ed)|sprain(?:ed)?|"
    r"panic attack|anxiety|depression|seizure|injur(?:y|ed)|"
    r"illness|unwell|hospitali[sz]ed?|medication|prescription|"
    r"diagnos(?:is|ed))\b|"
    r"\b(?:my\s+(?:health|medical\s+condition|symptoms?)|"
    r"i\s+(?:was|have been)\s+diagnosed with|"
    r"i\s+(?:suffer|am suffering)\s+from|"
    r"i\s+(?:broke|fractured|sprained)\s+(?:my|a|an)\s+"
    r"(?:ankle|arm|leg|wrist|foot|hand|rib|shoulder|hip|knee))\b",
    re.IGNORECASE,
)

_NAME_REQUEST = re.compile(
    r"\b(?:please\s+)?(?:call me|you can call me|people call me|"
    r"they call me|my friends call me|"
    r"i (?:go by|am called|prefer to be called))\s+"
    r"(?!when\b|if\b|back\b|later\b|"
    r"tomorrow\b|in\b|at\b)\S+",
    re.IGNORECASE,
)


def has_contact_detail(text: str) -> bool:
    # A run-scoped test identifier is not a phone number. Keep this exception
    # deliberately narrow: only a date-shaped MM-YYYYMMDD-L identifier
    # immediately after a standalone ``run`` is masked, so an unscoped ID or
    # any other contact detail in the same message still reaches the ordinary
    # contact detector.
    candidate = _RUN_ID_IN_CONTEXT.sub(
        lambda match: match.group(1) + "RUN-ID", text,
    )
    return bool(_CONTACT_DETAIL.search(candidate))


def has_health_detail(text: str) -> bool:
    return bool(_HEALTH_DETAIL.search(text))


def has_name_request(text: str) -> bool:
    return bool(_NAME_REQUEST.search(text))
