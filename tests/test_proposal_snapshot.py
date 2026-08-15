from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.excel_io import import_tender
from app.models import ContractAward, ProposalSnapshot, ProposalSnapshotFirmGroup, ProposalSnapshotItem, TenderStatus
from app.proposal_snapshot import (
    all_firms_have_contract_award,
    approve_proposal_snapshot,
    get_contract_award,
    get_snapshot,
    save_proposal_snapshot,
    upsert_contract_award,
)

CS_XLSX_PATH = Path(__file__).resolve().parent.parent / "CS.xlsx"


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_save_snapshot_freezes_firm_groups_and_items():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        snapshot = save_proposal_snapshot(session, tender.id)

        assert snapshot.tender_id == tender.id
        assert len(snapshot.firm_groups) == 2  # SNS and Awan won items; Libra won nothing
        names = {g.supplier_name for g in snapshot.firm_groups}
        assert names == {"M/s SNS Enterprises", "M/s Awan Tech"}
        assert snapshot.participating_firms_count == 3  # all 3 quoted, win or not

        for group in snapshot.firm_groups:
            assert len(group.items) > 0
            for item in group.items:
                assert item.description  # frozen text, not a live FK
                assert item.rate > 0


def test_save_snapshot_overwrites_previous_snapshot():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        save_proposal_snapshot(session, tender.id)

        save_proposal_snapshot(session, tender.id)
        # Old snapshot + its child rows are gone, not left behind as orphans
        # or duplicates - exactly one snapshot, with exactly 2 firm groups
        # (SNS + Awan), survives a second generate.
        assert len(session.exec(select(ProposalSnapshot)).all()) == 1
        assert len(session.exec(select(ProposalSnapshotFirmGroup)).all()) == 2


def test_save_snapshot_requires_at_least_one_awarded_item():
    with _fresh_session() as session:
        from app.models import Tender

        tender = Tender(inquiry_no="Empty")
        session.add(tender)
        session.commit()
        session.refresh(tender)

        with pytest.raises(ValueError):
            save_proposal_snapshot(session, tender.id)


def test_cannot_regenerate_once_approved():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        save_proposal_snapshot(session, tender.id)
        approve_proposal_snapshot(session, tender.id)

        with pytest.raises(ValueError):
            save_proposal_snapshot(session, tender.id)


def test_approve_requires_proposal_generated_status():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        # No snapshot generated yet - still draft.
        with pytest.raises(ValueError):
            approve_proposal_snapshot(session, tender.id)


def test_approve_sets_approved_at_and_status():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        save_proposal_snapshot(session, tender.id)
        snapshot = approve_proposal_snapshot(session, tender.id)

        assert snapshot.approved_at is not None
        assert tender.status == TenderStatus.proposal_approved


def test_upsert_contract_award_creates_then_updates():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        snapshot = save_proposal_snapshot(session, tender.id)
        group = snapshot.firm_groups[0]

        award = upsert_contract_award(session, snapshot.id, group.supplier_id, "CA-001")
        assert award.contract_no == "CA-001"

        # Re-submitting (e.g. correcting a typo) updates in place, not a new row.
        updated = upsert_contract_award(session, snapshot.id, group.supplier_id, "CA-001-FIXED")
        assert updated.id == award.id
        assert updated.contract_no == "CA-001-FIXED"
        assert len(session.exec(select(ContractAward)).all()) == 1


def test_upsert_contract_award_rejects_blank_number():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        snapshot = save_proposal_snapshot(session, tender.id)
        group = snapshot.firm_groups[0]

        with pytest.raises(ValueError):
            upsert_contract_award(session, snapshot.id, group.supplier_id, "   ")


def test_all_firms_have_contract_award_requires_every_firm():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        snapshot = save_proposal_snapshot(session, tender.id)
        assert len(snapshot.firm_groups) == 2

        assert all_firms_have_contract_award(session, snapshot.id) is False

        upsert_contract_award(session, snapshot.id, snapshot.firm_groups[0].supplier_id, "CA-001")
        assert all_firms_have_contract_award(session, snapshot.id) is False  # still missing the 2nd firm

        upsert_contract_award(session, snapshot.id, snapshot.firm_groups[1].supplier_id, "CA-002")
        assert all_firms_have_contract_award(session, snapshot.id) is True


def test_get_contract_award_returns_none_when_absent():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        snapshot = save_proposal_snapshot(session, tender.id)
        group = snapshot.firm_groups[0]

        assert get_contract_award(session, snapshot.id, group.supplier_id) is None
        upsert_contract_award(session, snapshot.id, group.supplier_id, "CA-001")
        assert get_contract_award(session, snapshot.id, group.supplier_id).contract_no == "CA-001"


# --- Full HTTP round trip -----------------------------------------------------


def _make_client():
    from app.db import get_session
    from app.main import app

    try:
        from fastapi.testclient import TestClient
    except ImportError:  # pragma: no cover
        TestClient = None

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app), engine


def test_award_editing_locked_once_proposal_approved():
    from app.main import app
    from app.models import Item, ItemMaster

    client, engine = _make_client()
    try:
        with Session(engine) as session:
            im = ItemMaster(part_no="X-1", description="Widget", default_unit="Nos")
            session.add(im)
            session.commit()
            item_master_id = im.id

        resp = client.post("/tenders", data={"inquiry_no": "Lock Test"}, follow_redirects=False)
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        client.post(f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"})
        with Session(engine) as session:
            item_id = session.exec(select(Item).where(Item.tender_id == tender_id)).one().id
        supplier_id = client.post("/suppliers/quick-create", data={"name": "Acme"}).json()["id"]
        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(supplier_id), f"rate__{item_id}": "10"},
        )

        # Award-editing works fine pre-approval.
        resp = client.post(
            f"/tenders/{tender_id}/items/{item_id}/award",
            data={"awarded_supplier_id": str(supplier_id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        client.post(f"/tenders/{tender_id}/generate-proposal")
        client.post(f"/tenders/{tender_id}/approve-proposal")

        # Locked now.
        resp = client.post(
            f"/tenders/{tender_id}/items/{item_id}/award",
            data={"awarded_supplier_id": str(supplier_id)},
        )
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_ca_download_requires_approved_proposal():
    from app.main import app
    from app.models import Item, ItemMaster

    client, engine = _make_client()
    try:
        with Session(engine) as session:
            im = ItemMaster(part_no="X-1", description="Widget", default_unit="Nos")
            session.add(im)
            session.commit()
            item_master_id = im.id

        resp = client.post("/tenders", data={"inquiry_no": "CA Gate Test"}, follow_redirects=False)
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        client.post(f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"})
        with Session(engine) as session:
            item_id = session.exec(select(Item).where(Item.tender_id == tender_id)).one().id
        supplier_id = client.post("/suppliers/quick-create", data={"name": "Acme"}).json()["id"]
        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(supplier_id), f"rate__{item_id}": "10"},
        )

        # Draft: CA download blocked.
        resp = client.post(f"/tenders/{tender_id}/proposal/contract/{supplier_id}", data={"contract_no": "C-1"})
        assert resp.status_code == 400

        client.post(f"/tenders/{tender_id}/generate-proposal")
        # proposal_generated: still blocked (not approved yet).
        resp = client.post(f"/tenders/{tender_id}/proposal/contract/{supplier_id}", data={"contract_no": "C-1"})
        assert resp.status_code == 400

        client.post(f"/tenders/{tender_id}/approve-proposal")
        # proposal_approved: now allowed.
        resp = client.post(f"/tenders/{tender_id}/proposal/contract/{supplier_id}", data={"contract_no": "C-1"})
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_contract_no_locks_once_tender_is_awarded():
    """Before finalizing, resubmitting a corrected number updates it in
    place. Once the tender is fully awarded, the submitted contract_no is
    ignored entirely - the persisted one always wins, so an already-issued
    number can't be silently overwritten with no audit trail."""
    from app.main import app
    from app.models import Item, ItemMaster

    client, engine = _make_client()
    try:
        with Session(engine) as session:
            im = ItemMaster(part_no="X-1", description="Widget", default_unit="Nos")
            session.add(im)
            session.commit()
            item_master_id = im.id

        resp = client.post("/tenders", data={"inquiry_no": "CA Lock Test"}, follow_redirects=False)
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        client.post(f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "5"})
        with Session(engine) as session:
            item_id = session.exec(select(Item).where(Item.tender_id == tender_id)).one().id
        supplier_id = client.post("/suppliers/quick-create", data={"name": "Acme"}).json()["id"]
        client.post(
            f"/tenders/{tender_id}/quote-entry", data={"supplier_id": str(supplier_id), f"rate__{item_id}": "10"}
        )
        client.post(f"/tenders/{tender_id}/generate-proposal")
        client.post(f"/tenders/{tender_id}/approve-proposal")

        resp = client.post(f"/tenders/{tender_id}/proposal/contract/{supplier_id}", data={"contract_no": "C-1"})
        assert resp.status_code == 200

        # Still correctable pre-finalize.
        resp = client.post(f"/tenders/{tender_id}/proposal/contract/{supplier_id}", data={"contract_no": "C-1-FIXED"})
        assert resp.status_code == 200
        with Session(engine) as session:
            snapshot = get_snapshot(session, tender_id)
            assert get_contract_award(session, snapshot.id, supplier_id).contract_no == "C-1-FIXED"

        client.post(f"/tenders/{tender_id}/mark-awarded")

        # Post-finalize: a different submitted number is ignored - the
        # persisted one is what actually gets used/kept.
        resp = client.post(
            f"/tenders/{tender_id}/proposal/contract/{supplier_id}", data={"contract_no": "SNEAKY-CHANGE"}
        )
        assert resp.status_code == 200
        with Session(engine) as session:
            snapshot = get_snapshot(session, tender_id)
            award = get_contract_award(session, snapshot.id, supplier_id)
            assert award.contract_no == "C-1-FIXED"
            assert len(session.exec(select(ContractAward)).all()) == 1

        resp = client.get(f"/tenders/{tender_id}/contract-award")
        assert "C-1-FIXED" in resp.text
        assert "locked now that this RFQ is finalized" in resp.text
        assert 'name="contract_no" required' not in resp.text
    finally:
        app.dependency_overrides.clear()
