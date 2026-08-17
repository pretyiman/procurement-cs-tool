from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.main import app
from app.models import Item, ItemMaster, ProposalSnapshot, ProposalSnapshotFirmGroup

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


def _award_a_tender(client, engine, inquiry_no, rate, lpr=None, second_supplier=False):
    """Drives a tender all the way to `awarded`, with one item quoted by
    one (or two, for a competitive/multi-bidder scenario) supplier(s)."""
    resp = client.post("/tenders", data={"inquiry_no": inquiry_no}, follow_redirects=False)
    tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])

    with Session(engine) as session:
        im = ItemMaster(part_no=f"{inquiry_no}-X", description=f"Widget for {inquiry_no}", default_unit="Nos")
        session.add(im)
        session.commit()
        session.refresh(im)
        item_master_id = im.id

    client.post(
        f"/tenders/{tender_id}/items",
        data={"item_master_id": str(item_master_id), "qty": "5", "lpr": str(lpr) if lpr is not None else ""},
        follow_redirects=False,
    )
    with Session(engine) as session:
        item_id = session.exec(select(Item).where(Item.tender_id == tender_id)).one().id

    supplier_id = client.post("/suppliers/quick-create", data={"name": f"M/s {inquiry_no} Supplier"}).json()["id"]
    client.post(
        f"/tenders/{tender_id}/quote-entry",
        data={"supplier_id": str(supplier_id), f"rate__{item_id}": str(rate)},
        follow_redirects=False,
    )
    if second_supplier:
        supplier2_id = client.post("/suppliers/quick-create", data={"name": f"M/s {inquiry_no} Rival"}).json()["id"]
        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(supplier2_id), f"rate__{item_id}": str(rate + 5)},
            follow_redirects=False,
        )

    client.post(f"/tenders/{tender_id}/generate-proposal", follow_redirects=False)
    client.post(f"/tenders/{tender_id}/approve-proposal", follow_redirects=False)

    with Session(engine) as session:
        snapshot_id = session.exec(select(ProposalSnapshot).where(ProposalSnapshot.tender_id == tender_id)).one().id
        group = session.exec(
            select(ProposalSnapshotFirmGroup).where(ProposalSnapshotFirmGroup.snapshot_id == snapshot_id)
        ).one()
        winning_supplier_id = group.supplier_id
    client.post(
        f"/tenders/{tender_id}/proposal/contract/{winning_supplier_id}",
        data={"contract_no": f"CA-{inquiry_no}"},
        follow_redirects=False,
    )
    client.post(f"/tenders/{tender_id}/mark-awarded", follow_redirects=False)
    return tender_id


def test_insights_empty_when_nothing_awarded_yet():
    client, engine = _make_client()
    try:
        resp = client.get("/insights")
        assert resp.status_code == 200
        assert "No awarded RFQs yet" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_insights_aggregates_across_awarded_tenders():
    client, engine = _make_client()
    try:
        _award_a_tender(client, engine, "INS-A", rate=100, lpr=120, second_supplier=True)
        _award_a_tender(client, engine, "INS-B", rate=50, lpr=None)

        resp = client.get("/insights")
        assert resp.status_code == 200
        text = resp.text

        assert "No awarded RFQs yet" not in text
        # Awarded value: (100*5*1.18) + (50*5*1.18) = 590 + 295 = 885.00
        assert "885.00" in text
        # LPR savings: (120-100)*5 = 100.00 (only INS-A's item has an LPR)
        assert "100.00" in text
        # 2 RFQs total.
        assert "across 2 RFQs" in text
        # INS-A had 2 bidders, INS-B had 1 -> avg 1.5
        assert "1.5" in text
        # INS-B's single item was single-source (only one supplier quoted it).
        assert ">1<" in text or "1</div>" in text or "1\n" in text
    finally:
        app.dependency_overrides.clear()


def test_insights_shows_rate_movement_row_only_when_lpr_present():
    client, engine = _make_client()
    try:
        _award_a_tender(client, engine, "INS-C", rate=90, lpr=100)

        resp = client.get("/insights")
        assert resp.status_code == 200
        assert "No items with a recorded Last Purchase Rate yet." not in resp.text
        assert "Widget for INS-C" in resp.text
        assert "-10.0%" in resp.text
    finally:
        app.dependency_overrides.clear()
