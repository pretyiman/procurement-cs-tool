import datetime

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.main import app
from app.models import Item, ItemMaster

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
    return TestClient(app), engine


def _item_master(engine, suffix="1") -> int:
    with Session(engine) as session:
        im = ItemMaster(part_no=f"X-{suffix}", description=f"Widget {suffix}", default_unit="Nos")
        session.add(im)
        session.commit()
        session.refresh(im)
        return im.id


def _create_tender(client, inquiry_no, **kwargs):
    data = {"inquiry_no": inquiry_no, **kwargs}
    resp = client.post("/tenders", data=data, follow_redirects=False)
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[-1])


def test_draft_unpublished_lands_on_items():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client, "Landing A")
        resp = client.get("/tenders")
        assert f'href="/tenders/{tender_id}"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_draft_published_no_opening_lands_on_quote_entry():
    client, engine = _make_client()
    try:
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        tender_id = _create_tender(client, "Landing B", issue_date=yesterday)
        resp = client.get("/tenders")
        assert f'href="/tenders/{tender_id}/quote-entry"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_draft_opening_passed_lands_on_comparative_summary():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(
            client, "Landing C",
            issue_date=(datetime.date.today() - datetime.timedelta(days=10)).isoformat(),
            opening_date=(datetime.date.today() - datetime.timedelta(days=1)).isoformat(),
        )
        resp = client.get("/tenders")
        assert f'href="/tenders/{tender_id}/comparative-summary"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_draft_every_item_resolved_lands_on_proposal():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client, "Landing D")
        item_master_id = _item_master(engine)
        client.post(f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"})
        with Session(engine) as session:
            item_id = session.exec(select(Item).where(Item.tender_id == tender_id)).one().id
        supplier_id = client.post("/suppliers/quick-create", data={"name": "Acme"}).json()["id"]
        client.post(
            f"/tenders/{tender_id}/quote-entry", data={"supplier_id": str(supplier_id), f"rate__{item_id}": "10"}
        )
        # No opening_date set, but the single item is fully resolved (awarded
        # by default to the only bidder) - ready-to-generate outranks the
        # date-only signals.
        resp = client.get("/tenders")
        assert f'href="/tenders/{tender_id}/proposal"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_proposal_generated_lands_on_proposal():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client, "Landing E")
        item_master_id = _item_master(engine)
        client.post(f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"})
        with Session(engine) as session:
            item_id = session.exec(select(Item).where(Item.tender_id == tender_id)).one().id
        supplier_id = client.post("/suppliers/quick-create", data={"name": "Acme"}).json()["id"]
        client.post(
            f"/tenders/{tender_id}/quote-entry", data={"supplier_id": str(supplier_id), f"rate__{item_id}": "10"}
        )
        client.post(f"/tenders/{tender_id}/generate-proposal")

        resp = client.get("/tenders")
        assert f'href="/tenders/{tender_id}/proposal"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_proposal_approved_and_awarded_land_on_contract_award():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client, "Landing F")
        item_master_id = _item_master(engine)
        client.post(f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"})
        with Session(engine) as session:
            item_id = session.exec(select(Item).where(Item.tender_id == tender_id)).one().id
        supplier_id = client.post("/suppliers/quick-create", data={"name": "Acme"}).json()["id"]
        client.post(
            f"/tenders/{tender_id}/quote-entry", data={"supplier_id": str(supplier_id), f"rate__{item_id}": "10"}
        )
        client.post(f"/tenders/{tender_id}/generate-proposal")
        client.post(f"/tenders/{tender_id}/approve-proposal")

        resp = client.get("/tenders")
        assert f'href="/tenders/{tender_id}/contract-award"' in resp.text

        client.post(f"/tenders/{tender_id}/proposal/contract/{supplier_id}", data={"contract_no": "CA-1"})
        client.post(f"/tenders/{tender_id}/mark-awarded")

        resp = client.get("/tenders")
        assert f'href="/tenders/{tender_id}/contract-award"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_dashboard_recent_rfqs_use_phase_landing_too():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(
            client, "Landing G",
            issue_date=(datetime.date.today() - datetime.timedelta(days=1)).isoformat(),
        )
        resp = client.get("/")
        assert resp.status_code == 200
        assert f'href="/tenders/{tender_id}/quote-entry"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_direct_item_page_navigation_still_works_regardless_of_phase():
    """The redirect only changes the *default* landing from a list -
    every phase page is still directly reachable (e.g. via phase nav)."""
    client, engine = _make_client()
    try:
        tender_id = _create_tender(
            client, "Landing H",
            issue_date=(datetime.date.today() - datetime.timedelta(days=10)).isoformat(),
        )
        resp = client.get(f"/tenders/{tender_id}")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()
