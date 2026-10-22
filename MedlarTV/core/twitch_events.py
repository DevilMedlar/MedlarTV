"""
MedlarTV Twitch Events Module
Detects and responds to raids, subs, channel points, and uses Twitch emotes
"""

import os
DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"
if DEBUG:
    print("[DEBUG twitch_events] Loaded twitch_events.py")
import re
import random
import requests
from typing import Optional, Dict, List, Tuple
from datetime import datetime

# Feature flags
ENABLE_RAID_DETECTION = os.getenv("ENABLE_RAID_DETECTION", "true").lower() == "true"
ENABLE_SUB_DETECTION = os.getenv("ENABLE_SUB_DETECTION", "true").lower() == "true"
ENABLE_CHANNEL_POINTS = os.getenv("ENABLE_CHANNEL_POINTS", "true").lower() == "true"
ENABLE_EMOTE_RESPONSES = os.getenv("ENABLE_EMOTE_RESPONSES", "true").lower() == "true"

# Twitch API
TWITCH_CLIENT_ID = os.getenv("APP_TWITCH_CLIENT_ID", "")
TWITCH_API_BASE = "https://api.twitch.tv/helix"

# Emote cache
_global_emotes = None
_channel_emotes = None

# Explicit whitelist of Twitch emotes to avoid third-party sets (7TV/BTTV/FFZ)
ALLOWED_TWITCH_EMOTES: List[str] = [
    "PogChamp", "Kappa", "LUL", "NotLikeThis", "BibleThump",
    "VoHiYo", "CoolCat", "DansGame", "ResidentSleeper", "4Head",
    "Kreygasm", "KappaPride", "Keepo"
]


def parse_usernotice(irc_message: str) -> Optional[Tuple[Dict[str, str], str]]:
    """Parse server event tags/channel; USERNOTICE trailing text is optional."""
    if not isinstance(irc_message, str) or any(c in irc_message for c in "\r\n\x00"):
        return None
    match = re.fullmatch(
        r"@([^\s]+) :tmi\.twitch\.tv USERNOTICE (#[^\s,:]+)(?: :.*)?",
        irc_message,
    )
    if match is None or match[2] == "#":
        return None
    tags = {}
    escapes = {"s": " ", ":": ";", "r": "\r", "n": "\n", "\\": "\\"}
    for item in match[1].split(";"):
        if not item:
            continue
        key, _, value = item.partition("=")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./-]*", key) or key in tags:
            return None
        tags[key] = re.sub(r"\\(.)", lambda m: escapes.get(m[1], m[1]), value)
    return tags, match[2].lower()


def detect_raid(irc_message: str) -> Optional[Dict]:
    """
    Detect raid from IRC message.
    
    IRC Format: @msg-id=raid :tmi.twitch.tv USERNOTICE #channel
    
    Returns:
        Dict with raid info or None
    """
    if DEBUG:
        print(f"[DEBUG twitch_events] detect_raid() message_length={len(irc_message)}")

    if not ENABLE_RAID_DETECTION:
        if DEBUG:
            print("[DEBUG twitch_events] detect_raid() → feature disabled (ENABLE_RAID_DETECTION=false)")
        return None
    
    notice = parse_usernotice(irc_message)
    if notice is None or notice[0].get("msg-id") != "raid":
        if DEBUG:
            print("[DEBUG twitch_events] detect_raid() → 'msg-id=raid' not found in message")
        return None
    
    try:
        # Extract raid info from tags
        tags = notice[0]
        if DEBUG:
            print(f"[DEBUG twitch_events] detect_raid() tag_count={len(tags)}")
        
        if "msg-param-viewerCount" in tags and "msg-param-displayName" in tags:
            raid_info = {
                "type": "raid",
                "raider": tags["msg-param-displayName"],
                "viewer_count": int(tags["msg-param-viewerCount"]),
                "timestamp": datetime.now().isoformat()
            }
            if DEBUG:
                print("[DEBUG twitch_events] detect_raid() detected")
            return raid_info
        else:
            if DEBUG:
                print("[DEBUG twitch_events] detect_raid() → required tags missing (msg-param-viewerCount/displayName)")
    except Exception as e:
        print(f"[Events] Error parsing raid: {type(e).__name__}")
        if DEBUG:
            print("[DEBUG twitch_events] detect_raid() parse failed")
    
    if DEBUG:
        print("[DEBUG twitch_events] detect_raid() → returning None")
    return None


def detect_subscription(irc_message: str) -> Optional[Dict]:
    """
    Detect subscription/resub from IRC message.
    
    IRC Format: @msg-id=sub/resub/subgift :tmi.twitch.tv USERNOTICE #channel
    
    Returns:
        Dict with sub info or None
    """
    if DEBUG:
        print(f"[DEBUG twitch_events] detect_subscription() message_length={len(irc_message)}")

    if not ENABLE_SUB_DETECTION:
        if DEBUG:
            print("[DEBUG twitch_events] detect_subscription() → feature disabled (ENABLE_SUB_DETECTION=false)")
        return None
    
    notice = parse_usernotice(irc_message)
    if notice is None or notice[0].get("msg-id") not in {"sub", "resub", "subgift", "submysterygift"}:
        if DEBUG:
            print("[DEBUG twitch_events] detect_subscription() → no sub msg-id found")
        return None
    
    try:
        # Extract sub info from tags
        tags = notice[0]
        if DEBUG:
            print(f"[DEBUG twitch_events] detect_subscription() tag_count={len(tags)}")
        
        msg_id = tags.get("msg-id", "")
        if DEBUG:
            print(f"[DEBUG twitch_events] detect_subscription() msg_id_length={len(msg_id)}")
        
        if msg_id in ["sub", "resub"]:
            info = {
                "type": "subscription",
                "sub_type": msg_id,
                "subscriber": tags.get("display-name", tags.get("login", "Unknown")),
                "months": int(tags.get("msg-param-cumulative-months", 1)),
                "tier": tags.get("msg-param-sub-plan", "1000"),
                "timestamp": datetime.now().isoformat()
            }
            if DEBUG:
                print("[DEBUG twitch_events] detect_subscription() subscription detected")
            return info
        
        elif msg_id == "subgift":
            info = {
                "type": "gift_subscription",
                "gifter": tags.get("display-name", "Anonymous"),
                "recipient": tags.get("msg-param-recipient-display-name", "Unknown"),
                "months": int(tags.get("msg-param-months", 1)),
                "tier": tags.get("msg-param-sub-plan", "1000"),
                "timestamp": datetime.now().isoformat()
            }
            if DEBUG:
                print("[DEBUG twitch_events] detect_subscription() gift detected")
            return info
        
        elif msg_id == "submysterygift":
            info = {
                "type": "mystery_gift",
                "gifter": tags.get("display-name", "Anonymous"),
                # The current batch, not the gifter's lifetime channel total.
                "count": int(tags.get("msg-param-mass-gift-count", 1)),
                "tier": tags.get("msg-param-sub-plan", "1000"),
                "timestamp": datetime.now().isoformat()
            }
            if DEBUG:
                print("[DEBUG twitch_events] detect_subscription() mystery gift detected")
            return info
    
    except Exception as e:
        print(f"[Events] Error parsing subscription: {type(e).__name__}")
        if DEBUG:
            print("[DEBUG twitch_events] detect_subscription() parse failed")
    
    if DEBUG:
        print("[DEBUG twitch_events] detect_subscription() → returning None")
    return None


def detect_channel_point_redemption(irc_message: str) -> Optional[Dict]:
    """
    Detect channel point redemption from IRC message.
    
    IRC Format: @msg-id=reward-redeemed :tmi.twitch.tv USERNOTICE #channel
    
    Returns:
        Dict with redemption info or None
    """
    if DEBUG:
        print(f"[DEBUG twitch_events] detect_channel_point_redemption() message_length={len(irc_message)}")

    if not ENABLE_CHANNEL_POINTS:
        if DEBUG:
            print("[DEBUG twitch_events] detect_channel_point_redemption() → feature disabled (ENABLE_CHANNEL_POINTS=false)")
        return None
    
    if "msg-id=reward-redeemed" not in irc_message:
        if DEBUG:
            print("[DEBUG twitch_events] detect_channel_point_redemption() → 'msg-id=reward-redeemed' not found")
        return None
    
    try:
        tags = {}
        if irc_message.startswith("@"):
            tag_str = irc_message.split(" ", 1)[0][1:]
            for tag in tag_str.split(";"):
                if "=" in tag:
                    key, value = tag.split("=", 1)
                    tags[key] = value
        if DEBUG:
            print(f"[DEBUG twitch_events] detect_channel_point_redemption() tag_count={len(tags)}")
        
        info = {
            "type": "channel_points",
            "user": tags.get("display-name", "Unknown"),
            "reward_id": tags.get("custom-reward-id", ""),
            "reward_title": tags.get("msg-param-reward-title", ""),
            "reward_cost": int(tags.get("msg-param-reward-cost", 0)),
            "user_input": tags.get("msg-param-user-input", ""),
            "timestamp": datetime.now().isoformat()
        }
        if DEBUG:
            print("[DEBUG twitch_events] detect_channel_point_redemption() detected")
        return info
    
    except Exception as e:
        print(f"[Events] Error parsing channel points: {type(e).__name__}")
        if DEBUG:
            print("[DEBUG twitch_events] detect_channel_point_redemption() parse failed")
    
    if DEBUG:
        print("[DEBUG twitch_events] detect_channel_point_redemption() → returning None")
    return None


def detect_bits(irc_message: str) -> Optional[Dict]:
    """
    Detect bits/cheering from IRC message.
    
    Returns:
        Dict with bits info or None
    """
    if DEBUG:
        print(f"[DEBUG twitch_events] detect_bits() message_length={len(irc_message)}")

    if "bits=" not in irc_message.lower():
        if DEBUG:
            print("[DEBUG twitch_events] detect_bits() → 'bits=' not found in message")
        return None
    
    try:
        tags = {}
        if irc_message.startswith("@"):
            tag_str = irc_message.split(" ", 1)[0][1:]
            for tag in tag_str.split(";"):
                if "=" in tag:
                    key, value = tag.split("=", 1)
                    tags[key] = value
        if DEBUG:
            print(f"[DEBUG twitch_events] detect_bits() tag_count={len(tags)}")
        
        if "bits" in tags:
            info = {
                "type": "bits",
                "user": tags.get("display-name", "Unknown"),
                "amount": int(tags.get("bits", 0)),
                "timestamp": datetime.now().isoformat()
            }
            if DEBUG:
                print("[DEBUG twitch_events] detect_bits() detected")
            return info
        else:
            if DEBUG:
                print("[DEBUG twitch_events] detect_bits() → 'bits' tag not found")
    
    except Exception as e:
        print(f"[Events] Error parsing bits: {type(e).__name__}")
        if DEBUG:
            print("[DEBUG twitch_events] detect_bits() parse failed")
    
    if DEBUG:
        print("[DEBUG twitch_events] detect_bits() → returning None")
    return None


def get_raid_response(raid_info: Dict) -> str:
    """Generate response for raid"""
    if DEBUG:
        print("[DEBUG twitch_events] get_raid_response() called")
    raider = raid_info["raider"]
    viewers = raid_info["viewer_count"]
    
    responses = [
        f"🚨 RAID ALERT! 🚨 Welcome {raider} and {viewers} raiders! PogChamp",
        f"⚡ {raider} just raided with {viewers} viewers! LET'S GOOO! 🔥",
        f"🎉 HUGE raid from {raider}! Welcome {viewers} new friends! 🌟",
        f"💥 {raider} brought {viewers} raiders! Thank you so much! 💖",
    ]
    
    chosen = random.choice(responses)
    if DEBUG:
        print(f"[DEBUG twitch_events] get_raid_response() response_length={len(chosen)}")
    return chosen


def get_sub_response(sub_info: Dict) -> str:
    """Generate response for subscription"""
    if DEBUG:
        print("[DEBUG twitch_events] get_sub_response() called")
    if sub_info["type"] == "subscription":
        subscriber = sub_info["subscriber"]
        months = sub_info["months"]
        
        if months == 1:
            responses = [
                f"🎉 Welcome to the squad, {subscriber}! Thanks for the sub! 💖",
                f"⭐ {subscriber} just subscribed! You're awesome! PogChamp",
                f"🔥 NEW SUB HYPE! Thanks {subscriber}! PogChamp",
            ]
        else:
            responses = [
                f"🎊 {subscriber} resubbed for {months} months! Legend! 👑",
                f"💖 {months} MONTHS! Thanks for the continued support, {subscriber}!",
                f"⚡ {subscriber} has been here for {months} months! Absolute legend! 🔥",
            ]
        
        chosen = random.choice(responses)
        if DEBUG:
            print(f"[DEBUG twitch_events] get_sub_response() response_length={len(chosen)}")
        return chosen
    
    elif sub_info["type"] == "gift_subscription":
        gifter = sub_info["gifter"]
        recipient = sub_info["recipient"]
        
        responses = [
            f"🎁 {gifter} just gifted a sub to {recipient}! So generous! 💖",
            f"⭐ Gift sub from {gifter} to {recipient}! Amazing! PogChamp",
            f"💝 {gifter} spreading the love with a gift sub for {recipient}! 🔥",
        ]
        
        chosen = random.choice(responses)
        if DEBUG:
            print(f"[DEBUG twitch_events] get_sub_response() response_length={len(chosen)}")
        return chosen
    
    elif sub_info["type"] == "mystery_gift":
        gifter = sub_info["gifter"]
        count = sub_info["count"]
        
        responses = [
            f"🎁✨ {gifter} just gifted {count} subs! LEGENDARY! 👑",
            f"💖 MASSIVE gift sub bomb from {gifter}! {count} subs! PogChamp",
            f"🚀 {gifter} is going CRAZY with {count} gift subs! Thank you! 🔥",
        ]
        
        chosen = random.choice(responses)
        if DEBUG:
            print(f"[DEBUG twitch_events] get_sub_response() response_length={len(chosen)}")
        return chosen
    
    fallback = f"Thanks for the sub! 💖"
    if DEBUG:
        print(f"[DEBUG twitch_events] get_sub_response() fallback_length={len(fallback)}")
    return fallback


def get_channel_point_response(redemption_info: Dict) -> str:
    """Generate response for channel point redemption"""
    if DEBUG:
        print("[DEBUG twitch_events] get_channel_point_response() called")
    user = redemption_info["user"]
    reward = redemption_info["reward_title"]
    
    msg = f"⭐ {user} redeemed: {reward}! Thanks for using your points! 💖"
    if DEBUG:
        print(f"[DEBUG twitch_events] get_channel_point_response() response_length={len(msg)}")
    return msg


def get_bits_response(bits_info: Dict) -> str:
    """Generate response for bits/cheering"""
    if DEBUG:
        print("[DEBUG twitch_events] get_bits_response() called")
    user = bits_info["user"]
    amount = bits_info["amount"]
    
    if amount >= 1000:
        msg = f"💎 HUGE CHEER! {user} just dropped {amount} bits! You're amazing! 🔥"
    elif amount >= 100:
        msg = f"⭐ {user} cheered {amount} bits! Thank you so much! PogChamp"
    else:
        msg = f"💖 Thanks for the {amount} bits, {user}! Appreciate you!"
    
    if DEBUG:
        print(f"[DEBUG twitch_events] get_bits_response() response_length={len(msg)}")
    return msg


def load_global_emotes(token: str) -> List[str]:
    """Load global Twitch emotes"""
    if DEBUG:
        print(f"[DEBUG twitch_events] load_global_emotes() called with token_present={bool(token)}")
    global _global_emotes
    
    if _global_emotes:
        if DEBUG:
            print(f"[DEBUG twitch_events] load_global_emotes() → returning cached {_global_emotes and len(_global_emotes)} emotes")
        return _global_emotes
    
    try:
        headers = {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}"
        }
        if DEBUG:
            print("[DEBUG twitch_events] load_global_emotes() request prepared")
        
        response = requests.get(
            f"{TWITCH_API_BASE}/chat/emotes/global",
            headers=headers
        )
        if DEBUG:
            print(f"[DEBUG twitch_events] load_global_emotes() status={response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            _global_emotes = [emote["name"] for emote in data["data"]]
            print(f"[Events] Loaded {len(_global_emotes)} global emotes")
            if DEBUG:
                print(f"[DEBUG twitch_events] load_global_emotes() emote_count={len(_global_emotes)}")
            return _global_emotes
        
        if DEBUG:
            print(f"[DEBUG twitch_events] load_global_emotes() non-200 status={response.status_code}")
        return []
    
    except Exception as e:
        print(f"[Events] Error loading global emotes: {type(e).__name__}")
        if DEBUG:
            print(f"[DEBUG twitch_events] load_global_emotes() exception_type={type(e).__name__}")
        return []


def load_channel_emotes(token: str, broadcaster_id: str) -> List[str]:
    """Load channel-specific emotes"""
    if DEBUG:
        print(f"[DEBUG twitch_events] load_channel_emotes() token_present={bool(token)}")
    global _channel_emotes
    
    if _channel_emotes:
        if DEBUG:
            print(f"[DEBUG twitch_events] load_channel_emotes() → returning cached {_channel_emotes and len(_channel_emotes)} emotes")
        return _channel_emotes
    
    try:
        headers = {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}"
        }
        if DEBUG:
            print("[DEBUG twitch_events] load_channel_emotes() request prepared")
        
        response = requests.get(
            f"{TWITCH_API_BASE}/chat/emotes",
            headers=headers,
            params={"broadcaster_id": broadcaster_id}
        )
        if DEBUG:
            print(f"[DEBUG twitch_events] load_channel_emotes() status={response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            _channel_emotes = [emote["name"] for emote in data["data"]]
            print(f"[Events] Loaded {len(_channel_emotes)} channel emotes")
            if DEBUG:
                print(f"[DEBUG twitch_events] load_channel_emotes() emote_count={len(_channel_emotes)}")
            return _channel_emotes
        
        if DEBUG:
            print(f"[DEBUG twitch_events] load_channel_emotes() non-200 status={response.status_code}")
        return []
    
    except Exception as e:
        print(f"[Events] Error loading channel emotes: {type(e).__name__}")
        if DEBUG:
            print(f"[DEBUG twitch_events] load_channel_emotes() exception_type={type(e).__name__}")
        return []


def add_random_emote(message: str, emotes: List[str]) -> str:
    """Add a random Twitch emote to message"""
    if DEBUG:
        print(f"[DEBUG twitch_events] add_random_emote() message_length={len(message)} emote_count={len(emotes) if emotes else 0}")
    if not ENABLE_EMOTE_RESPONSES or not emotes:
        if DEBUG:
            print("[DEBUG twitch_events] add_random_emote() → feature disabled or no emotes")
        return message
    
    # Common emotes that work well in responses
    good_emotes = [
        "PogChamp", "Kappa", "LUL", "NotLikeThis", "BibleThump",
        "VoHiYo", "CoolCat", "DansGame", "ResidentSleeper", "4Head"
    ]
    
    # Filter input emote pool to allowed Twitch emotes only
    allowed_pool = [e for e in emotes if e in ALLOWED_TWITCH_EMOTES]

    # Use good emotes if available, otherwise sample from allowed pool
    available_emotes = [e for e in good_emotes if e in allowed_pool]
    if not available_emotes:
        if DEBUG:
            print("[DEBUG twitch_events] add_random_emote() → no preferred emotes found, sampling from allowed pool")
        available_emotes = random.sample(allowed_pool or ["PogChamp"], 1 if not allowed_pool else min(10, len(allowed_pool)))
    else:
        if DEBUG:
            print(f"[DEBUG twitch_events] add_random_emote() → using preferred emotes={available_emotes}")
    
    emote = random.choice(available_emotes)
    result = f"{message} {emote}"
    if DEBUG:
        print(f"[DEBUG twitch_events] add_random_emote() result_length={len(result)}")
    return result


def extract_emotes_from_message(message: str, emotes: List[str]) -> List[str]:
    """Extract which emotes were used in a message"""
    if DEBUG:
        print(f"[DEBUG twitch_events] extract_emotes_from_message() message_length={len(message)} emote_count={len(emotes) if emotes else 0}")
    found_emotes = []
    for emote in emotes:
        if emote in message:
            if DEBUG:
                print("[DEBUG twitch_events] extract_emotes_from_message() found emote")
            found_emotes.append(emote)
    if DEBUG:
        print(f"[DEBUG twitch_events] extract_emotes_from_message() found_count={len(found_emotes)}")
    return found_emotes


# Example usage and testing
if __name__ == "__main__":
    print("=" * 60)
    print("MedlarTV Twitch Events - Testing")
    print("=" * 60)
    
    # Test raid detection
    print("\n--- Raid Detection ---")
    raid_msg = "@msg-id=raid;msg-param-displayName=TestRaider;msg-param-viewerCount=50 :tmi.twitch.tv USERNOTICE #channel"
    raid_info = detect_raid(raid_msg)
    if raid_info:
        print(f"Detected: {raid_info}")
        print(f"Response: {get_raid_response(raid_info)}")
    else:
        print("No raid detected")
    
    # Test sub detection
    print("\n--- Subscription Detection ---")
    sub_msg = "@msg-id=sub;display-name=TestSub;msg-param-cumulative-months=1;msg-param-sub-plan=1000 :tmi.twitch.tv USERNOTICE #channel"
    sub_info = detect_subscription(sub_msg)
    if sub_info:
        print(f"Detected: {sub_info}")
        print(f"Response: {get_sub_response(sub_info)}")
    else:
        print("No subscription detected")
    
    # Test gift sub
    print("\n--- Gift Sub Detection ---")
    gift_msg = "@msg-id=subgift;display-name=Gifter;msg-param-recipient-display-name=Lucky;msg-param-months=1 :tmi.twitch.tv USERNOTICE #channel"
    gift_info = detect_subscription(gift_msg)
    if gift_info:
        print(f"Detected: {gift_info}")
        print(f"Response: {get_sub_response(gift_info)}")
    else:
        print("No gift subscription detected")
    
    # Test channel points
    print("\n--- Channel Points Detection ---")
    points_msg = "@msg-id=reward-redeemed;display-name=Viewer;msg-param-reward-title=Hydrate;msg-param-reward-cost=100 :tmi.twitch.tv USERNOTICE #channel"
    points_info = detect_channel_point_redemption(points_msg)
    if points_info:
        print(f"Detected: {points_info}")
        print(f"Response: {get_channel_point_response(points_info)}")
    else:
        print("No channel points detected")
    
    # Test bits
    print("\n--- Bits Detection ---")
    bits_msg = "@bits=500;display-name=Cheerer :tmi.twitch.tv PRIVMSG #channel :Cheer500 Let's go!"
    bits_info = detect_bits(bits_msg)
    if bits_info:
        print(f"Detected: {bits_info}")
        print(f"Response: {get_bits_response(bits_info)}")
    else:
        print("No bits detected")

        print(f"Response: {get_bits_response(bits_info)}")
