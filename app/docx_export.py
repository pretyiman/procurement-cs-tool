"""Purchase Proposal (PP) and Contract Award (CA) document generation
(Phase 12), rendered via docxtpl from app/docx_templates/pp_template.docx
and ca_template.docx. Those templates were built by surgically editing
real Word documents the user supplied (CA.doc/PP.doc, kept local-only -
see CLAUDE.md "Data sensitivity") so all legal/procedural wording is
preserved verbatim; only genuinely per-contract values are Jinja tags.
Non-technical staff can still open and edit the .docx templates directly
in Word - this module only supplies data, never hardcodes document text.
"""

import datetime
import html
from io import BytesIO
from typing import Optional

from docxtpl import DocxTemplate

from .award_engine import ProposalFirmGroup, PurchaseProposal
from .models import BusinessRules, Supplier, Tender
from .number_words import amount_in_words, number_to_words, ordinal
from .paths import resource_path

CA_TEMPLATE_PATH = resource_path("docx_templates", "ca_template.docx")
PP_TEMPLATE_PATH = resource_path("docx_templates", "pp_template.docx")


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
    group: ProposalFirmGroup,
    supplier: Supplier,
    contract_no: str,
    rules: BusinessRules,
    contract_date: Optional[datetime.date] = None,
    agreement_date: Optional[datetime.date] = None,
) -> bytes:
    contract_date = contract_date or datetime.date.today()
    agreement_date = agreement_date or datetime.date.today()

    store_value = group.store_value
    if group.contract_value < rules.security_deposit_waived_below:
        security_deposit = 0.0
    else:
        security_deposit = store_value * rules.security_deposit_percent / 100
    stamp_duty = group.contract_value * rules.stamp_duty_percent / 100

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
                "ser": ai.item.ser,
                "part_no": _esc(ai.item.item_master.part_no),
                "description": _esc(ai.item.item_master.description),
                "unit": _esc(ai.item.item_master.default_unit),
                "qty": ai.item.qty,
                "rate": _money(ai.awarded_rate),
                "total_value": _money(ai.total_value),
            }
            for ai in group.items
        ],
    }

    doc = DocxTemplate(str(CA_TEMPLATE_PATH))
    doc.render(context)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def generate_purchase_proposal_doc(
    tender: Tender,
    proposal: PurchaseProposal,
    suppliers_by_id: dict,
) -> bytes:
    est_cost = sum(
        ai.item.qty * ai.item.lpr
        for group in proposal.firm_groups
        for ai in group.items
        if ai.item.lpr is not None
    )
    offered = proposal.grand_total.contract_value
    inc_dec_pct = ((offered - est_cost) / est_cost * 100) if est_cost else None

    context = {
        "tender_inquiry_no": _esc(tender.inquiry_no),
        "date": datetime.date.today().strftime("%d %b %Y"),
        "indent_no": _esc(tender.indent_no or tender.inquiry_no),
        "issue_date": tender.issue_date.strftime("%d %b %Y") if tender.issue_date else "___",
        "opening_date": tender.opening_date.strftime("%d %b %Y") if tender.opening_date else "___",
        "firms_invited_count": tender.firms_invited_count or "___",
        "subject_department": _esc(tender.department.name) if tender.department else "___",
        "total_item_count": sum(len(g.items) for g in proposal.firm_groups) + len(proposal.unresolved_items),
        # suppliers_by_id is expected to be cs.suppliers_by_id (every supplier
        # with >=1 quote on this tender, win or not) - i.e. participating firms.
        "participating_firms_count": len(suppliers_by_id),
        "tax_type": tender.tax_type.value,
        "tax_percent": f"{tender.tax_percent:g}",
        "delivery_days": tender.delivery_days,
        "current_month": datetime.date.today().strftime("%b"),
        "current_year": datetime.date.today().year,
        "firm_groups": [
            {
                "supplier_name": _esc(group.supplier_name),
                "firm_address": _esc(suppliers_by_id[group.supplier_id].address),
                "item_count": len(group.items),
                "store_value": _money(group.store_value),
                "tax_amount": _money(group.tax_amount),
                "contract_value": _money(group.contract_value),
            }
            for group in proposal.firm_groups
        ],
        "est_cost": _money(est_cost),
        "offered_rates": _money(offered),
        "overall_inc_dec": f"{inc_dec_pct:.2f}% {'inc' if (inc_dec_pct or 0) >= 0 else 'dec'}"
        if inc_dec_pct is not None
        else "N/A - no LPR history yet",
        "grand_store_value": _money(proposal.grand_total.store_value),
        "grand_tax_amount": _money(proposal.grand_total.tax_amount),
        "grand_contract_value": _money(proposal.grand_total.contract_value),
    }

    doc = DocxTemplate(str(PP_TEMPLATE_PATH))
    doc.render(context)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
