"""Comparative Statement calculation engine.

Pure derivation logic described in docs/data-model.md ("Derived" section):
lowest rate/firm per item, item totals, per-firm summary, grand totals.
No manual award overrides here (that's Phase 4's award_engine, which
layers on top of this) - this module always uses the computed-lowest
bidder, matching the existing CS.xlsx behaviour.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlmodel import Session, select

from .models import Item, Quote, Supplier, Tender


@dataclass
class ItemResult:
    item: Item
    lowest_supplier_id: Optional[int]
    lowest_rate: Optional[float]
    total_value: float
    inc_dec_pct: Optional[float]


@dataclass
class FirmSummary:
    supplier_id: int
    supplier_name: str
    item_count: int
    store_value: float
    tax_amount: float
    contract_value: float


@dataclass
class GrandTotal:
    item_count: int
    store_value: float
    tax_amount: float
    contract_value: float


@dataclass
class PackageTotal:
    """One supplier's total if they were awarded the entire item list as a
    single package, rather than item-by-item. Only a supplier who quoted
    every item can actually fulfill "the whole package" - fully_quoted
    marks that; a partial quoter is still reported (for transparency) but
    isn't a valid package candidate."""

    supplier_id: int
    supplier_name: str
    quoted_item_count: int
    total_item_count: int
    fully_quoted: bool
    store_value: float
    tax_amount: float
    contract_value: float


@dataclass
class ComparativeStatement:
    tender: Tender
    item_results: List[ItemResult]
    firm_summaries: List[FirmSummary]
    grand_total: GrandTotal
    suppliers_by_id: Dict[int, Supplier]
    package_totals: List[PackageTotal]


def compute_item_result(item: Item, quotes: List[Quote]) -> ItemResult:
    quoted = [(q.supplier_id, q.rate) for q in quotes if q.rate is not None]

    if quoted:
        lowest_supplier_id, lowest_rate = min(quoted, key=lambda pair: pair[1])
        total_value = item.qty * lowest_rate
    else:
        lowest_supplier_id = None
        lowest_rate = None
        total_value = 0.0

    inc_dec_pct = None
    if item.lpr is not None and lowest_rate is not None and item.lpr != 0:
        inc_dec_pct = (lowest_rate - item.lpr) / item.lpr * 100

    return ItemResult(
        item=item,
        lowest_supplier_id=lowest_supplier_id,
        lowest_rate=lowest_rate,
        total_value=total_value,
        inc_dec_pct=inc_dec_pct,
    )


def compute_firm_summaries(
    item_results: List[ItemResult],
    suppliers_by_id: Dict[int, Supplier],
    tax_percent: float,
) -> List[FirmSummary]:
    store_value_by_supplier: Dict[int, float] = {}
    item_count_by_supplier: Dict[int, int] = {}

    for result in item_results:
        if result.lowest_supplier_id is None:
            continue  # NQ by every firm - not awarded to anyone
        sid = result.lowest_supplier_id
        store_value_by_supplier[sid] = store_value_by_supplier.get(sid, 0.0) + result.total_value
        item_count_by_supplier[sid] = item_count_by_supplier.get(sid, 0) + 1

    summaries = []
    for sid, store_value in store_value_by_supplier.items():
        tax_amount = store_value * tax_percent / 100
        summaries.append(
            FirmSummary(
                supplier_id=sid,
                supplier_name=suppliers_by_id[sid].name,
                item_count=item_count_by_supplier[sid],
                store_value=store_value,
                tax_amount=tax_amount,
                contract_value=store_value + tax_amount,
            )
        )

    summaries.sort(key=lambda s: s.supplier_name)
    return summaries


def compute_grand_total(firm_summaries: List[FirmSummary]) -> GrandTotal:
    return GrandTotal(
        item_count=sum(s.item_count for s in firm_summaries),
        store_value=sum(s.store_value for s in firm_summaries),
        tax_amount=sum(s.tax_amount for s in firm_summaries),
        contract_value=sum(s.contract_value for s in firm_summaries),
    )


def compute_package_totals(
    items: List[Item],
    quotes_by_item: Dict[int, List[Quote]],
    suppliers_by_id: Dict[int, Supplier],
    tax_percent: float,
) -> List[PackageTotal]:
    """Each supplier's total if awarded every item as one package, instead
    of item-by-item. Ranked cheapest-first among suppliers who quoted every
    item (fully_quoted=True) - a partial quoter is listed but sorted after,
    since they can't actually fulfill the whole package."""
    total_item_count = len(items)
    supplier_ids = {q.supplier_id for quotes in quotes_by_item.values() for q in quotes if q.rate is not None}

    results = []
    for sid in supplier_ids:
        store_value = 0.0
        quoted_item_count = 0
        for item in items:
            quote = next(
                (q for q in quotes_by_item.get(item.id, []) if q.supplier_id == sid and q.rate is not None),
                None,
            )
            if quote is not None:
                store_value += item.qty * quote.rate
                quoted_item_count += 1

        tax_amount = store_value * tax_percent / 100
        results.append(
            PackageTotal(
                supplier_id=sid,
                supplier_name=suppliers_by_id[sid].name,
                quoted_item_count=quoted_item_count,
                total_item_count=total_item_count,
                fully_quoted=quoted_item_count == total_item_count,
                store_value=store_value,
                tax_amount=tax_amount,
                contract_value=store_value + tax_amount,
            )
        )

    results.sort(key=lambda r: (not r.fully_quoted, r.contract_value))
    return results


def build_comparative_statement(session: Session, tender_id: int) -> ComparativeStatement:
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise ValueError(f"Tender {tender_id} not found")

    items = session.exec(
        select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)
    ).all()
    item_ids = [i.id for i in items]

    quotes = (
        session.exec(select(Quote).where(Quote.item_id.in_(item_ids))).all()
        if item_ids
        else []
    )
    quotes_by_item: Dict[int, List[Quote]] = {}
    for q in quotes:
        quotes_by_item.setdefault(q.item_id, []).append(q)

    supplier_ids = {q.supplier_id for q in quotes}
    suppliers_by_id = {
        s.id: s
        for s in (
            session.exec(select(Supplier).where(Supplier.id.in_(supplier_ids))).all()
            if supplier_ids
            else []
        )
    }

    item_results = [compute_item_result(item, quotes_by_item.get(item.id, [])) for item in items]
    firm_summaries = compute_firm_summaries(item_results, suppliers_by_id, tender.tax_percent)
    grand_total = compute_grand_total(firm_summaries)
    package_totals = compute_package_totals(items, quotes_by_item, suppliers_by_id, tender.tax_percent)

    return ComparativeStatement(
        tender=tender,
        item_results=item_results,
        firm_summaries=firm_summaries,
        grand_total=grand_total,
        suppliers_by_id=suppliers_by_id,
        package_totals=package_totals,
    )
