"""Resource/data-path resolution that works both running from source and
as a PyInstaller-frozen executable (Phase 11).

In dev mode, app data (templates, docx_templates) live under app/ and the
DB lives at the repo root - unchanged from every earlier phase. When
frozen, PyInstaller extracts bundled data under sys._MEIPASS (preserving
the "app/..." prefix used when building, see PLAN.md Phase 11), and the
DB must live somewhere the installed app is actually allowed to write -
not next to a possibly read-only installed executable.
"""

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_path(*parts: str) -> Path:
    """Path to a bundled resource (templates, docx_templates, ...)."""
    if is_frozen():
        base = Path(sys._MEIPASS) / "app"  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


def user_data_dir() -> Path:
    """Writable directory for the SQLite DB. Repo root in dev mode (as
    before); a per-user app-data folder when frozen, so the DB survives
    reinstalls/updates and doesn't need the install location writable."""
    if is_frozen():
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ProcurementCSTool"
    else:
        base = Path(__file__).resolve().parent.parent
    base.mkdir(parents=True, exist_ok=True)
    return base
