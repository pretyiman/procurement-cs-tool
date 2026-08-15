from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.main import app
from app.models import ItemMaster, Tender, TenderStatus

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


def _tender_with_one_awarded_item(client, engine) -> int:
    resp = client.post(
        "/tenders", data={"inquiry_no": "Status Test", "tax_percent": "10"}, follow_redirects=False
    )
    tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])

    with Session(engine) as session:
        im = ItemMaster(part_no="X-1", description="Widget", default_unit="Nos")
        session.add(im)
        session.commit()
        session.refresh(im)
        item_master_id = im.id

    client.post(
        f"/tenders/{tender_id}/items",
        data={"item_master_id": str(item_master_id), "qty": "5"},
        follow_redirects=False,
    )
    with Session(engine) as session:
        from app.models import Item

        item_id = session.exec(select(Item).where(Item.tender_id == tender_id)).one().id

    supplier_id = client.post("/suppliers/quick-create", data={"name": "Acme"}).json()["id"]
    client.post(
        f"/tenders/{tender_id}/quote-entry",
        data={"supplier_id": str(supplier_id), f"rate__{item_id}": "10"},
        follow_redirects=False,
    )
    return tender_id


def test_generate_proposal_requires_at_least_one_awarded_item():
    client, engine = _make_client()
    try:
        resp = client.post("/tenders", data={"inquiry_no": "Empty Tender"}, follow_redirects=False)
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])

        resp = client.post(f"/tenders/{tender_id}/generate-proposal")
        assert resp.status_code == 400

        with Session(engine) as session:
            tender = session.get(Tender, tender_id)
            assert tender.status == TenderStatus.draft
    finally:
        app.dependency_overrides.clear()


def test_full_lifecycle_draft_to_proposal_approved_to_awarded():
    client, engine = _make_client()
    try:
        tender_id = _tender_with_one_awarded_item(client, engine)

        with Session(engine) as session:
            assert session.get(Tender, tender_id).status == TenderStatus.draft

        # Can't finalize before generating (let alone approving) the proposal.
        resp = client.post(f"/tenders/{tender_id}/mark-awarded")
        assert resp.status_code == 400

        resp = client.post(f"/tenders/{tender_id}/generate-proposal", follow_redirects=False)
        assert resp.status_code == 303
        with Session(engine) as session:
            assert session.get(Tender, tender_id).status == TenderStatus.proposal_generated

        # Regenerating is allowed while still proposal_generated (the
        # revise-after-rejection cycle) - award-editing isn't locked yet.
        resp = client.post(f"/tenders/{tender_id}/generate-proposal", follow_redirects=False)
        assert resp.status_code == 303

        # Can't finalize before approving.
        resp = client.post(f"/tenders/{tender_id}/mark-awarded")
        assert resp.status_code == 400

        resp = client.post(f"/tenders/{tender_id}/approve-proposal", follow_redirects=False)
        assert resp.status_code == 303
        with Session(engine) as session:
            assert session.get(Tender, tender_id).status == TenderStatus.proposal_approved

        # Once approved, regenerating is no longer allowed.
        resp = client.post(f"/tenders/{tender_id}/generate-proposal")
        assert resp.status_code == 400

        # Can't finalize until every awarded firm has a Contract Award.
        resp = client.post(f"/tenders/{tender_id}/mark-awarded")
        assert resp.status_code == 400

        from app.models import ProposalSnapshotFirmGroup

        with Session(engine) as session:
            group = session.exec(select(ProposalSnapshotFirmGroup)).one()
            supplier_id = group.supplier_id

        resp = client.post(
            f"/tenders/{tender_id}/proposal/contract/{supplier_id}",
            data={"contract_no": "CA-001"},
            follow_redirects=False,
        )
        assert resp.status_code == 200

        resp = client.post(f"/tenders/{tender_id}/mark-awarded", follow_redirects=False)
        assert resp.status_code == 303
        with Session(engine) as session:
            assert session.get(Tender, tender_id).status == TenderStatus.awarded
    finally:
        app.dependency_overrides.clear()
