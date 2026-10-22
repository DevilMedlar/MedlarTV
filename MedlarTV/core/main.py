import os
if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
    print("[DEBUG main] Loaded main.py")

"""
MedlarTV Main Entry Point (Refactored)
-------------------------------------
Clean initialization, clean shutdown, no duplicate systems, and perfect 
integration with the refactored Twitch listener, LLM brain, moderation, 
and event handlers.
...
"""

import logging
import os
from pathlib import Path
from dotenv import load_dotenv
import signal
import sys
import time
from typing import Dict, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse
import yaml
from MedlarTV.core.settings import get_settings, update_settings

load_dotenv()
if os.name == "nt":
    # Direct Core starts use the same local overrides as the Command Center launcher.
    from MedlarTV.core.local_credentials import apply_saved_credentials

    apply_saved_credentials()

from MedlarTV.tools.twitch_listener import start_listener, stop_listener
from MedlarTV.core.emotional_system import get_emotional_system
from MedlarTV.core.memory import load_memory
from MedlarTV.core.stream_management import verify_twitch_tokens
from MedlarTV.core.llm_brain import generate_response
from MedlarTV.core.mood_system import compute_mood, get_mood_label
from MedlarTV.core.content_filter import filter_message, get_safety_response


def configure_dependency_logging() -> None:
    """Keep dependency request URLs out of normal Core INFO/DEBUG logs."""
    for name in (
        "httpx",
        "primp",
        "wikipediaapi._http_client.sync_http_client",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

log = logging.getLogger("main")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
configure_dependency_logging()


BOOT_BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║                    MEDLAR  TACTICAL  AI                 ║
╚══════════════════════════════════════════════════════════╝
"""

APP = FastAPI(title="MedlarTV Commands", version="1.0.0")

def _load_commands_yaml() -> Dict[str, Any]:
    try:
        cfg = Path(__file__).resolve().parents[1] / "config" / "commands.yaml"
        with cfg.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("commands", {})
    except Exception:
        return {}

@APP.get("/api/commands")
def api_commands() -> JSONResponse:
    # Use the same source-grounded projection as the static site without hosting it.
    from MedlarTV.tools.build_commands_site import load_commands
    return JSONResponse({"commands": load_commands()}, headers={"Cache-Control": "no-store"})

@APP.get("/api/settings")
def api_get_settings() -> JSONResponse:
    return JSONResponse(get_settings(), headers={"Cache-Control": "no-store"})

@APP.post("/api/settings")
def api_update_settings(payload: Dict[str, Any]) -> JSONResponse:
    updated = update_settings(dict(payload or {}))
    return JSONResponse(updated, headers={"Cache-Control": "no-store"})

@APP.get("/settings")
def page_settings() -> HTMLResponse:
    html = """
    <!DOCTYPE html>
    <html lang=\"en\">
    <head>
      <meta charset=\"utf-8\" />
      <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
      <title>MedlarTV Settings</title>
      <style>
        :root { --bg:#0f1218; --card:#1a1f2b; --text:#e6e9ef; --muted:#9aa4b2; --accent:#6ae3ff; }
        body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; background:var(--bg); color:var(--text); }
        .container { max-width:720px; margin:24px auto; padding:0 16px; }
        .card { background:var(--card); border:1px solid #2b3242; border-radius:10px; padding:14px; }
        .row { display:flex; align-items:center; justify-content:space-between; padding:8px 0; }
        label { font-size:14px; }
      </style>
    </head>
    <body>
      <div class=\"container\">
        <h2>Settings</h2>
        <div class=\"card\">
          <div class=\"row\"><label>LLM Brain</label><input id=\"llm_brain\" type=\"checkbox\" /></div>
          <div class=\"row\"><label>Pokémon Auto-Catch</label><input id=\"pcg_auto_catch\" type=\"checkbox\" /></div>
          <div class=\"row\"><label>Ignore Viewer Poké Commands</label><input id=\"ignore_viewer_pokecatch\" type=\"checkbox\" /></div>
          <div class=\"row\"><label>Content Filter</label><input id=\"content_filter\" type=\"checkbox\" /></div>
          <div class=\"row\"><label>Timers</label><input id=\"timers\" type=\"checkbox\" /></div>
          <div class=\"row\"><label>Fuzzy Trigger</label><input id=\"fuzzy_trigger\" type=\"checkbox\" /></div>
        </div>
        <p style=\"margin-top:18px; font-size:12px; color:#8fa1b5\">Source: <a href=\"/api/settings\">/api/settings</a></p>
      </div>
      <script>
        async function load() {
          const r = await fetch('/api/settings', {cache:'no-store'});
          const s = await r.json();
          const keys = ['llm_brain','pcg_auto_catch','ignore_viewer_pokecatch','content_filter','timers','fuzzy_trigger'];
          for (const k of keys) {
            const el = document.getElementById(k);
            if (el) el.checked = !!s[k];
            if (el) el.onchange = async () => {
              const payload = {}; payload[k] = el.checked;
              await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
            };
          }
        }
        load();
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html, status_code=200)

@APP.get("/commands")
def page_commands() -> HTMLResponse:
    from MedlarTV.tools.build_commands_site import render_commands_html
    return HTMLResponse(content=render_commands_html(api=True), status_code=200)


# api_chat removed


## filter endpoints removed


## personality endpoints removed


## startup/shutdown hooks removed


def configure_logging():
    configure_dependency_logging()
    from MedlarTV.log_paths import resolve_log_directory

    # Launcher runs Core in the selected root; GUI uses the same resolver.
    base = resolve_log_directory(Path.cwd())
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    targets = {
        "twitch_listener": base / "twitch_listener_log.txt",
        "commands": base / "command_handler.txt",
        "fuzzy_trigger": base / "fuzzy_trigger.txt",
        "stream": base / "stream_management.txt",
        "emotions": base / "emotions.txt",
        "mood": base / "mood_system.txt",
        "llm_brain": base / "llm_brain.txt",
        "memory": base / "memory.txt",
        "main": base / "main.txt",
    }

    for name, path in targets.items():
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        exists = any(isinstance(h, logging.FileHandler) and getattr(h, "_medlar_path", None) == str(path) for h in logger.handlers)
        if exists:
            continue
        try:
            fh = logging.FileHandler(path, encoding="utf-8")
            setattr(fh, "_medlar_path", str(path))
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception:
            pass

# Production diagnostic output reports stages/types, never exception payloads.
def initialize_systems():
    log.info("[SYSTEM] Initializing MedlarTV core…")
    if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
        print("[DEBUG main] initialize_systems() called")

    try:
        if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
            print("[DEBUG main] initializing emotional system…")
        from MedlarTV.core.emotional_system import get_emotional_system
        get_emotional_system()
        if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
            print("[DEBUG main] emotional system initialized successfully")
        log.info("[OK] Emotional system initialized")
    except Exception as e:
        log.error("[FAIL] Emotional system failed: %s", type(e).__name__)

    try:
        from MedlarTV.avatar.bridge.auth import read_bootstrap
        from MedlarTV.avatar.bridge.runtime import start_publisher
        from MedlarTV.core.llm_brain import set_mood_observer
        bootstrap = read_bootstrap("publisher")
        if bootstrap is not None:
            from MedlarTV.core.emotional_system import get_emotion_state
            label = compute_mood(get_emotion_state()).get("label", "Neutral")
            publisher = start_publisher(bootstrap, label)
            set_mood_observer(publisher.offer)
    except Exception as exc:
        log.warning("[Avatar] Publishing unavailable: %s", type(exc).__name__)

    try:
        if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
            print("[DEBUG main] loading memory…")
        load_memory()
        if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
            print("[DEBUG main] memory loaded successfully")
        log.info("[OK] Memory loaded")
    except Exception as e:
        log.error("[FAIL] Memory load failed: %s", type(e).__name__)

    try:
        if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
            print("[DEBUG main] verifying twitch tokens…")
        verified = verify_twitch_tokens()
        if verified:
            if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
                print("[DEBUG main] twitch tokens verified")
            log.info("[OK] Twitch tokens verified")
        else:
            if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
                print("[DEBUG main] twitch tokens verification failed")
            log.error("[FAIL] Twitch token verification failed")
    except Exception as e:
        log.error("[FAIL] Twitch token verification failed: %s", type(e).__name__)

def shutdown(*_):
    stopped = True
    try:
        from MedlarTV.core.llm_brain import set_mood_observer
        from MedlarTV.avatar.bridge.runtime import stop_publisher
        set_mood_observer(None)
        stopped = stop_publisher()
    except Exception as exc:
        log.warning("[Avatar] Publisher cleanup incomplete: %s", type(exc).__name__)
        stopped = False
    if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
        print("[DEBUG main] shutdown() triggered")
    log.info("\n[SYSTEM] Shutting down MedlarTV…")
    try:
        if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
            print("[DEBUG main] stopping twitch listener…")
        stopped = (stop_listener() is not False) and stopped
        if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
            print("[DEBUG main] twitch listener stopped")
    except Exception as e:
        log.error("[Shutdown] Listener stop failed: %s", type(e).__name__)
        stopped = False

    if stopped:
        log.info("[SYSTEM] All systems offline.")
    else:
        log.warning("[SYSTEM] Cleanup incomplete; background work has not finished.")
    sys.exit(0 if stopped else 1)

def _stop_requested():
    marker = os.getenv("MEDLARTV_CORE_STOP_FILE", "").strip()
    return bool(marker and Path(marker).is_file())


def main():
    if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
        print("[DEBUG main] main() started")
        print(BOOT_BANNER)
        print("[DEBUG main] boot banner printed")
    configure_logging()
    if _stop_requested():
        shutdown()
        return

    if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
        print("[DEBUG main] calling initialize_systems()")
    initialize_systems()
    if _stop_requested():
        shutdown()
        return
    if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
        print("[DEBUG main] initialize_systems() completed")

    if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
        print("[DEBUG main] calling start_listener()")
    start_listener()
    if os.getenv("MEDLARTV_DEBUG", "false").lower() == "true":
        print("[DEBUG main] start_listener() returned")

    log.info("[SYSTEM] MedlarTV is now operational.")
    log.info("Press Ctrl+C to shut down.")

    while True:
        try:
            if _stop_requested():
                shutdown()
                return
            time.sleep(1)
        except KeyboardInterrupt:
            shutdown()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    main()
