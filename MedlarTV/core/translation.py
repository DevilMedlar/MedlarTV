import re
import logging
from typing import Optional, Tuple
from langdetect import detect, DetectorFactory
import requests
import os
DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"

log = logging.getLogger("translation")

# langdetect determinism
DetectorFactory.seed = 42

# Common aliases viewers actually use
LANGUAGE_ALIASES = {
    "sp": "es", "es": "es", "spanish": "es",
    "jp": "ja", "ja": "ja", "japanese": "ja",
    "kr": "ko", "ko": "ko", "korean": "ko",
    "cn": "zh", "zh": "zh", "chinese": "zh",
    "zh-cn": "zh", "cn-simp": "zh", "zt": "zh-hant",
    "fr": "fr", "french": "fr",
    "de": "de", "german": "de",
    "pt": "pt", "portuguese": "pt", "pt-br": "pt-br", "pb": "pt-br",
    "ru": "ru", "russian": "ru",
    "it": "it", "italian": "it",
    "en": "en", "english": "en",
    "tl": "tl", "tagalog": "tl", "filipino": "tl",
}

SERVICE_UNAVAILABLE = "Translator offline or no languages installed."


def _base_url() -> str:
    url = os.getenv("LIBRETRANSLATE_URL", "http://127.0.0.1:5000/translate").rstrip("/")
    return url[:-len("/translate")] if url.endswith("/translate") else url

def _lt_languages() -> set[str]:
    try:
        r = requests.get(f"{_base_url()}/languages", timeout=2)
        if r.status_code == 200:
            data = r.json()
            if not isinstance(data, list):
                return set()
            codes = set()
            for item in data:
                if not isinstance(item, dict):
                    continue
                c = str(item.get("code", "")).strip().lower()
                if c:
                    codes.add(c)
            return codes
    except (requests.exceptions.RequestException, ValueError, TypeError):
        return set()
    return set()

def get_supported_languages() -> set[str]:
    """Read the languages served by LibreTranslate, including after startup."""
    return _lt_languages()

def normalize_lang(code: str, supported: Optional[set[str]] = None) -> Optional[str]:
    if DEBUG:
        print(f"[DEBUG translation] normalize_lang() code_length={len(str(code or ''))}")
    if not code:
        if DEBUG:
            print("[DEBUG translation] normalize_lang(): code is None/empty → returning None")
        return None
    c = code.strip().lower()
    if DEBUG:
        print(f"[DEBUG translation] normalize_lang(): normalized_length={len(c)}")
    s = get_supported_languages() if supported is None else supported
    alias = LANGUAGE_ALIASES.get(c, c)
    if alias == "zh":
        if "zh-hans" in s:
            alias = "zh-hans"
        elif "zh" in s:
            alias = "zh"
        elif "zh-hant" in s:
            alias = "zh-hant"
    if alias == "pt-br" and "pt-br" not in s and "pb" in s:
        alias = "pb"
    if alias == "pt-br" and "pt-br" not in s and "pb" not in s and "pt" in s:
        alias = "pt"
    if alias == "pb" and "pb" not in s and "pt-br" in s:
        alias = "pt-br"
    result = alias if alias in s else None
    if DEBUG:
        print(f"[DEBUG translation] normalize_lang(): recognized={result is not None}")
    return result


def detect_language(text: str) -> str:
    if DEBUG:
        print(f"[DEBUG translation] detect_language() text_length={len(text or '')}")
    try:
        stripped = re.sub(r"\W+", "", text or "")
        if DEBUG:
            print(f"[DEBUG translation] detect_language(): stripped_length={len(stripped)}")
        if not text or len(stripped) < 3:
            if DEBUG:
                print("[DEBUG translation] detect_language(): too short → returning 'en'")
            return "en"
        try:
            resp = requests.post(f"{_base_url()}/detect", json={"q": text}, timeout=5)
            if DEBUG:
                print(f"[DEBUG translation] detect_language(): LT detect status={resp.status_code}")
            if resp.status_code == 200:
                arr = resp.json()
                if isinstance(arr, list) and arr:
                    lang = str(arr[0].get("language", "")).strip().lower()
                    if DEBUG:
                        print(f"[DEBUG translation] detect_language(): LT language_length={len(lang)}")
                    if lang:
                        return lang
        except Exception as e:
            if DEBUG:
                print(f"[DEBUG translation] detect_language(): LT detect error_type={type(e).__name__}")
        detected = detect(text)
        if DEBUG:
            print(f"[DEBUG translation] detect_language(): fallback_length={len(detected)}")
        return detected
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG translation] detect_language() exception_type={type(e).__name__}")
        return "en"


def get_multilingual_greeting(lang: str) -> str:
    if DEBUG:
        print(f"[DEBUG translation] get_multilingual_greeting() lang_length={len(lang or '')}")
    lang_norm = normalize_lang(lang) or "en"
    if DEBUG:
        print(f"[DEBUG translation] get_multilingual_greeting(): normalized_length={len(lang_norm)}")
    greeting = {
        "es": "¡Hola!", "ja": "やあ！", "ko": "안녕!", "zh": "嗨！",
        "fr": "Salut !", "de": "Hallo!", "pt": "Olá!", "ru": "Привет!", "it": "Ciao!", "en": "Hey!"
    }.get(lang_norm, "Hey!")
    if DEBUG:
        print(f"[DEBUG translation] get_multilingual_greeting(): returning {greeting!r}")
    return greeting


def add_language_indicator(msg: str, target_lang: str) -> str:
    if DEBUG:
        print(f"[DEBUG translation] add_language_indicator() message_length={len(msg)}")
    flags = {
        "es":"🇪🇸","ja":"🇯🇵","ko":"🇰🇷","zh":"🇨🇳",
        "zh-hans":"🇨🇳","zh-hant":"🇹🇼",
        "fr":"🇫🇷","de":"🇩🇪","pt":"🇵🇹","pt-br":"🇧🇷","pb":"🇧🇷",
        "ru":"🇷🇺","it":"🇮🇹","en":"🇺🇸","tl":"🇵🇭"
    }
    tl = normalize_lang(target_lang) or target_lang
    if DEBUG:
        print(f"[DEBUG translation] add_language_indicator(): target_length={len(tl)}")
    flag = flags.get(tl, "🌐")
    if DEBUG:
        print(f"[DEBUG translation] add_language_indicator(): flag={flag!r}")
    result = f"{msg} {flag}"
    if DEBUG:
        print(f"[DEBUG translation] add_language_indicator(): result_length={len(result)}")
    return result


# --- Translation engine ---


def translate_text(text: str, target_lang: str, source_lang: Optional[str] = None,
                   supported: Optional[set[str]] = None) -> Tuple[bool, str]:
    """
    Returns (ok, translated_or_error).
    - Uses LibreTranslate running locally.
    - Auto-detects source.
    """
    if DEBUG:
        print(f"[DEBUG translation] translate_text() text_length={len(text)}")

    languages = get_supported_languages() if supported is None else supported
    if not languages:
        return False, SERVICE_UNAVAILABLE

    tl = normalize_lang(target_lang, languages)
    if DEBUG:
        print(f"[DEBUG translation] translate_text(): target_recognized={tl is not None}")

    if not tl:
        msg = f"Unsupported language: {target_lang}"
        if DEBUG:
            print("[DEBUG translation] translate_text(): unsupported target")
        return False, msg

    sl = normalize_lang(source_lang, languages) if source_lang else None
    if source_lang and not sl:
        return False, f"Unsupported source language: {source_lang}"
    try:
        payload = {"q": text, "source": sl or "auto", "target": tl, "format": "text"}
        if DEBUG:
            print(f"[DEBUG translation] translate_text(): POST payload_length={len(text)}")

        resp = requests.post(
            f"{_base_url()}/translate",
            json=payload,
            timeout=7,
        )

        if DEBUG:
            print(f"[DEBUG translation] translate_text(): resp.status_code={resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            out = data.get("translatedText", "") if isinstance(data, dict) else ""
            out = out.strip() if isinstance(out, str) else ""
            if DEBUG:
                print(f"[DEBUG translation] translate_text(): translated_length={len(out)}")
            if not out:
                return False, "Translation failed (empty result)."
            return True, out

        msg = f"Translation server error: {resp.status_code}"
        if DEBUG:
            print(f"[DEBUG translation] translate_text(): {msg}")
        return False, msg

    except requests.exceptions.RequestException as e:
        msg = "Translator offline."
        if DEBUG:
            print(f"[DEBUG translation] translate_text(): exception_type={type(e).__name__}")
        return False, msg
    except (ValueError, TypeError):
        return False, "Translation failed (invalid server response)."


def supported_list_human() -> str:
    if DEBUG:
        print("[DEBUG translation] supported_list_human() called")
    s = sorted(get_supported_languages())
    if not s:
        return SERVICE_UNAVAILABLE
    result = "Supported: " + ", ".join(s)
    if DEBUG:
        print(f"[DEBUG translation] supported_list_human(): returning {result!r}")
    return result
 
