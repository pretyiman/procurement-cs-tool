from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.award_engine import build_purchase_proposal
from app.docx_export import generate_contract_draft
from app.excel_io import import_tender
from app.models import Supplier

CS_XLSX_PATH = Path(__file__).resolve().parent.parent / "CS.xlsx"


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_contract_draft_item_schedule_matches_proposal_exactly():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        proposal = build_purchase_proposal(session, tender.id)
        group = next(g for g in proposal.firm_groups if g.supplier_name == "M/s SNS Enterprises")
        supplier = session.get(Supplier, group.supplier_id)

        content = generate_contract_draft(proposal.tender, group, supplier)
        doc = Document(BytesIO(content))

        table = doc.tables[0]
        # header row + one row per awarded item, no leftover {%tr%} marker rows
        assert len(table.rows) == 1 + len(group.items)

        rendered_rows = [tuple(c.text for c in row.cells) for row in table.rows[1:]]
        expected_rows = [
            (
                str(ai.item.ser),
                ai.item.item_master.part_no,
                ai.item.item_master.description,
                ai.item.item_master.default_unit,
                str(ai.item.qty),
                f"{ai.awarded_rate:.2f}",
                f"{ai.total_value:.2f}",
            )
            for ai in group.items
        ]
        assert rendered_rows == expected_rows

        full_text = "\n".join(p.text for p in doc.paragraphs)
        assert f"{group.store_value:.2f}" in full_text
        assert f"{group.contract_value:.2f}" in full_text
        assert "{{" not in full_text and "{%" not in full_text


def test_ampersand_in_firm_name_survives_rendering():
    """Regression test: docxtpl's XML patching does an 'unescape html
    entities' pass, so un-escaped '&' in context values (e.g. a firm name
    like "M/s X & Sons") corrupts not just that value but nearby XML too.
    See docx_export._esc()."""
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        proposal = build_purchase_proposal(session, tender.id)
        group = proposal.firm_groups[0]
        supplier = session.get(Supplier, group.supplier_id)
        supplier.name = "M/s Test & Sons"
        group.supplier_name = "M/s Test & Sons"

        content = generate_contract_draft(proposal.tender, group, supplier)
        doc = Document(BytesIO(content))
        full_text = "\n".join(p.text for p in doc.paragraphs)

        assert "M/s Test & Sons" in full_text
        assert "Terms & Conditions" in full_text  # static heading, unrelated to the value above
        assert "{{" not in full_text and "{%" not in full_text
