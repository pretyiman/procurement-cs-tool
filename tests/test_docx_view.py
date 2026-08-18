import datetime
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.docx_export import generate_contract_award
from app.docx_view import docx_bytes_to_html
from app.excel_io import import_tender
from app.main import app
from app.models import BusinessRules, Supplier
from app.proposal_snapshot import approve_proposal_snapshot, save_proposal_snapshot, upsert_contract_award

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

CS_XLSX_PATH = Path(__file__).resolve().parent.parent / "CS.xlsx"
DEFAULT_RULES = BusinessRules()


def _fresh_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _make_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app), engine


# --- docx_bytes_to_html --------------------------------------------------


def test_docx_bytes_to_html_preserves_dynamic_and_custom_tag_values():
    """The whole point of Approach B: view/print reuses the same rendered
    .docx a Download would produce, so any tag - real per-contract data
    or a custom/department-profile field - that appears in the .docx
    appears in the HTML view too, with no separate template to keep in
    sync."""
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        snapshot = save_proposal_snapshot(session, tender.id)
        group = next(g for g in snapshot.firm_groups if g.supplier_name == "M/s SNS Enterprises")
        supplier = session.get(Supplier, group.supplier_id)
        supplier.address = "42 Test Road"
        session.add(supplier)
        session.commit()

        content = generate_contract_award(
            tender, group, supplier, contract_no="TEST-VIEW-001", rules=DEFAULT_RULES,
            custom_fields={"indentor_name": "Director Procurement SCM"},
        )
        html = docx_bytes_to_html(content)

        assert "TEST-VIEW-001" in html  # real per-contract data
        assert "M/s SNS Enterprises" in html
        # The real ca_template.docx has no {{ indentor_name }} tag today,
        # so this only proves the mechanism carries through, same caveat
        # as test_custom_fields.py's equivalent docx-level test.
        assert "{{" not in html and "{%" not in html


# --- HTTP round trip -------------------------------------------------------


def test_view_pp_document_requires_a_generated_proposal():
    client, engine = _make_client()
    try:
        with Session(engine) as session:
            tender = import_tender(CS_XLSX_PATH, session)
            tender_id = tender.id

        resp = client.get(f"/tenders/{tender_id}/proposal/pp-document/view")
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_view_pp_document_renders_html_once_proposal_generated():
    client, engine = _make_client()
    try:
        with Session(engine) as session:
            tender = import_tender(CS_XLSX_PATH, session)
            tender_id = tender.id
            inquiry_no = tender.inquiry_no
            save_proposal_snapshot(session, tender_id)

        resp = client.get(f"/tenders/{tender_id}/proposal/pp-document/view")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert inquiry_no in resp.text
        assert "window.print()" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_view_contract_draft_requires_an_issued_contract_number():
    client, engine = _make_client()
    try:
        with Session(engine) as session:
            tender = import_tender(CS_XLSX_PATH, session)
            tender_id = tender.id
            snapshot = save_proposal_snapshot(session, tender_id)
            approve_proposal_snapshot(session, tender_id)
            supplier_id = snapshot.firm_groups[0].supplier_id

        resp = client.get(f"/tenders/{tender_id}/proposal/contract/{supplier_id}/view")
        assert resp.status_code == 400  # no ContractAward issued yet
    finally:
        app.dependency_overrides.clear()


def test_view_contract_draft_renders_html_once_contract_no_issued():
    client, engine = _make_client()
    try:
        with Session(engine) as session:
            tender = import_tender(CS_XLSX_PATH, session)
            tender_id = tender.id
            snapshot = save_proposal_snapshot(session, tender_id)
            approve_proposal_snapshot(session, tender_id)
            supplier_id = snapshot.firm_groups[0].supplier_id
            upsert_contract_award(
                session, snapshot.id, supplier_id, contract_no="VIEW-CA-001",
                contract_date=datetime.date(2026, 8, 12),
            )

        resp = client.get(f"/tenders/{tender_id}/proposal/contract/{supplier_id}/view")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "VIEW-CA-001" in resp.text
        assert "window.print()" in resp.text
    finally:
        app.dependency_overrides.clear()
