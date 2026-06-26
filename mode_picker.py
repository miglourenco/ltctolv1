"""
First-launch mode picker.

Shown when AppSettings.mode is empty (fresh install). Lets the operator
choose between running this PC as the LTC host or as a remote control
for another LTCtoLV1 on the LAN. The choice is persisted to settings so
subsequent launches skip straight into the chosen mode.
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk
from typing import Optional


# Colours mirror main_window's dark theme so the picker doesn't look
# like a separate app when it pops up first thing.
_BG       = "#1E1E1E"
_BG_PAN   = "#252526"
_BG_HDR   = "#2D2D2D"
_BORDER   = "#3C3C3C"
_FG       = "#CCCCCC"
_FG_HEAD  = "#888888"
_FG_DIM   = "#666666"
_GO_BG    = "#166534"
_GO_ABG   = "#15803D"

_F_UI   = ("Segoe UI", 10)            if sys.platform == "win32" else ("Helvetica Neue", 12)
_F_UIB  = ("Segoe UI", 10, "bold")    if sys.platform == "win32" else ("Helvetica Neue", 12, "bold")
_F_BIG  = ("Segoe UI", 14, "bold")    if sys.platform == "win32" else ("Helvetica Neue", 16, "bold")


class ModePicker(tk.Toplevel):
    """Modal startup chooser. Caller waits on .wait_window(); the choice
    is read from .result ('host' | 'remote' | None for cancel)."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.title("LTCtoLV1 — choose mode")
        self.configure(bg=_BG)
        self.resizable(False, False)
        # No transient(): see RemotePicker for the same reasoning — with
        # the root withdrawn during startup, transient hides the dialog
        # from the taskbar on Windows.
        self.grab_set()
        self.result: Optional[str] = None

        body = tk.Frame(self, bg=_BG_PAN, padx=24, pady=20,
                        highlightthickness=1, highlightbackground=_BORDER)
        body.pack(padx=12, pady=12)

        tk.Label(
            body, text="How will you use LTCtoLV1?", bg=_BG_PAN, fg=_FG,
            font=_F_BIG,
        ).pack(anchor="w", pady=(0, 4))
        tk.Label(
            body,
            text="You can change this later in File ▸ Switch mode.",
            bg=_BG_PAN, fg=_FG_DIM, font=_F_UI, justify="left",
        ).pack(anchor="w", pady=(0, 16))

        # Two cards stacked vertically — each is a button-frame with an
        # explanatory paragraph underneath. Clicking anywhere in the card
        # commits the choice.
        self._card(body, title="Host (this PC drives LTC and the LV1)",
                   blurb=(
                       "Capture SMPTE LTC from an audio input on this machine and recall\n"
                       "Waves LV1 snapshots over OSC. Optionally serves a web remote\n"
                       "and a system-tray icon so other devices can control it."
                   ),
                   value="host")
        self._card(body, title="Remote control (this PC controls another LTCtoLV1)",
                   blurb=(
                       "Discover other LTCtoLV1 hosts on the LAN and control them\n"
                       "from here. Native desktop UI, no browser needed. Use this if\n"
                       "the LTC machine is the LV1 console itself."
                   ),
                   value="remote")

        # Footer with Cancel only — committing happens on card click so
        # the picker has the "two big buttons" feel.
        ftr = tk.Frame(body, bg=_BG_PAN)
        ftr.pack(fill="x", pady=(8, 0))
        tk.Button(ftr, text="Cancel", command=self._cancel,
                  bg="#383838", fg=_FG, activebackground="#505050",
                  relief="flat", padx=14, pady=4, font=_F_UI).pack(side="right")

        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # Centre + force to the front so a hidden root doesn't trap us
        # behind whatever terminal launched the app.
        self.after_idle(lambda: self._centre_on(parent))
        self.after_idle(self._raise_and_focus)

    def _raise_and_focus(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(300, lambda: self._safe_topmost_off())
            self.focus_force()
        except tk.TclError:
            pass

    def _safe_topmost_off(self) -> None:
        try:
            self.attributes("-topmost", False)
        except tk.TclError:
            pass

    # ─── Card factory ──────────────────────────────────────────────────
    def _card(self, parent: tk.Misc, title: str, blurb: str, value: str) -> None:
        card = tk.Frame(parent, bg=_BG_HDR, padx=16, pady=12,
                        highlightthickness=1, highlightbackground=_BORDER,
                        cursor="hand2")
        card.pack(fill="x", pady=6)
        # Bind on the frame and every child so any click commits.
        tk.Label(card, text=title, bg=_BG_HDR, fg=_FG, font=_F_UIB,
                 cursor="hand2").pack(anchor="w")
        tk.Label(card, text=blurb, bg=_BG_HDR, fg=_FG_HEAD, font=_F_UI,
                 justify="left", cursor="hand2").pack(anchor="w", pady=(4, 0))
        for w in [card] + list(card.winfo_children()):
            w.bind("<Button-1>", lambda _e, v=value: self._pick(v))
        # Hover affordance.
        card.bind("<Enter>", lambda _e: card.configure(bg="#3a3a3a"))
        card.bind("<Leave>", lambda _e: card.configure(bg=_BG_HDR))

    def _pick(self, value: str) -> None:
        self.result = value
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _centre_on(self, parent: tk.Misc) -> None:
        try:
            self.update_idletasks()
            w = self.winfo_width(); h = self.winfo_height()
            pw = parent.winfo_width(); ph = parent.winfo_height()
            px = parent.winfo_rootx(); py = parent.winfo_rooty()
            x = max(0, px + (pw - w) // 2)
            y = max(0, py + (ph - h) // 2)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
