"""
LTC to LV1 — main application window (tkinter + ttk).

Threading model:
  - All UI updates happen on the MAIN thread.
  - All shared state lives in AppController (lv1, audio, engine, cue_list,
    settings, scene catalog, etc.). MainWindow subscribes to the controller's
    event bus and marshals every event onto the main thread via after_idle().
  - Audio runs in its own thread, talks via queue.Queue[Timecode] which is
    drained by MainWindow's 25 Hz poll loop into controller.drain_tc_queue().
  - LV1Client runs its reader on its own thread; callbacks land on the
    controller which then re-emits onto the bus.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

try:
    import certifi as _certifi
    _SSL_CTX: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=_certifi.where())
except Exception:
    _SSL_CTX = None

from app_controller import (
    AppController,
    EVT_CUES,
    EVT_CUE_FIRED,
    EVT_DIRTY,
    EVT_DISCOVERY,
    EVT_LAST_FIRE,
    EVT_LV1_CATALOG,
    EVT_LV1_CURRENT,
    EVT_LV1_STATE,
    EVT_RECENT,
    EVT_RUNNING,
    EVT_SETTINGS,
    EVT_STATUS,
    EVT_TC,
)
from models import (
    CUE_FILE_DESCRIPTION,
    CUE_FILE_EXTENSION,
    Cue,
    ensure_projects_dir,
)
from scene_resolver import validate_all


# --- Constants --------------------------------------------------------------

_VERSION = "2.0.1"
_GH_OWNER_REPO = "miglourenco/ltctolv1"
_RELEASES_API = f"https://api.github.com/repos/{_GH_OWNER_REPO}/releases/latest"
_RELEASES_URL = f"https://github.com/{_GH_OWNER_REPO}/releases"

# Colour palette (matches the original dark theme)
_BG       = "#1E1E1E"
_BG_PAN   = "#252526"
_BG_WID   = "#3C3C3C"
_BG_TC    = "#0A0A0A"
_BG_SEL   = "#094771"
_BG_HDR   = "#2D2D2D"
_BORDER   = "#3C3C3C"

_FG       = "#CCCCCC"
_FG_HEAD  = "#888888"
_FG_DIM   = "#555555"

_TC_ON    = "#00FF41"
_TC_OFF   = "#1A3A1A"

_FG_OK    = "#4EC9B0"  # teal
_FG_ERR   = "#F44747"  # red
_FG_WARN  = "#D7BA7D"  # gold
_FG_FIRE  = "#4EC9B0"  # teal (cue fired flash)
_FG_DIS   = "#505050"  # disabled rows

# Cue-status colours
_STATUS_COLOR = {
    "OK":        "#4EC9B0",
    "RECOVERED": "#D7BA7D",
    "MISSING":   "#F44747",
    "EMPTY":     "#888888",
}

_BTN_BG  = "#383838"
_BTN_FG  = "#CCCCCC"
_BTN_ABG = "#505050"
_GO_BG   = "#166534"
_GO_ABG  = "#15803D"
_GO_DIS  = "#0D3D20"
_ST_BG   = "#7F1D1D"
_ST_ABG  = "#991B1B"
_ST_DIS  = "#3D1010"

_F_UI   = ("Segoe UI", 9)            if sys.platform == "win32" else ("Helvetica Neue", 11)
_F_UIB  = ("Segoe UI", 9, "bold")    if sys.platform == "win32" else ("Helvetica Neue", 11, "bold")
_F_TC   = ("Courier New", 52, "bold")
_F_FPS  = ("Courier New", 11)
_F_MONO = ("Courier New", 10)


# --- Button factory ---------------------------------------------------------


class _FlatButton(tk.Label):
    """tk.Label acting as a flat button — bg/fg respected on macOS too."""

    def __init__(
        self,
        parent,
        text: str,
        command,
        bg: str = _BTN_BG,
        abg: str = _BTN_ABG,
        fg: str = _BTN_FG,
        font=_F_UI,
        width: Optional[int] = None,
        px: int = 8,
        py: int = 3,
    ) -> None:
        super().__init__(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=font,
            padx=px,
            pady=py,
            cursor="hand2",
        )
        if width is not None:
            self.configure(width=width)
        self._cmd = command
        self._bg = bg
        self._abg = abg
        self._fg = fg
        self._enabled = True
        self.bind("<Button-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _press(self, _e=None):
        if not self._enabled:
            return
        self.configure(bg=self._abg)

    def _release(self, _e=None):
        if not self._enabled:
            return
        self.configure(bg=self._bg)
        if self._cmd:
            try:
                self._cmd()
            except Exception as exc:  # noqa: BLE001
                print(f"[button] {exc}")

    def _enter(self, _e=None):
        if self._enabled:
            self.configure(bg="#444")

    def _leave(self, _e=None):
        if self._enabled:
            self.configure(bg=self._bg)

    def config(self, **kw):  # type: ignore[override]
        if "state" in kw:
            st = kw.pop("state")
            self._enabled = st != "disabled"
            if self._enabled:
                super().config(bg=self._bg, fg=self._fg, cursor="hand2")
            else:
                super().config(bg=self._bg, fg=_FG_DIM, cursor="arrow")
        if kw:
            super().config(**kw)


def _btn(parent, text, cmd, **kw) -> _FlatButton:
    return _FlatButton(parent, text, cmd, **kw)


# --- Cue dialog -------------------------------------------------------------


class CueDialog(tk.Toplevel):
    """Add / edit a single cue."""

    def __init__(
        self,
        parent: tk.Tk,
        cue: Optional[Cue],
        scene_catalog: Dict[int, str],
        on_save,
    ) -> None:
        super().__init__(parent)
        self.title("Edit cue" if cue else "Add cue")
        self.configure(bg=_BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._on_save = on_save
        self._cue = cue
        self._catalog = scene_catalog

        body = tk.Frame(self, bg=_BG_PAN, padx=12, pady=12,
                        highlightthickness=1, highlightbackground=_BORDER)
        body.pack(padx=10, pady=10)

        # Timecode
        tk.Label(body, text="Timecode (HH:MM:SS:FF)", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI).grid(row=0, column=0, sticky="w", pady=2)
        self._tc_var = tk.StringVar(value=cue.timecode if cue else "00:00:00:00")
        tk.Entry(body, textvariable=self._tc_var, width=18, font=_F_MONO,
                 bg=_BG_WID, fg=_FG, insertbackground=_FG,
                 highlightthickness=0).grid(row=0, column=1, sticky="w", pady=2)

        # Label
        tk.Label(body, text="Label", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI).grid(row=1, column=0, sticky="w", pady=2)
        self._label_var = tk.StringVar(value=cue.label if cue else "")
        tk.Entry(body, textvariable=self._label_var, width=36, font=_F_UI,
                 bg=_BG_WID, fg=_FG, insertbackground=_FG,
                 highlightthickness=0).grid(row=1, column=1, sticky="w", pady=2)

        # Scene picker — dropdown built from the catalog
        tk.Label(body, text="LV1 Scene", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI).grid(row=2, column=0, sticky="w", pady=2)
        scene_row = tk.Frame(body, bg=_BG_PAN)
        scene_row.grid(row=2, column=1, sticky="w", pady=2)

        choices: List[str] = ["(custom — type below)"]
        for idx in sorted(scene_catalog):
            choices.append(f"[{idx}] {scene_catalog[idx]}")

        self._scene_choice_var = tk.StringVar(value=choices[0])
        cb = ttk.Combobox(scene_row, textvariable=self._scene_choice_var,
                          values=choices, state="readonly", width=36, font=_F_UI)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", self._on_scene_picked)

        sub = tk.Frame(body, bg=_BG_PAN)
        sub.grid(row=3, column=1, sticky="w", pady=2)
        tk.Label(sub, text="Name", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI).pack(side="left")
        self._scene_name_var = tk.StringVar(value=cue.scene_name if cue else "")
        tk.Entry(sub, textvariable=self._scene_name_var, width=22, font=_F_UI,
                 bg=_BG_WID, fg=_FG, insertbackground=_FG,
                 highlightthickness=0).pack(side="left", padx=(4, 8))
        tk.Label(sub, text="Index", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI).pack(side="left")
        self._scene_idx_var = tk.StringVar(
            value=str(cue.scene_index) if (cue and cue.scene_index is not None) else "0"
        )
        tk.Entry(sub, textvariable=self._scene_idx_var, width=6, font=_F_UI,
                 bg=_BG_WID, fg=_FG, insertbackground=_FG,
                 highlightthickness=0).pack(side="left", padx=4)

        if cue and cue.scene_name:
            for ch in choices[1:]:
                if ch.endswith(f"] {cue.scene_name}"):
                    self._scene_choice_var.set(ch)
                    break

        self._enabled_var = tk.BooleanVar(value=cue.enabled if cue else True)
        ttk.Checkbutton(body, text="Enabled", variable=self._enabled_var).grid(
            row=4, column=1, sticky="w", pady=(8, 0)
        )

        btns = tk.Frame(body, bg=_BG_PAN)
        btns.grid(row=5, column=0, columnspan=2, pady=(12, 0))
        _btn(btns, "Save", self._ok, bg=_GO_BG, abg=_GO_ABG, fg="#FFFFFF",
             width=8, px=14, py=5).pack(side="left", padx=4)
        _btn(btns, "Cancel", self.destroy, width=8, px=14, py=5).pack(side="left", padx=4)

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _on_scene_picked(self, _e=None) -> None:
        sel = self._scene_choice_var.get()
        if not sel.startswith("["):
            return
        try:
            rb = sel.index("]")
            idx = int(sel[1:rb])
            name = sel[rb + 1 :].strip()
        except (ValueError, IndexError):
            return
        self._scene_name_var.set(name)
        self._scene_idx_var.set(str(idx))

    def _ok(self) -> None:
        tc = self._tc_var.get().strip()
        if not tc:
            messagebox.showerror("Invalid", "Timecode is required.", parent=self)
            return
        try:
            idx_raw = self._scene_idx_var.get().strip()
            scene_index = int(idx_raw) if idx_raw else None
        except ValueError:
            messagebox.showerror("Invalid", "Scene index must be an integer.", parent=self)
            return
        self._on_save(
            timecode=tc,
            label=self._label_var.get().strip(),
            scene_name=self._scene_name_var.get().strip(),
            scene_index=scene_index,
            enabled=self._enabled_var.get(),
        )
        self.destroy()


# --- Main window ------------------------------------------------------------


class MainWindow:
    def __init__(self, root: tk.Tk, controller: AppController) -> None:
        self.root = root
        self.ctl = controller
        self.settings = controller.settings
        self.root.configure(bg=_BG)

        # UI-only state (display widgets, drag state, throttling)
        self._flash_after: Optional[str] = None
        self._last_tc_time: int = 0          # frames since last TC, for "no signal" timeout
        self._audio_devices: List[Dict[str, Any]] = []
        self._validated_once: bool = False
        # Set during _on_close so late after_idle callbacks bail before touching
        # destroyed widgets. The controller might still emit events for a few
        # milliseconds while LV1 reader thread and Flask requests wind down.
        self._shutting_down: bool = False
        # True while the window is withdrawn to the system tray. The poll loop
        # checks this to skip widget mutations (the engine still ticks so
        # cues continue to fire from the tray).
        self._ui_hidden: bool = False
        # Populated by main.py if the tray icon successfully starts.
        self._tray = None

        # Subscribe to the controller's event bus. Every callback marshals
        # back to the tk main thread via after_idle().
        self._unsub = self.ctl.subscribe(self._on_controller_event)

        # Register the "bring desktop UI to front" hook so the web remote's
        # Open UI button can call back into this window. Imported lazily so
        # the web_server module's not required at import time for headless
        # smoke tests.
        try:
            import web_server
            web_server.set_show_ui_hook(
                lambda: self.root.after_idle(self._show_and_focus)
            )
        except Exception:
            pass

        self._apply_theme()
        self._build_ui()
        self._refresh_audio_devices()
        self._start_discovery()
        self._restore_device_selection()
        self._poll_queue()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.settings.last_cue_file and os.path.isfile(self.settings.last_cue_file):
            self._load_cue_file(self.settings.last_cue_file)

        # Defer auto-connect until discovery has finished — see _on_discovery_done.
        self._auto_connect_pending = bool(self.settings.lv1_selected or self.settings.lv1_host)

        # Silent update check on startup (no popup unless an update is available)
        self.root.after(4000, lambda: self._check_updates(silent_if_ok=True))

    # === Controller bus → marshal to main thread ============================

    def _on_controller_event(self, name: str, payload: Dict[str, Any]) -> None:
        """Called on whatever thread the event was emitted from. Schedule the
        handler on the tk main loop so widget access is safe."""
        if self._shutting_down:
            return
        try:
            self.root.after_idle(lambda: self._dispatch_event(name, payload))
        except (RuntimeError, tk.TclError):
            # tk may already be shutting down — ignore.
            pass

    def _dispatch_event(self, name: str, payload: Dict[str, Any]) -> None:
        # Guard against events that landed AFTER the user closed the window:
        # after_idle fires before destroy completes, and any tk call against a
        # destroyed widget raises TclError. Bail cleanly.
        if self._shutting_down:
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            self._dispatch_event_inner(name, payload)
        except tk.TclError:
            # Widget destroyed between winfo_exists() and the call.
            pass

    def _dispatch_event_inner(self, name: str, payload: Dict[str, Any]) -> None:
        if name == EVT_TC:
            self._render_tc_from_event(payload)
        elif name == EVT_RUNNING:
            self._render_running_from_event(payload)
        elif name == EVT_LV1_STATE:
            self._render_lv1_state_from_event(payload)
        elif name == EVT_LV1_CATALOG:
            self._refresh_catalog_tree()
            self._refresh_tree()
            if not self._validated_once:
                self._validated_once = True
                self._show_validation_warnings()
        elif name == EVT_LV1_CURRENT:
            idx = payload.get("index")
            if idx is None:
                self._cur_scene_label.config(text="—", fg=_FG_DIM)
            else:
                name_str = self.ctl.scene_catalog.get(idx, "(unknown)")
                self._cur_scene_label.config(text=f"[{idx}] {name_str}", fg=_FG_OK)
            self._refresh_catalog_tree()
        elif name == EVT_CUES:
            self._refresh_tree()
            self._update_file_label()
        elif name == EVT_CUE_FIRED:
            self._refresh_tree()
        elif name == EVT_LAST_FIRE:
            self._render_last_fire(payload)
        elif name == EVT_DIRTY:
            self._update_file_label()
        elif name == EVT_DISCOVERY:
            self._render_discovery_from_event(payload)
        elif name == EVT_STATUS:
            self._set_status_label(payload.get("text", ""), bool(payload.get("warn")))
        elif name == EVT_RECENT:
            self._rebuild_recent_menu()
        elif name == EVT_SETTINGS:
            pass  # nothing to refresh — settings widgets are write-through

    # === Theme ==============================================================

    def _apply_theme(self) -> None:
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass

        s.configure(".", background=_BG, foreground=_FG,
                    fieldbackground=_BG_WID, troughcolor=_BG_PAN,
                    selectbackground=_BG_SEL, selectforeground=_FG,
                    bordercolor=_BORDER, darkcolor=_BG_PAN, lightcolor=_BG_PAN,
                    relief="flat")
        s.configure("TFrame", background=_BG)
        s.configure("TLabel", background=_BG, foreground=_FG, font=_F_UI)
        for w in ("TEntry", "TSpinbox"):
            s.configure(w, fieldbackground=_BG_WID, foreground=_FG,
                        bordercolor=_BORDER, lightcolor=_BG_WID,
                        darkcolor=_BG_WID, insertcolor=_FG,
                        arrowcolor=_FG_HEAD, relief="flat")
        s.configure("TCombobox", fieldbackground=_BG_WID, foreground=_FG,
                    bordercolor=_BORDER, lightcolor=_BG_WID, darkcolor=_BG_WID,
                    arrowcolor=_FG_HEAD, selectbackground=_BG_WID,
                    selectforeground=_FG, relief="flat")
        s.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", _BG_WID),
                ("disabled", _BG_PAN),
            ],
            foreground=[
                ("readonly", _FG),
                ("disabled", _FG_DIM),
            ],
            arrowcolor=[
                ("disabled", _FG_DIM),
            ],
            selectbackground=[
                ("readonly", _BG_WID),
                ("disabled", _BG_PAN),
            ],
            selectforeground=[
                ("readonly", _FG),
                ("disabled", _FG_DIM),
            ],
        )
        s.map(
            "TSpinbox",
            fieldbackground=[("disabled", _BG_PAN)],
            foreground=[("disabled", _FG_DIM)],
            arrowcolor=[("disabled", _FG_DIM)],
        )
        s.map(
            "TEntry",
            fieldbackground=[("disabled", _BG_PAN)],
            foreground=[("disabled", _FG_DIM)],
        )
        s.configure("TScrollbar", background=_BG_PAN, troughcolor=_BG,
                    arrowcolor=_FG_HEAD, bordercolor=_BG,
                    darkcolor=_BG_PAN, lightcolor=_BG_PAN, relief="flat")
        s.map("TScrollbar", background=[("active", "#505050")])
        s.configure("TSeparator", background=_BORDER)
        s.configure("TCheckbutton", background=_BG_PAN, foreground=_FG, font=_F_UI)
        s.map("TCheckbutton",
              background=[("active", _BG_PAN)],
              foreground=[("disabled", _FG_DIM)])
        s.configure("Treeview", background=_BG_PAN, foreground=_FG,
                    fieldbackground=_BG_PAN, bordercolor=_BORDER,
                    rowheight=24, font=_F_UI)
        s.configure("Treeview.Heading", background=_BG_HDR, foreground=_FG_HEAD,
                    relief="flat", font=_F_UIB, bordercolor=_BORDER)
        s.map("Treeview",
              background=[("selected", _BG_SEL)],
              foreground=[("selected", _FG)])
        s.map("Treeview.Heading", background=[("active", "#3A3A3A")])

    # === UI construction ====================================================

    def _build_ui(self) -> None:
        self._build_menubar()
        outer = tk.Frame(self.root, bg=_BG, padx=8, pady=8)
        outer.pack(fill="both", expand=True)
        self._build_device_panel(outer)
        self._build_tc_panel(outer)
        self._build_main_split(outer)
        self._build_footer(outer)

    def _build_menubar(self) -> None:
        menu = tk.Menu(self.root)
        self.root.config(menu=menu)
        file_m = tk.Menu(menu, tearoff=0)
        menu.add_cascade(label="File", menu=file_m)
        file_m.add_command(label="New cue list", command=self._new_list)
        file_m.add_command(label="Open…", command=self._open_list)
        # Open Recent submenu — rebuilt on every EVT_RECENT so the entries
        # stay in sync with controller state. Stored on self so the dispatcher
        # can delete/insert items.
        self._recent_menu = tk.Menu(file_m, tearoff=0)
        file_m.add_cascade(label="Open recent", menu=self._recent_menu)
        self._file_menu = file_m  # kept so the dispatcher can probe item count
        self._rebuild_recent_menu()
        file_m.add_separator()
        file_m.add_command(label="Save", command=self._save_list)
        file_m.add_command(label="Save as…", command=self._save_list_as)
        file_m.add_separator()
        file_m.add_command(label="Web remote settings…", command=self._show_web_settings)
        if sys.platform == "win32":
            file_m.add_command(label=f"Associate {CUE_FILE_EXTENSION} files with this app",
                               command=self._associate_file_type)
        file_m.add_separator()
        file_m.add_command(label="Minimize to tray", command=self._minimize_to_tray)
        file_m.add_command(label="Exit", command=self._on_close)

        help_m = tk.Menu(menu, tearoff=0)
        menu.add_cascade(label="Help", menu=help_m)
        help_m.add_command(label="Open web remote in browser",
                           command=self._open_web_remote)
        help_m.add_command(label="Check for updates…",
                           command=lambda: self._check_updates(silent_if_ok=False))
        help_m.add_separator()
        help_m.add_command(label="About LTC to LV1", command=self._show_about)

    def _build_device_panel(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=_BG)
        wrap.pack(fill="x", pady=(0, 6))
        hdr = tk.Frame(wrap, bg=_BG_HDR)
        hdr.pack(fill="x")
        tk.Label(hdr, text="DEVICES & LV1", bg=_BG_HDR, fg=_FG_HEAD,
                 font=_F_UIB, padx=8, pady=3).pack(side="left")

        body = tk.Frame(wrap, bg=_BG_PAN, padx=8, pady=6,
                        highlightthickness=1, highlightbackground=_BORDER)
        body.pack(fill="x")

        left = tk.Frame(body, bg=_BG_PAN)
        left.pack(side="left", fill="both", expand=True)

        # Audio row
        ar = tk.Frame(left, bg=_BG_PAN)
        ar.pack(fill="x", pady=2)
        tk.Label(ar, text="Audio Input", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI, width=12, anchor="w").pack(side="left")
        self._audio_var = tk.StringVar()
        self._audio_combo = ttk.Combobox(ar, textvariable=self._audio_var,
                                         width=32, state="readonly", font=_F_UI)
        self._audio_combo.pack(side="left", padx=(4, 8))
        tk.Label(ar, text="Ch", bg=_BG_PAN, fg=_FG_HEAD, font=_F_UI).pack(side="left")
        self._ch_var = tk.StringVar(value=str(self.settings.audio_channel))
        self._ch_combo = ttk.Combobox(ar, textvariable=self._ch_var,
                                      width=14, state="readonly", font=_F_UI)
        self._ch_combo.pack(side="left", padx=4)
        self._sr_force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ar, text="SR Force:", variable=self._sr_force_var,
                        command=self._on_sr_force_toggle).pack(side="left", padx=(8, 2))
        self._sr_var = tk.StringVar(value=str(self.settings.sample_rate))
        self._sr_combo = ttk.Combobox(ar, textvariable=self._sr_var,
                                      values=["44100", "48000", "96000"],
                                      width=7, state="disabled")
        self._sr_combo.pack(side="left")
        tk.Label(ar, text="Buffer:", bg=_BG_PAN, fg=_FG_HEAD, font=_F_UI).pack(side="left", padx=(8, 2))
        self._block_var = tk.StringVar(value=str(self.settings.block_size))
        self._block_combo = ttk.Combobox(
            ar, textvariable=self._block_var,
            values=["64", "128", "256", "512", "1024", "2048"],
            width=6, state="readonly", font=_F_UI,
        )
        self._block_combo.pack(side="left")
        _btn(ar, "↺", self._refresh_audio_devices, width=2, px=5, py=1).pack(side="left", padx=(6, 0))
        self._audio_combo.bind("<<ComboboxSelected>>", self._on_audio_device_changed)

        # LV1 row
        lr = tk.Frame(left, bg=_BG_PAN)
        lr.pack(fill="x", pady=2)
        tk.Label(lr, text="LV1 (discover)", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI, width=12, anchor="w").pack(side="left")
        self._lv1_disc_var = tk.StringVar()
        self._lv1_disc_combo = ttk.Combobox(lr, textvariable=self._lv1_disc_var,
                                            width=32, state="readonly", font=_F_UI)
        self._lv1_disc_combo.pack(side="left", padx=(4, 8))
        _btn(lr, "↺", self._start_discovery, width=2, px=5, py=1).pack(side="left", padx=(0, 8))

        tk.Label(lr, text="or IP:", bg=_BG_PAN, fg=_FG_HEAD, font=_F_UI).pack(side="left")
        self._lv1_host_var = tk.StringVar(value=self.settings.lv1_host)
        tk.Entry(lr, textvariable=self._lv1_host_var, width=14, font=_F_UI,
                 bg=_BG_WID, fg=_FG, insertbackground=_FG,
                 highlightthickness=0).pack(side="left", padx=4)
        tk.Label(lr, text="Port:", bg=_BG_PAN, fg=_FG_HEAD, font=_F_UI).pack(side="left")
        self._lv1_port_var = tk.StringVar(value=str(self.settings.lv1_port))
        tk.Entry(lr, textvariable=self._lv1_port_var, width=7, font=_F_UI,
                 bg=_BG_WID, fg=_FG, insertbackground=_FG,
                 highlightthickness=0).pack(side="left", padx=4)

        tf = tk.Frame(body, bg=_BG_PAN, padx=8)
        tf.pack(side="right", fill="y", pady=2)

        self._run_btn = _btn(tf, "▶  START", self._toggle_run,
                             bg=_GO_BG, abg=_GO_ABG, fg="#FFFFFF",
                             width=14, px=12, py=4,
                             font=("Segoe UI", 9, "bold"))
        self._run_btn.pack(pady=(0, 4))

        self._lv1_btn = _btn(tf, "● OFFLINE", self._toggle_lv1,
                             bg=_ST_BG, abg=_ST_ABG, fg="#FFFFFF",
                             width=14, px=12, py=4,
                             font=("Segoe UI", 9, "bold"))
        self._lv1_btn.pack()

    def _build_tc_panel(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=_BG)
        wrap.pack(fill="x", pady=(0, 6))

        tcf = tk.Frame(wrap, bg=_BG_TC,
                       highlightthickness=1, highlightbackground=_BORDER,
                       padx=14, pady=10)
        tcf.pack(side="left")
        self._tc_label = tk.Label(tcf, text="00:00:00:00", bg=_BG_TC, fg=_TC_OFF,
                                  font=_F_TC)
        self._tc_label.pack()
        row = tk.Frame(tcf, bg=_BG_TC)
        row.pack(fill="x")
        self._ltc_status = tk.Label(row, text="● Stopped", bg=_BG_TC, fg=_FG_DIM,
                                    font=_F_UI)
        self._ltc_status.pack(side="left")
        self._fps_label = tk.Label(row, text="-- fps", bg=_BG_TC, fg=_FG_DIM,
                                   font=_F_FPS)
        self._fps_label.pack(side="right")

        sf = tk.Frame(wrap, bg=_BG_PAN, padx=12, pady=8,
                      highlightthickness=1, highlightbackground=_BORDER)
        sf.pack(side="left", fill="both", expand=True, padx=(8, 0))
        tk.Label(sf, text="LV1 STATUS", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UIB).pack(anchor="w")
        self._lv1_state_label = tk.Label(sf, text="Disconnected", bg=_BG_PAN,
                                         fg=_FG_DIM, font=_F_MONO)
        self._lv1_state_label.pack(anchor="w", pady=(4, 0))
        tk.Label(sf, text="CURRENT SCENE", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UIB).pack(anchor="w", pady=(8, 0))
        self._cur_scene_label = tk.Label(sf, text="—", bg=_BG_PAN, fg=_FG_DIM,
                                         font=_F_MONO)
        self._cur_scene_label.pack(anchor="w", pady=(4, 0))
        tk.Label(sf, text="LAST FIRE", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UIB).pack(anchor="w", pady=(8, 0))
        self._last_fire_label = tk.Label(sf, text="—", bg=_BG_PAN, fg=_FG_DIM,
                                         font=_F_MONO, justify="left")
        self._last_fire_label.pack(anchor="w", pady=(4, 0))

    def _build_main_split(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=_BG)
        wrap.pack(fill="both", expand=True, pady=(0, 6))
        self._build_cue_panel(wrap)
        self._build_catalog_panel(wrap)

    def _build_cue_panel(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=_BG)
        wrap.pack(side="left", fill="both", expand=True)
        hdr = tk.Frame(wrap, bg=_BG_HDR)
        hdr.pack(fill="x")
        tk.Label(hdr, text="CUE LIST", bg=_BG_HDR, fg=_FG_HEAD,
                 font=_F_UIB, padx=8, pady=3).pack(side="left")
        self._file_label = tk.Label(hdr, text="(unsaved)", bg=_BG_HDR,
                                    fg=_FG_DIM, font=_F_UI)
        self._file_label.pack(side="right", padx=8)

        body = tk.Frame(wrap, bg=_BG_PAN, padx=4, pady=4,
                        highlightthickness=1, highlightbackground=_BORDER)
        body.pack(fill="both", expand=True)

        cols = ("order", "tc", "label", "scene", "status", "enabled")
        self._tree = ttk.Treeview(body, columns=cols, show="headings", height=15)
        for col, txt, w, anchor in [
            ("order", "#",        40,  "center"),
            ("tc", "Timecode",   110,  "center"),
            ("label", "Label",   200,  "w"),
            ("scene", "LV1 Scene", 220, "w"),
            ("status", "Status",  90,  "center"),
            ("enabled", "✓",      30,  "center"),
        ]:
            self._tree.heading(col, text=txt)
            self._tree.column(col, width=w, anchor=anchor, stretch=(col == "label" or col == "scene"))
        self._tree.tag_configure("disabled", foreground=_FG_DIS)
        self._tree.tag_configure("status_OK",        foreground=_STATUS_COLOR["OK"])
        self._tree.tag_configure("status_RECOVERED", foreground=_STATUS_COLOR["RECOVERED"])
        self._tree.tag_configure("status_MISSING",   foreground=_STATUS_COLOR["MISSING"])
        self._tree.tag_configure("status_EMPTY",     foreground=_STATUS_COLOR["EMPTY"])
        self._tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self._tree.yview)
        sb.pack(side="right", fill="y")
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.bind("<Double-1>", lambda _e: self._edit_cue())
        self._tree.bind("<Button-1>", self._on_tree_click)

        br = tk.Frame(wrap, bg=_BG)
        br.pack(fill="x", pady=(4, 0))
        _btn(br, "Quick Add +", self._tap).pack(side="left", padx=2)
        _btn(br, "+ Add",       self._add_cue).pack(side="left", padx=(12, 2))
        _btn(br, "Edit",        self._edit_cue).pack(side="left", padx=2)
        _btn(br, "Remove",      self._remove_cue).pack(side="left", padx=2)
        _btn(br, "▲",           self._move_up, width=2).pack(side="left", padx=2)
        _btn(br, "▼",           self._move_down, width=2).pack(side="left", padx=2)
        _btn(br, "▶ Test",      self._test_fire).pack(side="left", padx=(12, 2))
        _btn(br, "↺ Reset",     self._reset_fired).pack(side="left", padx=2)
        _btn(br, "Re-validate", self._revalidate).pack(side="left", padx=(12, 2))

    def _build_catalog_panel(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=_BG)
        wrap.pack(side="right", fill="y", padx=(6, 0))
        hdr = tk.Frame(wrap, bg=_BG_HDR)
        hdr.pack(fill="x")
        tk.Label(hdr, text="LV1 SCENES", bg=_BG_HDR, fg=_FG_HEAD,
                 font=_F_UIB, padx=8, pady=3).pack(side="left")
        hint_font = (
            ("Segoe UI", 8, "italic") if sys.platform == "win32"
            else ("Helvetica Neue", 10, "italic")
        )
        tk.Label(hdr, text="double-click → recall   ·   drag onto a cue → assign",
                 bg=_BG_HDR, fg=_FG_DIM, font=hint_font,
                 padx=8, pady=3).pack(side="right")

        body = tk.Frame(wrap, bg=_BG_PAN, padx=4, pady=4,
                        highlightthickness=1, highlightbackground=_BORDER)
        body.pack(fill="both", expand=True)
        cols = ("idx", "name")
        self._cat_tree = ttk.Treeview(body, columns=cols, show="headings",
                                      height=15, selectmode="browse")
        self._cat_tree.heading("idx", text="#")
        self._cat_tree.column("idx", width=40, anchor="center", stretch=False)
        self._cat_tree.heading("name", text="Scene name")
        self._cat_tree.column("name", width=240, anchor="w")
        self._cat_tree.tag_configure("current", background=_BG_SEL, foreground=_FG)
        self._cat_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self._cat_tree.yview)
        sb.pack(side="right", fill="y")
        self._cat_tree.configure(yscrollcommand=sb.set)
        self._cat_tree.bind("<Double-1>", self._on_catalog_double_click)
        self._cat_tree.bind("<ButtonPress-1>", self._on_catalog_press, add="+")
        self._cat_tree.bind("<B1-Motion>", self._on_catalog_motion)
        self._cat_tree.bind("<ButtonRelease-1>", self._on_catalog_release)

    def _build_footer(self, parent: tk.Frame) -> None:
        ftr = tk.Frame(parent, bg=_BG)
        ftr.pack(fill="x")
        tk.Label(ftr, text="Frame tolerance ±", bg=_BG, fg=_FG_HEAD,
                 font=_F_UI).pack(side="left")
        self._tol_var = tk.StringVar(value=str(self.settings.tolerance_frames))
        sp = ttk.Spinbox(ftr, from_=0, to=10, width=4, textvariable=self._tol_var,
                         command=self._update_tolerance)
        sp.pack(side="left", padx=(4, 12))
        self._tol_var.trace_add("write", lambda *_: self._update_tolerance())
        self._dry_var = tk.BooleanVar(value=self.settings.dry_run)
        ttk.Checkbutton(ftr, text="Dry-run (don't send OSC)",
                        variable=self._dry_var,
                        command=self._update_dry_run).pack(side="left", padx=8)
        self._status_label = tk.Label(ftr, text="", bg=_BG, fg=_FG_HEAD, font=_F_UI)
        self._status_label.pack(side="right")

    # === Device enumeration =================================================

    def _refresh_audio_devices(self) -> None:
        self._audio_devices = self.ctl.refresh_audio_devices()
        names = [f"{d['name']}  ({d['hostapi']})" for d in self._audio_devices]
        self._audio_combo.configure(values=names)
        if names and not self._audio_var.get():
            self._audio_var.set(names[0])
            self._on_audio_device_changed()

    def _restore_device_selection(self) -> None:
        target = self.settings.audio_device
        if target:
            for n in self._audio_combo["values"]:
                if n.startswith(target):
                    self._audio_var.set(n)
                    self._on_audio_device_changed()
                    break

    def _on_audio_device_changed(self, _e=None) -> None:
        sel = self._audio_var.get()
        idx = next(
            (i for i, n in enumerate(self._audio_combo["values"]) if n == sel),
            -1,
        )
        if idx < 0:
            return
        dev = self._audio_devices[idx]
        n_ch = int(dev.get("channels", 1))
        names = self.ctl.channel_names(dev["index"]) or [f"Ch {i + 1}" for i in range(n_ch)]
        labels = [f"{i + 1} — {n}" for i, n in enumerate(names)]
        self._ch_combo.configure(values=labels)
        if labels:
            cur_ch = int(self.settings.audio_channel)
            self._ch_var.set(labels[min(max(cur_ch - 1, 0), len(labels) - 1)])

    def _on_sr_force_toggle(self) -> None:
        if self._sr_force_var.get():
            self._sr_combo.configure(state="readonly")
        else:
            self._sr_combo.configure(state="disabled")

    def _get_sample_rate(self) -> int:
        try:
            return int(self._sr_var.get())
        except ValueError:
            return 48000

    # === LV1 discovery ======================================================

    def _start_discovery(self) -> None:
        if not self.ctl.start_discovery():
            return
        self._lv1_disc_combo.configure(values=["Discovering LV1s on the LAN…"])
        self._lv1_disc_var.set("Discovering LV1s on the LAN…")

    def _render_discovery_from_event(self, payload: Dict[str, Any]) -> None:
        scanning = bool(payload.get("scanning"))
        if scanning:
            return  # already painted in _start_discovery
        results = payload.get("results", [])
        labels = ["(none — use IP override)"]
        for r in results:
            ip = r.get("ip") or "?"
            labels.append(f"{r.get('host') or 'unknown'}  —  {ip}:{r.get('port') or '?'}")
        self._lv1_disc_combo.configure(values=labels)
        target = self.settings.lv1_selected
        chosen_label = labels[0]
        for i, r in enumerate(results, start=1):
            if f"{r.get('ip')}:{r.get('port')}" == target:
                chosen_label = labels[i]
                break
        else:
            if results:
                chosen_label = labels[1]
        self._lv1_disc_var.set(chosen_label)

        if getattr(self, "_auto_connect_pending", False):
            self._auto_connect_pending = False
            if self._resolve_target(quiet=True) is not None:
                self._connect_lv1()

    # === LV1 connect ========================================================

    def _resolve_target(self, quiet: bool = False):
        manual_host = self._lv1_host_var.get().strip()
        manual_port = 0
        try:
            manual_port = int(self._lv1_port_var.get().strip() or "0")
        except ValueError:
            manual_port = 0

        if manual_host and manual_port > 0:
            return manual_host, manual_port
        if manual_host:
            for r in self.ctl.discovered:
                if manual_host in r.addresses and r.port:
                    return manual_host, r.port
            if not quiet:
                messagebox.showinfo(
                    "LV1",
                    f"Host {manual_host} not found in current discovery results.\n"
                    "Either fill the port manually or pick a discovered LV1 from "
                    "the dropdown.",
                )
            return None
        sel = self._lv1_disc_var.get()
        for i, label in enumerate(self._lv1_disc_combo["values"]):
            if label == sel and i > 0:
                idx = i - 1
                if idx < len(self.ctl.discovered):
                    r = self.ctl.discovered[idx]
                    ip = r.addresses[0] if r.addresses else ""
                    if ip and r.port:
                        return ip, r.port
        if not quiet:
            messagebox.showinfo("LV1", "No LV1 selected and no manual override.")
        return None

    def _toggle_lv1(self) -> None:
        if self.ctl.lv1.is_connected() or (self.ctl.lv1_state and self.ctl.lv1_state.connected):
            self._disconnect_lv1()
        else:
            self._connect_lv1()

    def _connect_lv1(self) -> None:
        target = self._resolve_target()
        if not target:
            return
        host, port = target
        self._set_lv1_button(online=True, label="● connecting…")
        self._lv1_state_label.config(text=f"Connecting to {host}:{port}…", fg=_FG_WARN)
        self.ctl.lv1_connect(host, port)
        # Persist manual override fields too (lv1_host/port are overwritten
        # by lv1_connect; lv1_selected captures the dropdown choice).
        self.settings.lv1_host = self._lv1_host_var.get().strip()
        try:
            self.settings.lv1_port = int(self._lv1_port_var.get().strip() or "0")
        except ValueError:
            self.settings.lv1_port = 0
        sel = self._lv1_disc_var.get()
        for i, label in enumerate(self._lv1_disc_combo["values"]):
            if label == sel and i > 0:
                idx = i - 1
                if idx < len(self.ctl.discovered):
                    r = self.ctl.discovered[idx]
                    ip = r.addresses[0] if r.addresses else ""
                    if ip and r.port:
                        self.settings.lv1_selected = f"{ip}:{r.port}"
                        break

    def _disconnect_lv1(self) -> None:
        self._set_lv1_button(online=False, label="● disconnecting…")
        self._lv1_state_label.config(text="Disconnecting…", fg=_FG_WARN)
        self.ctl.lv1_disconnect_async()

    # === LV1 event rendering ================================================

    def _render_lv1_state_from_event(self, payload: Dict[str, Any]) -> None:
        connected = bool(payload.get("connected"))
        registered = bool(payload.get("registered"))
        host = payload.get("host")
        port = payload.get("port")
        last_error = payload.get("last_error")
        if registered:
            txt = f"Connected — {host}:{port}"
            fg = _FG_OK
            self._set_lv1_button(online=True, label="● ONLINE")
        elif connected:
            txt = f"Handshaking… ({host}:{port})"
            fg = _FG_WARN
            self._set_lv1_button(online=True, label="● connecting…")
        else:
            err = last_error
            txt = f"Disconnected{' — ' + err if err else ''}"
            fg = _FG_ERR if err else _FG_DIM
            self._set_lv1_button(online=False, label="● OFFLINE")
        self._lv1_state_label.config(text=txt, fg=fg)

    def _set_lv1_button(self, online: bool, label: str) -> None:
        if online:
            bg, abg = _GO_BG, _GO_ABG
        else:
            bg, abg = _ST_BG, _ST_ABG
        self._lv1_btn.configure(text=label, bg=bg, fg="#FFFFFF")
        self._lv1_btn._bg = bg
        self._lv1_btn._abg = abg

    def _refresh_catalog_tree(self) -> None:
        self._cat_tree.delete(*self._cat_tree.get_children())
        cur = self.ctl.lv1_current_scene
        for idx in sorted(self.ctl.scene_catalog):
            name = self.ctl.scene_catalog[idx]
            tags = ("current",) if idx == cur else ()
            self._cat_tree.insert("", "end", iid=str(idx), values=(idx, name), tags=tags)

    def _on_catalog_double_click(self, _e=None) -> None:
        sel = self._cat_tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        if not self.ctl.lv1.is_connected():
            messagebox.showwarning("Not connected", "Connect to the LV1 first.")
            return
        ok, err = self.ctl.lv1_recall_scene(idx)
        if not ok:
            messagebox.showwarning("Recall failed", err or "Unknown error")
            return
        name = self.ctl.scene_catalog.get(idx, "(unknown)")
        self.ctl.set_status(f"Recalled scene [{idx}] {name}")

    # --- Drag-and-drop: scene → cue ----------------------------------------

    def _on_catalog_press(self, event) -> None:
        row = self._cat_tree.identify_row(event.y)
        try:
            self._drag_scene_idx = int(row) if row else None
        except ValueError:
            self._drag_scene_idx = None
        self._dragging = False

    def _on_catalog_motion(self, event) -> None:
        if getattr(self, "_drag_scene_idx", None) is None:
            return
        if not self._dragging:
            self._dragging = True
            try:
                self.root.config(cursor="hand2")
            except Exception:
                pass

    def _on_catalog_release(self, event) -> None:
        scene_idx = getattr(self, "_drag_scene_idx", None)
        was_dragging = getattr(self, "_dragging", False)
        self._drag_scene_idx = None
        self._dragging = False
        try:
            self.root.config(cursor="")
        except Exception:
            pass
        if scene_idx is None or not was_dragging:
            return

        target = self.root.winfo_containing(event.x_root, event.y_root)
        if target is None:
            return
        widget = target
        while widget is not None:
            if widget is self._tree:
                break
            widget = getattr(widget, "master", None)
        if widget is not self._tree:
            return

        tree_y = event.y_root - self._tree.winfo_rooty()
        cue_row = self._tree.identify_row(tree_y)
        if not cue_row:
            return
        try:
            cue_id = int(cue_row)
        except ValueError:
            return
        if self.ctl.assign_scene_to_cue(cue_id, scene_idx):
            self._tree.selection_set(str(cue_id))

    def _show_validation_warnings(self) -> None:
        if not self.ctl.cue_list.cues:
            return
        issues = [
            v for v in validate_all(self.ctl.cue_list.cues, self.ctl.scene_catalog)
            if v.resolution.status in ("MISSING", "EMPTY")
        ]
        if not issues:
            return
        lines = []
        for v in issues:
            r = v.resolution
            base = f"  • #{v.cue_id} '{v.cue_label}' → '{v.scene_name}'"
            if r.status == "EMPTY":
                base = (f"  • #{v.cue_id} '{v.cue_label}' has no scene name "
                        f"(imported from MIDI; index hint = {v.resolution.index})")
            elif r.suggestion_name:
                base += f"  ← did you mean '{r.suggestion_name}'?"
            lines.append(base)
        messagebox.showwarning(
            "Cues with issues",
            f"{len(issues)} cue(s) won't fire until you fix them:\n\n"
            + "\n".join(lines),
        )

    # === Dirty tracking =====================================================

    def _update_file_label(self) -> None:
        base = os.path.basename(self.ctl.current_file) if self.ctl.current_file else "(unsaved)"
        self._file_label.config(text=("• " + base) if self.ctl.dirty else base)

    def _confirm_discard_changes(self, prompt: str) -> bool:
        if not self.ctl.dirty:
            return True
        ans = messagebox.askyesnocancel(
            "Unsaved changes",
            prompt + "\n\nSave changes first?",
        )
        if ans is None:
            return False
        if ans:
            self._save_list()
            return not self.ctl.dirty
        return True

    # === Cue list ops =======================================================

    def _refresh_tree(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for i, c in enumerate(self.ctl.cue_list.cues, start=1):
            scene_disp = (
                f"[{c.scene_index}] {c.scene_name}"
                if c.scene_name and c.scene_index is not None
                else c.scene_name
                or (f"[{c.scene_index}]" if c.scene_index is not None else "")
            )
            tags: List[str] = []
            if not c.enabled:
                tags.append("disabled")
            else:
                tags.append(f"status_{c.scene_status}")
            self._tree.insert(
                "", "end", iid=str(c.id),
                values=(
                    i,
                    c.timecode,
                    c.label,
                    scene_disp,
                    c.scene_status,
                    "●" if c.enabled else "○",
                ),
                tags=tags,
            )

    def _selected_cue(self) -> Optional[Cue]:
        sel = self._tree.selection()
        if not sel:
            return None
        return self.ctl.cue_list.by_id(int(sel[0]))

    def _on_tree_click(self, e) -> None:
        region = self._tree.identify("region", e.x, e.y)
        col = self._tree.identify_column(e.x)
        if region == "cell" and col == "#6":  # the enabled column
            row = self._tree.identify_row(e.y)
            if row:
                self.ctl.toggle_cue_enabled(int(row))

    def _add_cue(self) -> None:
        CueDialog(self.root, None, dict(self.ctl.scene_catalog), self._on_dialog_save)

    def _edit_cue(self) -> None:
        cue = self._selected_cue()
        if cue is None:
            return
        CueDialog(self.root, cue, dict(self.ctl.scene_catalog),
                  lambda **kw: self._on_dialog_save(cue_id=cue.id, **kw))

    def _on_dialog_save(self, cue_id: Optional[int] = None, **kw) -> None:
        if cue_id is None:
            self.ctl.add_cue(
                label=kw.get("label", ""),
                timecode=kw.get("timecode", "00:00:00:00"),
                scene_name=kw.get("scene_name", ""),
                scene_index=kw.get("scene_index"),
                enabled=kw.get("enabled", True),
            )
        else:
            self.ctl.update_cue(cue_id, **kw)

    def _remove_cue(self) -> None:
        cue = self._selected_cue()
        if cue is None:
            return
        if not messagebox.askyesno("Remove cue", f"Remove cue '{cue.label}'?"):
            return
        self.ctl.remove_cue(cue.id)

    def _move_up(self) -> None:
        cue = self._selected_cue()
        if cue and self.ctl.move_cue_up(cue.id):
            self._tree.selection_set(str(cue.id))

    def _move_down(self) -> None:
        cue = self._selected_cue()
        if cue and self.ctl.move_cue_down(cue.id):
            self._tree.selection_set(str(cue.id))

    def _tap(self) -> None:
        new = self.ctl.tap_at_current_tc()
        if new is not None:
            self._tree.selection_set(str(new.id))

    def _test_fire(self) -> None:
        cue = self._selected_cue()
        if cue is None:
            return
        ok, err = self.ctl.test_fire_cue(cue.id)
        if not ok and err:
            messagebox.showwarning("Cannot fire", err)

    def _reset_fired(self) -> None:
        self.ctl.reset_fired()

    def _revalidate(self) -> None:
        if not self.ctl.scene_catalog:
            messagebox.showinfo("Re-validate", "Not connected to an LV1 yet.")
            return
        self.ctl.revalidate()
        self._show_validation_warnings()

    # === Audio start/stop ===================================================

    def _toggle_run(self) -> None:
        if self.ctl.running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if self.ctl.running:
            return
        sel = self._audio_var.get()
        idx = next(
            (i for i, n in enumerate(self._audio_combo["values"]) if n == sel),
            -1,
        )
        if idx < 0:
            messagebox.showerror("Start", "Pick an audio device first.")
            return
        dev = self._audio_devices[idx]
        ch_label = self._ch_var.get()
        ch1 = 1
        if ch_label and ch_label[0].isdigit():
            ch1 = int(ch_label.split(" ", 1)[0])
        sr = self._get_sample_rate() if self._sr_force_var.get() else int(
            dev.get("default_samplerate", 48000)
        )
        try:
            block_size = int(self._block_var.get())
        except ValueError:
            block_size = 512

        ok, err = self.ctl.start_capture(
            device_index=dev["index"],
            channel_zero_based=ch1 - 1,
            sample_rate=sr,
            block_size=block_size,
            device_label=sel.split("  (")[0],
        )
        if not ok:
            messagebox.showerror("Audio", f"Failed to start audio:\n{err}")
            return

        # Auto-connect the LV1 too, if a target is configured.
        if not self.ctl.lv1.is_connected() and self._resolve_target(quiet=True) is not None:
            self._connect_lv1()

    def _stop(self) -> None:
        self.ctl.stop_capture()

    def _render_running_from_event(self, payload: Dict[str, Any]) -> None:
        running = bool(payload.get("running"))
        if running:
            self._last_tc_time = 0
            self._ltc_status.config(text="● Waiting for LTC signal…", fg=_FG_WARN)
            self._run_btn.configure(text="■  STOP", bg=_ST_BG, fg="#FFFFFF")
            self._run_btn._bg = _ST_BG
            self._run_btn._abg = _ST_ABG
            self._set_audio_controls_enabled(False)
        else:
            self._ltc_status.config(text="● Stopped", fg=_FG_DIM)
            self._run_btn.configure(text="▶  START", bg=_GO_BG, fg="#FFFFFF")
            self._run_btn._bg = _GO_BG
            self._run_btn._abg = _GO_ABG
            self._tc_label.config(fg=_TC_OFF)
            self._set_audio_controls_enabled(True)

    def _set_audio_controls_enabled(self, enabled: bool) -> None:
        combo_state = "readonly" if enabled else "disabled"
        self._audio_combo.configure(state=combo_state)
        self._ch_combo.configure(state=combo_state)
        self._block_combo.configure(state=combo_state)
        if enabled and self._sr_force_var.get():
            self._sr_combo.configure(state="readonly")
        else:
            self._sr_combo.configure(state="disabled")

    # === Timecode polling ===================================================

    def _poll_queue(self) -> None:
        """Main thread, ~25 Hz. Drains the timecode queue through the engine
        (which is what actually fires cues), updates the TC display, and
        polls the audio capture for signal status + stream health.

        When the UI is hidden in the tray we still pump the engine (cues
        MUST keep firing) but skip widget mutations — tk wouldn't draw them
        anyway and skipping saves a handful of Tcl calls per tick."""
        if self.ctl.running and not self.ctl.recovering:
            # Detect unexpected stream death.
            if not self.ctl.audio.stream_active or self.ctl.audio.callback_stalled:
                self.ctl.set_recovering(True)
                try:
                    self.ctl.audio.stop()
                except Exception:
                    pass
                if not self._ui_hidden:
                    self._ltc_status.config(text="● Driver reset — restarting…", fg=_FG_WARN)
                    self._tc_label.config(fg=_TC_OFF)
                self.root.after(2000, self._auto_restart)
                self.root.after(40, self._poll_queue)
                return

            latest = self.ctl.drain_tc_queue()

            if latest is not None and not self._ui_hidden:
                self._tc_label.config(text=str(latest))
                fps = self.ctl.audio.detected_fps
                if fps:
                    self._fps_label.config(text=f"{fps:.2f} fps".rstrip("0").rstrip("."),
                                            fg=_FG_HEAD)
            if latest is not None:
                self._last_tc_time = 0

            self._last_tc_time += 1
            locked = self.ctl.audio.is_locked
            signal_ok = self.ctl.audio.signal_present
            if locked:
                new_state = "LOCKED"
            elif signal_ok:
                new_state = "AUDIO_NOT_LTC"
            elif self._last_tc_time > 25:
                new_state = "NO_SIGNAL"
            else:
                new_state = None

            if new_state is not None and self.ctl.update_signal_state(new_state):
                # Signal state change is observable via the web remote even
                # when the tk UI is hidden, so we always update the
                # controller; only the desktop widgets are skipped.
                if not self._ui_hidden:
                    self._render_signal_state(new_state)

        self.root.after(40, self._poll_queue)   # 40 ms ≈ 25 Hz

    def _render_signal_state(self, new_state: str) -> None:
        if new_state == "LOCKED":
            self._ltc_status.config(text="● LTC OK", fg=_FG_OK)
            self._tc_label.config(fg=_TC_ON)
        elif new_state == "AUDIO_NOT_LTC":
            self._ltc_status.config(
                text="● Audio present but no LTC (wrong channel / SR?)",
                fg=_FG_WARN,
            )
            self._tc_label.config(fg=_TC_OFF)
        else:  # NO_SIGNAL
            self._tc_label.config(fg=_TC_OFF)
            self._ltc_status.config(text="● No signal", fg=_FG_ERR)

    def _render_tc_from_event(self, payload: Dict[str, Any]) -> None:
        # The poll loop already updated _tc_label/_fps_label for tk efficiency.
        # This handler exists so web/non-tk subscribers still get a clean event.
        # We leave the UI labels alone here to avoid double-painting.
        pass

    def _auto_restart(self) -> None:
        if not self.ctl.running:
            self.ctl.set_recovering(False)
            return
        try:
            self.ctl.audio.start()
            self.ctl.set_recovering(False)
            self._ltc_status.config(text="● Restarted — waiting for signal",
                                    fg=_FG_WARN)
        except Exception as exc:  # noqa: BLE001
            self._ltc_status.config(text=f"● Restart failed: {exc}", fg=_FG_ERR)
            self.root.after(2000, self._auto_restart)

    # === Engine fire rendering ==============================================

    def _render_last_fire(self, payload: Dict[str, Any]) -> None:
        self._last_fire_label.config(
            text=(
                f"[{payload.get('scene_index')}] "
                f"{payload.get('scene_name') or ''}\n"
                f"target {payload.get('target_tc')}  fired @ {payload.get('fired_tc')}"
            ),
            fg=_FG_OK,
        )

    # === File ops ===========================================================

    def _new_list(self) -> None:
        if not self._confirm_discard_changes("Start a new cue list?"):
            return
        self.ctl.new_cue_list()

    def _open_list(self) -> None:
        if not self._confirm_discard_changes("Open a different cue list?"):
            return
        path = filedialog.askopenfilename(
            title="Open cue list",
            initialdir=self._dialog_initial_dir(),
            filetypes=[
                (CUE_FILE_DESCRIPTION, f"*{CUE_FILE_EXTENSION}"),
                ("JSON cue lists (legacy)", "*.json"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._load_cue_file(path)

    def _open_recent(self, path: str) -> None:
        """File ▸ Open recent ▸ <entry> handler."""
        if not self._confirm_discard_changes("Open a different cue list?"):
            return
        if not os.path.isfile(path):
            messagebox.showerror(
                "Open recent",
                f"File no longer exists:\n  {path}\n\nRemoving from the recent list.",
            )
            self.ctl._prune_recent(path)
            return
        self._load_cue_file(path)

    def _rebuild_recent_menu(self) -> None:
        """Wipe + repopulate the Open Recent submenu from controller state.
        Stale entries (file no longer exists) are kept but rendered as
        disabled so the operator sees they used to be there."""
        m = getattr(self, "_recent_menu", None)
        if m is None:
            return
        try:
            m.delete(0, "end")
        except tk.TclError:
            return
        entries = self.ctl._recent_files_payload()
        if not entries:
            m.add_command(label="(no recent files)", state="disabled")
            return
        for i, e in enumerate(entries, start=1):
            label = f"{i}. {e['name']}    —    {e['path']}"
            if e["exists"]:
                m.add_command(label=label, command=lambda p=e["path"]: self._open_recent(p))
            else:
                m.add_command(label=f"{label}    (missing)", state="disabled")
        m.add_separator()
        m.add_command(label="Clear recent files", command=self.ctl.clear_recent)

    def _dialog_initial_dir(self) -> str:
        """Where Save/Open dialogs should land first time. Prefer the current
        file's folder, fall back to ~/Documents/LTCtoLV1 (created if missing)."""
        if self.ctl.current_file and os.path.isdir(os.path.dirname(self.ctl.current_file)):
            return os.path.dirname(self.ctl.current_file)
        return ensure_projects_dir()

    def _load_cue_file(self, path: str) -> None:
        ok, err, was_midi = self.ctl.load_cue_file(path)
        if not ok:
            messagebox.showerror("Open", f"Failed to load:\n{err}")
            return
        if was_midi:
            messagebox.showinfo(
                "Imported from MIDI",
                f"This cue list uses the old MIDI Program Change format.\n"
                f"Each cue's program number was kept as a scene-index hint, "
                f"but the scene NAME is empty — you'll need to associate "
                f"each cue with the right scene by name before they can fire.\n\n"
                f"Open each cue with Edit and pick the scene from the dropdown, "
                f"then Save the list — it will be saved in the new format.",
            )
            self._validated_once = False
            if self.ctl.scene_catalog:
                self._show_validation_warnings()

    def _save_list(self) -> None:
        if self.ctl.current_file:
            self._write_cue_file(self.ctl.current_file)
        else:
            self._save_list_as()

    def _save_list_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save cue list",
            initialdir=self._dialog_initial_dir(),
            defaultextension=CUE_FILE_EXTENSION,
            filetypes=[
                (CUE_FILE_DESCRIPTION, f"*{CUE_FILE_EXTENSION}"),
                ("JSON cue lists (legacy)", "*.json"),
            ],
        )
        if path:
            self._write_cue_file(path)

    def _write_cue_file(self, path: str) -> None:
        ok, err = self.ctl.save_cue_file(path)
        if not ok:
            messagebox.showerror("Save", f"Failed to save:\n{err}")

    # === Misc ===============================================================

    def _update_tolerance(self) -> None:
        try:
            t = int(self._tol_var.get())
        except ValueError:
            return
        self.ctl.set_tolerance(t)

    def _update_dry_run(self) -> None:
        self.ctl.set_dry_run(self._dry_var.get())

    def _set_status_label(self, text: str, warn: bool) -> None:
        self._status_label.config(text=text, fg=_FG_WARN if warn else _FG_HEAD)

    def _show_about(self) -> None:
        messagebox.showinfo(
            "LTC to LV1",
            f"LTC to LV1  v{_VERSION}\n\n"
            "Reads SMPTE LTC from an audio input and recalls Waves LV1\n"
            "snapshots over OSC at frame-accurate timecodes.\n\n"
            "Includes a built-in web remote — see File ▸ Web remote settings.\n\n"
            "MIT licensed.",
        )

    # === Web remote =========================================================

    def _show_web_settings(self) -> None:
        WebSettingsDialog(self.root, self.ctl)

    def _open_web_remote(self) -> None:
        if not getattr(self.settings, "web_enabled", False):
            messagebox.showinfo(
                "Web remote",
                "The web remote is currently disabled.\n\n"
                "Enable it in File ▸ Web remote settings.",
            )
            return
        port = int(getattr(self.settings, "web_port", 8080))
        webbrowser.open(f"http://127.0.0.1:{port}/")

    # === Tray integration ===================================================

    def _minimize_to_tray(self) -> None:
        """Hide the desktop window. The engine + web remote keep running.
        The tray icon (if available) is the way back."""
        try:
            self.root.withdraw()
        except tk.TclError:
            return
        self._ui_hidden = True
        if self._tray is None:
            # No tray running — surface a hint so the operator knows how to
            # bring the window back (Alt-Tab is fine but not obvious).
            self.ctl.set_status(
                "Window hidden. No tray icon — pass --start-minimized off, "
                "or use the web remote's 'Open UI' button.",
                warn=True,
            )

    def _show_and_focus(self) -> None:
        """Bring the desktop window back from the tray AND to the front,
        even past fullscreen apps when possible. Called from:
          - tray menu 'Open UI'
          - web remote 'Open UI' button (via /api/window/show)"""
        try:
            self.root.deiconify()
            # state('normal') un-minimises if it was iconified rather than
            # withdrawn (e.g. classic taskbar minimise).
            try:
                self.root.state("normal")
            except tk.TclError:
                pass
            # The topmost-flicker trick is the documented Tk workaround for
            # Windows' focus-stealing-prevention. Setting topmost briefly
            # forces the window above the foreground; we drop it back so
            # the user can still cover it with other apps afterwards.
            self.root.lift()
            try:
                self.root.attributes("-topmost", True)
                self.root.after(250, lambda: self._safe_topmost_off())
            except tk.TclError:
                pass
            try:
                self.root.focus_force()
            except tk.TclError:
                pass
            # On Windows, also call SetForegroundWindow via ctypes — pure-Tk
            # lift() doesn't always win the focus race against a fullscreen
            # foreground process (e.g. the LV1 mix app).
            if sys.platform == "win32":
                self._win32_force_foreground()
        finally:
            was_hidden = self._ui_hidden
            self._ui_hidden = False
            # While hidden the poll loop and dispatcher skipped widget
            # mutations to save Tcl calls. The controller's state moved on
            # though — TC, signal, scene catalog, last fire, cues, LV1
            # connection — and the widgets still show whatever they were
            # last painted with before the minimise. Force a full repaint
            # from the current controller snapshot so the UI reflects
            # reality immediately, instead of slowly catching up only on
            # the next event-per-field.
            if was_hidden:
                self._repaint_from_controller_state()

    def _repaint_from_controller_state(self) -> None:
        """Re-sync every desktop widget with the controller's current state.
        Used after returning from the tray, where the dispatcher's
        memoised "no change since last paint" assumption no longer holds."""
        snap = self.ctl.snapshot()

        # Running / transport — repaint the START/STOP button and signal
        # status banner before we touch the TC text below.
        self._render_running_from_event({
            "running": snap.get("running"),
            "recovering": snap.get("recovering"),
            "signal": snap.get("signal"),
        })

        # TC + FPS display
        cur_tc = snap.get("current_tc")
        if cur_tc:
            self._tc_label.config(text=cur_tc)
        fps = snap.get("fps")
        if fps:
            self._fps_label.config(
                text=f"{fps:.2f} fps".rstrip("0").rstrip("."),
                fg=_FG_HEAD,
            )

        # Signal state — bypass the memoisation in update_signal_state so
        # the TC label colour gets re-applied even if the state hasn't
        # actually changed since before the minimise.
        sig = snap.get("signal")
        if sig:
            self._render_signal_state(sig)

        # LV1 connection + current scene + last fire
        lv1 = snap.get("lv1") or {}
        if lv1:
            self._render_lv1_state_from_event(lv1)
        idx = snap.get("lv1_current_scene")
        if idx is None:
            self._cur_scene_label.config(text="—", fg=_FG_DIM)
        else:
            name = snap.get("lv1_current_scene_name") or "(unknown)"
            self._cur_scene_label.config(text=f"[{idx}] {name}", fg=_FG_OK)
        if snap.get("last_fire"):
            self._render_last_fire(snap["last_fire"])

        # Cues + scene catalog tables
        self._refresh_tree()
        self._refresh_catalog_tree()
        self._update_file_label()

        # Status bar
        ls = snap.get("last_status")
        if ls:
            self._set_status_label(ls.get("text", ""), bool(ls.get("warn")))

    def _safe_topmost_off(self) -> None:
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _win32_force_foreground(self) -> None:
        try:
            import ctypes
            hwnd = self.root.winfo_id()
            # Some Tk builds return the inner draw HWND; walking up to the
            # owner window via GetAncestor(GA_ROOT) gets us the actual
            # top-level so SetForegroundWindow targets it.
            GA_ROOT = 2
            user32 = ctypes.windll.user32
            root_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            user32.ShowWindow(root_hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(root_hwnd)
        except Exception:
            pass

    def _quit_from_tray(self) -> None:
        """Tray 'Quit' handler. Skips the unsaved-changes prompt unless the
        window is visible (operator can't see the dialog from the tray)."""
        if not self._ui_hidden:
            self._on_close()
            return
        # Quietly tear down — saving any in-flight changes would risk
        # corrupting a file the user hasn't reviewed.
        self._shutting_down = True
        try:
            self._unsub()
        except Exception:
            pass
        try:
            self.ctl.shutdown()
        except Exception:
            pass
        try:
            self.ctl.save_settings()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # === File type association ==============================================

    def _associate_file_type(self) -> None:
        """Register the .ltcv1 extension so double-click on a cue list file
        opens this app. Per-user (HKCU) — no admin required, no UAC prompt."""
        try:
            import file_assoc
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Associate", f"Couldn't load file_assoc:\n{exc}")
            return
        if not getattr(sys, "frozen", False):
            messagebox.showinfo(
                "Associate",
                "File association only works for the built .exe.\n"
                "Build the app with PyInstaller (release.bat) before "
                "registering the file type.",
            )
            return
        if file_assoc.register():
            messagebox.showinfo(
                "Associate",
                f"{CUE_FILE_EXTENSION} files will now open in LTCtoLV1.\n\n"
                "If Explorer still shows the old icon, restart it from "
                "Task Manager (or sign out and back in).",
            )
        else:
            messagebox.showerror(
                "Associate",
                "Couldn't write the file association.\n"
                "Try running the app once as Administrator if the problem persists.",
            )

    # === Update check =======================================================

    def _check_updates(self, silent_if_ok: bool = False) -> None:
        def _worker() -> None:
            try:
                req = urllib.request.Request(
                    _RELEASES_API,
                    headers={"User-Agent": f"LTCtoLV1/{_VERSION}"},
                )
                with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                if not silent_if_ok:
                    self.root.after_idle(
                        lambda: messagebox.showwarning(
                            "Update check",
                            f"Couldn't reach GitHub:\n{exc}",
                        )
                    )
                return
            latest_tag = str(data.get("tag_name") or "").lstrip("v")
            latest_url = str(data.get("html_url") or _RELEASES_URL)
            if not latest_tag:
                if not silent_if_ok:
                    self.root.after_idle(lambda: messagebox.showinfo(
                        "Update check", "No releases published yet."
                    ))
                return
            if _version_tuple(latest_tag) > _version_tuple(_VERSION):
                self.root.after_idle(
                    lambda: self._prompt_update(latest_tag, latest_url)
                )
            elif not silent_if_ok:
                self.root.after_idle(lambda: messagebox.showinfo(
                    "Update check",
                    f"You're on the latest version (v{_VERSION}).",
                ))

        t = threading.Thread(target=_worker, name="UpdateCheck", daemon=True)
        t.start()

    def _prompt_update(self, latest_tag: str, latest_url: str) -> None:
        if messagebox.askyesno(
            "Update available",
            f"A new version of LTC to LV1 is available:\n\n"
            f"   You have:   v{_VERSION}\n"
            f"   Available:  v{latest_tag}\n\n"
            "Open the release page?",
        ):
            webbrowser.open(latest_url)

    def _on_close(self) -> None:
        if not self._confirm_discard_changes("Quit without saving?"):
            return
        # Unsubscribe FIRST so the wind-down (stop_capture, lv1.disconnect)
        # doesn't schedule any more after_idle callbacks against widgets that
        # are about to be destroyed.
        self._shutting_down = True
        try:
            self._unsub()
        except Exception:
            pass
        try:
            self.ctl.shutdown()
        except Exception:
            pass
        try:
            self.ctl.save_settings()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# --- Web settings dialog ----------------------------------------------------


class WebSettingsDialog(tk.Toplevel):
    """Tiny modal to toggle the web remote, change its port, and surface the
    LAN URLs that operators can hand off to phones / tablets. The LV1 host
    typically has at least two NICs (control + soundgrid), so we list every
    routable IPv4 we can detect, ranked by likelihood."""

    def __init__(self, parent: tk.Tk, ctl: AppController) -> None:
        super().__init__(parent)
        self.title("Web remote settings")
        self.configure(bg=_BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._parent = parent
        self._ctl = ctl
        self._cached_ips: Optional[List[str]] = None
        self._destroyed: bool = False
        # Centre on the parent window — Toplevel defaults to (0,0), which on
        # multi-monitor / large-window setups lands in the corner away from
        # where the operator is actually looking. Defer until after the body
        # is built so the size is known.
        self.after_idle(self._center_on_parent)

        body = tk.Frame(self, bg=_BG_PAN, padx=16, pady=14,
                        highlightthickness=1, highlightbackground=_BORDER)
        body.pack(padx=10, pady=10)

        tk.Label(
            body,
            text=(
                "The web remote lets you control LTCtoLV1 from any browser\n"
                "on the same network — phone, tablet, or another laptop."
            ),
            bg=_BG_PAN, fg=_FG, font=_F_UI, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self._enabled_var = tk.BooleanVar(value=bool(getattr(ctl.settings, "web_enabled", False)))
        ttk.Checkbutton(body, text="Enable web remote on app start",
                        variable=self._enabled_var).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=2
        )

        # System integration toggles. Autostart only does anything for the
        # frozen .exe / .app — for source runs we still let the user toggle
        # it but the action quietly no-ops.
        self._tray_var = tk.BooleanVar(value=bool(getattr(ctl.settings, "tray_enabled", True)))
        ttk.Checkbutton(body, text="Show system-tray icon (takes effect after restart)",
                        variable=self._tray_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=2
        )
        self._autostart_var = tk.BooleanVar(value=bool(getattr(ctl.settings, "autostart_enabled", False)))
        ttk.Checkbutton(body, text="Start automatically on login (hidden in tray)",
                        variable=self._autostart_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=2
        )

        tk.Label(body, text="Port:", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI).grid(row=4, column=0, sticky="w", pady=(8, 2))
        self._port_var = tk.StringVar(value=str(getattr(ctl.settings, "web_port", 8080)))
        tk.Entry(body, textvariable=self._port_var, width=8, font=_F_UI,
                 bg=_BG_WID, fg=_FG, insertbackground=_FG,
                 highlightthickness=0).grid(row=4, column=1, sticky="w", pady=(8, 2))
        # Live-refresh the URL list when the operator types a different port.
        self._port_var.trace_add("write", lambda *_: self._refresh_urls())

        # LAN URL list — populated asynchronously since _local_ipv4s() can do
        # subprocess calls (ipconfig / ifconfig) that take ~hundreds of ms.
        tk.Label(body, text="Open from a phone or tablet on the LAN:",
                 bg=_BG_PAN, fg=_FG_HEAD, font=_F_UI, justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(12, 2)
        )
        self._urls_frame = tk.Frame(body, bg=_BG_PAN)
        self._urls_frame.grid(row=6, column=0, columnspan=2, sticky="w",
                              pady=(0, 4))
        self._urls_placeholder = tk.Label(
            self._urls_frame,
            text="Detecting LAN addresses…",
            bg=_BG_PAN, fg=_FG_DIM, font=_F_MONO,
        )
        self._urls_placeholder.pack(anchor="w")

        info = (
            "Changes to the port take effect after restarting the app.\n"
            "The remote binds to 0.0.0.0 (open to anyone on the LAN)."
        )
        tk.Label(body, text=info, bg=_BG_PAN, fg=_FG_DIM, font=_F_UI,
                 justify="left").grid(row=7, column=0, columnspan=2,
                                       sticky="w", pady=(8, 0))

        btns = tk.Frame(body, bg=_BG_PAN)
        btns.grid(row=8, column=0, columnspan=2, pady=(14, 0))
        _btn(btns, "Save", self._ok, bg=_GO_BG, abg=_GO_ABG, fg="#FFFFFF",
             width=8, px=14, py=5).pack(side="left", padx=4)
        _btn(btns, "Cancel", self.destroy, width=8, px=14, py=5).pack(side="left", padx=4)

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self._on_destroy)

        # Kick off IP detection on a worker thread.
        threading.Thread(
            target=self._fetch_ips, name="WebSettingsDetectIPs", daemon=True
        ).start()

    def _on_destroy(self) -> None:
        self._destroyed = True
        self.destroy()

    def _center_on_parent(self) -> None:
        """Position the dialog over the middle of the parent window. Safe to
        call after destroy — bails on TclError."""
        try:
            self.update_idletasks()
            w = self.winfo_width()
            h = self.winfo_height()
            p = self._parent
            pw = p.winfo_width()
            ph = p.winfo_height()
            px = p.winfo_rootx()
            py = p.winfo_rooty()
            x = max(0, px + (pw - w) // 2)
            y = max(0, py + (ph - h) // 2)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

    def _fetch_ips(self) -> None:
        """Worker thread — runs the (possibly slow) subprocess-backed IP scan,
        then marshals the result back to the tk main loop."""
        try:
            from zdns_discover import _local_ipv4s, _rank_ip
            ips = _local_ipv4s()
            # Rank best-first so the most reachable URL is on top of the list.
            ips = sorted(set(ips), key=_rank_ip, reverse=True)
        except Exception:
            ips = []
        if self._destroyed:
            return
        try:
            self.after_idle(lambda: self._render_ips(ips))
        except tk.TclError:
            pass

    def _render_ips(self, ips: List[str]) -> None:
        if self._destroyed or not self._urls_frame.winfo_exists():
            return
        self._cached_ips = ips
        self._refresh_urls()

    def _refresh_urls(self) -> None:
        """Re-paint the URL list whenever the IP cache or port changes."""
        if self._destroyed or not self._urls_frame.winfo_exists():
            return
        # Drop the placeholder + any prior rows.
        for w in self._urls_frame.winfo_children():
            w.destroy()
        ips = self._cached_ips
        if ips is None:
            tk.Label(self._urls_frame, text="Detecting LAN addresses…",
                     bg=_BG_PAN, fg=_FG_DIM, font=_F_MONO).pack(anchor="w")
            return
        if not ips:
            tk.Label(self._urls_frame,
                     text="(no LAN addresses detected — check NIC config)",
                     bg=_BG_PAN, fg=_FG_WARN, font=_F_UI).pack(anchor="w")
            return
        try:
            port = int(self._port_var.get().strip() or "8080")
        except ValueError:
            port = 0
        if port < 1 or port > 65535:
            tk.Label(self._urls_frame,
                     text="(enter a valid port to see URLs)",
                     bg=_BG_PAN, fg=_FG_DIM, font=_F_UI).pack(anchor="w")
            return

        for ip in ips:
            url = f"http://{ip}:{port}/"
            row = tk.Frame(self._urls_frame, bg=_BG_PAN)
            row.pack(fill="x", anchor="w")
            tk.Label(row, text=url, bg=_BG_PAN, fg=_FG, font=_F_MONO).pack(side="left")
            _btn(row, "Copy", lambda u=url: self._copy(u),
                 width=4, px=6, py=0).pack(side="left", padx=(8, 0))

    def _copy(self, text: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()  # ensure clipboard is owned even after window closes
        except tk.TclError:
            pass

    def _ok(self) -> None:
        try:
            port = int(self._port_var.get().strip() or "8080")
            if port < 1 or port > 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid", "Port must be a number 1–65535.",
                                 parent=self)
            return
        autostart_on = bool(self._autostart_var.get())
        self._ctl.update_settings(
            web_enabled=bool(self._enabled_var.get()),
            web_port=port,
            tray_enabled=bool(self._tray_var.get()),
            autostart_enabled=autostart_on,
        )
        self._ctl.save_settings()
        # Apply autostart immediately — registering/unregistering is cheap and
        # the user reasonably expects the change to take effect right now.
        try:
            import autostart
            if autostart.is_supported():
                if autostart_on:
                    if not autostart.enable(start_minimized=True):
                        messagebox.showwarning(
                            "Autostart",
                            "Autostart only registers from the built app.\n"
                            "It will activate after you next launch the .exe / .app.",
                            parent=self,
                        )
                else:
                    autostart.disable()
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning(
                "Autostart",
                f"Couldn't update autostart:\n{exc}",
                parent=self,
            )
        self._destroyed = True
        self.destroy()


# --- Module-level helpers ---------------------------------------------------


def _version_tuple(v: str) -> tuple:
    out = []
    for part in v.strip().lstrip("v").split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out) or (0,)
