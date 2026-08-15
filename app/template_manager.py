"""Settings > Document Templates: lets an admin download the current
pp_template.docx/ca_template.docx, edit it in Word, and upload it back -
without needing filesystem access to the app's install directory. See
CLAUDE.md "Frozen tech decisions" (docxtpl) and paths.docx_template_path()
for why uploads are stored under user_data_dir(), not next to the
bundled defaults.
"""

import datetime
import tempfile
from pathlib import Path

from .docx_export import generate_contract_award, generate_purchase_proposal_doc
from .models import (
    BusinessRules,
    Department,
    ProposalSnapshot,
    ProposalSnapshotFirmGroup,
    ProposalSnapshotItem,
    Supplier,
    Tender,
)
from .paths import custom_docx_templates_dir, docx_template_path

TEMPLATE_NAMES = {
    "ca_template.docx": "Contract Award",
    "pp_template.docx": "Purchase Proposal",
}

WD_FORMAT_DOCX = 16  # wdFormatXMLDocument


def convert_doc_to_docx(content: bytes) -> bytes:
    """.doc (the old binary Word format) and .docx (a zip of XML) are
    completely different file formats - docxtpl/python-docx can only ever
    read .docx. There's no reliable pure-Python .doc->.docx converter that
    preserves formatting, so this drives an actual installed Word via COM
    automation (the same approach used once already in this project, to
    build ca_template.docx/pp_template.docx from the user's original
    CA.doc/PP.doc). Raises ValueError with a friendly message if Word
    isn't available - callers should let the admin save as .docx manually
    in that case, not treat it as a hard blocker."""
    try:
        import pythoncom
        import win32com.client
    except ImportError as e:
        raise ValueError(
            "Converting a .doc file requires Microsoft Word to be installed on this "
            "computer. Please open the file in Word, use File > Save As > Word Document "
            "(.docx), and upload the .docx file instead."
        ) from e

    with tempfile.TemporaryDirectory() as tmp_dir:
        doc_path = Path(tmp_dir) / "upload.doc"
        docx_path = Path(tmp_dir) / "upload.docx"
        doc_path.write_bytes(content)

        pythoncom.CoInitialize()
        word = None
        document = None
        try:
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            document = word.Documents.Open(str(doc_path))
            document.SaveAs2(str(docx_path), FileFormat=WD_FORMAT_DOCX)
        except Exception as e:
            raise ValueError(
                f"Could not convert this .doc file using Word: {e}. Try saving it as "
                ".docx manually in Word instead."
            ) from e
        finally:
            if document is not None:
                document.Close(False)
            if word is not None:
                word.Quit()
            pythoncom.CoUninitialize()

        if not docx_path.exists():
            raise ValueError("Word did not produce a .docx file from this upload.")
        return docx_path.read_bytes()


def _require_known_template(name: str) -> None:
    """Every route touching a template name takes it from the URL - this
    is the one allowlist check standing between that and an arbitrary
    filesystem path, so every call site must go through it."""
    if name not in TEMPLATE_NAMES:
        raise ValueError(f"Unknown template {name!r}")


def list_templates() -> list:
    rows = []
    for name, label in TEMPLATE_NAMES.items():
        custom_path = custom_docx_templates_dir() / name
        active_path = docx_template_path(name)
        is_custom = custom_path.exists()
        rows.append(
            {
                "name": name,
                "label": label,
                "is_custom": is_custom,
                "modified": datetime.datetime.fromtimestamp(active_path.stat().st_mtime),
            }
        )
    return rows


def read_active_template(name: str) -> bytes:
    _require_known_template(name)
    return docx_template_path(name).read_bytes()


def save_custom_template(name: str, content: bytes) -> None:
    _require_known_template(name)
    d = custom_docx_templates_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(content)


def restore_default_template(name: str) -> None:
    _require_known_template(name)
    custom_path = custom_docx_templates_dir() / name
    if custom_path.exists():
        custom_path.unlink()


def _dummy_ca_args():
    department = Department(id=1, name="Sample Department")
    tender = Tender(
        id=1,
        inquiry_no="SAMPLE/2026/001",
        indent_no="SAMPLE-IND-001",
        department=department,
        issue_date=datetime.date.today(),
        opening_date=datetime.date.today(),
        delivery_days=60,
        warranty_months=3,
        tax_percent=18.0,
    )
    item = ProposalSnapshotItem(
        id=1,
        firm_group_id=1,
        ser=1,
        part_no="X-1",
        description="Sample Item",
        unit="Nos",
        qty=10,
        rate=100.0,
        total_value=1000.0,
        is_override=False,
        override_reason=None,
    )
    group = ProposalSnapshotFirmGroup(
        id=1,
        snapshot_id=1,
        supplier_id=1,
        supplier_name="M/s Sample Firm",
        store_value=1000.0,
        tax_amount=180.0,
        contract_value=1180.0,
    )
    group.items = [item]
    supplier = Supplier(id=1, name="M/s Sample Firm", address="Sample Address")
    rules = BusinessRules()
    return tender, group, supplier, rules


def _dummy_pp_args():
    tender, group, _supplier, _rules = _dummy_ca_args()
    snapshot = ProposalSnapshot(
        id=1,
        tender_id=1,
        generated_at=datetime.datetime.utcnow(),
        indent_no="SAMPLE-IND-001",
        department_name="Sample Department",
        firms_invited_count=3,
        issue_date=tender.issue_date,
        opening_date=tender.opening_date,
        delivery_days=60,
        warranty_months=3,
        tax_type="GST",
        tax_percent=18.0,
        participating_firms_count=1,
        total_item_count=1,
        grand_item_count=1,
        grand_store_value=1000.0,
        grand_tax_amount=180.0,
        grand_contract_value=1180.0,
    )
    snapshot.firm_groups = [group]
    suppliers_by_id = {1: Supplier(id=1, name="M/s Sample Firm", address="Sample Address")}
    return tender, snapshot, suppliers_by_id


def validate_template(name: str, content: bytes) -> None:
    """Renders the uploaded template against synthetic sample data - the
    exact same generate_* functions used for real documents, so this stays
    in sync automatically if the context they build ever changes. Raises
    ValueError with a human-readable message on any failure (corrupt
    file, broken {{ }}/{%tr %} tag, etc.) instead of letting an admin
    silently break every future Contract Award / Purchase Proposal."""
    _require_known_template(name)
    try:
        if name == "ca_template.docx":
            tender, group, supplier, rules = _dummy_ca_args()
            generate_contract_award(
                tender, group, supplier, contract_no="SAMPLE-001", rules=rules, template_bytes=content
            )
        else:
            tender, snapshot, suppliers_by_id = _dummy_pp_args()
            generate_purchase_proposal_doc(tender, snapshot, suppliers_by_id, template_bytes=content)
    except Exception as e:  # noqa: BLE001 - surfaced verbatim to the admin, any failure reason is relevant
        raise ValueError(f"Could not use this file as a {TEMPLATE_NAMES[name]} template: {e}") from e
