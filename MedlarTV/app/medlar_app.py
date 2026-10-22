import sys
import os
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
try:
    if hasattr(sys, "_MEIPASS"):
        candidates = [
            os.path.join(sys._MEIPASS, "_internal", "PySide6", "plugins"),
            os.path.join(sys._MEIPASS, "PySide6", "plugins"),
        ]
        for p in candidates:
            if os.path.isdir(p):
                os.environ["QT_PLUGIN_PATH"] = p
                break
except Exception:
    pass
import time
import threading
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QPushButton, QHBoxLayout, QLabel, QCheckBox, QLineEdit, QPlainTextEdit, QFrame, QStackedWidget, QScrollArea, QMessageBox
from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtGui import QIcon
from dotenv import load_dotenv
import subprocess
import signal
import yaml

# Ensure package import works in both source and frozen modes
try:
    import MedlarTV  # noqa: F401
except Exception:
    candidates = [
        Path.cwd(),
        Path(sys.executable).resolve().parent.parent,
        Path(__file__).resolve().parents[2],
        Path(os.path.expanduser("~")) / "medlartv",
        Path("C:/Users/znorr/medlartv"),
    ]
    for base in candidates:
        if (base / "MedlarTV").exists():
            sys.path.insert(0, str(base))
            break

# Defer MedlarTV imports to runtime to avoid startup failures in packaged mode


def _ollama_endpoint_open(root=None):
    """Check the same Ollama API endpoint used by desktop chat."""
    from MedlarTV.core.ollama_endpoint import ollama_api_ready

    try:
        return ollama_api_ready(timeout=0.2)
    except ValueError:
        return False


def _irc_connected_after_log_chunk(chunk: bytes, previous: bool = False) -> bool:
    """Return the latest explicit IRC state found in a current-run log chunk."""
    text = chunk.decode("utf-8", errors="ignore")
    states = {
        "connected": text.rfind("[IRC] State: CONNECTED"),
        "disconnected": text.rfind("[IRC] State: DISCONNECTED"),
        "auth_failed": text.rfind("[IRC] State: AUTH_FAILED"),
        "join_failed": text.rfind("[IRC] State: JOIN_FAILED"),
        "retrying": text.rfind("[IRC] State: RETRYING"),
        "connecting": text.rfind("[IRC] State: CONNECTING"),
    }
    latest_state, latest_at = max(states.items(), key=lambda item: item[1])
    return latest_state == "connected" if latest_at >= 0 else previous


def _verification_status_after_log_chunk(chunk, previous="PENDING"):
    """Only an aggregate success makes verification OK; latest failure wins."""
    stages = {b"stage=broadcaster_validation", b"stage=broadcaster_scope",
              b"stage=app_token", b"stage=channel_lookup", b"stage=complete"}
    for line in chunk.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue
        _, marker, payload = line.partition(b"[TwitchVerify] ")
        fields = payload.split() if marker else []
        if len(fields) >= 2 and fields[0] in stages:
            if fields[1] == b"outcome=failed":
                previous = "FAIL"
            elif fields[0] == b"stage=complete" and fields[1] == b"outcome=ok":
                previous = "OK"
        elif b"verify_twitch_tokens() -> True" in line:
            previous = "OK"
        elif b"verify_twitch_tokens() -> False" in line or b"Twitch token verification FAILED" in line:
            previous = "FAIL"
    return previous


class _VerificationLog:
    """Track appended records from one Start, with bounded reads and line buffering."""
    def __init__(self, path):
        self.path, self.offset, self.identity = path, 0, None
        self.status, self.pending = "PENDING", b""
        self.anchor = None
        try:
            info = path.stat()
            self.offset, self.identity = info.st_size, (info.st_dev, info.st_ino)
            with path.open("rb") as stream:
                self.anchor = self._anchor_at(stream, self.offset)
        except OSError:
            pass

    @staticmethod
    def _anchor_at(stream, offset):
        stream.seek(max(0, offset - 64))
        return stream.read(min(64, offset))

    def read_status(self):
        try:
            with self.path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                identity = (info.st_dev, info.st_ino)
                rewritten = identity != self.identity or info.st_size < self.offset
                if not rewritten and self.anchor is not None:
                    rewritten = self._anchor_at(stream, self.offset) != self.anchor
                if rewritten:
                    self.offset, self.pending, self.status = 0, b"", "PENDING"
                self.identity = identity
                stream.seek(self.offset)
                chunk = stream.read(65536)
                self.offset += len(chunk)
                self.anchor = self._anchor_at(stream, self.offset)
            buffered = self.pending + chunk
            end = buffered.rfind(b"\n") + 1
            self.status = _verification_status_after_log_chunk(buffered[:end], self.status)
            self.pending = buffered[end:][-2048:]
            return self.status if self.offset >= info.st_size else "PENDING"
        except OSError:
            return "PENDING"


class _IRCLog:
    """Track complete IRC records for one Start, with identity and bounded reads."""
    def __init__(self, path):
        self.path, self.offset, self.identity = path, 0, None
        self.status, self.pending = False, b""
        self.anchor = None
        try:
            info = path.stat()
            self.offset, self.identity = info.st_size, (info.st_dev, info.st_ino)
            with path.open("rb") as stream:
                self.anchor = self._anchor_at(stream, self.offset)
        except OSError:
            pass

    @staticmethod
    def _anchor_at(stream, offset):
        stream.seek(max(0, offset - 64))
        return stream.read(min(64, offset))

    def read_status(self):
        try:
            with self.path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                identity = (info.st_dev, info.st_ino)
                rewritten = identity != self.identity or info.st_size < self.offset
                if not rewritten and self.anchor is not None:
                    rewritten = self._anchor_at(stream, self.offset) != self.anchor
                if rewritten:
                    self.offset, self.pending, self.status = 0, b"", False
                self.identity = identity
                stream.seek(self.offset)
                chunk = stream.read(65536)
                self.offset += len(chunk)
                self.anchor = self._anchor_at(stream, self.offset)
            buffered = self.pending + chunk
            end = buffered.rfind(b"\n") + 1
            self.status = _irc_connected_after_log_chunk(buffered[:end], self.status)
            self.pending = buffered[end:][-2048:]
            return self.status if self.offset >= info.st_size else False
        except OSError:
            return False


class ControlCenter(QMainWindow):
    chat_response = Signal(str, str, object)

    def __init__(self):
        super().__init__()
        try:
            load_dotenv()
        except Exception:
            pass
        self.setWindowTitle("Medlar Control Center")
        self.setMinimumSize(640, 540)
        self.resize(840, 650)

        try:
            icon_path_env = os.getenv("APP_ICON_PATH", "").strip()
            icon_candidates = []
            if icon_path_env:
                icon_candidates.append(Path(icon_path_env))
            bases = [
                Path(__file__).resolve().parent,
                Path(__file__).resolve().parents[1] / "app",
                Path.cwd() / "MedlarTV" / "app",
                Path.cwd(),
                Path("C:/Users/znorr/medlartv"),
            ]
            for b in bases:
                icon_candidates.extend([
                    b / "medlar.ico",
                ])
            for p in icon_candidates:
                try:
                    if p.exists():
                        self.setWindowIcon(QIcon(str(p)))
                        break
                except Exception:
                    pass
        except Exception:
            pass

        self.status_label = QLabel("Status: Idle")
        self.status_label.setAlignment(Qt.AlignLeft)
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("serviceStatus")
        self.launcher_proc = None
        self._stop_file = None
        self._twitch_listener_log_offset = None
        self._twitch_irc_scan_offset = None
        self._twitch_irc_connected = False
        self._twitch_verification_log = None
        self.ollama_proc = None
        self._ollama_stop_file = None
        self._ollama_ready = False
        self._ollama_starting = False
        self._ollama_error = None
        self._ollama_start_deadline = 0.0
        self._shutdown_failure = None
        self._stop_jobs = {}
        self._restart_pending = False
        self._close_pending = False
        self._close_ready = False
        self._stop_timer = QTimer(self)
        self._stop_timer.timeout.connect(self._poll_stops)

        self.btn_start = QPushButton("Start Twitch services")
        self.btn_stop = QPushButton("Stop Twitch services")
        self.btn_restart = QPushButton("Restart Twitch services")

        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_restart.clicked.connect(self.on_restart)

        self.chk_llm = QCheckBox("LLM Brain")
        self.chk_pcg = QCheckBox("Pokémon Auto-Catch")
        self.chk_filter = QCheckBox("Content Filter")
        self.chk_timers = QCheckBox("Timers")
        self.chk_fuzzy = QCheckBox("Fuzzy Trigger")
        self.chk_memory = QCheckBox("Viewer memory")
        self.chk_memory.setToolTip("Remember and recall viewer facts and chat context. Off clears temporary context; saved facts and Forget remain available.")

        try:
            for c in (self.chk_llm, self.chk_pcg, self.chk_filter, self.chk_timers, self.chk_fuzzy, self.chk_memory):
                c.setTristate(False)
        except Exception:
            pass

        self.chk_llm.clicked.connect(lambda checked: self.update_toggle("llm_brain", checked))
        self.chk_pcg.clicked.connect(lambda checked: self.update_toggle("pcg_auto_catch", checked))
        self.chk_filter.clicked.connect(lambda checked: self.update_toggle("content_filter", checked))
        self.chk_timers.clicked.connect(lambda checked: self.update_toggle("timers", checked))
        self.chk_fuzzy.clicked.connect(lambda checked: self.update_toggle("fuzzy_trigger", checked))
        self.chk_memory.clicked.connect(lambda checked: self.update_toggle("remember_users", checked))

        self.chat_transcript = QPlainTextEdit()
        self.chat_transcript.setReadOnly(True)
        self.chat_transcript.setPlaceholderText("Ask Medlar something to start a conversation.")
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Message Medlar...")
        self.chat_input.setMaxLength(1000)
        self.chat_input.returnPressed.connect(self.send_chat_message)
        self.btn_chat_send = QPushButton("Send")
        self.btn_chat_send.clicked.connect(self.send_chat_message)
        self.btn_chat_clear = QPushButton("New chat")
        self.btn_chat_clear.clicked.connect(self.clear_chat)
        self.chat_status = QLabel("Ready. Chat messages are not posted to Twitch.")
        self._chat_history = []
        self._chat_busy = False
        self._chat_closed = False
        self.chat_response.connect(self._on_chat_response)
        self._build_pages()

        # Explicit checkout selection: discovery must not replace the root
        # already used by settings, and service validation uses the same choice.
        if not os.getenv("MEDLARTV_ROOT", "").strip():
            try:
                bases = [
                    Path(sys.executable).resolve().parents[3] if hasattr(sys, "_MEIPASS") or sys.executable else Path.cwd(),
                    Path(__file__).resolve().parents[2],
                    Path.cwd(),
                    Path(os.path.expanduser("~")) / "medlartv",
                    Path("C:/Users/znorr/medlartv"),
                ]
                root = None
                for b in bases:
                    try:
                        if (b / "MedlarTV" / "config").exists():
                            root = b
                            break
                    except Exception:
                        pass
                if root:
                    os.environ["MEDLARTV_ROOT"] = str(root)
            except Exception:
                pass

        try:
            self.refresh_settings()
        except Exception:
            pass

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(2000)
        self._ollama_timer = QTimer(self)
        self._ollama_timer.timeout.connect(self._check_ollama_startup)
        QTimer.singleShot(0, self._start_ollama)

        try:
            app = QApplication.instance()
            if app:
                app.installEventFilter(self)
        except Exception:
            pass

    def _build_pages(self):
        central = QWidget()
        central.setObjectName("commandCenter")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        brand = QLabel("MEDLAR  /  COMMAND CENTER")
        brand.setObjectName("brand")
        layout.addWidget(brand)

        layout.addWidget(self.status_label)

        self.pages = QStackedWidget()
        self.home_page = QWidget()
        self.chat_page = QWidget()
        self.twitch_page = QWidget()
        self.settings_page = QWidget()
        self._pages = {
            "home": self.home_page,
            "chat": self.chat_page,
            "twitch": self.twitch_page,
            "settings": self.settings_page,
        }
        for page in self._pages.values():
            page.setObjectName("page")
            self.pages.addWidget(page)
        layout.addWidget(self.pages, 1)

        def heading(page_layout, title, description):
            title_label = QLabel(title)
            title_label.setObjectName("pageTitle")
            page_layout.addWidget(title_label)
            if description:
                detail_label = QLabel(description)
                detail_label.setObjectName("pageDetail")
                detail_label.setWordWrap(True)
                page_layout.addWidget(detail_label)

        def back_button(page_layout):
            button = QPushButton("← Back to Home")
            button.setObjectName("backButton")
            button.clicked.connect(lambda _checked=False: self.navigate_to("home"))
            page_layout.addWidget(button, alignment=Qt.AlignLeft)
            return button

        home_layout = QVBoxLayout(self.home_page)
        home_layout.setContentsMargins(0, 8, 0, 0)
        home_layout.setSpacing(14)
        heading(home_layout, "Hi", "")

        def menu_card(title, detail, action, destination):
            card = QFrame()
            card.setObjectName("menuCard")
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(18, 14, 18, 14)
            text_layout = QVBoxLayout()
            text_layout.setSpacing(4)
            label = QLabel(title)
            label.setObjectName("cardTitle")
            text_layout.addWidget(label)
            description = QLabel(detail)
            description.setObjectName("cardDetail")
            description.setWordWrap(True)
            text_layout.addWidget(description)
            card_layout.addLayout(text_layout, 1)
            button = QPushButton(action)
            button.clicked.connect(lambda _checked=False: self.navigate_to(destination))
            card_layout.addWidget(button)
            home_layout.addWidget(card)
            return button

        self.btn_home_chat = menu_card("Private Chat", "A private conversation with Medlar. Nothing here is posted to Twitch.", "Open Chat  →", "chat")
        self.btn_home_twitch = menu_card("Twitch", "Control the Medlar service stack and see its status.", "Open Twitch  →", "twitch")
        self.btn_home_settings = menu_card("Settings", "Edit personality, features, blocked lists, and connections.", "Open Settings  →", "settings")
        home_layout.addStretch(1)

        chat_layout = QVBoxLayout(self.chat_page)
        chat_layout.setContentsMargins(0, 8, 0, 0)
        chat_layout.setSpacing(10)
        self.btn_chat_home = back_button(chat_layout)
        heading(chat_layout, "Private chat", "Talk to Medlar here without sending messages to Twitch.")
        coupling = QLabel("Command Center starts local Ollama when it opens. Private chat does not require a Twitch connection.")
        coupling.setObjectName("pageDetail")
        coupling.setWordWrap(True)
        chat_layout.addWidget(coupling)
        chat_layout.addWidget(self.chat_transcript, 1)
        chat_row = QHBoxLayout()
        chat_row.setSpacing(8)
        chat_row.addWidget(self.chat_input, 1)
        chat_row.addWidget(self.btn_chat_send)
        chat_row.addWidget(self.btn_chat_clear)
        chat_layout.addLayout(chat_row)
        chat_layout.addWidget(self.chat_status)

        twitch_layout = QVBoxLayout(self.twitch_page)
        twitch_layout.setContentsMargins(0, 8, 0, 0)
        twitch_layout.setSpacing(14)
        self.btn_twitch_home = back_button(twitch_layout)
        heading(twitch_layout, "Twitch services", "Connect or disconnect Twitch while keeping the local brain available for private chat.")
        service_card = QFrame()
        service_card.setObjectName("menuCard")
        service_layout = QVBoxLayout(service_card)
        service_layout.setContentsMargins(18, 18, 18, 18)
        service_title = QLabel("Service controls")
        service_title.setObjectName("cardTitle")
        service_layout.addWidget(service_title)
        service_note = QLabel("Stopping Twitch leaves Command Center's Ollama running. Closing Command Center stops both.")
        service_note.setObjectName("cardDetail")
        service_note.setWordWrap(True)
        service_layout.addWidget(service_note)
        service_row = QHBoxLayout()
        service_row.setSpacing(8)
        self.btn_start.setObjectName("primaryButton")
        service_row.addWidget(self.btn_start)
        service_row.addWidget(self.btn_stop)
        service_row.addWidget(self.btn_restart)
        service_layout.addLayout(service_row)
        twitch_layout.addWidget(service_card)
        twitch_layout.addStretch(1)

        settings_layout = QVBoxLayout(self.settings_page)
        settings_layout.setContentsMargins(0, 8, 0, 0)
        settings_layout.setSpacing(10)
        self.btn_settings_home = back_button(settings_layout)
        heading(settings_layout, "Settings", "Edit Medlar's personality, feature switches, blocked lists, and saved connections.")
        self._build_settings_pages(settings_layout)

        central.setStyleSheet("""
            QWidget#commandCenter { background-color: #0f1522; color: #e9f0f8; font-family: 'Segoe UI'; font-size: 13px; }
            QWidget#page { background-color: transparent; }
            QLabel { color: #e9f0f8; }
            QLabel#brand, QLabel#sectionTitle { color: #75d7eb; font-size: 11px; font-weight: bold; }
            QLabel#pageTitle { font-size: 25px; font-weight: bold; }
            QLabel#pageDetail, QLabel#cardDetail { color: #b2c2d2; }
            QLabel#cardTitle { font-size: 17px; font-weight: bold; }
            QLabel#serviceStatus { background-color: #172b3c; border: 1px solid #31566a; border-radius: 8px; padding: 10px 12px; }
            QFrame#menuCard { background-color: #192437; border: 1px solid #344459; border-radius: 10px; }
            QScrollArea#settingsSidebar { background-color: #192437; border: 1px solid #344459; border-radius: 10px; }
            QWidget#settingsSidebarContent { background-color: #192437; }
            QPushButton#settingsCategory { text-align: left; border-color: transparent; background-color: transparent; }
            QPushButton#settingsCategory:hover { background-color: #304960; }
            QPushButton#settingsCategory[selected="true"] { background-color: #17647a; border-color: #2793a5; }
            QScrollArea, QScrollArea > QWidget > QWidget { background-color: transparent; border: none; }
            QPushButton { background-color: #24344b; color: #e9f0f8; border: 1px solid #3b526a; border-radius: 7px; padding: 9px 13px; }
            QPushButton:hover { background-color: #304960; }
            QPushButton[selected="true"], QPushButton#primaryButton { background-color: #17647a; border-color: #2793a5; }
            QPushButton#backButton { background-color: transparent; border: none; color: #75d7eb; padding-left: 0; }
            QPushButton:disabled { color: #8295a8; background-color: #1b2736; }
            QPlainTextEdit, QLineEdit { background-color: #111b2a; color: #e9f0f8; border: 1px solid #40546a; border-radius: 7px; padding: 9px; selection-background-color: #17647a; }
            QCheckBox { color: #e9f0f8; spacing: 9px; padding: 3px 0; }
        """)
        self.setCentralWidget(central)
        self.navigate_to("home")

    def navigate_to(self, page: str):
        target = self._pages.get(page)
        if target is None:
            return
        self.pages.setCurrentWidget(target)

    def _build_settings_pages(self, layout):
        settings_row = QHBoxLayout()
        settings_row.setSpacing(12)
        self.settings_sidebar = QScrollArea()
        self.settings_sidebar.setObjectName("settingsSidebar")
        self.settings_sidebar.setAccessibleName("Settings categories")
        self.settings_sidebar.setWidgetResizable(True)
        self.settings_sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.settings_sidebar.setFixedWidth(184)
        sidebar_content = QWidget()
        sidebar_content.setObjectName("settingsSidebarContent")
        sidebar_layout = QVBoxLayout(sidebar_content)
        sidebar_layout.setContentsMargins(10, 12, 10, 12)
        sidebar_layout.setSpacing(6)
        sidebar_heading = QLabel("CATEGORIES")
        sidebar_heading.setObjectName("sectionTitle")
        sidebar_layout.addWidget(sidebar_heading)
        self.edit_settings_search = QLineEdit()
        self.edit_settings_search.setPlaceholderText("Search settings")
        self.edit_settings_search.setAccessibleName("Search settings by name or description")
        sidebar_layout.addWidget(self.edit_settings_search)
        self.btn_settings_personality = QPushButton("Personality")
        self.btn_settings_features = QPushButton("Features")
        self.btn_settings_moderation = QPushButton("Moderation")
        self.btn_settings_connections = QPushButton("Connections")
        self._settings_buttons = {
            "personality": self.btn_settings_personality,
            "features": self.btn_settings_features,
            "moderation": self.btn_settings_moderation,
            "connections": self.btn_settings_connections,
        }
        for name, button in self._settings_buttons.items():
            button.setObjectName("settingsCategory")
            button.setCheckable(True)
            button.setAccessibleName(f"{button.text()} settings")
            button.clicked.connect(lambda _checked=False, section=name: self.show_settings_section(section))
            sidebar_layout.addWidget(button)
        self.settings_search_status = QLabel("No matching settings")
        self.settings_search_status.setObjectName("pageDetail")
        self.settings_search_status.setWordWrap(True)
        self.settings_search_status.hide()
        sidebar_layout.addWidget(self.settings_search_status)
        sidebar_layout.addStretch(1)
        self.settings_sidebar.setWidget(sidebar_content)
        settings_row.addWidget(self.settings_sidebar)

        self.settings_stack = QStackedWidget()
        self._settings_sections = {}
        self._editor_revisions = {"personality": None, "moderation": None}
        self._editor_loaded = set()
        self._editor_snapshots = {"personality": ("", "", ""), "moderation": ("", "")}

        def section_page(name):
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            content = QWidget()
            content.setObjectName("page")
            content_layout = QVBoxLayout(content)
            content_layout.setContentsMargins(0, 6, 12, 4)
            content_layout.setSpacing(8)
            area.setWidget(content)
            self.settings_stack.addWidget(area)
            self._settings_sections[name] = area
            return content_layout

        personality = section_page("personality")
        personality.addWidget(QLabel("Name"))
        self.personality_name = QLineEdit()
        self.personality_name.setMaxLength(80)
        personality.addWidget(self.personality_name)
        personality.addWidget(QLabel("Tone"))
        self.personality_tone = QLineEdit()
        self.personality_tone.setMaxLength(1000)
        personality.addWidget(self.personality_tone)
        trigger_label = QLabel("Trigger keywords or phrases - one per line")
        trigger_label.setWordWrap(True)
        personality.addWidget(trigger_label)
        self.personality_triggers = QPlainTextEdit()
        self.personality_triggers.setPlaceholderText("medlar\nmedlartv")
        self.personality_triggers.setFixedHeight(100)
        personality.addWidget(self.personality_triggers)
        personality_actions = QHBoxLayout()
        self.btn_personality_save = QPushButton("Save personality")
        self.btn_personality_save.setObjectName("primaryButton")
        self.btn_personality_save.clicked.connect(self.save_personality)
        self.btn_personality_reload = QPushButton("Discard edits / Reload")
        self.btn_personality_reload.clicked.connect(lambda: self._request_editor_reload("personality"))
        personality_actions.addWidget(self.btn_personality_save)
        personality_actions.addWidget(self.btn_personality_reload)
        personality.addLayout(personality_actions)
        self.personality_status = QLabel("Saved changes affect the next reply or trigger check.")
        self.personality_status.setObjectName("pageDetail")
        self.personality_status.setWordWrap(True)
        personality.addWidget(self.personality_status)
        personality.addStretch(1)

        features = section_page("features")
        feature_note = QLabel("Feature switches save immediately. LLM Brain and Content Filter also affect private Chat.")
        feature_note.setObjectName("pageDetail")
        feature_note.setWordWrap(True)
        features.addWidget(feature_note)
        feature_card = QFrame()
        feature_card.setObjectName("menuCard")
        feature_card_layout = QVBoxLayout(feature_card)
        feature_card_layout.setContentsMargins(18, 12, 18, 12)
        for checkbox in (self.chk_llm, self.chk_pcg, self.chk_filter, self.chk_timers, self.chk_fuzzy, self.chk_memory):
            feature_card_layout.addWidget(checkbox)
        features.addWidget(feature_card)
        feature_actions = QHBoxLayout()
        self.btn_features_retry = QPushButton("Retry unsaved switches")
        self.btn_features_retry.setEnabled(False)
        self.btn_features_retry.clicked.connect(self.retry_feature_changes)
        self.btn_features_reload = QPushButton("Reload switches")
        self.btn_features_reload.clicked.connect(self.request_feature_reload)
        feature_actions.addWidget(self.btn_features_retry)
        feature_actions.addWidget(self.btn_features_reload)
        features.addLayout(feature_actions)
        self.features_status = QLabel("Switches save when changed.")
        self.features_status.setObjectName("pageDetail")
        self.features_status.setWordWrap(True)
        features.addWidget(self.features_status)
        features.addStretch(1)

        moderation = section_page("moderation")
        moderation_note = QLabel("Blocked words and topics filter outgoing bot replies when Content Filter is enabled. One item per line; saves apply to the next reply.")
        moderation_note.setObjectName("pageDetail")
        moderation_note.setWordWrap(True)
        moderation.addWidget(moderation_note)
        moderation.addWidget(QLabel("Blocked words"))
        self.moderation_words = QPlainTextEdit()
        self.moderation_words.setFixedHeight(90)
        moderation.addWidget(self.moderation_words)
        topics_label = QLabel("Blocked topics or phrases")
        topics_label.setWordWrap(True)
        moderation.addWidget(topics_label)
        self.moderation_topics = QPlainTextEdit()
        self.moderation_topics.setFixedHeight(90)
        moderation.addWidget(self.moderation_topics)
        moderation_actions = QHBoxLayout()
        self.btn_moderation_save = QPushButton("Save blocked lists")
        self.btn_moderation_save.setObjectName("primaryButton")
        self.btn_moderation_save.clicked.connect(self.save_moderation)
        self.btn_moderation_reload = QPushButton("Discard edits / Reload")
        self.btn_moderation_reload.clicked.connect(lambda: self._request_editor_reload("moderation"))
        moderation_actions.addWidget(self.btn_moderation_save)
        moderation_actions.addWidget(self.btn_moderation_reload)
        moderation.addLayout(moderation_actions)
        self.moderation_status = QLabel("Only the two blocked lists are edited here.")
        self.moderation_status.setObjectName("pageDetail")
        self.moderation_status.setWordWrap(True)
        moderation.addWidget(self.moderation_status)

        connections = section_page("connections")
        connection_note = QLabel("New values are saved for this Windows user, outside the project. Existing .env entries are not changed. Restart Medlar services after saving. Saved values are never shown here.")
        connection_note.setObjectName("pageDetail")
        connection_note.setWordWrap(True)
        connections.addWidget(connection_note)
        from MedlarTV.core.local_credentials import CREDENTIAL_FIELDS
        self.connection_inputs = {}
        self.connection_saved_labels = {}
        for key, label in CREDENTIAL_FIELDS.items():
            field_label = QLabel(label)
            field_label.setWordWrap(True)
            connections.addWidget(field_label)
            row = QHBoxLayout()
            field = QLineEdit()
            field.setEchoMode(QLineEdit.Password)
            field.setPlaceholderText("Enter a new value")
            row.addWidget(field, 1)
            button = QPushButton("Save")
            button.clicked.connect(lambda _checked=False, name=key: self.save_connection(name))
            row.addWidget(button)
            saved = QLabel("Not checked")
            saved.setObjectName("pageDetail")
            saved.setWordWrap(True)
            row.addWidget(saved)
            connections.addLayout(row)
            self.connection_inputs[key] = field
            self.connection_saved_labels[key] = saved
        self.connection_status = QLabel("A saved local value overrides the existing environment on the next service start.")
        self.connection_status.setObjectName("pageDetail")
        self.connection_status.setWordWrap(True)
        connections.addWidget(self.connection_status)
        connections.addStretch(1)

        settings_content = QVBoxLayout()
        settings_content.setContentsMargins(0, 0, 0, 0)
        settings_content.addWidget(self.settings_stack, 1)
        self.settings_empty_state = QLabel("No settings found. Try a different search.")
        self.settings_empty_state.setObjectName("pageDetail")
        self.settings_empty_state.setWordWrap(True)
        self.settings_empty_state.hide()
        settings_content.addWidget(self.settings_empty_state)
        settings_row.addLayout(settings_content, 1)
        layout.addLayout(settings_row, 1)
        self._settings_search_index = {
            "personality": "Personality Name Tone Trigger keywords or phrases one per line "
                           "Save personality Discard edits Reload Saved changes affect the next reply or trigger check",
            "features": "Features " + feature_note.text() + " " +
                        " ".join(checkbox.text() for checkbox in
                                 (self.chk_llm, self.chk_pcg, self.chk_filter, self.chk_timers, self.chk_fuzzy)),
            "moderation": "Moderation " + moderation_note.text() + " "
                          "Blocked words Blocked topics or phrases Save blocked lists Discard edits Reload",
            "connections": "Connections " + connection_note.text() + " " +
                           " ".join(CREDENTIAL_FIELDS.values()),
        }
        self.edit_settings_search.textChanged.connect(self._search_settings)
        self.show_settings_section("features")

    def _search_settings(self, query: str):
        terms = query.casefold().split()
        matches = []
        for name, button in self._settings_buttons.items():
            match = all(term in self._settings_search_index[name].casefold() for term in terms)
            button.setVisible(match)
            if match:
                matches.append(name)
        found = bool(matches)
        self.settings_search_status.setVisible(not found)
        self.settings_stack.setVisible(found)
        self.settings_empty_state.setVisible(not found)
        if found and self._current_settings_section() not in matches:
            self.show_settings_section(matches[0])

    def _current_settings_section(self):
        current = self.settings_stack.currentWidget()
        return next(name for name, widget in self._settings_sections.items() if widget == current)

    def show_settings_section(self, section: str):
        target = self._settings_sections.get(section)
        if target is None:
            return
        self.settings_stack.setCurrentWidget(target)
        for name, button in self._settings_buttons.items():
            button.setChecked(name == section)
            button.setProperty("selected", name == section)
            button.style().unpolish(button)
            button.style().polish(button)
        if section == "personality" and section not in self._editor_loaded:
            self.reload_personality()
        elif section == "moderation" and section not in self._editor_loaded:
            self.reload_moderation()
        elif section == "connections":
            self.refresh_connection_status()

    def _config_file(self, name: str) -> Path:
        return self._settings_path().parent / name

    def _editor_contents(self, section: str):
        if section == "personality":
            return (self.personality_name.text(), self.personality_tone.text(), self.personality_triggers.toPlainText())
        return (self.moderation_words.toPlainText(), self.moderation_topics.toPlainText())

    def _request_editor_reload(self, section: str):
        reload_editor = self.reload_personality if section == "personality" else self.reload_moderation
        if self._editor_contents(section) == self._editor_snapshots[section]:
            return reload_editor()
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Unsaved settings")
        dialog.setText(f"Save changes to {section} before reloading?")
        dialog.setInformativeText("Reloading will discard changes that have not been saved.")
        save_button = dialog.addButton("Save", QMessageBox.AcceptRole)
        discard_button = dialog.addButton("Discard", QMessageBox.DestructiveRole)
        dialog.addButton("Cancel", QMessageBox.RejectRole)
        dialog.setDefaultButton(save_button)
        dialog.exec()
        if dialog.clickedButton() == save_button:
            return self.save_personality() if section == "personality" else self.save_moderation()
        if dialog.clickedButton() == discard_button:
            return reload_editor()
        return False

    def reload_personality(self):
        from MedlarTV.app.config_editor_store import load_personality
        try:
            fields, revision = load_personality(self._config_file("personality.yaml"))
        except (OSError, ValueError) as exc:
            self.personality_status.setText(f"Could not load personality: {exc}")
            return False
        self.personality_name.setText(fields["name"])
        self.personality_tone.setText(fields["tone"])
        self.personality_triggers.setPlainText("\n".join(fields["trigger_keywords"]))
        self._editor_revisions["personality"] = revision
        self._editor_loaded.add("personality")
        self._editor_snapshots["personality"] = self._editor_contents("personality")
        self.personality_status.setText("Loaded. Changes affect the next reply or trigger check.")
        return True

    def save_personality(self):
        from MedlarTV.app.config_editor_store import save_personality
        revision = self._editor_revisions["personality"]
        if revision is None:
            self.personality_status.setText("Load personality before saving.")
            return False
        try:
            updated = save_personality(
                self._config_file("personality.yaml"),
                self.personality_name.text(),
                self.personality_tone.text(),
                [line.strip() for line in self.personality_triggers.toPlainText().splitlines() if line.strip()],
                revision,
            )
        except (OSError, ValueError) as exc:
            self.personality_status.setText(f"Not saved: {exc}")
            return False
        if self.reload_personality() and self._editor_revisions["personality"] == updated:
            self.personality_status.setText("Saved. Changes affect the next reply or trigger check.")
            return True
        return False

    def reload_moderation(self):
        from MedlarTV.app.config_editor_store import load_moderation
        try:
            fields, revision = load_moderation(self._config_file("content_filter.yaml"))
        except (OSError, ValueError) as exc:
            self.moderation_status.setText(f"Could not load blocked lists: {exc}")
            return False
        self.moderation_words.setPlainText("\n".join(fields["blocked_words"]))
        self.moderation_topics.setPlainText("\n".join(fields["blocked_topics"]))
        self._editor_revisions["moderation"] = revision
        self._editor_loaded.add("moderation")
        self._editor_snapshots["moderation"] = self._editor_contents("moderation")
        self.moderation_status.setText("Loaded. Changes affect the next filtered reply.")
        return True

    def save_moderation(self):
        from MedlarTV.app.config_editor_store import save_moderation
        revision = self._editor_revisions["moderation"]
        if revision is None:
            self.moderation_status.setText("Load blocked lists before saving.")
            return False
        try:
            updated = save_moderation(
                self._config_file("content_filter.yaml"),
                [line.strip() for line in self.moderation_words.toPlainText().splitlines() if line.strip()],
                [line.strip() for line in self.moderation_topics.toPlainText().splitlines() if line.strip()],
                revision,
            )
        except (OSError, ValueError) as exc:
            self.moderation_status.setText(f"Not saved: {exc}")
            return False
        if self.reload_moderation() and self._editor_revisions["moderation"] == updated:
            self.moderation_status.setText("Saved. Changes affect the next filtered reply.")
            return True
        return False

    def refresh_connection_status(self):
        from MedlarTV.core.local_credentials import credential_present
        try:
            for key, label in self.connection_saved_labels.items():
                label.setText("Saved locally" if credential_present(key) else "No local value")
        except OSError:
            self.connection_status.setText("Could not check local credentials on this computer.")

    def save_connection(self, key: str):
        from MedlarTV.core.local_credentials import save_credential
        field = self.connection_inputs[key]
        value = field.text()
        if not value.strip():
            self.connection_status.setText("Enter a new value before saving.")
            return
        try:
            save_credential(key, value)
        except (OSError, ValueError):
            self.connection_status.setText("Could not save this connection on this computer.")
            return
        field.clear()
        self.connection_saved_labels[key].setText("Saved locally")
        self.connection_status.setText("Saved locally. Restart Medlar services to use the new value; existing .env was not changed.")

    def refresh_settings(self):
        try:
            values = self._load_settings()
        except (OSError, ValueError) as exc:
            self._feature_load_failed = True
            self._apply_feature_values(editable=False)
            self.features_status.setText(f"Could not load feature switches: {type(exc).__name__}. Fix the file and reload; nothing was changed.")
            return False
        self._feature_values = values
        self._feature_load_failed = False
        self._apply_feature_values()
        self.features_status.setText(f"Feature switches loaded. Pending requests: {self._feature_request_summary()}. Retry applies these requests."
                                     if getattr(self, "_feature_pending", {}) else "Feature switches loaded.")
        return True

    def _feature_request_summary(self):
        labels = {"llm_brain": "LLM Brain", "pcg_auto_catch": "Pokémon Auto-Catch",
                  "ignore_viewer_pokecatch": "Ignore viewer Pokémon commands", "content_filter": "Content Filter",
                  "timers": "Timers", "fuzzy_trigger": "Fuzzy Trigger", "remember_users": "Viewer memory"}
        return "; ".join(f"{labels[key]}: {'on' if value else 'off'}"
                         for key, value in getattr(self, "_feature_pending", {}).items())

    def _apply_feature_values(self, editable=True):
        values = getattr(self, "_feature_values", {})
        widgets = {"llm_brain": self.chk_llm, "pcg_auto_catch": self.chk_pcg,
                   "content_filter": self.chk_filter, "timers": self.chk_timers, "fuzzy_trigger": self.chk_fuzzy,
                   "remember_users": self.chk_memory}
        for key, widget in widgets.items():
            blocked = widget.blockSignals(True)
            try:
                widget.setChecked(values.get(key, False))
                widget.setEnabled(editable and bool(values) and not getattr(self, "_feature_load_failed", False))
            finally:
                widget.blockSignals(blocked)
        self.btn_features_retry.setEnabled(bool(getattr(self, "_feature_pending", {})))

    def retry_feature_changes(self):
        pending = dict(getattr(self, "_feature_pending", {}))
        if not pending:
            return True
        try:
            self._save_settings(pending)
        except (OSError, ValueError) as exc:
            self._apply_feature_values()
            self.features_status.setText(f"Not saved: {exc}. Requests retained ({self._feature_request_summary()}); reload and review if the file changed.")
            return False
        self._feature_values.update(pending)
        self._feature_pending.clear()
        self._apply_feature_values()
        if not self.refresh_settings():
            self.features_status.setText("Feature switches saved, but reload failed. Confirmed saved values retained; fix the file and reload.")
            return False
        self.features_status.setText("Feature switches saved, but the save lock remains; review it before another save."
                                     if getattr(self, "_feature_lock_cleanup_failed", False) else "Feature switches saved.")
        return True

    def request_feature_reload(self):
        discard = False
        if getattr(self, "_feature_pending", {}):
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Unsaved switches")
            dialog.setText("Reload current feature values for review?")
            keep = dialog.addButton("Keep requests and reload", QMessageBox.AcceptRole)
            drop = dialog.addButton("Discard requests and reload", QMessageBox.DestructiveRole)
            cancel = dialog.addButton("Cancel", QMessageBox.RejectRole)
            dialog.setDefaultButton(cancel)
            dialog.exec()
            if dialog.clickedButton() not in (keep, drop):
                return False
            discard = dialog.clickedButton() is drop
        if not self.refresh_settings():
            return False
        if discard:
            self._feature_pending.clear()
            self._apply_feature_values()
            self.features_status.setText("Feature switches reloaded. Unsaved requests discarded.")
        return True

    def send_chat_message(self):
        """Start one reply at a time without blocking the Qt event loop."""
        if self._chat_busy or self._chat_closed:
            return
        message = self.chat_input.text().strip()
        if not message:
            return
        if not self.chk_llm.isChecked():
            self.chat_status.setText("Enable LLM Brain to chat with Medlar.")
            return
        if not self._ollama_ready or self.ollama_proc is None or self.ollama_proc.poll() is not None:
            self._ollama_ready = False
            self.chat_status.setText(self._ollama_error or "Local Ollama is starting. Please try again shortly.")
            return

        self._chat_busy = True
        self.chat_input.clear()
        self.chat_input.setEnabled(False)
        self.btn_chat_send.setEnabled(False)
        self.btn_chat_clear.setEnabled(False)
        self.chat_transcript.appendPlainText(f"You: {message}")
        self.chat_status.setText("Medlar is thinking...")
        history = list(self._chat_history)
        filter_enabled = self.chk_filter.isChecked()
        try:
            threading.Thread(
                target=self._generate_chat_reply,
                args=(message, history, filter_enabled),
                daemon=True,
                name="medlar-control-chat",
            ).start()
        except Exception as exc:
            self._on_chat_response("", str(exc), history)

    def _generate_chat_reply(self, message: str, history: list, filter_enabled: bool):
        try:
            from MedlarTV.core.llm_brain import generate_response

            reply = generate_response(message, "Operator", history=history, context="desktop")
            if not reply:
                raise RuntimeError("Medlar returned an empty reply")
            if filter_enabled:
                from MedlarTV.core.content_filter import filter_message, get_safety_response

                allowed, filtered, _ = filter_message(reply)
                reply = filtered if allowed and filtered is not None else get_safety_response()
                if history and history[-1].get("role") == "assistant":
                    history[-1] = {"role": "assistant", "content": reply}
            self.chat_response.emit(reply, "", history)
        except Exception as exc:
            try:
                self.chat_response.emit("", str(exc), history)
            except RuntimeError:
                pass  # The window was closed while the worker was finishing.

    def _on_chat_response(self, reply: str, error: str, history: list):
        if self._chat_closed:
            return
        self._chat_busy = False
        self.chat_input.setEnabled(True)
        self.btn_chat_send.setEnabled(True)
        self.btn_chat_clear.setEnabled(True)
        if error:
            self.chat_status.setText(f"Reply failed: {error}")
        else:
            self._chat_history = history
            self.chat_transcript.appendPlainText(f"Medlar: {reply}\n")
            self.chat_status.setText("Ready. Chat messages are not posted to Twitch.")
        self.chat_input.setFocus()

    def clear_chat(self):
        if self._chat_busy:
            return
        self._chat_history = []
        self.chat_transcript.clear()
        self.chat_status.setText("New chat started.")

    def update_toggle(self, key: str, value: bool):
        from MedlarTV.app.config_editor_store import FEATURE_DEFAULTS
        if key not in FEATURE_DEFAULTS or type(value) is not bool:
            self._apply_feature_values()
            self.features_status.setText("Not saved: invalid feature switch or value.")
            return False
        if not hasattr(self, "_feature_pending"):
            self._feature_pending = {}
        self._feature_pending[key] = value
        return self.retry_feature_changes()

    def _settings_path(self) -> Path:
        env_root = os.getenv("MEDLARTV_ROOT", "").strip()
        base = Path(env_root) if env_root else Path(__file__).resolve().parents[2]
        p = base / "MedlarTV" / "config" / "app_settings.yaml"
        return p

    def _load_settings(self) -> dict:
        from MedlarTV.app.config_editor_store import load_features
        path = self._settings_path().resolve()
        values, revision = load_features(path)
        self._feature_values = values
        self._feature_revision = revision
        self._feature_path = path
        return dict(values)

    def _save_settings(self, data: dict) -> str:
        from MedlarTV.app.config_editor_store import save_features, ConfigConflictError, FeatureSaveCleanupError
        revision = getattr(self, "_feature_revision", None)
        if revision is None:
            raise ConfigConflictError("Load valid feature settings before saving")
        path = self._settings_path().resolve()
        if path != self._feature_path:
            raise ConfigConflictError("Feature configuration location changed; reload before saving")
        try:
            updated = save_features(path, data, revision)
        except FeatureSaveCleanupError as exc:
            updated = exc.revision
            self._feature_lock_cleanup_failed = True
        else:
            self._feature_lock_cleanup_failed = False
        self._feature_revision = updated
        return updated

    def refresh_status(self):
        if getattr(self, "_stop_jobs", None):
            return
        if self._shutdown_failure:
            self.status_label.setText(self._shutdown_failure)
            return
        brain = getattr(self, "ollama_proc", None)
        if brain is not None and brain.poll() is not None and self._ollama_ready:
            self._ollama_failed("Local Ollama exited unexpectedly.")
        brain_status = "starting" if self._ollama_starting else "ready" if self._ollama_ready else "unavailable"
        launcher = self.launcher_proc
        if launcher is None or launcher.poll() is not None:
            twitch_status = "stopped" if launcher is None else "launcher exited"
            if self._ollama_error:
                self.status_label.setText(f"Status: Twitch {twitch_status} | {self._ollama_error}")
            else:
                self.status_label.setText(f"Status: Twitch {twitch_status} | Ollama {brain_status}")
            return
        irc = False
        ollama = self._ollama_ready
        verification = getattr(self, "_twitch_verification_log", None)
        twitch_verification = verification.read_status() if verification is not None else "PENDING"
        # No baseline means no current-attempt evidence. Never search other roots
        # or rescan historical logs to infer a running connection.
        cursor = getattr(self, "_twitch_irc_log", None)
        irc = cursor.read_status() if cursor is not None else False
        self.status_label.setText(f"Status: Twitch services running | IRC={'OK' if irc else 'OFF'} | Ollama={'OK' if ollama else 'OFF'} | Twitch={twitch_verification}")

    def _service_root(self) -> Path:
        # A supplied root is authoritative; missing prerequisites must not
        # redirect services into a different checkout from the settings editors.
        explicit_root = os.getenv("MEDLARTV_ROOT", "").strip()
        if explicit_root:
            root = Path(explicit_root)
            if (root / "launcher.py").is_file() and (root / ".venv" / "Scripts" / "python.exe").is_file():
                return root
            raise FileNotFoundError("Configured MEDLARTV_ROOT is missing launcher.py or its virtual environment")
        candidates = [Path(__file__).resolve().parents[2], Path.cwd()]
        try:
            candidates.insert(0, Path(sys.executable).resolve().parents[3])
        except IndexError:
            pass
        candidates.extend((Path(os.path.expanduser("~")) / "medlartv", Path("C:/Users/znorr/medlartv")))
        for root in candidates:
            if (root / "launcher.py").is_file() and (root / ".venv" / "Scripts" / "python.exe").is_file():
                return root
        raise FileNotFoundError("Medlar launcher.py and its virtual environment were not found")

    def _spawn_launcher_mode(self, mode: str):
        root = self._service_root()
        self._root = root
        python = root / ".venv" / "Scripts" / "python.exe"
        stop_file = Path(tempfile.gettempdir()) / f"medlartv-{mode}-stop-{uuid.uuid4().hex}.flag"
        env = os.environ.copy()
        env["MEDLARTV_STOP_FILE"] = str(stop_file)
        env["MEDLARTV_OWNER_PID"] = str(os.getpid())
        env["MEDLARTV_ROOT"] = str(root)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if mode == "ollama":
            from MedlarTV.core.local_credentials import CREDENTIAL_FIELDS

            for key in CREDENTIAL_FIELDS:
                env.pop(key, None)
        flags = 0
        startupinfo = None
        if os.name == "nt":
            # One private hidden console per launcher. Services inherit it and
            # create separate groups; GUI and the other launcher cannot receive
            # their targeted Control-Break event.
            flags = subprocess.CREATE_NEW_CONSOLE
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        from MedlarTV.log_paths import resolve_log_directory

        # Pin the child's inherited log destination to the same Start baseline.
        logs_dir = resolve_log_directory(root, env)
        logs_dir.mkdir(parents=True, exist_ok=True)
        if mode == "twitch":
            self._twitch_irc_log = _IRCLog(logs_dir / "twitch_listener_log.txt")
            self._twitch_verification_log = _VerificationLog(logs_dir / "stream_management.txt")
        spawn_log = None
        try:
            spawn_log = (logs_dir / f"{mode}_spawn.log").open("a", encoding="utf-8")
        except OSError:
            pass
        try:
            proc = subprocess.Popen(
                [str(python), str(root / "launcher.py"), f"--mode={mode}"],
                cwd=str(root), env=env, shell=False,
                stdout=spawn_log if spawn_log else subprocess.DEVNULL,
                stderr=spawn_log if spawn_log else subprocess.DEVNULL,
                creationflags=flags, startupinfo=startupinfo, start_new_session=os.name != "nt",
            )
        finally:
            if spawn_log is not None:
                spawn_log.close()
        return proc, stop_file

    def _ollama_failed(self, reason: str):
        self._ollama_timer.stop()
        self._ollama_ready = False
        self._ollama_starting = False
        self._ollama_error = reason
        self.status_label.setText(f"Status: Local brain unavailable ({reason})")
        self.chat_status.setText(reason)

    def _start_ollama(self):
        if getattr(self, "_close_pending", False) or self._chat_closed:
            return
        """Start the app-owned Ollama launcher after Qt has shown the window."""
        if self._chat_closed or self.ollama_proc is not None:
            return
        try:
            self._service_root()
            if _ollama_endpoint_open():
                self._ollama_failed("Ollama is already running outside Command Center; close it and reopen the app.")
                return
            proc, stop_file = self._spawn_launcher_mode("ollama")
        except Exception as exc:
            self._ollama_failed(f"Could not start local Ollama: {exc}")
            return
        self.ollama_proc = proc
        self._ollama_stop_file = stop_file
        self._ollama_starting = True
        self._ollama_error = None
        self._ollama_start_deadline = time.monotonic() + 20
        self.status_label.setText("Status: Starting local brain...")
        self.chat_status.setText("Starting local Ollama for private chat...")
        self._ollama_timer.start(250)

    def _check_ollama_startup(self):
        if self._chat_closed:
            self._ollama_timer.stop()
            return
        proc = self.ollama_proc
        if proc is None:
            self._ollama_failed("Local Ollama did not start.")
            return
        rc = proc.poll()
        if rc is not None:
            self._ollama_failed(f"Local Ollama exited during startup ({rc}).")
            return
        if _ollama_endpoint_open():
            self._ollama_timer.stop()
            self._ollama_ready = True
            self._ollama_starting = False
            self._ollama_error = None
            self.chat_status.setText("Ready. Chat messages are not posted to Twitch.")
            self.refresh_status()
            return
        if time.monotonic() >= self._ollama_start_deadline:
            self._ollama_error = "Local Ollama did not become ready in time."
            self.chat_status.setText(self._ollama_error)
            if self._stop_ollama():
                self._ollama_failed("Local Ollama did not become ready in time.")

    def on_start(self):
        """Connect Twitch services without taking ownership of Ollama."""
        if getattr(self, "_stop_jobs", None) or getattr(self, "_close_pending", False) or self._chat_closed:
            self.status_label.setText("Status: Waiting for owned services to stop")
            return
        if self._shutdown_failure:
            self.status_label.setText(self._shutdown_failure)
            return
        if self.launcher_proc is not None and self.launcher_proc.poll() is None:
            self.status_label.setText("Status: Twitch services already running")
            return
        if not self._ollama_ready or self.ollama_proc is None or self.ollama_proc.poll() is not None:
            self.status_label.setText(f"Status: {self._ollama_error}" if self._ollama_error
                                      else "Status: Wait for local Ollama before starting Twitch services")
            return
        try:
            proc, stop_file = self._spawn_launcher_mode("twitch")
        except Exception as exc:
            self.status_label.setText(f"Status: Twitch start failed ({exc})")
            return
        self.launcher_proc = proc
        self._stop_file = stop_file
        self._shutdown_failure = None
        QTimer.singleShot(800, self._post_start_check)
        self.status_label.setText("Status: Starting Twitch services...")

    def _post_start_check(self):
        try:
            if self._chat_closed:
                return
            if hasattr(self, "launcher_proc") and self.launcher_proc:
                rc = self.launcher_proc.poll()
                if rc is not None:
                    try:
                        logs_dir = Path.cwd() / "logs"
                        with (logs_dir / "control_center.log").open("a", encoding="utf-8") as f:
                            f.write(f"start failed rc={rc}\n")
                    except Exception:
                        pass
                    if rc == 2:
                        if os.name == "nt":
                            self.status_label.setText("Status: Twitch launcher exited; its Windows job closed")
                        else:
                            self._shutdown_failure = "Status: Twitch shutdown incomplete (launcher reported a live child)"
                            self.status_label.setText(self._shutdown_failure)
                    else:
                        self.status_label.setText(f"Status: Twitch launcher exited ({rc}); local brain remains available")
        except Exception:
            pass

    def _force_stop_owned_launcher(self, proc):
        """Fallback only for the launcher PID started by this window and its descendants."""
        if proc.poll() is not None:
            return
        if (proc is not self.launcher_proc and proc is not self.ollama_proc) or (
                not isinstance(proc.pid, int) or isinstance(proc.pid, bool)
                or proc.pid <= 0 or proc.pid == os.getpid()):
            raise ValueError("Launcher ownership does not match")
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=1)

    def on_stop(self):
        """Request Twitch shutdown; returning False also means still stopping."""
        self._restart_pending = False
        if not getattr(self, "_stop_jobs", None):
            self._shutdown_failure = None
        return self._begin_stop("twitch")

    def _stop_ollama(self):
        self._ollama_timer.stop()
        self._ollama_ready = False
        self._ollama_starting = False
        return self._begin_stop("ollama")

    def _begin_stop(self, mode):
        """Never wait for a child or taskkill on the Qt thread."""
        if not hasattr(self, "_stop_jobs"):
            self._stop_jobs = {}
            self._stop_timer = QTimer(self)
            self._stop_timer.timeout.connect(self._poll_stops)
        proc = self.launcher_proc if mode == "twitch" else self.ollama_proc
        marker = self._stop_file if mode == "twitch" else self._ollama_stop_file
        if proc is None:
            return True
        if mode in self._stop_jobs:
            return False
        job = {"proc": proc, "marker": marker, "deadline": time.monotonic() + 25,
               "forced": False, "done": threading.Event(), "error": None}
        self._stop_jobs[mode] = job
        try:
            if proc.poll() is None:
                if marker is None:
                    raise OSError("No stop marker for owned launcher")
                marker.write_text("stop", encoding="utf-8")
        except OSError as exc:
            # Keep the same grace period even when file IPC is unavailable.
            job["request_error"] = type(exc).__name__
        self.status_label.setText("Status: Stopping owned services...")
        self._stop_timer.start(100)
        self._poll_stops()
        return mode not in self._stop_jobs and proc.poll() is not None and not self._shutdown_failure

    def _poll_stops(self):
        for mode, job in list(self._stop_jobs.items()):
            proc = job["proc"]
            rc = proc.poll()
            if rc is not None:
                # Wait for an in-flight force call before permitting a restart.
                if job["forced"] and not job["done"].is_set():
                    continue
                try:
                    if rc == 2 and os.name != "nt":
                        raise RuntimeError("launcher reported incomplete child shutdown")
                    if job["marker"] is not None:
                        job["marker"].unlink(missing_ok=True)
                    if mode == "twitch" and self.launcher_proc is proc:
                        self.launcher_proc = None
                        self._stop_file = None
                    elif mode == "ollama" and self.ollama_proc is proc:
                        self.ollama_proc = None
                        self._ollama_stop_file = None
                    self._stop_jobs.pop(mode)
                except (OSError, RuntimeError) as exc:
                    self._stop_failed(mode, exc)
            elif not job["forced"] and time.monotonic() >= job["deadline"]:
                job["forced"] = True
                job["deadline"] = time.monotonic() + 7
                def force(current=job):
                    try:
                        self._force_stop_owned_launcher(current["proc"])
                    except Exception as exc:
                        current["error"] = type(exc).__name__
                    finally:
                        current["done"].set()
                threading.Thread(target=force, name=f"Medlar-stop-{mode}", daemon=True).start()
            elif job["forced"] and (job["done"].is_set() or time.monotonic() >= job["deadline"]):
                self._stop_failed(mode, RuntimeError(job["error"] or "owned launcher is still running"))
        if self._stop_jobs:
            return
        self._stop_timer.stop()
        if self._shutdown_failure:
            self._restart_pending = False
            self._close_pending = False
            self._chat_closed = False
            self.status_label.setText(self._shutdown_failure)
            return
        self.status_label.setText("Status: Twitch stopped | Ollama ready" if self._ollama_ready
                                  else "Status: Twitch stopped | Ollama unavailable")
        if getattr(self, "_close_pending", False):
            # closeEvent starts both requests before arming this completion.
            if self.launcher_proc is None and self.ollama_proc is None:
                self._close_ready = True
                QTimer.singleShot(0, self.close)
        elif getattr(self, "_restart_pending", False):
            self._restart_pending = False
            self.on_start()

    def _stop_failed(self, mode, exc):
        job = self._stop_jobs.get(mode)
        # Retain ownership until a force worker returns, even past its deadline.
        if job is None or not job["forced"] or job["done"].is_set():
            self._stop_jobs.pop(mode, None)
        self._shutdown_failure = f"Status: Shutdown incomplete ({mode}: {exc})"
        self._restart_pending = False
        self._close_pending = False
        self._chat_closed = False
        self.status_label.setText(self._shutdown_failure)

    def _shutdown_all(self):
        """Request both independently owned launchers without sequential waits."""
        twitch_stopped = self.on_stop()
        ollama_stopped = self._stop_ollama()
        return twitch_stopped and ollama_stopped

    # Resolve non-credential intent before beginning owned-service shutdown.
    # Connection fields keep their existing individual Save behavior and are
    # neither inspected nor included in this combined save operation.
    def _restore_close_drafts(self, drafts, feature_pending):
        if "personality" in drafts:
            name, tone, triggers = drafts["personality"]
            self.personality_name.setText(name)
            self.personality_tone.setText(tone)
            self.personality_triggers.setPlainText(triggers)
        if "moderation" in drafts:
            words, topics = drafts["moderation"]
            self.moderation_words.setPlainText(words)
            self.moderation_topics.setPlainText(topics)
        if feature_pending:
            self._feature_pending = dict(feature_pending)
            self._apply_feature_values()

    def _confirm_close_settings(self):
        snapshots = getattr(self, "_editor_snapshots", {})
        drafts = {section: self._editor_contents(section)
                  for section in ("personality", "moderation") if section in snapshots
                  and self._editor_contents(section) != snapshots[section]}
        feature_pending = dict(getattr(self, "_feature_pending", {}))
        if not drafts and not feature_pending:
            return True
        labels = {"personality": "Personality", "moderation": "Blocked lists"}
        sections = [labels[section] for section in drafts]
        if feature_pending:
            sections.append("Feature switches")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Unsaved settings")
        dialog.setText("Save unsaved settings before closing?")
        dialog.setInformativeText("Unsaved: " + ", ".join(sections)
                                  + ". Connection values are excluded; use their individual Save buttons.")
        save_button = dialog.addButton("Save", QMessageBox.AcceptRole)
        discard_button = dialog.addButton("Discard", QMessageBox.DestructiveRole)
        cancel_button = dialog.addButton("Cancel", QMessageBox.RejectRole)
        dialog.setDefaultButton(cancel_button)
        dialog.exec()
        selected = dialog.clickedButton()
        if selected == discard_button:
            return True
        if selected != save_button:
            return False
        try:
            for section in drafts:
                save = self.save_personality if section == "personality" else self.save_moderation
                if not save():
                    raise ValueError("A settings section did not finish saving")
            if feature_pending and not self.retry_feature_changes():
                raise ValueError("Feature requests did not finish saving")
        except Exception:
            # Multi-section saves can partially succeed. Keep current revisions
            # and saved files, but restore exact visible intent for review/retry.
            self._restore_close_drafts(drafts, feature_pending)
            QMessageBox.warning(self, "Settings not fully saved",
                                "The app remains open and drafts are retained. Some sections may already be saved; review their status.")
            return False
        return True

    def closeEvent(self, event):
        if getattr(self, "_close_ready", False):
            super().closeEvent(event)
            return
        if not getattr(self, "_close_pending", False) and not self._confirm_close_settings():
            event.ignore()
            return
        self._chat_closed = True
        stopped = self._shutdown_all()
        if not stopped:
            self._close_pending = not bool(self._shutdown_failure)
            self._chat_closed = self._close_pending
            event.ignore()
            return
        self._close_ready = True
        super().closeEvent(event)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Quit and not getattr(self, "_close_ready", False):
            self.close()
            return True
        return super().eventFilter(watched, event)

    def on_restart(self):
        if self.on_stop():
            self.on_start()
        elif not self._shutdown_failure and not getattr(self, "_close_pending", False):
            self._restart_pending = True


def main():
    # simple file logging to help diagnose startup issues
    _log_file = None
    try:
        logs_dir = Path.cwd() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        _log_file = logs_dir / "control_center.log"
        with _log_file.open("a", encoding="utf-8") as f:
            f.write("launching app\n")
    except Exception:
        pass

    try:
        app = QApplication(sys.argv)
    except Exception as e:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"Failed to create QApplication: {e}", "Medlar Control Center", 0)
        except Exception:
            pass
        return
    try:
        app.setQuitOnLastWindowClosed(True)
    except Exception:
        pass

    try:
        w = ControlCenter()
    except Exception as e:
        try:
            if _log_file:
                with _log_file.open("a", encoding="utf-8") as f:
                    f.write(f"control center init failed: {e}\n")
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"Control Center failed: {e}", "Medlar Control Center", 0)
        except Exception:
            pass
        return
    w.show()
    try:
        if _log_file:
            with _log_file.open("a", encoding="utf-8") as f:
                f.write("window shown\n")
    except Exception:
        pass
    try:
        w.raise_(); w.activateWindow()
    except Exception:
        pass
    try:
        t = QTimer()
        t.setSingleShot(True)
        def _bring():
            try:
                w.showNormal(); w.raise_(); w.activateWindow()
            except Exception:
                pass
        t.timeout.connect(_bring)
        t.start(250)
    except Exception:
        pass
    try:
        if _log_file:
            with _log_file.open("a", encoding="utf-8") as f:
                f.write("entering event loop\n")
    except Exception:
        pass
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
