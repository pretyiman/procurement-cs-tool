"""Contract Award Draft generation (Phase 9).

One Word document per winning firm, rendered from
app/docx_templates/contract_template.docx via docxtpl. The template is a
real .docx with Jinja placeholders across five sections (cover, item
schedule, terms & conditions, security of contract, signatures) - it's
meant to be opened and edited directly in Word by non-technical staff
(legal/security review their sections' wording there), not maintained as
code. This module only supplies the data context; it never hardcodes
contract wording.
"""

import datetime
import html
from io import BytesIO
from pathlib import Path

from docxtpl import DocxTemplate

from .award_engine import ProposalFirmGroup
from .models import Supplier, Tender

TEMPLATE_PATH = Path(__file__).resolve().parent / "docx_templates" / "contract_template.docx"


def _esc(value) -> str:
    """docxtpl's XML patching does an 'unescape html entities' pass on the
    rendered output, which means it expects substituted text to already be
    HTML/XML-escaped going in - an un-escaped "&" (e.g. in a firm name like
    "M/s Zafar & Sons") otherwise produces malformed intermediate XML and
    silently corrupts nearby text elsewhere in the document, not just the
    offending value. Escape every free-text field before it reaches the
    template context."""
    return html.escape(str(value)) if value else "-"


def _context(tender: Tender, group: ProposalFirmGroup, supplier: Supplier) -> dict:
    return {
        "tender_inquiry_no": _esc(tender.inquiry_no),
        "date": datetime.date.today().strftime("%d-%b-%Y"),
        "firm_name": _esc(group.supplier_name),
        "firm_address": _esc(supplier.address),
        "firm_contact_person": _esc(supplier.contact_person),
        "firm_phone": _esc(supplier.phone),
        "firm_email": _esc(supplier.email),
        "firm_tax_no": _esc(supplier.tax_no),
        "gst_percent": tender.gst_percent,
        "items": [
            {
                "ser": ai.item.ser,
                "part_no": _esc(ai.item.item_master.part_no),
                "description": _esc(ai.item.item_master.description),
                "unit": _esc(ai.item.item_master.default_unit),
                "qty": ai.item.qty,
                "rate": f"{ai.awarded_rate:.2f}",
                "total_value": f"{ai.total_value:.2f}",
            }
            for ai in group.items
        ],
        "store_value": f"{group.store_value:.2f}",
        "gst_amount": f"{group.gst_amount:.2f}",
        "contract_value": f"{group.contract_value:.2f}",
    }


def generate_contract_draft(tender: Tender, group: ProposalFirmGroup, supplier: Supplier) -> bytes:
    doc = DocxTemplate(str(TEMPLATE_PATH))
    doc.render(_context(tender, group, supplier))
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
