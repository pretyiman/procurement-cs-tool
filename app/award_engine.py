"""Award resolution + Purchase Proposal.

Default award = the computed-lowest bidder from cs_engine. An officer may
override an item's award to a different (quoting) supplier via
Item.awarded_supplier_id, which requires Item.award_reason when it differs
from the lowest bidder (see validate_override, used at write time by the
UI). Purchase Proposal simply regroups awarded items by firm - it must
never recompute prices independently of cs_engine/award resolution.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlmodel import Session, select

from .cs_engine import GrandTotal, ItemResult, build_comparative_statement
from .models import Item, Quote, Tender


@dataclass
class AwardedItem:
    item: Item
    awarded_supplier_id: Optional[int]
    awarded_rate: Optional[float]
    total_value: float
    is_override: bool
    override_reason: Optional[str]
    invalid_override: bool  # True if item.awarded_supplier_id no longer has a valid quote


@dataclass
class ProposalFirmGroup:
    supplier_id: int
    supplier_name: str
    items: List[AwardedItem]
    store_value: float
    gst_amount: float
    contract_value: float


@dataclass
class PurchaseProposal:
    tender: Tender
    firm_groups: List[ProposalFirmGroup]
    unresolved_items: List[Item]
    grand_total: GrandTotal


def resolve_award(item: Item, cs_item_result: ItemResult, rate_map: Dict[int, float]) -> AwardedItem:
    """Tolerant resolution for display: never raises. A stale/invalid
    override (supplier no longer has a quote for this item) resolves to
    "unresolved" rather than silently falling back to a different firm."""
    if item.awarded_supplier_id is not None:
        rate = rate_map.get(item.awarded_supplier_id)
        if rate is None:
            return AwardedItem(
                item=item,
                awarded_supplier_id=None,
                awarded_rate=None,
                total_value=0.0,
                is_override=True,
                override_reason=item.award_reason,
                invalid_override=True,
            )
        is_override = item.awarded_supplier_id != cs_item_result.lowest_supplier_id
        return AwardedItem(
            item=item,
            awarded_supplier_id=item.awarded_supplier_id,
            awarded_rate=rate,
            total_value=item.qty * rate,
            is_override=is_override,
            override_reason=item.award_reason,
            invalid_override=False,
        )

    return AwardedItem(
        item=item,
        awarded_supplier_id=cs_item_result.lowest_supplier_id,
        awarded_rate=cs_item_result.lowest_rate,
        total_value=cs_item_result.total_value,
        is_override=False,
        override_reason=None,
        invalid_override=False,
    )


def validate_override(item: Item, cs_item_result: ItemResult, rate_map: Dict[int, float]) -> None:
    """Strict validation for write time (setting/clearing an override).
    Raises ValueError with a user-facing message if the proposed
    item.awarded_supplier_id / item.award_reason are not acceptable."""
    if item.awarded_supplier_id is None:
        return  # clearing an override (back to default-lowest) is always fine

    if item.awarded_supplier_id not in rate_map:
        raise ValueError(
            "Cannot award this item to that supplier: they did not quote it (NQ)."
        )

    if item.awarded_supplier_id != cs_item_result.lowest_supplier_id:
        if not item.award_reason or not item.award_reason.strip():
            raise ValueError(
                "A reason is required when awarding to a firm other than the lowest quoted rate."
            )


def _quotes_by_item(session: Session, item_ids: List[int]) -> Dict[int, Dict[int, float]]:
    quotes = (
        session.exec(select(Quote).where(Quote.item_id.in_(item_ids))).all() if item_ids else []
    )
    by_item: Dict[int, Dict[int, float]] = {}
    for q in quotes:
        if q.rate is not None:
            by_item.setdefault(q.item_id, {})[q.supplier_id] = q.rate
    return by_item


def resolve_awarded_items(session: Session, tender_id: int):
    """Returns (awarded_items, comparative_statement)."""
    cs = build_comparative_statement(session, tender_id)
    item_ids = [r.item.id for r in cs.item_results]
    quotes_by_item = _quotes_by_item(session, item_ids)

    awarded_items = [
        resolve_award(r.item, r, quotes_by_item.get(r.item.id, {})) for r in cs.item_results
    ]
    return awarded_items, cs


def build_purchase_proposal(session: Session, tender_id: int) -> PurchaseProposal:
    awarded_items, cs = resolve_awarded_items(session, tender_id)

    unresolved_items = [ai.item for ai in awarded_items if ai.awarded_supplier_id is None]
    awarded_only = [ai for ai in awarded_items if ai.awarded_supplier_id is not None]

    grouped: Dict[int, List[AwardedItem]] = {}
    for ai in awarded_only:
        grouped.setdefault(ai.awarded_supplier_id, []).append(ai)

    firm_groups = []
    for supplier_id, items in grouped.items():
        items = sorted(items, key=lambda ai: ai.item.ser)
        store_value = sum(ai.total_value for ai in items)
        gst_amount = store_value * cs.tender.gst_percent / 100
        firm_groups.append(
            ProposalFirmGroup(
                supplier_id=supplier_id,
                supplier_name=cs.suppliers_by_id[supplier_id].name,
                items=items,
                store_value=store_value,
                gst_amount=gst_amount,
                contract_value=store_value + gst_amount,
            )
        )
    firm_groups.sort(key=lambda g: g.supplier_name)

    grand_total = GrandTotal(
        item_count=sum(len(g.items) for g in firm_groups),
        store_value=sum(g.store_value for g in firm_groups),
        gst_amount=sum(g.gst_amount for g in firm_groups),
        contract_value=sum(g.contract_value for g in firm_groups),
    )

    return PurchaseProposal(
        tender=cs.tender,
        firm_groups=firm_groups,
        unresolved_items=unresolved_items,
        grand_total=grand_total,
    )
