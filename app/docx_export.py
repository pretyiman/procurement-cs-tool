"""Purchase Proposal (PP) and Contract Award (CA) document generation
(Phase 12), rendered via docxtpl from app/docx_templates/pp_template.docx
and ca_template.docx. Those templates were built by surgically editing
real Word documents the user supplied (CA.doc/PP.doc, kept local-only -
see CLAUDE.md "Data sensitivity") so all legal/procedural wording is
preserved verbatim; only genuinely per-contract values are Jinja tags.
Non-technical staff can still open and edit the .docx templates directly
in Word - this module only supplies data, never hardcodes document text.

Both functions render from a frozen ProposalSnapshot (app/models.py /
app/proposal_snapshot.py), never from live Item/Quote/catalog data - once
a proposal is generated (and especially once approved), what a firm's
Contract Award or the Purchase Proposal document says must not silently
drift just because someone edited a catalog item's description elsewhere,
or the tender's own award decisions later got locked/unlocked. supplier's
address is the one deliberate exception - looked up live, since a firm's
current mailing address is what you want on a document going out today,
not a historical snapshot of it."""

import datetime
import html
from io import BytesIO
from typing import Optional

from docxtpl import DocxTemplate

from .models import BusinessRules, ProposalSnapshot, ProposalSnapshotFirmGroup, Supplier, Tender
from .number_words import amount_in_words, number_to_words, ordinal
from .paths import docx_template_path


def _esc(value) -> str:
    """docxtpl's XML patching does an 'unescape html entities' pass on the
    rendered output, which means it expects substituted text to already be
    HTML/XML-escaped going in - an un-escaped "&" (e.g. in a firm name like
    "M/s Zafar & Sons") otherwise produces malformed intermediate XML and
    silently corrupts nearby text elsewhere in the document, not just the
    offending value. Escape every free-text field before it reaches the
    template context."""
    return html.escape(str(value)) if value else "-"


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _date_words(d: datetime.date) -> str:
    """12 Aug 2026 -> "12th day of August Two Thousand Twenty Six", the
    sample CA's agreement-date phrasing."""
    return f"{ordinal(d.day)} day of {d.strftime('%B')} {number_to_words(d.year)}"


def generate_contract_award(
    tender: Tender,
    group: ProposalSnapshotFirmGroup,
    supplier: Supplier,
    contract_no: str,
    rules: BusinessRules,
    contract_date: Optional[datetime.date] = None,
    agreement_date: Optional[datetime.date] = None,
    template_bytes: Optional[bytes] = None,
    custom_fields: Optional[dict] = None,
) -> bytes:
    contract_date = contract_date or datetime.date.today()
    agreement_date = agreement_date or datetime.date.today()

    # Both security deposit (bank guarantee) and stamp duty are calculated
    # on store value (the quoted price excl. GST/PST) - not contract value
    # (incl. tax). Only the waiver threshold check uses contract value,
    # since that's the figure procurement staff actually compare against
    # a Rs threshold ("is this contract worth more/less than X").
    store_value = group.store_value
    if group.contract_value < rules.security_deposit_waived_below:
        security_deposit = 0.0
    else:
        security_deposit = store_value * rules.security_deposit_percent / 100
    stamp_duty = store_value * rules.stamp_duty_percent / 100

    context = {
        "firm_name": _esc(group.supplier_name),
        "firm_address": _esc(supplier.address),
        "agreement_date_words": _date_words(agreement_date),
        "indent_no": _esc(tender.indent_no or tender.inquiry_no),
        "indent_date": tender.issue_date.strftime("%d %b %Y") if tender.issue_date else "___",
        "opening_date": tender.opening_date.strftime("%d %b %Y") if tender.opening_date else "___",
        "delivery_days": tender.delivery_days,
        "warranty_months": f"{tender.warranty_months:02d}",
        "contract_no": _esc(contract_no),
        "contract_date": contract_date.strftime("%d %b %Y"),
        "tax_type": tender.tax_type.value,
        "tax_percent": f"{tender.tax_percent:g}",
        "store_value": _money(store_value),
        "tax_amount": _money(group.tax_amount),
        "contract_value": _money(group.contract_value),
        "amount_in_words": amount_in_words(group.contract_value),
        "security_deposit": _money(security_deposit),
        "stamp_duty": _money(stamp_duty),
        "items": [
            {
                "ser": item.ser,
                "part_no": _esc(item.part_no),
                "description": _esc(item.description),
                "unit": _esc(item.unit),
                "qty": item.qty,
                "rate": _money(item.rate),
                "total_value": _money(item.total_value),
            }
            for item in group.items
        ],
    }

    # Custom fields (Settings > Custom Fields) are merged in first, real
    # data second, so real per-contract data always wins even if a custom
    # field somehow shares a name with it (also blocked at creation time -
    # see custom_fields.RESERVED_TAG_NAMES).
    full_context = {**(custom_fields or {}), **context}

    doc = DocxTemplate(BytesIO(template_bytes)) if template_bytes is not None else DocxTemplate(
        str(docx_template_path("ca_template.docx"))
    )
    doc.render(full_context)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def generate_purchase_proposal_doc(
    tender: Tender,
    snapshot: ProposalSnapshot,
    suppliers_by_id: dict,
    template_bytes: Optional[bytes] = None,
    custom_fields: Optional[dict] = None,
) -> bytes:
    est_cost = sum(
        item.qty * item.lpr
        for group in snapshot.firm_groups
        for item in group.items
        if item.lpr is not None
    )
    offered = snapshot.grand_contract_value
    inc_dec_pct = ((offered - est_cost) / est_cost * 100) if est_cost else None

    context = {
        "tender_inquiry_no": _esc(tender.inquiry_no),
        "date": datetime.date.today().strftime("%d %b %Y"),
        "indent_no": _esc(snapshot.indent_no),
        "issue_date": snapshot.issue_date.strftime("%d %b %Y") if snapshot.issue_date else "___",
        "opening_date": snapshot.opening_date.strftime("%d %b %Y") if snapshot.opening_date else "___",
        "firms_invited_count": snapshot.firms_invited_count or "___",
        "subject_department": _esc(snapshot.department_name) if snapshot.department_name else "___",
        "total_item_count": snapshot.total_item_count,
        "participating_firms_count": snapshot.participating_firms_count,
        "tax_type": snapshot.tax_type,
        "tax_percent": f"{snapshot.tax_percent:g}",
        "delivery_days": snapshot.delivery_days,
        "current_month": datetime.date.today().strftime("%b"),
        "current_year": datetime.date.today().year,
        "firm_groups": [
            {
                "supplier_name": _esc(group.supplier_name),
                # Supplier's current address (looked up live by the caller),
                # not frozen - see module docstring.
                "firm_address": _esc(suppliers_by_id[group.supplier_id].address)
                if group.supplier_id in suppliers_by_id
                else "-",
                "item_count": len(group.items),
                "store_value": _money(group.store_value),
                "tax_amount": _money(group.tax_amount),
                "contract_value": _money(group.contract_value),
            }
            for group in snapshot.firm_groups
        ],
        "est_cost": _money(est_cost),
        "offered_rates": _money(offered),
        "overall_inc_dec": f"{inc_dec_pct:.2f}% {'inc' if (inc_dec_pct or 0) >= 0 else 'dec'}"
        if inc_dec_pct is not None
        else "N/A - no LPR history yet",
        "grand_store_value": _money(snapshot.grand_store_value),
        "grand_tax_amount": _money(snapshot.grand_tax_amount),
        "grand_contract_value": _money(snapshot.grand_contract_value),
    }

    full_context = {**(custom_fields or {}), **context}

    doc = DocxTemplate(BytesIO(template_bytes)) if template_bytes is not None else DocxTemplate(
        str(docx_template_path("pp_template.docx"))
    )
    doc.render(full_context)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
