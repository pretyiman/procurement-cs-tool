import datetime

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.excel_io import get_or_create_item_master, get_or_create_supplier
from app.lpr_history import get_last_purchase_rate
from app.models import Item, Quote, Tender, TenderStatus


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _make_awarded_tender(session, item_master_id, rate, supplier_name="Acme", awarded_date=None, override_supplier_id=None):
    tender = Tender(inquiry_no="T", status=TenderStatus.awarded, awarded_date=awarded_date)
    session.add(tender)
    session.flush()

    item = Item(tender_id=tender.id, item_master_id=item_master_id, ser=1, qty=10)
    session.add(item)
    session.flush()

    supplier = get_or_create_supplier(session, supplier_name)
    session.add(Quote(item_id=item.id, supplier_id=supplier.id, rate=rate))

    if override_supplier_id is not None:
        item.awarded_supplier_id = override_supplier_id
        session.add(item)

    session.commit()
    return tender, item, supplier


def test_no_prior_history_returns_none():
    with _fresh_session() as session:
        im = get_or_create_item_master(session, "X-1", "Widget", "Nos")
        session.commit()
        assert get_last_purchase_rate(session, im.id) is None


def test_returns_rate_from_awarded_tender():
    with _fresh_session() as session:
        im = get_or_create_item_master(session, "X-1", "Widget", "Nos")
        session.commit()
        _make_awarded_tender(session, im.id, rate=100, awarded_date=datetime.date(2026, 1, 1))

        assert get_last_purchase_rate(session, im.id) == 100


def test_ignores_draft_and_proposal_generated_tenders():
    with _fresh_session() as session:
        im = get_or_create_item_master(session, "X-1", "Widget", "Nos")
        session.commit()

        for status in (TenderStatus.draft, TenderStatus.proposal_generated):
            tender = Tender(inquiry_no="T", status=status)
            session.add(tender)
            session.flush()
            item = Item(tender_id=tender.id, item_master_id=im.id, ser=1, qty=5)
            session.add(item)
            session.flush()
            supplier = get_or_create_supplier(session, "Acme")
            session.add(Quote(item_id=item.id, supplier_id=supplier.id, rate=50))
        session.commit()

        assert get_last_purchase_rate(session, im.id) is None


def test_uses_override_rate_not_lowest():
    with _fresh_session() as session:
        im = get_or_create_item_master(session, "X-1", "Widget", "Nos")
        session.commit()

        tender = Tender(inquiry_no="T", status=TenderStatus.awarded, awarded_date=datetime.date(2026, 1, 1))
        session.add(tender)
        session.flush()
        item = Item(tender_id=tender.id, item_master_id=im.id, ser=1, qty=10)
        session.add(item)
        session.flush()

        cheap = get_or_create_supplier(session, "Cheap Co")
        expensive = get_or_create_supplier(session, "Expensive Co")
        session.add(Quote(item_id=item.id, supplier_id=cheap.id, rate=50))
        session.add(Quote(item_id=item.id, supplier_id=expensive.id, rate=80))
        item.awarded_supplier_id = expensive.id  # overridden away from the lowest
        session.add(item)
        session.commit()

        assert get_last_purchase_rate(session, im.id) == 80


def test_picks_most_recently_awarded_tender():
    with _fresh_session() as session:
        im = get_or_create_item_master(session, "X-1", "Widget", "Nos")
        session.commit()
        _make_awarded_tender(session, im.id, rate=100, awarded_date=datetime.date(2026, 1, 1))
        _make_awarded_tender(session, im.id, rate=120, awarded_date=datetime.date(2026, 6, 1))

        assert get_last_purchase_rate(session, im.id) == 120


def test_exclude_tender_id_ignores_that_tender():
    with _fresh_session() as session:
        im = get_or_create_item_master(session, "X-1", "Widget", "Nos")
        session.commit()
        tender, _item, _supplier = _make_awarded_tender(
            session, im.id, rate=100, awarded_date=datetime.date(2026, 1, 1)
        )

        assert get_last_purchase_rate(session, im.id, exclude_tender_id=tender.id) is None


def test_full_lifecycle_auto_fills_lpr_on_next_tender():
    """End-to-end through the real HTTP routes: award tender A at rate 100,
    finalize it (which must set awarded_date), then adding the same
    catalog item to a brand-new tender B (no LPR typed in) must come back
    with lpr=100 automatically."""
    from sqlalchemy.pool import StaticPool as _StaticPool
    from sqlmodel import create_engine as _create_engine

    from app.db import get_session
    from app.main import app

    try:
        from fastapi.testclient import TestClient
    except ImportError:  # pragma: no cover
        TestClient = None

    engine = _create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=_StaticPool
    )
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    client = TestClient(app)
    try:
        with Session(engine) as session:
            im = get_or_create_item_master(session, "X-1", "Widget", "Nos")
            session.commit()
            item_master_id = im.id

        resp = client.post("/tenders", data={"inquiry_no": "Tender A"}, follow_redirects=False)
        tender_a_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        client.post(
            f"/tenders/{tender_a_id}/quote-entry",
            data={"item_master_id": str(item_master_id), "qty": "10", "supplier_name": "Acme", "rate": "100"},
            follow_redirects=False,
        )
        client.post(f"/tenders/{tender_a_id}/generate-proposal", follow_redirects=False)
        resp = client.post(f"/tenders/{tender_a_id}/mark-awarded", follow_redirects=False)
        assert resp.status_code == 303

        with Session(engine) as session:
            assert session.get(Tender, tender_a_id).awarded_date == datetime.date.today()

        resp = client.post("/tenders", data={"inquiry_no": "Tender B"}, follow_redirects=False)
        tender_b_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        resp = client.post(
            f"/tenders/{tender_b_id}/items",
            data={"item_master_id": str(item_master_id), "qty": "20"},  # no lpr given
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with Session(engine) as session:
            line = session.exec(select(Item).where(Item.tender_id == tender_b_id)).one()
            assert line.lpr == 100
    finally:
        app.dependency_overrides.clear()
