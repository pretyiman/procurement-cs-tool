from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.cs_engine import build_comparative_statement
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


def test_total_quotes_count_matches_actual_quote_rows():
    with _fresh_session() as session:
        _make_tender_with_items_and_quotes(
            session,
            1,
            {
                1: {"Supplier A": 10, "Supplier B": 20},
                2: {"Supplier A": 10, "Supplier B": None},  # NQ - shouldn't count
            },
        )
        cs = build_comparative_statement(session, 1)
        assert cs.total_quotes_count == 3


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
