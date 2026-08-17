from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.lock as lock_module
from app.db import get_session
from app.main import app
from app.models import LockSettings

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


def _make_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    lock_module._unlocked_this_session = False  # each test starts with a clean in-memory unlock flag
    return TestClient(app), engine


def test_app_is_never_gated_when_no_passcode_is_set():
    """Default behavior (no admin has ever visited Settings > Lock) -
    every route works exactly as before, no redirect to /lock anywhere."""
    client, engine = _make_client()
    try:
        for path in ("/", "/tenders", "/items", "/insights", "/settings/business-rules"):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 200, f"{path} was unexpectedly gated"
    finally:
        app.dependency_overrides.clear()


def test_setting_a_passcode_gates_every_route_until_unlocked():
    client, engine = _make_client()
    try:
        resp = client.post("/settings/lock", data={"passcode": "1234"}, follow_redirects=False)
        assert resp.status_code == 303

        # Locking is only actually engaged once the sidebar/POST /lock/engage
        # fires (setting a passcode doesn't itself lock the session you're
        # already in) - simulate that explicitly here.
        lock_module.engage_lock()

        resp = client.get("/tenders", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/lock")

        resp = client.get("/lock")
        assert resp.status_code == 200
        assert "Local passcode" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_wrong_passcode_is_rejected_correct_passcode_unlocks():
    client, engine = _make_client()
    try:
        client.post("/settings/lock", data={"passcode": "1234"})
        lock_module.engage_lock()

        resp = client.post("/lock/unlock", data={"passcode": "wrong", "next": "/"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "error=1" in resp.headers["location"]

        # Still locked.
        resp = client.get("/tenders", follow_redirects=False)
        assert resp.status_code == 303

        resp = client.post("/lock/unlock", data={"passcode": "1234", "next": "/tenders"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/tenders"

        # Now unlocked.
        resp = client.get("/tenders", follow_redirects=False)
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_sidebar_lock_button_relocks_without_touching_the_passcode():
    client, engine = _make_client()
    try:
        client.post("/settings/lock", data={"passcode": "1234"})
        client.post("/lock/unlock", data={"passcode": "1234", "next": "/"})
        resp = client.get("/tenders", follow_redirects=False)
        assert resp.status_code == 200

        resp = client.post("/lock/engage", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/lock"

        resp = client.get("/tenders", follow_redirects=False)
        assert resp.status_code == 303

        # The same passcode still works - engage_lock didn't clear it.
        resp = client.post("/lock/unlock", data={"passcode": "1234", "next": "/"}, follow_redirects=False)
        assert resp.headers["location"] == "/"
    finally:
        app.dependency_overrides.clear()


def test_blank_save_does_not_accidentally_disable_an_existing_passcode():
    """A real bug this guards against: the Settings > Lock page's "Save"
    button submits an empty passcode field whenever the admin just wanted
    to leave it unchanged - that must not silently turn the lock off."""
    client, engine = _make_client()
    try:
        client.post("/settings/lock", data={"passcode": "1234"})
        with Session(engine) as session:
            assert session.exec(select(LockSettings).where(LockSettings.id == 1)).one().passcode_hash is not None

        client.post("/settings/lock", data={"passcode": ""})
        with Session(engine) as session:
            assert session.exec(select(LockSettings).where(LockSettings.id == 1)).one().passcode_hash is not None

        client.post("/settings/lock", data={"passcode": "", "clear": "1"})
        with Session(engine) as session:
            assert session.exec(select(LockSettings).where(LockSettings.id == 1)).one().passcode_hash is None
    finally:
        app.dependency_overrides.clear()


def test_lock_and_unlock_routes_are_always_reachable_even_while_locked():
    client, engine = _make_client()
    try:
        client.post("/settings/lock", data={"passcode": "1234"})
        lock_module.engage_lock()

        resp = client.get("/lock", follow_redirects=False)
        assert resp.status_code == 200
        resp = client.post("/lock/unlock", data={"passcode": "wrong", "next": "/"}, follow_redirects=False)
        assert resp.status_code == 303  # redirects back to /lock, not gated into a loop
    finally:
        app.dependency_overrides.clear()
