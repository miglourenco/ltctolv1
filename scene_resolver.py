"""
Resolve a cue's stored scene name to a current LV1 scene index.

A cue carries:
  - scene_name:   the authoritative reference (what the user typed)
  - scene_index:  the last-known index for that name (hint / fallback)

When the catalog changes (e.g. the user reordered scenes on the LV1),
resolve() reconciles the two and returns a status:

  OK         — name found in catalog, index matched the stored hint
  RECOVERED  — name found in catalog, but at a different index (hint refreshed)
  MISSING    — name not in catalog (cue is broken until renamed)
  EMPTY      — no name stored (imported from a MIDI-era cue list)

For MISSING cues we also offer a fuzzy suggestion if there's a close match.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Dict, List, Optional


# Match anything within 60% similarity. Lower = more permissive, higher
# = stricter. Empirically 0.6 catches typos and case/whitespace edits
# but rejects unrelated names.
FUZZY_CUTOFF = 0.6


@dataclass
class Resolution:
    status: str  # "OK" | "RECOVERED" | "MISSING" | "EMPTY"
    index: Optional[int]
    suggestion_name: Optional[str] = None
    suggestion_index: Optional[int] = None


def resolve(
    scene_name: str,
    hint_index: Optional[int],
    catalog: Dict[int, str],
) -> Resolution:
    """Look up scene_name in catalog. Returns the current index + status."""
    if not scene_name:
        # Imported from an old MIDI cue list: only the index hint is available.
        if hint_index is not None and hint_index in catalog:
            return Resolution(status="RECOVERED", index=hint_index)
        return Resolution(status="EMPTY", index=hint_index)

    # Exact match (case-sensitive — scene names ARE case sensitive on LV1)
    for idx, name in catalog.items():
        if name == scene_name:
            if hint_index == idx:
                return Resolution(status="OK", index=idx)
            return Resolution(status="RECOVERED", index=idx)

    # Case-insensitive exact match — treat as RECOVERED (the name drifted)
    lc = scene_name.lower()
    for idx, name in catalog.items():
        if name.lower() == lc:
            return Resolution(status="RECOVERED", index=idx)

    # No match — try a fuzzy suggestion
    names = list(catalog.values())
    matches = difflib.get_close_matches(scene_name, names, n=1, cutoff=FUZZY_CUTOFF)
    if matches:
        sugg_name = matches[0]
        sugg_idx = next((i for i, n in catalog.items() if n == sugg_name), None)
        return Resolution(
            status="MISSING",
            index=None,
            suggestion_name=sugg_name,
            suggestion_index=sugg_idx,
        )
    return Resolution(status="MISSING", index=None)


# --- Bulk validation --------------------------------------------------------


@dataclass
class CueValidation:
    cue_id: int
    cue_label: str
    scene_name: str
    resolution: Resolution


def validate_all(cues: list, catalog: Dict[int, str]) -> List[CueValidation]:
    """Resolve every cue against the catalog. Returns one CueValidation
    per cue (in cue order). Caller can filter by .resolution.status."""
    out: List[CueValidation] = []
    for cue in cues:
        # Duck-typed: works with our Cue dataclass without an import.
        name = getattr(cue, "scene_name", "") or ""
        hint = getattr(cue, "scene_index", None)
        r = resolve(name, hint, catalog)
        out.append(
            CueValidation(
                cue_id=getattr(cue, "id", 0),
                cue_label=getattr(cue, "label", ""),
                scene_name=name,
                resolution=r,
            )
        )
    return out


def issues_only(validations: List[CueValidation]) -> List[CueValidation]:
    """Filter for cues that need user attention (MISSING / EMPTY)."""
    return [v for v in validations if v.resolution.status in ("MISSING", "EMPTY")]
