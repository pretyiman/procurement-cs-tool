from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.award_engine import (
    build_purchase_proposal,
    resolve_awarded_items,
    validate_override,
)
from app.cs_engine import build_comparative_statement
from app.excel_io import import_tender
from app.models import Item, Quote, Supplier

CS_XLSX_PATH = Path(__file__).resolve().parent.parent / "CS.xlsx"


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _load_fixture(session: Session):
    tender = import_tender(CS_XLSX_PATH, session)
    return tender


def test_proposal_matches_cs_summary_when_no_overrides():
    with _fresh_session() as session:
        tender = _load_fixture(session)
        cs = build_comparative_statement(session, tender.id)
        proposal = build_purchase_proposal(session, tender.id)

        cs_by_name = {s.supplier_name: s for s in cs.firm_summaries}
        proposal_by_name = {g.supplier_name: g for g in proposal.firm_groups}

        assert set(cs_by_name) == set(proposal_by_name)
        for name, cs_summary in cs_by_name.items():
            group = proposal_by_name[name]
            assert len(group.items) == cs_summary.item_count
            assert group.store_value == pytest.approx(cs_summary.store_value)
            assert group.gst_amount == pytest.approx(cs_summary.gst_amount)
            assert group.contract_value == pytest.approx(cs_summary.contract_value)

        assert proposal.grand_total.item_count == cs.grand_total.item_count
        assert proposal.grand_total.store_value == pytest.approx(cs.grand_total.store_value)
        assert proposal.grand_total.gst_amount == pytest.approx(cs.grand_total.gst_amount)
        assert proposal.grand_total.contract_value == pytest.approx(cs.grand_total.contract_value)


def test_override_moves_item_between_firms_and_updates_subtotals():
    with _fresh_session() as session:
        tender = _load_fixture(session)

        # Ser 3 "Brush flat painting" D/R: Awan 150 (lowest), SNS 350, Libra 455.
        # qty 5 -> Awan currently gets 750 of its 211,134 store value.
        item3 = session.exec(select(Item).where(Item.tender_id == tender.id, Item.ser == 3)).one()
        sns = session.exec(select(Supplier).where(Supplier.name == "M/s SNS Enterprises")).one()

        cs_before = build_comparative_statement(session, tender.id)
        result3 = next(r for r in cs_before.item_results if r.item.ser == 3)
        assert result3.lowest_rate == 150  # sanity check on fixture assumption

        rate_map = {
            q.supplier_id: q.rate
            for q in session.exec(select(Quote).where(Quote.item_id == item3.id)).all()
            if q.rate is not None
        }

        item3.awarded_supplier_id = sns.id
        item3.award_reason = "Awan Tech disqualified for this item - quality issue"
        validate_override(item3, result3, rate_map)  # must not raise
        session.add(item3)
        session.commit()

        proposal = build_purchase_proposal(session, tender.id)
        groups_by_name = {g.supplier_name: g for g in proposal.firm_groups}

        assert any(ai.item.ser == 3 for ai in groups_by_name["M/s SNS Enterprises"].items)
        assert not any(ai.item.ser == 3 for ai in groups_by_name["M/s Awan Tech"].items)

        awan = groups_by_name["M/s Awan Tech"]
        assert len(awan.items) == 10
        assert awan.store_value == pytest.approx(211134 - 5 * 150)

        sns_group = groups_by_name["M/s SNS Enterprises"]
        assert len(sns_group.items) == 11
        assert sns_group.store_value == pytest.approx(209655 + 5 * 350)

        # Grand total store value is unchanged - awarding a different (still
        # valid) firm doesn't change the price paid for this fixture item
        # since we used SNS's own quoted rate, not an arbitrary number.
        assert proposal.grand_total.store_value == pytest.approx(
            awan.store_value
            + sns_group.store_value
            + sum(g.store_value for g in proposal.firm_groups if g.supplier_name not in ("M/s Awan Tech", "M/s SNS Enterprises"))
        )


def test_override_without_reason_is_rejected_when_not_lowest():
    with _fresh_session() as session:
        tender = _load_fixture(session)
        item3 = session.exec(select(Item).where(Item.tender_id == tender.id, Item.ser == 3)).one()
        sns = session.exec(select(Supplier).where(Supplier.name == "M/s SNS Enterprises")).one()

        cs = build_comparative_statement(session, tender.id)
        result3 = next(r for r in cs.item_results if r.item.ser == 3)
        rate_map = {
            q.supplier_id: q.rate
            for q in session.exec(select(Quote).where(Quote.item_id == item3.id)).all()
            if q.rate is not None
        }

        item3.awarded_supplier_id = sns.id
        item3.award_reason = None  # no reason given, and SNS is not the lowest bidder

        with pytest.raises(ValueError, match="reason is required"):
            validate_override(item3, result3, rate_map)


def test_override_to_supplier_who_did_not_quote_is_rejected():
    with _fresh_session() as session:
        tender = _load_fixture(session)
        # Ser 1 was NQ by every firm in the fixture.
        item1 = session.exec(select(Item).where(Item.tender_id == tender.id, Item.ser == 1)).one()
        any_supplier = session.exec(select(Supplier)).first()

        cs = build_comparative_statement(session, tender.id)
        result1 = next(r for r in cs.item_results if r.item.ser == 1)

        item1.awarded_supplier_id = any_supplier.id
        item1.award_reason = "trying anyway"

        with pytest.raises(ValueError, match="did not quote"):
            validate_override(item1, result1, rate_map={})


def test_clearing_override_is_always_valid():
    with _fresh_session() as session:
        tender = _load_fixture(session)
        item3 = session.exec(select(Item).where(Item.tender_id == tender.id, Item.ser == 3)).one()
        item3.awarded_supplier_id = None
        item3.award_reason = None

        cs = build_comparative_statement(session, tender.id)
        result3 = next(r for r in cs.item_results if r.item.ser == 3)
        validate_override(item3, result3, rate_map={})  # must not raise


def test_unresolved_items_excluded_from_firm_groups_but_listed_separately():
    with _fresh_session() as session:
        tender = _load_fixture(session)
        proposal = build_purchase_proposal(session, tender.id)

        unresolved_sers = {i.ser for i in proposal.unresolved_items}
        assert unresolved_sers == {1, 21}

        for group in proposal.firm_groups:
            for ai in group.items:
                assert ai.item.ser not in unresolved_sers
