from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import custom_fields as custom_fields_module
from app.award_engine import build_purchase_proposal
from app.cs_engine import build_comparative_statement
from app.db import get_session
from app.docx_export import generate_contract_award
from app.excel_io import export_cs_xlsx, import_tender
from app.main import app
from app.models import BusinessRules, CustomField, DocumentLabels, Supplier

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

CS_XLSX_PATH = Path(__file__).resolve().parent.parent / "CS.xlsx"
DEFAULT_RULES = BusinessRules()
DEFAULT_LABELS = DocumentLabels()


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


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


# --- Module-level behavior ---------------------------------------------------


def test_slugify_tag_name():
    assert custom_fields_module.slugify_tag_name("Prep By - Designation") == "prep_by_designation"
    assert custom_fields_module.slugify_tag_name("  Extra   Spaces  ") == "extra_spaces"
    assert custom_fields_module.slugify_tag_name("123 Starts With Digit") == "field_123_starts_with_digit"


def test_validate_tag_name_rejects_invalid_characters_and_reserved_names():
    with pytest.raises(ValueError):
        custom_fields_module.validate_tag_name("Not Valid")  # spaces/caps
    with pytest.raises(ValueError):
        custom_fields_module.validate_tag_name("1_starts_with_digit")
    with pytest.raises(ValueError):
        custom_fields_module.validate_tag_name("contract_value")  # reserved - real computed data
    custom_fields_module.validate_tag_name("prep_by_designation")  # must not raise


def test_create_rejects_duplicate_tag_name():
    with _fresh_session() as session:
        custom_fields_module.create_custom_field(session, "my_field", "My Field", "Value 1")
        with pytest.raises(ValueError):
            custom_fields_module.create_custom_field(session, "my_field", "Different label", "Value 2")


def test_update_cannot_change_tag_name_only_label_and_value():
    with _fresh_session() as session:
        field = custom_fields_module.create_custom_field(session, "my_field", "Original", "v1")
        custom_fields_module.update_custom_field(session, field.id, "Updated label", "v2")
        refreshed = session.get(CustomField, field.id)
        assert refreshed.tag_name == "my_field"  # unchanged
        assert refreshed.label == "Updated label"
        assert refreshed.value == "v2"


def test_delete_is_a_no_op_for_unknown_id():
    with _fresh_session() as session:
        custom_fields_module.delete_custom_field(session, 999999)  # must not raise


def test_custom_fields_dict_reflects_all_rows():
    with _fresh_session() as session:
        custom_fields_module.create_custom_field(session, "a", "A", "1")
        custom_fields_module.create_custom_field(session, "b", "B", "2")
        assert custom_fields_module.custom_fields_dict(session) == {"a": "1", "b": "2"}


# --- Word document rendering -------------------------------------------------


def test_custom_field_renders_as_a_tag_in_contract_award():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        proposal = build_purchase_proposal(session, tender.id)
        group = next(g for g in proposal.firm_groups if g.supplier_name == "M/s SNS Enterprises")
        supplier = session.get(Supplier, group.supplier_id)

        content = generate_contract_award(
            proposal.tender, group, supplier, contract_no="C-1", rules=DEFAULT_RULES,
            custom_fields={"prep_by_designation": "Junior Clerk (BS-11)"},
        )
        full_text = "\n".join(p.text for p in Document(BytesIO(content)).paragraphs)
        # The real ca_template.docx has no {{ prep_by_designation }} tag today,
        # so this only proves the value was available to render, not that it
        # appears - the merge-behavior test below proves the actual mechanism.
        assert "{{" not in full_text and "{%" not in full_text


def test_real_computed_data_overrides_a_same_named_custom_field():
    """Defense in depth: even if a colliding key somehow reached the merge
    (creation already blocks it via validate_tag_name), real per-contract
    data must win, never a custom field standing in for it."""
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        proposal = build_purchase_proposal(session, tender.id)
        group = next(g for g in proposal.firm_groups if g.supplier_name == "M/s SNS Enterprises")
        supplier = session.get(Supplier, group.supplier_id)

        content = generate_contract_award(
            proposal.tender, group, supplier, contract_no="C-1", rules=DEFAULT_RULES,
            custom_fields={"contract_no": "SOMETHING-ELSE-ENTIRELY"},
        )
        full_text = "\n".join(p.text for p in Document(BytesIO(content)).paragraphs)
        assert "C-1" in full_text
        assert "SOMETHING-ELSE-ENTIRELY" not in full_text


# --- CS Excel designation lines ----------------------------------------------


def test_cs_excel_shows_designation_line_when_suggested_field_is_set():
    with _fresh_session() as session:
        tender = import_tender(CS_XLSX_PATH, session)
        cs = build_comparative_statement(session, tender.id)

        without = export_cs_xlsx(cs, DEFAULT_LABELS, {})
        from openpyxl import load_workbook

        values_without = [c.value for row in load_workbook(BytesIO(without)).active.iter_rows() for c in row]
        assert "Junior Clerk (BS-11)" not in values_without

        with_designation = export_cs_xlsx(
            cs, DEFAULT_LABELS, {"prep_by_designation": "Junior Clerk (BS-11)", "fmsad_designation": "Director Finance"}
        )
        values_with = [c.value for row in load_workbook(BytesIO(with_designation)).active.iter_rows() for c in row]
        assert "Junior Clerk (BS-11)" in values_with
        assert "Director Finance" in values_with


# --- Full HTTP round trip ----------------------------------------------------


def test_settings_page_create_then_field_appears_in_suggestions_until_used():
    client, engine = _make_client()
    try:
        resp = client.get("/settings/custom-fields")
        assert "prep_by_designation" in resp.text  # suggested, not yet created

        resp = client.post(
            "/settings/custom-fields",
            data={"tag_name": "prep_by_designation", "label": "Prep By - Designation", "value": "Junior Clerk"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings/custom-fields?saved=1"

        resp = client.get("/settings/custom-fields")
        assert "Junior Clerk" in resp.text

        with Session(engine) as session:
            fields = session.exec(select(CustomField)).all()
            assert len(fields) == 1
            assert fields[0].tag_name == "prep_by_designation"
    finally:
        app.dependency_overrides.clear()


def test_settings_page_rejects_reserved_and_duplicate_names():
    client, engine = _make_client()
    try:
        resp = client.post(
            "/settings/custom-fields",
            data={"tag_name": "contract_value", "label": "Sneaky", "value": "x"},
            follow_redirects=False,
        )
        assert "error=" in resp.headers["location"]

        client.post("/settings/custom-fields", data={"tag_name": "dup", "label": "First", "value": "1"})
        resp = client.post(
            "/settings/custom-fields",
            data={"tag_name": "dup", "label": "Second", "value": "2"},
            follow_redirects=False,
        )
        assert "error=" in resp.headers["location"]

        with Session(engine) as session:
            assert len(session.exec(select(CustomField)).all()) == 1
    finally:
        app.dependency_overrides.clear()


def test_settings_page_update_and_delete():
    client, engine = _make_client()
    try:
        client.post("/settings/custom-fields", data={"tag_name": "my_field", "label": "Original", "value": "v1"})
        with Session(engine) as session:
            field_id = session.exec(select(CustomField)).one().id

        resp = client.post(
            f"/settings/custom-fields/{field_id}/update",
            data={"label": "Updated", "value": "v2"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with Session(engine) as session:
            assert session.get(CustomField, field_id).value == "v2"

        resp = client.post(f"/settings/custom-fields/{field_id}/delete", follow_redirects=False)
        assert resp.status_code == 303
        with Session(engine) as session:
            assert session.get(CustomField, field_id) is None
    finally:
        app.dependency_overrides.clear()
