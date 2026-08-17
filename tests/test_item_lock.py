import datetime

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.main import app
from app.models import Item, ItemMaster, Tender

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

_LOCK_DISABLED_REASON = (
    "_items_locked() in main.py is disabled per user request (2026-08) - "
    "items/quotes/dates are freely editable at any stage for now. The "
    "original restriction is commented out there, not deleted, and these "
    "tests are skipped rather than deleted for the same reason - un-skip "
    "them alongside restoring the commented-out body."
)


def _make_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app), engine


def _item_master(engine) -> int:
    with Session(engine) as session:
        im = ItemMaster(part_no="X-1", description="Widget", default_unit="Nos")
        session.add(im)
        session.commit()
        session.refresh(im)
        return im.id


def test_items_editable_while_draft_and_unpublished():
    client, engine = _make_client()
    try:
        resp = client.post("/tenders", data={"inquiry_no": "Lock Test A"}, follow_redirects=False)
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        item_master_id = _item_master(engine)

        resp = client.post(
            f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with Session(engine) as session:
            assert len(session.exec(select(Item).where(Item.tender_id == tender_id)).all()) == 1
    finally:
        app.dependency_overrides.clear()


def test_items_editable_when_issue_date_is_in_the_future():
    client, engine = _make_client()
    try:
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        resp = client.post(
            "/tenders", data={"inquiry_no": "Lock Test B", "issue_date": tomorrow}, follow_redirects=False
        )
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        item_master_id = _item_master(engine)

        resp = client.post(
            f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    finally:
        app.dependency_overrides.clear()


@pytest.mark.skip(reason=_LOCK_DISABLED_REASON)
def test_items_locked_once_issue_date_has_passed():
    client, engine = _make_client()
    try:
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        resp = client.post(
            "/tenders", data={"inquiry_no": "Lock Test C", "issue_date": yesterday}, follow_redirects=False
        )
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        item_master_id = _item_master(engine)

        resp = client.post(
            f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"}
        )
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


@pytest.mark.skip(reason=_LOCK_DISABLED_REASON)
def test_items_locked_once_status_leaves_draft():
    client, engine = _make_client()
    try:
        resp = client.post("/tenders", data={"inquiry_no": "Lock Test D"}, follow_redirects=False)
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        item_master_id = _item_master(engine)
        client.post(f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"})

        with Session(engine) as session:
            item_id = session.exec(select(Item).where(Item.tender_id == tender_id)).one().id

        supplier_id = client.post("/suppliers/quick-create", data={"name": "Acme"}).json()["id"]
        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(supplier_id), f"rate__{item_id}": "10"},
        )
        client.post(f"/tenders/{tender_id}/generate-proposal")

        # Status is now proposal_generated - items lock even without a
        # published issue_date, since the RFQ has moved past drafting.
        second_item_master_id = _item_master2(engine)
        resp = client.post(
            f"/tenders/{tender_id}/items", data={"item_master_id": str(second_item_master_id), "qty": "1"}
        )
        assert resp.status_code == 400

        resp = client.post(f"/tenders/{tender_id}/items/{item_id}/delete")
        assert resp.status_code == 400

        resp = client.post(f"/tenders/{tender_id}/items/save-quantities", data={f"qty__{item_id}": "99"})
        assert resp.status_code == 400
        with Session(engine) as session:
            assert session.get(Item, item_id).qty == 5  # unchanged
    finally:
        app.dependency_overrides.clear()


def _item_master2(engine) -> int:
    with Session(engine) as session:
        im = ItemMaster(part_no="X-2", description="Gadget", default_unit="Nos")
        session.add(im)
        session.commit()
        session.refresh(im)
        return im.id


def test_blank_issue_date_never_locks_by_date():
    client, engine = _make_client()
    try:
        resp = client.post("/tenders", data={"inquiry_no": "Lock Test E"}, follow_redirects=False)
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        with Session(engine) as session:
            tender = session.get(Tender, tender_id)
            assert tender.issue_date is None
        item_master_id = _item_master(engine)

        resp = client.post(
            f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    finally:
        app.dependency_overrides.clear()


@pytest.mark.skip(reason=_LOCK_DISABLED_REASON)
def test_tender_detail_page_shows_lock_banner_when_locked():
    client, engine = _make_client()
    try:
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        resp = client.post(
            "/tenders", data={"inquiry_no": "Lock Test F", "issue_date": yesterday}, follow_redirects=False
        )
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])

        resp = client.get(f"/tenders/{tender_id}")
        assert "Items are locked" in resp.text
        assert 'id="add-item-form"' not in resp.text
    finally:
        app.dependency_overrides.clear()
