"""Optional local workspace lock (Settings > Lock, sidebar Lock button).

Explicitly NOT real security - there's no user account system in this app
(single local desktop tool, SQLite file on disk), so this is a convenience
passcode to keep the screen from sitting open, not an access-control
system. Matches the design reference's own copy: "This machine's workspace
is locked. Data stays local - no account, no server."

Unlock state is a plain in-memory flag, not a signed cookie/session - it
resets to locked every time the app process restarts (so leaving the app
running but walking away doesn't help an already-unlocked session, but a
fresh launch always re-prompts if a passcode is set). Fine for a
single-process local app; not meant to survive multiple workers.
"""

import hashlib

from sqlmodel import Session, select

from .models import LockSettings

_unlocked_this_session = False


def _hash(passcode: str) -> str:
    return hashlib.sha256(passcode.encode("utf-8")).hexdigest()


def get_lock_settings(session: Session) -> LockSettings:
    settings = session.exec(select(LockSettings).where(LockSettings.id == 1)).first()
    if settings is None:
        settings = LockSettings(id=1)
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings


def is_lock_enabled(session: Session) -> bool:
    return get_lock_settings(session).passcode_hash is not None


def is_unlocked(session: Session) -> bool:
    return not is_lock_enabled(session) or _unlocked_this_session


def try_unlock(session: Session, passcode: str) -> bool:
    global _unlocked_this_session
    settings = get_lock_settings(session)
    if settings.passcode_hash and _hash(passcode) == settings.passcode_hash:
        _unlocked_this_session = True
        return True
    return False


def engage_lock() -> None:
    """Sidebar "Lock" button - re-locks immediately, without touching the
    configured passcode."""
    global _unlocked_this_session
    _unlocked_this_session = False


def set_passcode(session: Session, passcode: str) -> None:
    """Blank/whitespace-only passcode disables the lock entirely. Either
    way, always leaves the current session unlocked afterward - reaching
    this at all means the admin already had access (the lock was off, or
    they'd already unlocked to get to Settings), so there's no reason to
    immediately re-challenge them for a passcode they just typed in
    themselves. The new passcode only takes effect the next time the lock
    is actually engaged (sidebar "Lock" button, or the next app launch)."""
    global _unlocked_this_session
    settings = get_lock_settings(session)
    passcode = passcode.strip()
    settings.passcode_hash = _hash(passcode) if passcode else None
    session.add(settings)
    session.commit()
    _unlocked_this_session = True
