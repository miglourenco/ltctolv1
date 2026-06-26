"""
Remote-mode "pick a host to control" dialog.

Listens for LTCtoLV1 announcements on the LAN (via announce.Discoverer)
and presents a live-updating list. Operator can also fall back to typing
a host:port manually for cross-subnet setups where multicast isn't
forwarded.
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk
from typing import List, Optional, Tuple

from announce import Discoverer, HostEntry


# Theme constants (mirrors main_window).
_BG       = "#1E1E1E"
_BG_PAN   = "#252526"
_BG_WID   = "#3C3C3C"
_BG_HDR   = "#2D2D2D"
_BG_SEL   = "#094771"
_BORDER   = "#3C3C3C"
_FG       = "#CCCCCC"
_FG_HEAD  = "#888888"
_FG_DIM   = "#555555"
_GO_BG    = "#166534"
_GO_ABG   = "#15803D"

_F_UI   = ("Segoe UI", 10)            if sys.platform == "win32" else ("Helvetica Neue", 12)
_F_UIB  = ("Segoe UI", 10, "bold")    if sys.platform == "win32" else ("Helvetica Neue", 12, "bold")
_F_MONO = ("Courier New", 10)


class RemotePicker(tk.Toplevel):
    """Modal connect-to-host dialog.

    Caller waits via .wait_window() and reads .result, which is either
    (host, port) on Connect, or None on Cancel."""

    def __init__(self, parent: tk.Misc,
                 default_host: str = "", default_port: int = 8080) -> None:
        super().__init__(parent)
        self.title("LTCtoLV1 — connect to host")
        self.configure(bg=_BG)
        # NOT transient — with the root window withdrawn during the picker
        # phase, transient() can make the dialog vanish from the Windows
        # taskbar (it would follow a parent that isn't visible). Keeping
        # it as a standalone top-level guarantees the operator can find
        # it via alt-tab.
        self.grab_set()
        self.result: Optional[Tuple[str, int]] = None

        # ── Discovery runs only while the dialog is up. ──────────────────
        self._discoverer = Discoverer(on_change=self._on_change_thread)
        self._discoverer.start()
        self._entries: List[HostEntry] = []
        self._destroyed = False

        body = tk.Frame(self, bg=_BG_PAN, padx=16, pady=14,
                        highlightthickness=1, highlightbackground=_BORDER)
        body.pack(padx=12, pady=12, fill="both", expand=True)

        tk.Label(body, text="LTCtoLV1 hosts on the LAN",
                 bg=_BG_PAN, fg=_FG_HEAD, font=_F_UIB).pack(anchor="w")
        tk.Label(body,
                 text="Picks up other LTCtoLV1 instances that are broadcasting on the LAN.",
                 bg=_BG_PAN, fg=_FG_DIM, font=_F_UI, justify="left").pack(
            anchor="w", pady=(0, 6)
        )

        # ── Discovered list (treeview) ───────────────────────────────────
        list_frame = tk.Frame(body, bg=_BG_PAN)
        list_frame.pack(fill="both", expand=True)
        cols = ("host", "ip", "port", "version")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings",
                                  height=8, selectmode="browse")
        self._tree.heading("host", text="Host")
        self._tree.heading("ip", text="IP")
        self._tree.heading("port", text="Port")
        self._tree.heading("version", text="Version")
        self._tree.column("host", width=180, anchor="w")
        self._tree.column("ip", width=140, anchor="w")
        self._tree.column("port", width=60, anchor="center")
        self._tree.column("version", width=80, anchor="center")
        self._tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        sb.pack(side="right", fill="y")
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.bind("<Double-1>", lambda _e: self._connect_selected())
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        self._placeholder = tk.Label(body, text="Scanning the LAN…",
                                     bg=_BG_PAN, fg=_FG_DIM, font=_F_UI)
        self._placeholder.pack(anchor="w", pady=(4, 0))

        # ── Manual host:port row ─────────────────────────────────────────
        man = tk.Frame(body, bg=_BG_PAN)
        man.pack(fill="x", pady=(12, 4))
        tk.Label(man, text="Or enter host:", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI).pack(side="left")
        self._host_var = tk.StringVar(value=default_host or "")
        tk.Entry(man, textvariable=self._host_var, width=18, font=_F_UI,
                 bg=_BG_WID, fg=_FG, insertbackground=_FG,
                 highlightthickness=0).pack(side="left", padx=(6, 8))
        tk.Label(man, text="Port:", bg=_BG_PAN, fg=_FG_HEAD,
                 font=_F_UI).pack(side="left")
        self._port_var = tk.StringVar(value=str(default_port))
        tk.Entry(man, textvariable=self._port_var, width=7, font=_F_UI,
                 bg=_BG_WID, fg=_FG, insertbackground=_FG,
                 highlightthickness=0).pack(side="left", padx=6)

        # ── Buttons ──────────────────────────────────────────────────────
        btns = tk.Frame(body, bg=_BG_PAN)
        btns.pack(fill="x", pady=(10, 0))
        tk.Button(btns, text="Cancel", command=self._cancel,
                  bg="#383838", fg=_FG, activebackground="#505050",
                  relief="flat", padx=14, pady=4, font=_F_UI).pack(side="right", padx=(6, 0))
        self._connect_btn = tk.Button(btns, text="Connect",
                                       command=self._connect_clicked,
                                       bg=_GO_BG, fg="#fff",
                                       activebackground=_GO_ABG,
                                       relief="flat", padx=18, pady=4,
                                       font=_F_UIB)
        self._connect_btn.pack(side="right")

        self.bind("<Return>", lambda _e: self._connect_clicked())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # Force the picker to the front + take focus. Important when the
        # root window is hidden — without this the dialog can land behind
        # the terminal that launched us and look like the app hung.
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

    # ─── Discovery → UI marshalling ──────────────────────────────────────
    def _on_change_thread(self, entries: List[HostEntry]) -> None:
        # Discoverer fires us on its background thread; bounce to tk.
        if self._destroyed:
            return
        try:
            self.after_idle(lambda: self._refresh_list(entries))
        except tk.TclError:
            pass

    def _refresh_list(self, entries: List[HostEntry]) -> None:
        if self._destroyed:
            return
        prev_sel = self._selected_uuid()
        self._entries = entries
        try:
            self._tree.delete(*self._tree.get_children())
        except tk.TclError:
            return
        for e in entries:
            self._tree.insert("", "end", iid=e.uuid,
                              values=(e.hostname, e.best_ip, e.web_port, e.version))
        if entries:
            self._placeholder.config(text="")
            # Restore selection if the same entry is still in the list,
            # otherwise pre-select the first row so Enter just works.
            if prev_sel and self._tree.exists(prev_sel):
                self._tree.selection_set(prev_sel)
            else:
                self._tree.selection_set(entries[0].uuid)
            self._on_select()
        else:
            self._placeholder.config(text="No LTCtoLV1 hosts found yet — make sure the host is running and on the same LAN.")

    def _on_select(self, _e: object = None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        entry = next((e for e in self._entries if e.uuid == sel[0]), None)
        if entry is None:
            return
        # Pre-fill the manual fields so the operator can edit before Connect.
        self._host_var.set(entry.best_ip)
        self._port_var.set(str(entry.web_port))

    def _selected_uuid(self) -> Optional[str]:
        sel = self._tree.selection()
        return sel[0] if sel else None

    # ─── Actions ─────────────────────────────────────────────────────────
    def _connect_selected(self) -> None:
        self._on_select()
        self._connect_clicked()

    def _connect_clicked(self) -> None:
        host = self._host_var.get().strip()
        try:
            port = int(self._port_var.get().strip() or "0")
        except ValueError:
            port = 0
        if not host or port <= 0:
            return
        self.result = (host, port)
        self._destroyed = True
        self._discoverer.stop()
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self._destroyed = True
        try:
            self._discoverer.stop()
        except Exception:
            pass
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
