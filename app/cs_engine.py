"""Comparative Statement calculation engine.

Pure derivation logic described in docs/data-model.md ("Derived" section):
lowest rate/firm per item, item totals, per-firm summary, grand totals.
No manual award overrides here (that's Phase 4's award_engine, which
layers on top of this) - this module always uses the computed-lowest
bidder, matching the existing CS.xlsx behaviour.
"""

import itertools
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlmodel import Session, select

from .models import Item, Quote, Supplier, Tender

# Past this many combinations, brute-forcing every possible supplier subset
# stops being "fast enough for a page load" - compute_best_bundle falls
# back to a greedy approximation instead of hanging the request. Realistic
# RFQ supplier counts (dozens at most) stay well under this for the bundle
# sizes anyone would actually ask for.
MAX_BUNDLE_BRUTE_FORCE_COMBINATIONS = 200_000


@dataclass
class ItemResult:
    item: Item
    lowest_supplier_id: Optional[int]
    lowest_rate: Optional[float]
    total_value: float
    inc_dec_pct: Optional[float]
    tied_supplier_ids: List[int] = field(default_factory=list)  # every supplier at the minimum rate; len>1 = a genuine tie

    @property
    def is_tied(self) -> bool:
        return len(self.tied_supplier_ids) > 1


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
class SupplierLowestCount:
    """How many items a supplier is the (deterministic) lowest bidder on,
    and how much of the tender's value that represents - for the "who's
    actually cheapest, and how often" leaderboard. Partitioned cleanly:
    each item counts toward exactly one supplier (its resolved
    lowest_supplier_id), so counts sum to the total awarded item count
    even when some of those items were genuine price ties - see
    ItemResult.is_tied for that detail at the row level."""

    supplier_id: int
    supplier_name: str
    item_count: int
    store_value: float


@dataclass
class SupplierBundle:
    """The cheapest combination of exactly `bundle_size` suppliers that
    covers the most items - coverage maximized first, cost minimized
    second among combinations tied on coverage. Partial bidders are
    eligible for membership (unlike PackageTotal, which only ever
    considers one supplier who individually covers everything) - a bundle
    only needs its *members' union* to cover an item, not each member
    alone. "Bundle size 1" is the same computation degenerating to a
    single supplier, so it subsumes what PackageTotal's top (fully-
    quoted) entry represents, just without requiring full coverage from
    one firm."""

    supplier_ids: List[int]
    supplier_names: List[str]
    bundle_size: int
    covered_item_count: int
    coverable_item_count: int  # items at least one supplier (anyone) quoted
    fully_covered: bool
    store_value: float
    tax_amount: float
    contract_value: float
    approximate: bool  # True if found via the greedy fallback, not exhaustive search


@dataclass
class ComparativeStatement:
    tender: Tender
    item_results: List[ItemResult]
    firm_summaries: List[FirmSummary]
    grand_total: GrandTotal
    suppliers_by_id: Dict[int, Supplier]
    package_totals: List[PackageTotal]
    lowest_count_leaderboard: List[SupplierLowestCount]


def compute_item_result(item: Item, quotes: List[Quote]) -> ItemResult:
    quoted = [(q.supplier_id, q.rate) for q in quotes if q.rate is not None]

    if quoted:
        lowest_rate = min(rate for _, rate in quoted)
        # Every supplier at the minimum rate, not just whichever quote row
        # happened to come back first from the database - a real tie gets
        # flagged (ItemResult.is_tied), not silently hidden behind
        # incidental query order. The deterministic pick among them
        # (lowest supplier_id) is a disclosed, stable tie-break - not an
        # accident of insertion order - used as the actual award default.
        tied_supplier_ids = sorted(sid for sid, rate in quoted if rate == lowest_rate)
        lowest_supplier_id = tied_supplier_ids[0]
        total_value = item.qty * lowest_rate
    else:
        lowest_supplier_id = None
        lowest_rate = None
        total_value = 0.0
        tied_supplier_ids = []

    inc_dec_pct = None
    if item.lpr is not None and lowest_rate is not None and item.lpr != 0:
        inc_dec_pct = (lowest_rate - item.lpr) / item.lpr * 100

    return ItemResult(
        item=item,
        lowest_supplier_id=lowest_supplier_id,
        lowest_rate=lowest_rate,
        total_value=total_value,
        inc_dec_pct=inc_dec_pct,
        tied_supplier_ids=tied_supplier_ids,
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

    # Tie-break on supplier_id too - without it, a genuine tie in
    # contract_value would fall back to Python set iteration order (how
    # supplier_ids above was built), which depends on hash values, not any
    # business rule. This makes the order fully deterministic; a real tie
    # at the top is still surfaced separately (see the route) rather than
    # silently resolved by this ordering.
    results.sort(key=lambda r: (not r.fully_quoted, r.contract_value, r.supplier_id))
    return results


def compute_lowest_count_leaderboard(
    item_results: List[ItemResult], suppliers_by_id: Dict[int, Supplier]
) -> List[SupplierLowestCount]:
    counts: Dict[int, int] = {}
    values: Dict[int, float] = {}
    for r in item_results:
        if r.lowest_supplier_id is None:
            continue
        counts[r.lowest_supplier_id] = counts.get(r.lowest_supplier_id, 0) + 1
        values[r.lowest_supplier_id] = values.get(r.lowest_supplier_id, 0.0) + r.total_value

    leaderboard = [
        SupplierLowestCount(
            supplier_id=sid,
            supplier_name=suppliers_by_id[sid].name,
            item_count=count,
            store_value=values.get(sid, 0.0),
        )
        for sid, count in counts.items()
    ]
    leaderboard.sort(key=lambda s: (-s.item_count, s.supplier_name))
    return leaderboard


def _evaluate_bundle(
    combo: Tuple[int, ...], rates: Dict[int, Dict[int, float]], items_by_id: Dict[int, Item]
) -> Tuple[int, float]:
    """(covered_item_count, store_value) for a specific set of suppliers -
    each covered item's cost is the cheapest rate among just this combo's
    members who quoted it, same idea as a per-item award but scoped to a
    subset of suppliers instead of everyone."""
    covered = 0
    store_value = 0.0
    combo_set = set(combo)
    for item_id, rate_map in rates.items():
        applicable = [rate for sid, rate in rate_map.items() if sid in combo_set]
        if applicable:
            covered += 1
            store_value += items_by_id[item_id].qty * min(applicable)
    return covered, store_value


def _greedy_bundle(
    supplier_ids: List[int],
    rates: Dict[int, Dict[int, float]],
    items_by_id: Dict[int, Item],
    bundle_size: int,
) -> Tuple[Tuple[int, ...], int, float]:
    """Approximation used only when brute-forcing every combination would
    be too slow: repeatedly add whichever remaining supplier covers the
    most still-uncovered items, tie-broken by lowest cost for that new
    coverage. Not guaranteed optimal (unlike the exhaustive search), but
    a reasonable stand-in for large supplier counts."""
    chosen: List[int] = []
    covered_items: set = set()
    remaining = list(supplier_ids)

    for _ in range(bundle_size):
        best_sid = None
        best_new_coverage = -1
        best_marginal_cost = None
        for sid in remaining:
            new_items = [iid for iid, rate_map in rates.items() if sid in rate_map and iid not in covered_items]
            marginal_cost = sum(items_by_id[iid].qty * rates[iid][sid] for iid in new_items)
            new_coverage = len(new_items)
            if new_coverage > best_new_coverage or (
                new_coverage == best_new_coverage and (best_marginal_cost is None or marginal_cost < best_marginal_cost)
            ):
                best_sid = sid
                best_new_coverage = new_coverage
                best_marginal_cost = marginal_cost
        if best_sid is None:
            break
        chosen.append(best_sid)
        remaining.remove(best_sid)
        covered_items.update(iid for iid, rate_map in rates.items() if best_sid in rate_map)

    combo = tuple(chosen)
    covered, store_value = _evaluate_bundle(combo, rates, items_by_id)
    return combo, covered, store_value


def compute_best_bundle(
    items: List[Item],
    quotes_by_item: Dict[int, List[Quote]],
    suppliers_by_id: Dict[int, Supplier],
    tax_percent: float,
    bundle_size: int,
) -> Optional[SupplierBundle]:
    """The best `bundle_size`-supplier combination available - None if
    bundle_size is invalid or there aren't enough distinct quoting
    suppliers to form one."""
    if bundle_size < 1:
        return None

    items_by_id = {item.id: item for item in items}
    rates: Dict[int, Dict[int, float]] = {}
    for item in items:
        for q in quotes_by_item.get(item.id, []):
            if q.rate is not None:
                rates.setdefault(item.id, {})[q.supplier_id] = q.rate

    coverable_item_count = len(rates)
    quoting_supplier_ids = sorted({sid for rate_map in rates.values() for sid in rate_map})
    if bundle_size > len(quoting_supplier_ids):
        return None

    num_combinations = math.comb(len(quoting_supplier_ids), bundle_size)
    if num_combinations <= MAX_BUNDLE_BRUTE_FORCE_COMBINATIONS:
        best_combo: Optional[Tuple[int, ...]] = None
        best_covered = -1
        best_value: Optional[float] = None
        # combinations() over a sorted input iterates in a fixed, sorted
        # order, so ties (equal coverage AND equal cost) resolve to
        # whichever combo comes first there - deterministic, not
        # incidental like an unordered set/dict would be.
        for combo in itertools.combinations(quoting_supplier_ids, bundle_size):
            covered, store_value = _evaluate_bundle(combo, rates, items_by_id)
            if covered > best_covered or (covered == best_covered and (best_value is None or store_value < best_value)):
                best_combo, best_covered, best_value = combo, covered, store_value
        approximate = False
    else:
        best_combo, best_covered, best_value = _greedy_bundle(quoting_supplier_ids, rates, items_by_id, bundle_size)
        approximate = True

    if best_combo is None or best_value is None:
        return None

    tax_amount = best_value * tax_percent / 100
    return SupplierBundle(
        supplier_ids=list(best_combo),
        supplier_names=[suppliers_by_id[sid].name for sid in best_combo],
        bundle_size=bundle_size,
        covered_item_count=best_covered,
        coverable_item_count=coverable_item_count,
        fully_covered=best_covered == coverable_item_count and coverable_item_count > 0,
        store_value=best_value,
        tax_amount=tax_amount,
        contract_value=best_value + tax_amount,
        approximate=approximate,
    )


def compute_bundle_lineup(
    items: List[Item],
    quotes_by_item: Dict[int, List[Quote]],
    suppliers_by_id: Dict[int, Supplier],
    tax_percent: float,
    bundle_sizes: List[int],
) -> List[SupplierBundle]:
    """One SupplierBundle per requested size, in ascending size order,
    skipping any size that isn't achievable (bigger than the number of
    quoting suppliers) or requested twice."""
    bundles = []
    for size in sorted(set(bundle_sizes)):
        bundle = compute_best_bundle(items, quotes_by_item, suppliers_by_id, tax_percent, size)
        if bundle is not None:
            bundles.append(bundle)
    return bundles


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
    lowest_count_leaderboard = compute_lowest_count_leaderboard(item_results, suppliers_by_id)

    return ComparativeStatement(
        tender=tender,
        item_results=item_results,
        firm_summaries=firm_summaries,
        grand_total=grand_total,
        suppliers_by_id=suppliers_by_id,
        package_totals=package_totals,
        lowest_count_leaderboard=lowest_count_leaderboard,
    )
