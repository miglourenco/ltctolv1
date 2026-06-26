"""
Data models: Cue, CueList, AppSettings.
No I/O side effects at import time.

Cue schema notes:
  - The cue's authoritative reference to a snapshot is its `scene_name`.
    The LV1 may reorder scenes between sessions; the name stays stable.
  - `scene_index` is a cached hint of the last known index for that name.
    The cue engine refreshes it from the LV1's live catalog. If the catalog
    changes, the index is updated transparently as long as the name still
    resolves.
  - `scene_status` is RUNTIME ONLY (never serialised): "OK" / "RECOVERED"
    / "MISSING" / "EMPTY" — set by the resolver after each catalog update.

Migration from the old MIDI cue-list format:
  - Old field `program` (MIDI Program Change 0-127) maps to `scene_index`
    (LV1 PC N triggered scene N). `scene_name` is left empty so the user
    can re-associate by name. The resolver flags these as "EMPTY".
  - Old field `channel` is dropped (no equivalent for OSC recall).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import List, Optional


# ── Custom file type ─────────────────────────────────────────────────────────
# The desktop app registers this extension with the OS so cue list files can
# be opened by double-click. Content is plain JSON (Cue.from_dict still loads
# legacy ".json" exports), but giving the file its own extension lets the OS
# bind it to LTCtoLV1 exclusively.
CUE_FILE_EXTENSION = ".ltcv1"
CUE_FILE_DESCRIPTION = "LTCtoLV1 cue list"
WINDOWS_PROGID = "LTCtoLV1.cuelist"

# Maximum entries kept in AppSettings.recent_files. Older entries are dropped
# off the bottom; new entries push to the top (most-recent-first).
RECENT_FILES_MAX = 10


def default_projects_dir() -> str:
    """Where cue list files live by default.

    Windows: %USERPROFILE%\\Documents\\LTCtoLV1
    macOS:   ~/Documents/LTCtoLV1
    Linux:   ~/Documents/LTCtoLV1

    On Windows we ask the shell for the *current* Documents path because
    OneDrive (and corporate group policy) often relocates it from the literal
    ~/Documents. Falling back to expanduser() if the shell call fails.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            CSIDL_PERSONAL = 0x0005
            SHGFP_TYPE_CURRENT = 0
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            res = ctypes.windll.shell32.SHGetFolderPathW(
                None, CSIDL_PERSONAL, None, SHGFP_TYPE_CURRENT, buf
            )
            base = buf.value if res == 0 and buf.value else os.path.expanduser("~/Documents")
        except Exception:
            base = os.path.expanduser("~/Documents")
    else:
        base = os.path.expanduser("~/Documents")
    return os.path.join(base, "LTCtoLV1")


def ensure_projects_dir() -> str:
    """Return default_projects_dir() after best-effort mkdir. Never raises."""
    path = default_projects_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _norm(path: str) -> str:
    """Canonical key for deduping recent-files entries — case-insensitive on
    Windows, case-sensitive elsewhere."""
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except Exception:
        return path


# --- Cue --------------------------------------------------------------------


@dataclass
class Cue:
    id: int
    label: str
    timecode: str  # canonical "HH:MM:SS:FF"
    scene_name: str  # authoritative reference (may be "" for migrated cues)
    scene_index: Optional[int] = None  # last-known index hint
    enabled: bool = True

    # Runtime-only fields (never serialised)
    fired: bool = field(default=False, repr=False, compare=False)
    scene_status: str = field(default="EMPTY", repr=False, compare=False)

    # ---- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "timecode": self.timecode,
            "scene_name": self.scene_name,
            "scene_index": self.scene_index,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        # Detect old MIDI format (program/channel) and migrate.
        if "scene_name" not in d and "program" in d:
            # Old MIDI cue list — map Program Change to scene index hint.
            scene_name = ""
            scene_index: Optional[int] = int(d.get("program", 0))
        else:
            scene_name = str(d.get("scene_name", ""))
            raw_idx = d.get("scene_index", None)
            scene_index = int(raw_idx) if raw_idx is not None else None

        return cls(
            id=int(d.get("id", 0)),
            label=str(d.get("label", "")),
            timecode=str(d.get("timecode", "00:00:00:00")),
            scene_name=scene_name,
            scene_index=scene_index,
            enabled=bool(d.get("enabled", True)),
        )

    # ---- helpers -----------------------------------------------------------

    def timecode_as_frames(self, fps: float = 25.0) -> int:
        """Convert HH:MM:SS:FF to absolute frame count."""
        try:
            parts = self.timecode.replace(";", ":").split(":")
            h, m, s, f = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
            return (h * 3600 + m * 60 + s) * round(fps) + f
        except Exception:
            return -1


# --- CueList ----------------------------------------------------------------


class CueList:
    def __init__(self) -> None:
        self.cues: List[Cue] = []
        self._next_id: int = 1

    # ---- I/O ---------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "CueList":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        cl = cls()
        cl.cues = [Cue.from_dict(d) for d in data]
        cl._next_id = max((c.id for c in cl.cues), default=0) + 1
        return cl

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                [c.to_dict() for c in self.cues],
                fh,
                indent=2,
                ensure_ascii=False,
            )

    @classmethod
    def was_migrated_from_midi(cls, path: str) -> bool:
        """True if any cue in the file uses the old MIDI program/channel schema."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return False
        if not isinstance(data, list):
            return False
        return any(
            "program" in d and "scene_name" not in d
            for d in data
            if isinstance(d, dict)
        )

    # ---- mutation ----------------------------------------------------------

    def add(
        self,
        label: str,
        timecode: str,
        scene_name: str = "",
        scene_index: Optional[int] = None,
    ) -> Cue:
        cue = Cue(
            id=self._next_id,
            label=label,
            timecode=timecode,
            scene_name=scene_name,
            scene_index=scene_index,
        )
        self._next_id += 1
        self.cues.append(cue)
        return cue

    def replace(self, cue_id: int, **kwargs) -> bool:
        cue = self.by_id(cue_id)
        if cue is None:
            return False
        for k, v in kwargs.items():
            if hasattr(cue, k):
                setattr(cue, k, v)
        return True

    def remove(self, cue_id: int) -> bool:
        before = len(self.cues)
        self.cues = [c for c in self.cues if c.id != cue_id]
        return len(self.cues) < before

    def move_up(self, cue_id: int) -> bool:
        idx = self._index(cue_id)
        if idx is None or idx == 0:
            return False
        self.cues[idx], self.cues[idx - 1] = self.cues[idx - 1], self.cues[idx]
        return True

    def move_down(self, cue_id: int) -> bool:
        idx = self._index(cue_id)
        if idx is None or idx >= len(self.cues) - 1:
            return False
        self.cues[idx], self.cues[idx + 1] = self.cues[idx + 1], self.cues[idx]
        return True

    def reset_fired_flags(self) -> None:
        for c in self.cues:
            c.fired = False

    # ---- queries -----------------------------------------------------------

    def by_id(self, cue_id: int) -> Optional[Cue]:
        return next((c for c in self.cues if c.id == cue_id), None)

    def _index(self, cue_id: int) -> Optional[int]:
        return next((i for i, c in enumerate(self.cues) if c.id == cue_id), None)

    def __len__(self) -> int:
        return len(self.cues)


# --- AppSettings ------------------------------------------------------------

_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
_SETTINGS_DIR = os.path.join(_APPDATA, "LTCtoLV1")
SETTINGS_PATH = os.path.join(_SETTINGS_DIR, "settings.json")


@dataclass
class AppSettings:
    # Audio
    audio_device: str = ""
    audio_channel: int = 1  # 1-based for UI; convert to 0-based when using
    sample_rate: int = 48000
    block_size: int = 512  # PortAudio buffer size in samples
    # LV1 connection
    lv1_selected: str = ""  # encoded "ip:port" from the discovery dropdown
    lv1_host: str = ""  # manual override
    lv1_port: int = 0  # 0 = auto-discover the port for lv1_host
    # Engine
    tolerance_frames: int = 1
    dry_run: bool = False
    # UI state
    last_cue_file: str = ""
    recent_files: List[str] = field(default_factory=list)
    # Web remote
    web_enabled: bool = False
    web_port: int = 8080
    # System integration
    tray_enabled: bool = True           # show a system-tray icon
    autostart_enabled: bool = False     # launch on user login (starts minimized to tray)
    # Operating mode — "host" runs LTC capture + LV1 control + web/tray locally;
    # "remote" connects to another LTCtoLV1 host on the LAN and acts as a
    # native remote control. Empty string forces the picker on first launch.
    mode: str = ""
    remote_host: str = ""               # last connected remote host (auto-fill)
    remote_port: int = 8080

    @classmethod
    def load(cls) -> "AppSettings":
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            return cls(
                **{k: v for k, v in d.items() if k in cls.__dataclass_fields__}
            )
        except Exception:
            return cls()

    def save(self) -> None:
        os.makedirs(_SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
