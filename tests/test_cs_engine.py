from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.cs_engine import build_comparative_statement, compute_best_bundle, compute_bundle_lineup
from app.excel_io import get_or_create_item_master, get_or_create_supplier, import_tender
from app.models import Item, Quote, Supplier

CS_XLSX_PATH = Path(__file__).resolve().parent.parent / "CS.xlsx"


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _cs_for_fixture(session: Session):
    tender = import_tender(CS_XLSX_PATH, session)
    return build_comparative_statement(session, tender.id)


def test_firm_summaries_match_known_good_numbers():
    with _fresh_session() as session:
        cs = _cs_for_fixture(session)

        summaries_by_name = {s.supplier_name: s for s in cs.firm_summaries}

        # M/s Libra Enterprises quoted but won zero items - it must not
        # appear in the summary at all, even though it exists as a supplier.
        assert set(summaries_by_name) == {"M/s SNS Enterprises", "M/s Awan Tech"}

        sns = summaries_by_name["M/s SNS Enterprises"]
        assert sns.item_count == 10
        assert sns.store_value == pytest.approx(209655)
        assert sns.tax_amount == pytest.approx(37737.90, abs=0.01)
        assert sns.contract_value == pytest.approx(247392.90, abs=0.01)

        awan = summaries_by_name["M/s Awan Tech"]
        assert awan.item_count == 11
        assert awan.store_value == pytest.approx(211134)
        assert awan.tax_amount == pytest.approx(38004.12, abs=0.01)
        assert awan.contract_value == pytest.approx(249138.12, abs=0.01)


def test_grand_total_matches_known_good_numbers():
    with _fresh_session() as session:
        cs = _cs_for_fixture(session)

        assert cs.grand_total.item_count == 21
        assert cs.grand_total.store_value == pytest.approx(420789)
        assert cs.grand_total.tax_amount == pytest.approx(75742.02, abs=0.01)
        assert cs.grand_total.contract_value == pytest.approx(496531.02, abs=0.01)


def test_items_nq_by_every_firm_are_excluded_but_still_present():
    with _fresh_session() as session:
        cs = _cs_for_fixture(session)

        for ser in (1, 21):
            result = next(r for r in cs.item_results if r.item.ser == ser)
            assert result.lowest_supplier_id is None
            assert result.lowest_rate is None
            assert result.total_value == 0.0

        # Total item rows in the CS view (23) vs awarded items (21).
        assert len(cs.item_results) == 23


def test_specific_item_lowest_bidder():
    with _fresh_session() as session:
        cs = _cs_for_fixture(session)

        # Ser 2 "Brush Brass Wire 6 Row": Awan 850, SNS 350, Libra 900 -> SNS wins.
        result = next(r for r in cs.item_results if r.item.ser == 2)
        winner = session.get(Supplier, result.lowest_supplier_id)
        assert winner.name == "M/s SNS Enterprises"
        assert result.lowest_rate == 350
        assert result.total_value == pytest.approx(35 * 350)


def _make_tender_with_items_and_quotes(session, tender_id, rates_by_item_and_supplier, qty=1):
    """rates_by_item_and_supplier: {item_ser: {supplier_name: rate_or_None}}."""
    from app.models import Tender, TenderStatus

    tender = Tender(id=tender_id, inquiry_no=f"T{tender_id}", status=TenderStatus.draft)
    session.add(tender)
    session.flush()

    supplier_ids = {}
    for ser, rates in rates_by_item_and_supplier.items():
        im = get_or_create_item_master(session, f"P-{ser}", f"Item {ser}", "Nos")[0]
        item = Item(tender_id=tender.id, item_master_id=im.id, ser=ser, qty=qty)
        session.add(item)
        session.flush()
        for supplier_name, rate in rates.items():
            if supplier_name not in supplier_ids:
                supplier_ids[supplier_name] = get_or_create_supplier(session, supplier_name)[0].id
            session.add(Quote(item_id=item.id, supplier_id=supplier_ids[supplier_name], rate=rate))
    session.commit()
    return tender


def _quotes_by_item(session, tender_id):
    items = session.exec(select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)).all()
    item_ids = [i.id for i in items]
    quotes = session.exec(select(Quote).where(Quote.item_id.in_(item_ids))).all() if item_ids else []
    by_item = {}
    for q in quotes:
        by_item.setdefault(q.item_id, []).append(q)
    return items, by_item


def test_package_total_can_favour_a_supplier_that_wins_fewer_items():
    """Supplier A is individually cheapest on 3/5 items, Supplier B on the
    other 2 - but B's total across all 5 items is lower than A's total.
    Package mode must rank B above A despite A winning more line items."""
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"Supplier A": 100, "Supplier B": 110},
                2: {"Supplier A": 100, "Supplier B": 110},
                3: {"Supplier A": 100, "Supplier B": 110},
                4: {"Supplier A": 100, "Supplier B": 40},
                5: {"Supplier A": 100, "Supplier B": 40},
            },
        )
        cs = build_comparative_statement(session, 1)

        # Item-wise: A wins 3 items, B wins 2.
        winners = [r.item.ser for r in cs.item_results if r.lowest_supplier_id is not None]
        a_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "Supplier A")
        b_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "Supplier B")
        a_wins = sum(1 for r in cs.item_results if r.lowest_supplier_id == a_id)
        b_wins = sum(1 for r in cs.item_results if r.lowest_supplier_id == b_id)
        assert a_wins == 3
        assert b_wins == 2

        # Package totals: A = 500, B = 410 - B is cheaper overall despite
        # winning fewer individual items.
        totals_by_name = {p.supplier_name: p for p in cs.package_totals}
        assert totals_by_name["Supplier A"].store_value == pytest.approx(500)
        assert totals_by_name["Supplier B"].store_value == pytest.approx(410)
        assert totals_by_name["Supplier A"].fully_quoted is True
        assert totals_by_name["Supplier B"].fully_quoted is True

        # Ranked cheapest-first among fully-quoting suppliers.
        assert [p.supplier_name for p in cs.package_totals] == ["Supplier B", "Supplier A"]


def test_partial_quoter_is_ranked_last_and_flagged_not_fully_quoted():
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"Full Quoter": 10, "Partial Quoter": 1},
                2: {"Full Quoter": 10, "Partial Quoter": 1},
                3: {"Full Quoter": 10},  # Partial Quoter didn't quote this one
            },
        )
        cs = build_comparative_statement(session, 1)

        totals_by_name = {p.supplier_name: p for p in cs.package_totals}
        assert totals_by_name["Full Quoter"].fully_quoted is True
        assert totals_by_name["Full Quoter"].quoted_item_count == 3
        assert totals_by_name["Partial Quoter"].fully_quoted is False
        assert totals_by_name["Partial Quoter"].quoted_item_count == 2
        assert totals_by_name["Partial Quoter"].store_value == pytest.approx(2)

        # Partial Quoter's total (2) is far cheaper than Full Quoter's (30),
        # but a partial quoter still can't rank above a full quoter.
        assert [p.supplier_name for p in cs.package_totals] == ["Full Quoter", "Partial Quoter"]


def test_tied_lowest_rate_is_flagged_and_deterministic():
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"Supplier A": 100, "Supplier B": 100, "Supplier C": 150},
                2: {"Supplier A": 50, "Supplier B": 60},
            },
        )
        cs = build_comparative_statement(session, 1)

        tied_result = next(r for r in cs.item_results if r.item.ser == 1)
        a_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "Supplier A")
        b_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "Supplier B")
        assert tied_result.is_tied is True
        assert set(tied_result.tied_supplier_ids) == {a_id, b_id}
        # Deterministic pick: lowest supplier_id among the tied ones, not
        # whichever quote row happened to come back first from the DB.
        assert tied_result.lowest_supplier_id == min(a_id, b_id)

        untied_result = next(r for r in cs.item_results if r.item.ser == 2)
        assert untied_result.is_tied is False
        assert untied_result.tied_supplier_ids == [untied_result.lowest_supplier_id]


def test_lowest_count_leaderboard_partitions_items_cleanly():
    """Counts must sum to the total awarded item count - a tie is flagged
    at the row level (is_tied) but only credited to the one deterministic
    winner here, so the leaderboard numbers stay additive/comparable."""
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"Supplier A": 10, "Supplier B": 20},
                2: {"Supplier A": 10, "Supplier B": 20},
                3: {"Supplier A": 10, "Supplier B": 5},
                4: {"Supplier A": 100, "Supplier B": 100},  # tie
            },
        )
        cs = build_comparative_statement(session, 1)

        board_by_name = {s.supplier_name: s for s in cs.lowest_count_leaderboard}
        assert board_by_name["Supplier A"].item_count == 3  # items 1, 2, and the tie (lower id)
        assert board_by_name["Supplier B"].item_count == 1  # item 3
        assert sum(s.item_count for s in cs.lowest_count_leaderboard) == 4
        # Ranked by count descending.
        assert [s.supplier_name for s in cs.lowest_count_leaderboard] == ["Supplier A", "Supplier B"]




def test_package_ranking_is_deterministic_on_a_tie():
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"Supplier A": 100, "Supplier B": 100},
                2: {"Supplier A": 50, "Supplier B": 50},
            },
        )
        cs = build_comparative_statement(session, 1)
        a_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "Supplier A")
        b_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "Supplier B")

        assert cs.package_totals[0].contract_value == cs.package_totals[1].contract_value
        # Deterministic tie-break by supplier_id, not Python set iteration order.
        assert [p.supplier_id for p in cs.package_totals] == sorted([a_id, b_id])


def test_package_total_includes_tax():
    with _fresh_session() as session:
        tender = _make_tender_with_items_and_quotes(
            session, 1, {1: {"Only Supplier": 200}, 2: {"Only Supplier": 300}}
        )
        tender.tax_percent = 10
        session.add(tender)
        session.commit()

        cs = build_comparative_statement(session, 1)
        pkg = cs.package_totals[0]
        assert pkg.store_value == pytest.approx(500)
        assert pkg.tax_amount == pytest.approx(50)
        assert pkg.contract_value == pytest.approx(550)


# --- compute_best_bundle / compute_bundle_lineup -----------------------------


def test_bundle_of_size_one_matches_the_only_full_coverage_supplier():
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session, 1, {1: {"Only Supplier": 10}, 2: {"Only Supplier": 20}}
        )
        cs = build_comparative_statement(session, 1)
        items, quotes_by_item = _quotes_by_item(session, 1)

        bundle = compute_best_bundle(items, quotes_by_item, cs.suppliers_by_id, cs.tender.tax_percent, 1)
        supplier_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "Only Supplier")
        assert bundle.supplier_ids == [supplier_id]
        assert bundle.fully_covered is True
        assert bundle.store_value == pytest.approx(30)


def test_bundle_includes_partial_bidders_and_beats_the_single_full_supplier():
    """Two partial bidders (A covers 1-2 cheaply, B covers 3-4 cheaply)
    combine into a size-2 bundle that fully covers everything far cheaper
    than the one supplier (C) who individually covers all 4 items."""
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"A": 10, "C": 50},
                2: {"A": 10, "C": 50},
                3: {"B": 10, "C": 50},
                4: {"B": 10, "C": 50},
            },
        )
        cs = build_comparative_statement(session, 1)
        items, quotes_by_item = _quotes_by_item(session, 1)
        a_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "A")
        b_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "B")
        c_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "C")

        size1 = compute_best_bundle(items, quotes_by_item, cs.suppliers_by_id, 0, 1)
        assert size1.supplier_ids == [c_id]  # only C can cover everything alone
        assert size1.store_value == pytest.approx(200)

        size2 = compute_best_bundle(items, quotes_by_item, cs.suppliers_by_id, 0, 2)
        assert set(size2.supplier_ids) == {a_id, b_id}
        assert size2.fully_covered is True
        assert size2.store_value == pytest.approx(40)  # far cheaper than the size-1 bundle


def test_bundle_size_larger_than_supplier_count_returns_none():
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(session, 1, {1: {"A": 10, "B": 20}})
        cs = build_comparative_statement(session, 1)
        items, quotes_by_item = _quotes_by_item(session, 1)

        assert compute_best_bundle(items, quotes_by_item, cs.suppliers_by_id, 0, 3) is None
        assert compute_best_bundle(items, quotes_by_item, cs.suppliers_by_id, 0, 0) is None


def test_bundle_partial_coverage_when_full_coverage_unreachable_at_that_size():
    """Item 3 is only ever quoted by C - a size-1 bundle stuck on A or B
    can't reach it, so the best size-1 bundle is whichever of A/B covers
    the most (both cover 2 of 3) at the lowest cost, clearly marked as
    not fully covered."""
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"A": 10, "B": 10},
                2: {"A": 10, "B": 10},
                3: {"C": 10},
            },
        )
        cs = build_comparative_statement(session, 1)
        items, quotes_by_item = _quotes_by_item(session, 1)

        bundle = compute_best_bundle(items, quotes_by_item, cs.suppliers_by_id, 0, 1)
        assert bundle.covered_item_count == 2
        assert bundle.coverable_item_count == 3
        assert bundle.fully_covered is False


def test_bundle_lineup_skips_unreachable_sizes_and_orders_ascending():
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(session, 1, {1: {"A": 10, "B": 20}})
        cs = build_comparative_statement(session, 1)
        items, quotes_by_item = _quotes_by_item(session, 1)

        lineup = compute_bundle_lineup(items, quotes_by_item, cs.suppliers_by_id, 0, [3, 1, 2, 1])
        assert [b.bundle_size for b in lineup] == [1, 2]  # size 3 unreachable, dup size 1 collapsed


def test_bundle_greedy_fallback_still_returns_a_valid_answer():
    """Force the brute-force safety cap down to near-zero so the greedy
    approximation path runs instead. This scenario is deliberately
    adversarial to a coverage-first greedy (C alone covers everything in
    one round, so greedy grabs it immediately, even though A+B together
    is far cheaper) - the point isn't that greedy finds the true optimum
    (it documented-ly doesn't have to), just that it still returns a
    valid, self-consistent, fully-covering answer rather than something
    broken."""
    import app.cs_engine as cs_engine_module

    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"A": 10, "C": 50},
                2: {"A": 10, "C": 50},
                3: {"B": 10, "C": 50},
                4: {"B": 10, "C": 50},
            },
        )
        cs = build_comparative_statement(session, 1)
        items, quotes_by_item = _quotes_by_item(session, 1)

        original_cap = cs_engine_module.MAX_BUNDLE_BRUTE_FORCE_COMBINATIONS
        cs_engine_module.MAX_BUNDLE_BRUTE_FORCE_COMBINATIONS = 0
        try:
            bundle = compute_best_bundle(items, quotes_by_item, cs.suppliers_by_id, 0, 2)
        finally:
            cs_engine_module.MAX_BUNDLE_BRUTE_FORCE_COMBINATIONS = original_cap

        assert bundle.approximate is True
        assert len(bundle.supplier_ids) == 2
        assert bundle.fully_covered is True
        assert bundle.covered_item_count == 4
        # Not necessarily optimal (that's the true A+B combo at 40), but
        # must be at least as good as it - never cheaper than the actual
        # best possible answer.
        assert bundle.store_value >= 40


def test_bundle_item_breakdown_includes_covered_and_uncovered_items():
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"A": 10},
                2: {"A": 10},
                3: {"B": 10},
                4: {"B": 10},
            },
        )
        cs = build_comparative_statement(session, 1)
        items, quotes_by_item = _quotes_by_item(session, 1)
        a_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "A")
        b_id = next(s.id for s in cs.suppliers_by_id.values() if s.name == "B")

        bundle = compute_best_bundle(items, quotes_by_item, cs.suppliers_by_id, 0, 2)
        assert set(bundle.supplier_ids) == {a_id, b_id}
        assert len(bundle.items) == 4  # every item on the tender, covered or not

        by_ser = {a.item.ser: a for a in bundle.items}
        assert by_ser[1].supplier_name == "A"
        assert by_ser[1].rate == pytest.approx(10)
        assert by_ser[3].supplier_name == "B"
        assert by_ser[3].rate == pytest.approx(10)

        # Neither A nor B alone covers everything (no third, fully-quoting
        # supplier here) - a size-1 bundle necessarily leaves 2 items
        # unassigned, shown as such rather than silently dropped.
        bundle1 = compute_best_bundle(items, quotes_by_item, cs.suppliers_by_id, 0, 1)
        by_ser1 = {a.item.ser: a for a in bundle1.items}
        uncovered_sers = {ser for ser, a in by_ser1.items() if a.supplier_id is None}
        assert len(uncovered_sers) == 2
        for ser in uncovered_sers:
            assert by_ser1[ser].rate is None
            assert by_ser1[ser].total_value == 0.0
