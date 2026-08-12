from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.cs_engine import build_comparative_statement
from app.excel_io import import_tender
from app.models import Supplier

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
        assert sns.gst_amount == pytest.approx(37737.90, abs=0.01)
        assert sns.contract_value == pytest.approx(247392.90, abs=0.01)

        awan = summaries_by_name["M/s Awan Tech"]
        assert awan.item_count == 11
        assert awan.store_value == pytest.approx(211134)
        assert awan.gst_amount == pytest.approx(38004.12, abs=0.01)
        assert awan.contract_value == pytest.approx(249138.12, abs=0.01)


def test_grand_total_matches_known_good_numbers():
    with _fresh_session() as session:
        cs = _cs_for_fixture(session)

        assert cs.grand_total.item_count == 21
        assert cs.grand_total.store_value == pytest.approx(420789)
        assert cs.grand_total.gst_amount == pytest.approx(75742.02, abs=0.01)
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
