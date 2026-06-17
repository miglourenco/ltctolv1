"""
LTC to LV1 — main application window (tkinter + ttk).

Threading model:
  - All UI updates and CueEngine calls happen on the MAIN thread.
  - Audio runs in its own thread, talks via queue.Queue[Timecode].
  - LV1Client runs its reader on its own thread; callbacks are marshalled
    onto the main thread via root.after_idle().
"""

from __future__ import annotations

import json
import os
import queue
import ssl
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

try:
    import certifi as _certifi
    _SSL_CTX: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=_certifi.where())
except Exception:
    _SSL_CTX = None

from audio_capture import AudioCapture, get_channel_names, list_audio_devices, reinit_portaudio
from cue_engine import CueEngine
from ltc_decoder import Timecode
from lv1_osc_client import ConnectionState, LV1Client, SceneCatalogSnapshot
from models import AppSettings, Cue, CueList
from scene_resolver import CueValidation, validate_all
from zdns_discover import DiscoveryEntry, DiscoveryScanner


# --- Constants --------------------------------------------------------------

_VERSION = "1.0.4"
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

        # Build label list "[idx] name", plus a "(custom)" sentinel so the user
        # can still hand-type a name that isn't in the catalog (offline editing).
        choices: List[str] = ["(custom — type below)"]
        for idx in sorted(scene_catalog):
            choices.append(f"[{idx}] {scene_catalog[idx]}")

        self._scene_choice_var = tk.StringVar(value=choices[0])
        cb = ttk.Combobox(scene_row, textvariable=self._scene_choice_var,
                          values=choices, state="readonly", width=36, font=_F_UI)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", self._on_scene_picked)

        # Custom name + index (always visible — also gets populated by the picker)
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

        # If editing an existing cue with a name that IS in the catalog, preset the picker.
        if cue and cue.scene_name:
            for ch in choices[1:]:
                if ch.endswith(f"] {cue.scene_name}"):
                    self._scene_choice_var.set(ch)
                    break

        # Enabled
        self._enabled_var = tk.BooleanVar(value=cue.enabled if cue else True)
        ttk.Checkbutton(body, text="Enabled", variable=self._enabled_var).grid(
            row=4, column=1, sticky="w", pady=(8, 0)
        )

        # Buttons
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
        # "[5] My Scene Name" → idx=5, name="My Scene Name"
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
    def __init__(self, root: tk.Tk, settings: AppSettings) -> None:
        self.root = root
        self.settings = settings
        self.root.configure(bg=_BG)

        # Core objects
        self._tc_queue: "queue.Queue[Timecode]" = queue.Queue(maxsize=200)
        self._audio = AudioCapture(self._tc_queue)
        # Identify this client in the LV1's MyRemote ControlPanel by hostname
        # so multiple machines running LTCtoLV1 are distinguishable.
        import socket as _socket
        try:
            _hostname = _socket.gethostname() or "unknown"
        except Exception:
            _hostname = "unknown"
        self._lv1 = LV1Client(device_name=f"LTC - {_hostname}")
        self._cue_list = CueList()
        self._engine = CueEngine(
            self._lv1,
            tolerance_frames=settings.tolerance_frames,
            dry_run=settings.dry_run,
        )
        self._engine.on_cue_fired = self._on_cue_fired
        self._engine.on_cue_skipped = self._on_cue_skipped
        self._engine.on_send_error = self._on_send_error

        # Discovery scanner
        self._scanner = DiscoveryScanner(timeout_s=5.0)
        self._discovered: List[DiscoveryEntry] = []

        # LV1 callbacks → marshal to UI thread
        self._lv1.on_connection_change = lambda s: self.root.after_idle(
            lambda: self._on_lv1_connection(s)
        )
        self._lv1.on_catalog_change = lambda c: self.root.after_idle(
            lambda: self._on_lv1_catalog(c)
        )
        self._lv1.on_current_scene_change = lambda i: self.root.after_idle(
            lambda: self._on_lv1_current_scene(i)
        )
        self._lv1.on_log = lambda lvl, m: self.root.after_idle(
            lambda: self._log(lvl, m)
        )

        # UI state
        self._running = False
        self._recovering = False   # True while waiting to auto-restart after stream death
        # Tracks the LTC status label state so we only re-paint on change.
        # Values: None | "LOCKED" | "AUDIO_NOT_LTC" | "NO_SIGNAL"
        self._last_signal_ok: Optional[str] = None
        self._current_tc: Optional[Timecode] = None
        self._current_file: Optional[str] = None
        self._flash_after: Optional[str] = None
        # Frames-since-last-TC counter. The poll loop ticks every 40 ms.
        self._last_tc_time: int = 0
        self._audio_devices: List = []
        self._detected_sr: int = settings.sample_rate
        self._lv1_state: Optional[ConnectionState] = None
        self._lv1_current_scene: Optional[int] = None
        self._scene_catalog: Dict[int, str] = {}
        # Dirty flag — True when the cue list has unsaved changes.
        self._dirty: bool = False

        self._apply_theme()
        self._build_ui()
        self._refresh_audio_devices()
        self._start_discovery()
        self._restore_device_selection()
        self._poll_queue()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if settings.last_cue_file and os.path.isfile(settings.last_cue_file):
            self._load_cue_file(settings.last_cue_file)

        # Defer auto-connect until discovery has finished — see _on_discovery_done.
        # (Auto-connecting during the 5 s scan would fail with "no LV1 selected"
        # because both the dropdown and the discovered cache are still empty.)
        self._auto_connect_pending = bool(settings.lv1_selected or settings.lv1_host)

        # Silent update check on startup (no popup unless an update is available)
        self.root.after(4000, lambda: self._check_updates(silent_if_ok=True))

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
        s.map("TCombobox",
              fieldbackground=[("readonly", _BG_WID)],
              selectbackground=[("readonly", _BG_WID)],
              selectforeground=[("readonly", _FG)])
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
        file_m.add_separator()
        file_m.add_command(label="Save", command=self._save_list)
        file_m.add_command(label="Save as…", command=self._save_list_as)
        file_m.add_separator()
        file_m.add_command(label="Exit", command=self._on_close)

        help_m = tk.Menu(menu, tearoff=0)
        menu.add_cascade(label="Help", menu=help_m)
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

        # Right: single transport button (toggles START ↔ STOP) above the
        # LV1 connect button, both right-aligned.
        tf = tk.Frame(body, bg=_BG_PAN, padx=8)
        tf.pack(side="right", fill="y", pady=2)

        # Transport button on top — same dimensions as the LV1 connect button.
        self._run_btn = _btn(tf, "▶  START", self._toggle_run,
                             bg=_GO_BG, abg=_GO_ABG, fg="#FFFFFF",
                             width=14, px=12, py=4,
                             font=("Segoe UI", 9, "bold"))
        self._run_btn.pack(pady=(0, 4))

        # LV1 connect underneath. Red = offline (default), green = online.
        self._lv1_btn = _btn(tf, "● OFFLINE", self._toggle_lv1,
                             bg=_ST_BG, abg=_ST_ABG, fg="#FFFFFF",
                             width=14, px=12, py=4,
                             font=("Segoe UI", 9, "bold"))
        self._lv1_btn.pack()

    def _build_tc_panel(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=_BG)
        wrap.pack(fill="x", pady=(0, 6))

        # Left: TC display
        tcf = tk.Frame(wrap, bg=_BG_TC,
                       highlightthickness=1, highlightbackground=_BORDER,
                       padx=14, pady=10)
        tcf.pack(side="left")
        self._tc_label = tk.Label(tcf, text="00:00:00:00", bg=_BG_TC, fg=_TC_OFF,
                                  font=_F_TC)
        self._tc_label.pack()
        # Status row: signal indicator + FPS, side-by-side under the TC display
        row = tk.Frame(tcf, bg=_BG_TC)
        row.pack(fill="x")
        self._ltc_status = tk.Label(row, text="● Stopped", bg=_BG_TC, fg=_FG_DIM,
                                    font=_F_UI)
        self._ltc_status.pack(side="left")
        self._fps_label = tk.Label(row, text="-- fps", bg=_BG_TC, fg=_FG_DIM,
                                   font=_F_FPS)
        self._fps_label.pack(side="right")

        # Right: LV1 status + current scene
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
        # Persistent "last fired cue" indicator — survives catalog updates and
        # other status messages so the operator can verify what the engine did.
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
        # Initial label state is set later via _update_file_label() once _dirty exists

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

        # Buttons row
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
        # Inline hint so the gestures are discoverable
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
        # Double-click on a scene recalls it on the LV1 (single-click selects only).
        self._cat_tree.bind("<Double-1>", self._on_catalog_double_click)
        # Drag a scene onto a cue row → associate the cue with that scene.
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
        self._dry_var = tk.BooleanVar(value=self.settings.dry_run)
        ttk.Checkbutton(ftr, text="Dry-run (don't send OSC)",
                        variable=self._dry_var,
                        command=self._update_dry_run).pack(side="left", padx=8)
        self._status_label = tk.Label(ftr, text="", bg=_BG, fg=_FG_HEAD, font=_F_UI)
        self._status_label.pack(side="right")

    # === Device enumeration =================================================

    def _refresh_audio_devices(self) -> None:
        reinit_portaudio()
        self._audio_devices = list_audio_devices()
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
        names = get_channel_names(dev["index"], n_ch, dev.get("hostapi", ""))
        if not names:
            names = [f"Ch {i + 1}" for i in range(n_ch)]
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
        if self._scanner.is_running:
            return
        self._set_status("Scanning for LV1s…")
        # Show a "Discovering…" placeholder in the dropdown for the duration of the scan
        self._lv1_disc_combo.configure(values=["Discovering LV1s on the LAN…"])
        self._lv1_disc_var.set("Discovering LV1s on the LAN…")
        self._scanner.start(on_complete=lambda res: self.root.after_idle(
            lambda: self._on_discovery_done(res)
        ))

    def _on_discovery_done(self, results: List[DiscoveryEntry]) -> None:
        self._discovered = results
        labels = ["(none — use IP override)"]
        for r in results:
            ip = r.addresses[0] if r.addresses else "?"
            labels.append(f"{r.host or 'unknown'}  —  {ip}:{r.port or '?'}")
        self._lv1_disc_combo.configure(values=labels)
        # Restore previous selection if it still exists
        target = self.settings.lv1_selected
        chosen_label = labels[0]
        for i, r in enumerate(results, start=1):
            ip = r.addresses[0] if r.addresses else ""
            if f"{ip}:{r.port}" == target:
                chosen_label = labels[i]
                break
        else:
            if results:
                chosen_label = labels[1]
        self._lv1_disc_var.set(chosen_label)
        self._set_status(f"Discovery: {len(results)} LV1{'s' if len(results) != 1 else ''} found")

        # Now that discovery has populated the dropdown / cache, honour any
        # pending auto-connect from settings (silently — no popup if nothing
        # to connect to, since the user didn't click Connect).
        if getattr(self, "_auto_connect_pending", False):
            self._auto_connect_pending = False
            if self._resolve_target(quiet=True) is not None:
                self._connect_lv1()

    # === LV1 connect ========================================================

    def _resolve_target(self, quiet: bool = False) -> Optional[tuple[str, int]]:
        manual_host = self._lv1_host_var.get().strip()
        manual_port = 0
        try:
            manual_port = int(self._lv1_port_var.get().strip() or "0")
        except ValueError:
            manual_port = 0

        if manual_host and manual_port > 0:
            return manual_host, manual_port
        # If host given but port=0, look it up in the discovered list
        if manual_host:
            for r in self._discovered:
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
        # Otherwise, use the dropdown selection
        sel = self._lv1_disc_var.get()
        for i, label in enumerate(self._lv1_disc_combo["values"]):
            if label == sel and i > 0:
                r = self._discovered[i - 1]
                ip = r.addresses[0] if r.addresses else ""
                if ip and r.port:
                    return ip, r.port
        if not quiet:
            messagebox.showinfo("LV1", "No LV1 selected and no manual override.")
        return None

    def _toggle_lv1(self) -> None:
        if self._lv1.is_connected() or (self._lv1_state and self._lv1_state.connected):
            self._disconnect_lv1()
        else:
            self._connect_lv1()

    def _connect_lv1(self) -> None:
        target = self._resolve_target()
        if not target:
            return
        host, port = target
        # Immediate feedback before the reader thread reports back.
        self._set_lv1_button(online=True, label="● connecting…")
        self._lv1_state_label.config(text=f"Connecting to {host}:{port}…",
                                      fg=_FG_WARN)
        self._lv1.auto_reconnect = True
        self._lv1.connect(host, port)
        self.settings.lv1_host = self._lv1_host_var.get().strip()
        try:
            self.settings.lv1_port = int(self._lv1_port_var.get().strip() or "0")
        except ValueError:
            self.settings.lv1_port = 0
        # Save discovery selection
        sel = self._lv1_disc_var.get()
        for i, label in enumerate(self._lv1_disc_combo["values"]):
            if label == sel and i > 0:
                r = self._discovered[i - 1]
                ip = r.addresses[0] if r.addresses else ""
                if ip and r.port:
                    self.settings.lv1_selected = f"{ip}:{r.port}"
                    break

    def _disconnect_lv1(self) -> None:
        # Immediate visual feedback — disconnect() can take a moment to wind
        # down the reader thread, so we don't want the button to look ONLINE
        # while we're actually tearing the connection down.
        self._set_lv1_button(online=False, label="● disconnecting…")
        self._lv1_state_label.config(text="Disconnecting…", fg=_FG_WARN)
        self._lv1.auto_reconnect = False
        # Run the actual disconnect on a worker thread. LV1Client.disconnect()
        # calls reader.join(timeout=2.0), which would freeze the entire main
        # thread (including the TC display poll loop) until the reader exits.
        # The connection_change callback fires with connected=False once the
        # reader finishes — that's what updates the button to "● OFFLINE".
        threading.Thread(
            target=self._lv1.disconnect,
            name="LV1DisconnectWorker",
            daemon=True,
        ).start()

    # === LV1 callbacks (already marshalled to UI thread) ====================

    def _on_lv1_connection(self, state: ConnectionState) -> None:
        self._lv1_state = state
        if state.registered:
            txt = f"Connected — {state.host}:{state.port}"
            fg = _FG_OK
            self._set_lv1_button(online=True, label="● ONLINE")
        elif state.connected:
            txt = f"Handshaking… ({state.host}:{state.port})"
            fg = _FG_WARN
            self._set_lv1_button(online=True, label="● connecting…")
        else:
            err = state.last_error
            txt = f"Disconnected{' — ' + err if err else ''}"
            fg = _FG_ERR if err else _FG_DIM
            self._set_lv1_button(online=False, label="● OFFLINE")
        self._lv1_state_label.config(text=txt, fg=fg)

    def _set_lv1_button(self, online: bool, label: str) -> None:
        """Update the LV1 connect button: green = online, red = offline."""
        if online:
            bg, abg = _GO_BG, _GO_ABG
        else:
            bg, abg = _ST_BG, _ST_ABG
        self._lv1_btn.configure(text=label, bg=bg, fg="#FFFFFF")
        self._lv1_btn._bg = bg
        self._lv1_btn._abg = abg

    def _on_lv1_catalog(self, snap: SceneCatalogSnapshot) -> None:
        self._scene_catalog = dict(snap.scenes)
        self._refresh_catalog_tree()
        self._engine.resolve_against_catalog(self._scene_catalog)
        self._refresh_tree()
        # Validation warning popup — only on first catalog arrival per session
        if not getattr(self, "_validated_once", False):
            self._validated_once = True
            self._show_validation_warnings()

    def _on_lv1_current_scene(self, idx: Optional[int]) -> None:
        self._lv1_current_scene = idx
        if idx is None:
            self._cur_scene_label.config(text="—", fg=_FG_DIM)
        else:
            name = self._scene_catalog.get(idx, "(unknown)")
            self._cur_scene_label.config(text=f"[{idx}] {name}", fg=_FG_OK)
        self._refresh_catalog_tree()

    def _refresh_catalog_tree(self) -> None:
        self._cat_tree.delete(*self._cat_tree.get_children())
        cur = self._lv1_current_scene
        for idx in sorted(self._scene_catalog):
            name = self._scene_catalog[idx]
            tags = ("current",) if idx == cur else ()
            self._cat_tree.insert("", "end", iid=str(idx), values=(idx, name), tags=tags)

    def _on_catalog_double_click(self, _e=None) -> None:
        """Double-click on the LV1 scene catalog → recall that scene on the LV1."""
        sel = self._cat_tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        if not self._lv1.is_connected():
            messagebox.showwarning("Not connected", "Connect to the LV1 first.")
            return
        name = self._scene_catalog.get(idx, "(unknown)")
        self._lv1.recall_scene(idx)
        self._set_status(f"Recalled scene [{idx}] {name}")

    # --- Drag-and-drop: scene → cue ----------------------------------------

    def _on_catalog_press(self, event) -> None:
        """Record the scene under the cursor in case the user drags onto a cue."""
        row = self._cat_tree.identify_row(event.y)
        try:
            self._drag_scene_idx = int(row) if row else None
        except ValueError:
            self._drag_scene_idx = None
        self._dragging = False

    def _on_catalog_motion(self, event) -> None:
        """Switch cursor to a drag-affordance once the user actually moves."""
        if getattr(self, "_drag_scene_idx", None) is None:
            return
        if not self._dragging:
            self._dragging = True
            try:
                self.root.config(cursor="hand2")
            except Exception:
                pass

    def _on_catalog_release(self, event) -> None:
        """If the release ended over a cue row, assign the scene to that cue."""
        scene_idx = getattr(self, "_drag_scene_idx", None)
        was_dragging = getattr(self, "_dragging", False)
        self._drag_scene_idx = None
        self._dragging = False
        try:
            self.root.config(cursor="")
        except Exception:
            pass
        if scene_idx is None or not was_dragging:
            return  # not a drag — let single/double click handle it normally

        # Find what widget the mouse is hovering over now
        target = self.root.winfo_containing(event.x_root, event.y_root)
        if target is None:
            return
        # The cue tree, or a cell inside it — walk up the master chain
        widget = target
        while widget is not None:
            if widget is self._tree:
                break
            widget = getattr(widget, "master", None)
        if widget is not self._tree:
            return

        # Identify the cue row under the mouse
        tree_y = event.y_root - self._tree.winfo_rooty()
        cue_row = self._tree.identify_row(tree_y)
        if not cue_row:
            return
        try:
            cue_id = int(cue_row)
        except ValueError:
            return
        cue = self._cue_list.by_id(cue_id)
        if cue is None:
            return

        scene_name = self._scene_catalog.get(scene_idx, "")
        self._cue_list.replace(
            cue_id,
            scene_name=scene_name,
            scene_index=scene_idx,
        )
        self._engine.load_cue_list(self._cue_list)
        if self._scene_catalog:
            self._engine.resolve_against_catalog(self._scene_catalog)
        self._refresh_tree()
        self._tree.selection_set(str(cue_id))
        self._mark_dirty()
        self._set_status(
            f"Cue '{cue.label}' → scene [{scene_idx}] {scene_name}"
        )

    def _show_validation_warnings(self) -> None:
        if not self._cue_list.cues:
            return
        issues = [
            v for v in validate_all(self._cue_list.cues, self._scene_catalog)
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

    def _mark_dirty(self) -> None:
        if self._dirty:
            return
        self._dirty = True
        self._update_file_label()

    def _clear_dirty(self) -> None:
        self._dirty = False
        self._update_file_label()

    def _update_file_label(self) -> None:
        base = os.path.basename(self._current_file) if self._current_file else "(unsaved)"
        self._file_label.config(text=("• " + base) if self._dirty else base)

    def _confirm_discard_changes(self, prompt: str) -> bool:
        """If there are unsaved changes, ask the user to Save / Don't save / Cancel.
        Returns True if it's OK to proceed (changes saved or discarded);
        False if the user cancelled."""
        if not self._dirty:
            return True
        ans = messagebox.askyesnocancel(
            "Unsaved changes",
            prompt + "\n\nSave changes first?",
        )
        if ans is None:  # Cancel
            return False
        if ans:  # Yes — save
            self._save_list()
            # If save was cancelled (no path picked), _dirty stays True
            return not self._dirty
        # No — discard
        return True

    # === Cue list ops =======================================================

    def _refresh_tree(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for i, c in enumerate(self._cue_list.cues, start=1):
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
        return self._cue_list.by_id(int(sel[0]))

    def _on_tree_click(self, e) -> None:
        region = self._tree.identify("region", e.x, e.y)
        col = self._tree.identify_column(e.x)
        if region == "cell" and col == "#6":  # the enabled column
            row = self._tree.identify_row(e.y)
            if row:
                self._toggle_enabled(int(row))

    def _add_cue(self) -> None:
        CueDialog(self.root, None, self._scene_catalog, self._on_dialog_save)

    def _edit_cue(self) -> None:
        cue = self._selected_cue()
        if cue is None:
            return
        CueDialog(self.root, cue, self._scene_catalog,
                  lambda **kw: self._on_dialog_save(cue_id=cue.id, **kw))

    def _on_dialog_save(self, cue_id: Optional[int] = None, **kw) -> None:
        if cue_id is None:
            self._cue_list.add(
                label=kw.get("label", ""),
                timecode=kw.get("timecode", "00:00:00:00"),
                scene_name=kw.get("scene_name", ""),
                scene_index=kw.get("scene_index"),
            )
            new = self._cue_list.cues[-1]
            new.enabled = kw.get("enabled", True)
        else:
            self._cue_list.replace(cue_id, **kw)
        self._engine.load_cue_list(self._cue_list)
        if self._scene_catalog:
            self._engine.resolve_against_catalog(self._scene_catalog)
        self._refresh_tree()
        self._mark_dirty()

    def _remove_cue(self) -> None:
        cue = self._selected_cue()
        if cue is None:
            return
        if not messagebox.askyesno("Remove cue", f"Remove cue '{cue.label}'?"):
            return
        self._cue_list.remove(cue.id)
        self._engine.load_cue_list(self._cue_list)
        self._refresh_tree()
        self._mark_dirty()

    def _move_up(self) -> None:
        cue = self._selected_cue()
        if cue and self._cue_list.move_up(cue.id):
            self._engine.load_cue_list(self._cue_list)
            self._refresh_tree()
            self._tree.selection_set(str(cue.id))
            self._mark_dirty()

    def _move_down(self) -> None:
        cue = self._selected_cue()
        if cue and self._cue_list.move_down(cue.id):
            self._engine.load_cue_list(self._cue_list)
            self._refresh_tree()
            self._tree.selection_set(str(cue.id))
            self._mark_dirty()

    def _toggle_enabled(self, cue_id: int) -> None:
        cue = self._cue_list.by_id(cue_id)
        if cue is None:
            return
        cue.enabled = not cue.enabled
        self._refresh_tree()
        self._tree.selection_set(str(cue.id))
        self._mark_dirty()

    def _tap(self) -> None:
        if self._current_tc is None:
            return
        tc_str = str(self._current_tc)
        new = self._cue_list.add(
            label=f"Cue {len(self._cue_list) + 1}",
            timecode=tc_str,
        )
        self._engine.load_cue_list(self._cue_list)
        if self._scene_catalog:
            self._engine.resolve_against_catalog(self._scene_catalog)
        self._refresh_tree()
        self._tree.selection_set(str(new.id))
        self._mark_dirty()

    def _test_fire(self) -> None:
        cue = self._selected_cue()
        if cue is None:
            return
        if cue.scene_index is None or cue.scene_status in ("MISSING", "EMPTY"):
            messagebox.showwarning(
                "Cannot fire",
                f"Cue '{cue.label}' has no resolved LV1 scene.",
            )
            return
        if self._dry_var.get():
            self._log("info", f"[dry-run] would recall scene {cue.scene_index}")
            return
        if not self._lv1.is_connected():
            messagebox.showwarning("Not connected", "Connect to the LV1 first.")
            return
        self._lv1.recall_scene(cue.scene_index)
        self._set_status(f"Test fire: scene {cue.scene_index} → {cue.scene_name}")

    def _reset_fired(self) -> None:
        self._engine.reset()

    def _revalidate(self) -> None:
        if not self._scene_catalog:
            messagebox.showinfo("Re-validate", "Not connected to an LV1 yet.")
            return
        self._engine.resolve_against_catalog(self._scene_catalog)
        self._refresh_tree()
        self._show_validation_warnings()

    # === Audio start/stop ===================================================

    def _toggle_run(self) -> None:
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if self._running:
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
            self._audio.configure(dev["index"], ch1 - 1, sr)
            self._audio.start()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Audio", f"Failed to start audio:\n{exc}")
            return

        # Auto-connect the LV1 too, if a target is configured.
        if not self._lv1.is_connected() and self._resolve_target(quiet=True) is not None:
            self._connect_lv1()

        self._running = True
        self._recovering = False
        self._last_signal_ok = None
        self._last_tc_time = 0
        self._ltc_status.config(text="● Waiting for LTC signal…", fg=_FG_WARN)
        # Morph the transport button into STOP mode
        self._run_btn.configure(text="■  STOP", bg=_ST_BG, fg="#FFFFFF")
        self._run_btn._bg = _ST_BG
        self._run_btn._abg = _ST_ABG
        self.settings.audio_device = sel.split("  (")[0]
        self.settings.audio_channel = ch1

    def _stop(self) -> None:
        if not self._running:
            return
        try:
            self._audio.stop()
        except Exception:
            pass
        self._running = False
        self._recovering = False
        self._last_signal_ok = None
        self._ltc_status.config(text="● Stopped", fg=_FG_DIM)
        # Morph the transport button back into START mode
        self._run_btn.configure(text="▶  START", bg=_GO_BG, fg="#FFFFFF")
        self._run_btn._bg = _GO_BG
        self._run_btn._abg = _GO_ABG
        self._tc_label.config(fg=_TC_OFF)

    # === Timecode polling ===================================================

    def _poll_queue(self) -> None:
        """Main thread, ~25 Hz. Drains the timecode queue into the cue engine,
        updates the TC display, and polls the audio capture for signal status
        and stream health (driver reset auto-recovery)."""
        if self._running and not self._recovering:
            # Detect unexpected stream death (e.g. SoundGrid driver reset, ASIO
            # silent hang). Reset and try again in 2 s.
            if not self._audio.stream_active or self._audio.callback_stalled:
                self._recovering = True
                self._last_signal_ok = None
                self._audio.stop()
                self._ltc_status.config(text="● Driver reset — restarting…", fg=_FG_WARN)
                self._tc_label.config(fg=_TC_OFF)
                self.root.after(2000, self._auto_restart)
                self.root.after(40, self._poll_queue)
                return

            latest: Optional[Timecode] = None
            try:
                while True:
                    tc = self._tc_queue.get_nowait()
                    self._engine.on_timecode(tc)
                    self._engine.set_fps(tc.fps)
                    latest = tc
            except queue.Empty:
                pass

            if latest is not None:
                self._current_tc = latest
                self._tc_label.config(text=str(latest))
                self._last_tc_time = 0
                fps = self._audio.detected_fps
                if fps:
                    self._fps_label.config(text=f"{fps:.2f} fps".rstrip("0").rstrip("."),
                                            fg=_FG_HEAD)

            self._last_tc_time += 1
            # Three distinct states for the LTC pipeline:
            #   - locked         → decoder is producing frames (true "LTC OK")
            #   - signal present → audio above noise floor BUT no LTC pattern
            #                       (wrong channel, wrong sample rate, garbage)
            #   - no signal      → audio input silent for >1 s
            locked = self._audio.is_locked
            signal_ok = self._audio.signal_present
            if locked:
                new_state = "LOCKED"
            elif signal_ok:
                new_state = "AUDIO_NOT_LTC"
            elif self._last_tc_time > 25:
                new_state = "NO_SIGNAL"
            else:
                new_state = None  # still in waiting-for-first-frame grace period

            if new_state and new_state != self._last_signal_ok:
                self._last_signal_ok = new_state
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

        self.root.after(40, self._poll_queue)   # 40 ms ≈ 25 Hz

    def _auto_restart(self) -> None:
        """Re-arm the audio capture after a detected driver reset."""
        if not self._running:
            self._recovering = False
            return
        try:
            self._audio.start()
            self._recovering = False
            self._ltc_status.config(text="● Restarted — waiting for signal",
                                    fg=_FG_WARN)
        except Exception as exc:  # noqa: BLE001
            # Try again in 2 s — driver may still be busy
            self._ltc_status.config(text=f"● Restart failed: {exc}", fg=_FG_ERR)
            self.root.after(2000, self._auto_restart)

    # === Engine callbacks ===================================================

    def _on_cue_fired(self, cue: Cue) -> None:
        self._refresh_tree()
        # Two-line fire indicator: scene that was recalled + timing info.
        # Persists in the LV1 STATUS column until the next fire — catalog
        # updates etc. no longer wipe it.
        cur = str(self._current_tc) if self._current_tc else "??:??:??:??"
        self._last_fire_label.config(
            text=(
                f"[{cue.scene_index}] {cue.scene_name or cue.label}\n"
                f"target {cue.timecode}  fired @ {cur}"
            ),
            fg=_FG_OK,
        )

    def _on_cue_skipped(self, cue: Cue, reason: str) -> None:
        self._set_status(f"Skipped '{cue.label}': {reason}", warn=True)

    def _on_send_error(self, msg: str) -> None:
        self._set_status(f"LV1 send error: {msg}", warn=True)

    # === File ops ===========================================================

    def _new_list(self) -> None:
        if not self._confirm_discard_changes("Start a new cue list?"):
            return
        self._cue_list = CueList()
        self._current_file = None
        self._engine.load_cue_list(self._cue_list)
        self._refresh_tree()
        self._clear_dirty()

    def _open_list(self) -> None:
        if not self._confirm_discard_changes("Open a different cue list?"):
            return
        path = filedialog.askopenfilename(
            title="Open cue list",
            filetypes=[("JSON cue lists", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self._load_cue_file(path)

    def _load_cue_file(self, path: str) -> None:
        try:
            was_midi = CueList.was_migrated_from_midi(path)
            cl = CueList.load(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Open", f"Failed to load:\n{exc}")
            return
        self._cue_list = cl
        self._current_file = path
        self.settings.last_cue_file = path
        self._engine.load_cue_list(self._cue_list)
        if self._scene_catalog:
            self._engine.resolve_against_catalog(self._scene_catalog)
        self._refresh_tree()
        self._clear_dirty()
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
            # Reset validated_once so the warning fires after the next catalog load
            self._validated_once = False
            if self._scene_catalog:
                self._show_validation_warnings()

    def _save_list(self) -> None:
        if self._current_file:
            self._write_cue_file(self._current_file)
        else:
            self._save_list_as()

    def _save_list_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save cue list",
            defaultextension=".json",
            filetypes=[("JSON cue lists", "*.json")],
        )
        if path:
            self._write_cue_file(path)
            self._current_file = path
            self.settings.last_cue_file = path

    def _write_cue_file(self, path: str) -> None:
        try:
            self._cue_list.save(path)
            self._set_status(f"Saved: {os.path.basename(path)}")
            self._clear_dirty()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save", f"Failed to save:\n{exc}")

    # === Misc ===============================================================

    def _update_tolerance(self) -> None:
        try:
            t = int(self._tol_var.get())
        except ValueError:
            return
        self._engine.tolerance_frames = t
        self.settings.tolerance_frames = t

    def _update_dry_run(self) -> None:
        v = self._dry_var.get()
        self._engine.dry_run = v
        self.settings.dry_run = v
        self._set_status("Dry-run ON" if v else "Dry-run OFF")

    def _set_status(self, text: str, warn: bool = False) -> None:
        self._status_label.config(text=text, fg=_FG_WARN if warn else _FG_HEAD)

    def _log(self, level: str, msg: str) -> None:
        # Only surface warnings/errors in the status bar — info-level logs
        # (catalog updates, registration confirmations, etc.) would otherwise
        # clobber the "fired cue" message, hiding what the operator most needs
        # to see. Info still goes to stdout for debugging.
        if level in ("warn", "warning", "error"):
            self._set_status(msg, warn=True)
        else:
            print(f"[lv1] {msg}")

    def _show_about(self) -> None:
        messagebox.showinfo(
            "LTC to LV1",
            f"LTC to LV1  v{_VERSION}\n\n"
            "Reads SMPTE LTC from an audio input and recalls Waves LV1\n"
            "snapshots over OSC at frame-accurate timecodes.\n\n"
            "MIT licensed.",
        )

    # === Update check =======================================================

    def _check_updates(self, silent_if_ok: bool = False) -> None:
        """Hit the GitHub releases API and offer to open the page if a newer
        tag is available. Runs on a background thread; the UI prompt is
        marshalled back to the main thread."""
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
        # Ask to save first if the cue list has unsaved changes
        if not self._confirm_discard_changes("Quit without saving?"):
            return
        try:
            self._stop()
        except Exception:
            pass
        try:
            self._lv1.disconnect()
        except Exception:
            pass
        try:
            self.settings.save()
        except Exception:
            pass
        self.root.destroy()


# --- Module-level helpers ---------------------------------------------------


def _version_tuple(v: str) -> tuple:
    """Parse 'X.Y.Z' into (X, Y, Z) for comparison. Non-numeric segments
    become 0 so they sort lower than any real number."""
    out = []
    for part in v.strip().lstrip("v").split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out) or (0,)
