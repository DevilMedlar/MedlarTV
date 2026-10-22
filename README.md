# MedlarTV — Tactical AI Companion

Local AI with dynamic personality, Twitch integration, emotion-driven replies, and stream utilities.

## Overview
- Real-time Twitch chat listener that responds contextually and safely.
- Emotion/mood engine drives emotes and tone across replies.
- Command system with both code-defined handlers and a static command catalog site.
- Stream management helpers (title/category), moderation, timers, and event effects.

## Quick Start (Windows)
- Install Python 3.11+.
- Create a virtual environment and install dependencies:
  - `python -m venv .venv`
  - `.venv\Scripts\pip install -r requirements.txt`
- If updating an existing checkout, rerun the install command above. Twitch web search now uses `ddgs` from the main requirements.
- Set required environment variables (see “Twitch Setup”).
- Open Command Center with the virtual environment activated:
  - `python -m MedlarTV.app.medlar_app`
- Use its Twitch page to start or stop the Twitch services. For standalone command-line operation, `python launcher.py` still starts the complete stack and stops its owned processes on Ctrl+C.

## Command Center Chat
- Command Center starts its own local Ollama service when it opens, so private Chat does not require a Twitch connection. Twitch Stop leaves Ollama available; closing Command Center stops Ollama and the remaining services it started.
- Stop and Restart keep the window responsive while owned services exit. Restart waits for the old Twitch launcher to finish; pressing Stop cancels a queued restart. Closing keeps the window open during cleanup and reports incomplete shutdown if an owned process cannot be stopped.
- Core receives a private stop marker and runs listener cleanup before exiting. Each Windows launcher requests its own hidden console at creation; owned service groups inherit it for targeted Control-Break requests. LibreTranslate and Avatar use a small Python entry adapter to turn the first request into KeyboardInterrupt so their existing cleanup can run. No terminal shell is launched. Each launcher allows 15 seconds for cooperative exit within a shared 20-second cleanup budget, then escalates only its owned process trees. A launcher without a console uses the same bounded fallback. Command Center allows 25 seconds for launcher exit before a background force attempt and a 7-second watchdog; independently running or reused services are not terminated. Offline checks verify creation settings and cleanup control flow; actual window display and native cleanup still require live QA.
- Type a message and press Enter or **Send**. Replies use the existing Medlar brain and its configured Ollama model when available; the local fallback is used when Ollama is unavailable.
- This conversation is held in the control center process and is never posted to Twitch. **New chat** clears its conversation history. Close the window to discard the session.
- The **LLM Brain** toggle must be on to send. It also controls Twitch LLM replies. The **Content Filter** toggle applies to both control center and Twitch replies. A reply in progress finishes before another message or a new chat can start.
- Feature switches save immediately through validated, revision-checked atomic writes. Unknown settings and comments are preserved; quoted booleans, malformed/missing files and competing saves are rejected without replacing the configuration with defaults. A failed save restores the last confirmed checkbox values and keeps the requested change under **Retry unsaved switches**. **Reload switches** lets you keep or explicitly discard those requests after reviewing current values; failed reloads keep them. Omitted switch keys use the existing runtime defaults without writing them automatically. Configuration paths come from `MEDLARTV_ROOT` or the source checkout, not the caller directory.
- The configured `OLLAMA_URL` or `OLLAMA_HOST` determines where prompts are processed. Leave it at the default `http://127.0.0.1:11434` for local processing. Set `MEDLARTV_DEBUG=false` for quieter diagnostics. The scoped model path described below omits raw prompt text from its debug output; other application components may have their own diagnostics.

LibreTranslate runs in its own environment under this repo, separate from MedlarTV's `.venv`. Set it up once from the repo root:

```powershell
python -m venv .libretranslate\.venv
.\.libretranslate\.venv\Scripts\python.exe -m pip install -r requirements-libretranslate.txt
```

Twitch Start launches this service at `LIBRETRANSLATE_URL` (default `http://127.0.0.1:5000/translate`) and Twitch Stop shuts down only the child it launched. It reuses an already running LibreTranslate at that port without taking ownership. The launcher also supports an existing `C:/LibreTranslate` install, or a custom `LIBRETRANSLATE_HOME` and `LIBRETRANSLATE_PYTHON`. LibreTranslate pins an older `requests` release, so do not install it in MedlarTV's main `.venv`.

The Python install does **not** include language models. By default, LibreTranslate downloads all available models when it first starts. The current upstream index has 100 pairs totaling about 8.37 GiB compressed, with more disk needed after extraction; first startup can take a long time. To limit the initial model set, set `LIBRETRANSLATE_LANGUAGES` to comma-separated codes such as `en,es` before Twitch Start. If a model download was interrupted, set `LIBRETRANSLATE_UPDATE_MODELS=true` for one startup to fetch missing pairs, then unset it. `!tlang` lists only languages the running server actually supports. Until `/languages` is ready, translation commands report that the translator is offline.

## Avatar Bridge

- The optional bridge scans loopback ports 8765-8775. An exhausted range or failed bridge startup disables the bridge and console while Core continues. The console remains opt-in through `ENABLE_AVATAR_CONSOLE=1`.
- Each launcher start creates separate temporary publisher and subscriber capabilities for the configured primary Twitch channel. Dedicated anonymous child stdin pipes carry the selected endpoint and capabilities: the bridge receives both, Core receives only the publisher capability, and the console receives only the subscriber capability. They are never passed in command arguments, environment variables, files or credential storage. Unmanaged server/console starts require this authenticated bootstrap; the old unauthenticated direct-client default is removed. The launcher binds `127.0.0.1`; an explicitly bootstrapped direct server may still override `BRIDGE_HOST`.
- Authentication must complete within three seconds before delivery or channel registration. The validated capability determines permissions, independent of a client-supplied role. Only the publisher can update mood, and only the subscriber can register/unregister the configured channel. Browser-origin connections are rejected. WebSocket transport payload logging is disabled even under debug logging.
- Core publishes its initial canonical mood and subsequent Twitch generation mood labels through a background worker with one latest-value slot. Producer calls perform no socket I/O; repeated labels are deduplicated and reconnect resends current state, with retries capped at 15 seconds. The bridge retains one current mood for late console subscribers. Desktop private chat does not publish. Existing console gestures are unchanged; this adds no graphics or speech.
- Bridge input must be a JSON object. Channels are limited to 100 characters, moods to 1-64 characters, and control characters are rejected. The bridge accepts at most 64 connections including pending authentication; the subscription helper retains a 32-channel defense while authenticated clients are restricted to the primary channel. Disconnect or authentication/handshake failure removes memberships. Incoming WebSocket messages are capped at 16 KiB with a queue high-water mark of 16 frames. Sends request a two-second timeout per recipient; one failed recipient does not stop the others. Core shutdown detaches the mood hook and allows at most three seconds for publisher exit within the existing launcher cleanup budget. The console uses the same cooperative service-entry adapter as the bridge.
- Offline mocked checks cover authentication and publishing. Actual anonymous-pipe forwarding, authenticated WebSocket behavior and native shutdown still require a separate authorized local QA run.

## Twitch Setup
- Required environment variables:
  - `MEDLARTV_TWITCH_TOKEN`: Bot OAuth token (prefixed `oauth:` is ok).
  - `TWITCH_NICK`: Bot username (default `MedlarTV`).
  - `TWITCH_CHANNEL`: Target channel (e.g., `#devilmedlar`).
  - `APP_TWITCH_CLIENT_ID`: Twitch application client ID.
  - Optional: `DEVILMEDLAR_TWITCH_TOKEN` (App/Broadcaster token for channel APIs).
- The IRC listener connects and decorates replies with mood-aware emotes.
  - Source: `MedlarTV/tools/twitch_listener.py`
- Ordinary Twitch conversation answers without automatic external search or added search links. Use `!search <query>` for web results or `!wiki <topic>` for Wikipedia; their existing permissions and configuration switches still apply.
- IRC raid, sub/resub, individual gift and community gift notices are handled separately from viewer chat, including notices without trailing text. Existing event flags apply; replies retain the configured primary channel destination, including notices received in joined copilot channels. The last 256 event IDs are deduplicated by source channel in memory. General channel-point redemptions are not supported by the legacy `reward-redeemed` detector; full reward events and title/cost metadata need a separately approved EventSub integration.
- With **Content Filter** enabled, bot Twitch replies are checked after formatting, emotes, and final length limits, and again before writing queued chat. Valid blocked-word/topic saves apply without restarting. Missing or malformed policy withholds chat instead of sending unchecked text. A blocked reply can receive one fresh mood/personality redirect in the response worker; the redirect and a neutral fallback must pass the same checks or the reply is suppressed. This is literal matching, not comprehensive semantic topic detection. Private Command Center chat and viewer messages remain outside the outgoing filter; auth/PING traffic and the two internal Pokemon operational commands retain their control paths.

## Twitch Conversation Memory

- In the configured primary channel, Medlar keeps recent dialogue separately for each viewer. It retains up to 10 completed role messages, up to 64 viewer statements for six hours, and up to 24 filtered public channel messages for ten minutes. This context is in memory and ends with the Twitch process. Numbered and grouped word lists can be recalled after intervening chat with varied phrasing, including `What were those words?` for the viewer's own established list; personal facts are included only when the viewer asks about the relevant fact.
- Clear self-reports about birthdays, age, marriage, parenthood, supported favorites, an explicitly declared real name, and supported lasting health conditions can be saved by Twitch room ID and user ID in `%LOCALAPPDATA%\MedlarTV\viewer_profiles.sqlite3`. Without both IDs, no durable fact is saved. Test words and casual tangents are not profile facts. The profile store does not import interaction logs.
- Durable profiles are capped at 16 facts per viewer, 20,000 facts total, and a 32 MiB SQLite page limit. At capacity, Medlar declines new facts and tells the viewer; it does not silently delete existing facts. Corrections and forgetting remain available.
- `!forget <fact>` removes one saved fact, such as `!forget favorite color`, `!forget name`, or `!forget health condition`; `!forgetme` or `!forget all` clears the viewer's saved facts. These commands also clear that viewer's current chat context. Bare `!forget` shows usage. Other viewers' profiles are unaffected.
- Age is recorded as a dated self-report. A birth year is derived only when the birthday and the age's local date support it; conflicting direct reports require clarification. Relative birthday claims require a known viewer time zone or clarification, and dated claims cannot exceed the latest currently reachable local date. Medlar does not announce birthdays on its own. Durable profiles can save simple parenthood reports but reject favorite values with child relationships, names, ages, schools, health details, parenthetical or person-like clauses, and ambiguous second sentences.
- `My favorite food is pizza. I have diabetes.` saves `favorite_food=pizza` and a separate, explicitly self-reported `health_condition=diabetes`. Supported condition phrases are deliberately limited to avoid inferring a diagnosis from ordinary chat. A different condition requires an explicit correction; a matching negation or `!forget health condition` removes it. Health and real-name facts are excluded from routine model context and broad memory summaries. The same viewer can directly ask about a saved condition or name. Generated replies use the Twitch handle for recognized forms of address; ordinary modal and month uses of a word such as `May` remain intact. The existing Pilot and Co-Pilot role prefixes still apply.
- Email addresses, phone numbers, numbered mailing addresses, bare domains, URLs, and other recognized contact details are rejected by shared profile and session intake checks. New interaction analytics records omit free-form chat and reply text. The scoped Twitch, model, event, emote, filter, translation, and stream-management diagnostics record lengths, counts, statuses, or error types instead of raw chat, command arguments, API error bodies, and reply text because process output can be logged. Session intake drops child-identifying, school, health, and name-request text such as `Call me Vesper`, including unsupported health reports and broad descriptions such as a heart condition that are not durable facts. Public context accepts only complete recognized game or group statements, short group cheers such as `GG everyone`, and numbered teaching forms; unnumbered one-word messages remain in their author's session only. Pattern filters and name matching can miss unusual details and omit harmless chat; they are not general privacy detectors.
- Command Center Chat stays separate. The added Twitch memory applies only to the configured primary channel because replies from other joined channels still use the primary reply destination; that routing needs a separate change before cross-channel memory can be enabled.

## Components
- Core runtime: `MedlarTV/core/main.py`. Its FastAPI callables are dormant in the normal launcher path; launching Core does not serve them over HTTP.
- Twitch Listener: `MedlarTV/tools/twitch_listener.py` (connects to IRC, processes chat).
- Avatar Bridge (optional): `MedlarTV/avatar/bridge/server.py` and `avatar_client/console_client.py`.
- Emotional System: `MedlarTV/core/emotional_system.py` (state, baselines, decay, influences).
- Sentiment Lexicon: `MedlarTV/core/sentiment_advanced.py` (keyword clusters, intensifiers).
- Emote Selector: `MedlarTV/core/emotion_emote_selector.py` (maps emotions to emotes).
- Mood Vector: `MedlarTV/core/mood_system.py` (valence, energy, warmth, snark).
- Moderation/Filter: `MedlarTV/core/content_filter.py` and `core/moderation.py`.
- Stream Management: `MedlarTV/core/stream_management.py`.
- Command Handlers: `MedlarTV/core/command_handlers.py`.
- Command Catalog Builder: `MedlarTV/tools/build_commands_site.py`.

## Commands — Add/Remove
- Code-defined commands live in `MedlarTV/core/command_handlers.py`.
  - Add a handler function and register it in `COMMAND_REGISTRY` with permissions and optional usage.
  - Example template is included in the file.
  - The listener routes `!<command>` via `execute_command(...)`.
- Static command catalog lives in `MedlarTV/config/commands.yaml`.
  - Used to generate a public catalog site (see “Command Catalog Site”).
  - Each entry: `name: { response, allowed_roles, description, usage }`.
- Registry-backed and listener-inline commands are implemented in code. YAML supplies catalog text; removing a YAML entry does not disable a built-in handler. Command-editor enable/disable controls are not implemented.
- Reply for `!commands` is served by the handler; the catalog URL can be configured via environment or YAML.

## Command Catalog Site
- Build a static site from the implemented command registry, listener-inline commands, and YAML catalog text. Unimplemented YAML entries are visibly unavailable:
  - `python MedlarTV/tools/build_commands_site.py --out commands_site`
  - Publish `commands_site` folder (e.g., GitHub Pages).
- Windows convenience script: `update-commands-site.bat --public-repo "C:\path\to\medlar-commands-site"` builds into a separate, existing public checkout without staging, committing or pushing. It uses this project's virtual environment and its own script location, regardless of the caller directory. The checkout's fetch and push origins must both identify `DevilMedlar/medlar-commands-site`, as referenced by the current catalog URL; no destination is created or guessed.
- Add `--publish` only when intending to upload the catalog. The script refuses existing staged work, pre-existing edits to generated files, and branches with unrelated unpushed/divergent commits. It stages and commits only `docs/index.html` and `docs/commands.json`, then pushes that new commit to the explicitly verified origin branch. Commit/push hooks are disabled for these scoped operations; tags are not included. Each failure stops subsequent steps, with no automatic reset or rollback. A push failure can leave the new local commit for review; do not retry it blindly. No real push is needed to test these safeguards.
- The Twitch auto-reply and `!commands` can point to this site.
  - URL precedence: nonempty `COMMANDS_URL`, then `MEDLAR_COMMANDS_URL`, then explicitly configured `COMMANDS_HOST`/`COMMANDS_PORT`, then the YAML `commands` URL, then the existing public URL. Empty settings are unset; explicit HTTP and private LAN URLs are preserved.

## Emotions & Moods — Add/Align
- Add a new emotion across four places to keep consistency:
  - Defaults and baselines: `MedlarTV/core/emotional_system.py` → `DEFAULT_EMOTIONS` and `EMOTION_INFLUENCES`.
  - Sentiment keywords: `MedlarTV/core/sentiment_advanced.py` → `EMOTION_WORDS`.
  - Emote mapping: `MedlarTV/core/emotion_emote_selector.py` → `EMOTION_EMOTE_MAP`.
  - Mood contributions: `MedlarTV/core/mood_system.py` → include in energy/warmth where appropriate.
- After edits, run `python -m compileall -q MedlarTV` to validate syntax.

## Adjusting Weights & Behavior
- Baselines/Decay (per emotion):
  - Create `MedlarTV/config/emotions.yaml` to override code defaults.
  - Structure:
    ```yaml
    emotions:
      happiness: { baseline: 0.4, decay: 0.92 }
      arousal:   { baseline: 0.05, decay: 0.90 }
    ```
- Influence Graph (how one emotion affects others):
  - Edit `EMOTION_INFLUENCES` in `MedlarTV/core/emotional_system.py`.
  - Keep weights gentle; changes ripple through expression.
- Personality Trait Multipliers:
  - `MedlarTV/config/personality.yaml` → under `traits`, set per-emotion scaling (e.g., `supportive: 1.2`).
- Mood Labels and Vector:
  - Adjust logic in `MedlarTV/core/mood_system.py` for how emotions map to valence/energy/warmth/snark.
- Memory/Mood Weights (optional):
  - `MedlarTV/data/memory.yaml` can store mood weight preferences for some systems.

## Moderation & Safety
- Configure `MedlarTV/config/content_filter.yaml`:
  - `blocked_words`, `blocked_topics`, `response_modes`, `safety` (cooldowns, emoji limits).
- Link whitelist: `MedlarTV/config/link_whitelist.yaml`.
- `MedlarTV/core/moderation.py` contains a dormant moderation checker; it is not wired into normal incoming chat. Its URL parser compares actual hostnames, including URLs with user-info or ports. This repair does not enable moderation.

## Timers & Events
- Timers: `MedlarTV/config/timers.yaml` → periodic chat messages by time or chat count.
- Event rules: `MedlarTV/config/events.yaml` → Firebot-like effects (chat, shoutout, play sound) triggered on joins or custom events.
- Co-Pilots: `MedlarTV/config/copilots.yaml` and chat commands `!addcopilot`, `!removecopilot`, `!listcopilots`.

## Mood-Aware Replies
- All outgoing messages pass through decoration to append an emote based on current emotion.
  - Source: `MedlarTV/tools/twitch_listener.py`.
  - Emote mapping: `MedlarTV/core/emotion_emote_selector.py`.

## Logging & Diagnostics
- Interaction logs in `logs/` directory (JSONL files).
- Toggle logging via `ENABLE_INTERACTION_LOGGING` env var.
- Health checks: `!status` reports bounded readiness evidence, including skipped or unverified checks.
- Diagnostics default to static source/safe-config inspection. Saving requires an explicit local destination. Full private capture additionally requires explicit opt-in and a sensitive-output warning; it is not the default.

## Environment Reference (Common)
- `MEDLARTV_TWITCH_TOKEN`, `TWITCH_NICK`, `TWITCH_CHANNEL`, `APP_TWITCH_CLIENT_ID`.
- `MEDLARTV_DEBUG=true` to enable verbose diagnostics.
- `ENABLE_EMOTE_RESPONSES`, `ENABLE_RAID_DETECTION`, `ENABLE_SUB_DETECTION`, `ENABLE_CHANNEL_POINTS` (feature flags).
- `COMMANDS_URL` (primary) or `MEDLAR_COMMANDS_URL` (alias) sets the command catalog link. Explicit `COMMANDS_HOST`/`COMMANDS_PORT` precedes the YAML fallback.

## Launch Details
- `launcher.py` starts Ollama, optional LibreTranslate, Avatar Bridge, and the Core runtime. The dormant Core HTTP API is not served by this normal path.
- Twitch Listener can be run via the Core or independently; launcher starts main systems and monitors them.

## Development Tips
- Keep emotion names consistent across emotional system, sentiment, emotes, and mood logic.
- Prefer configuration files in `MedlarTV/config/` for safe runtime tweaks.
- Follow existing code style and use small, incremental changes; validate with `compileall` before launching.

## Safety Notes
- Never hardcode secrets in code or YAML; use environment variables.
- Respect Twitch rate limits and message length (≤500 chars).

## Troubleshooting
- Verify tokens and client ID env vars.
- Check `logs/*.log` produced by `launcher.py` for startup issues.
- Use `MEDLARTV_DEBUG=true` to surface detailed diagnostics during development.

## Utility evidence and limits
- `!followage` distinguishes HTTP authorization/scope, rate-limit, service, transport/TLS, and malformed-response errors. Empty follower details are unverified with the existing app-token provider; they do not prove a user is not following. Detailed followage requires approved scoped user-token routing and live acceptance, which remain pending.
- Search configuration and optional backends initialize on an explicit search, rather than Core import. Invalid or nonpositive result limits fall back to the configured default.
- Counter replies report unconfirmed persistence when saving fails. Compressed sound playback uses a fixed hidden MediaPlayer program; actual playback and codec acceptance remain untested offline.
