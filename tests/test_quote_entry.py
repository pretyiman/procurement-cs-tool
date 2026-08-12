from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.main import app
from app.models import Item, ItemMaster, Quote, Supplier

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


def _make_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app), engine


def _create_tender(client) -> int:
    resp = client.post(
        "/tenders",
        data={"inquiry_no": "Quote Entry Test", "gst_percent": "10"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[-1])


def _create_item_master(engine, part_no="X-1", description="Test Widget", unit="Nos") -> int:
    with Session(engine) as session:
        im = ItemMaster(part_no=part_no, description=description, default_unit=unit)
        session.add(im)
        session.commit()
        session.refresh(im)
        return im.id


def test_quote_entry_creates_line_and_quote_for_new_item():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client)
        item_master_id = _create_item_master(engine)

        resp = client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={
                "item_master_id": str(item_master_id),
                "qty": "10",
                "supplier_name": "Acme Co",
                "rate": "25",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with Session(engine) as session:
            line = session.exec(select(Item).where(Item.tender_id == tender_id)).one()
            assert line.qty == 10
            assert line.item_master_id == item_master_id

            supplier = session.exec(select(Supplier).where(Supplier.name == "Acme Co")).one()
            quote = session.exec(
                select(Quote).where(Quote.item_id == line.id, Quote.supplier_id == supplier.id)
            ).one()
            assert quote.rate == 25
    finally:
        app.dependency_overrides.clear()


def test_second_suppliers_quote_does_not_disturb_first():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client)
        item_master_id = _create_item_master(engine)

        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"item_master_id": str(item_master_id), "qty": "10", "supplier_name": "Acme Co", "rate": "25"},
            follow_redirects=False,
        )
        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"item_master_id": str(item_master_id), "qty": "10", "supplier_name": "Beta Ltd", "rate": "22"},
            follow_redirects=False,
        )

        with Session(engine) as session:
            lines = session.exec(select(Item).where(Item.tender_id == tender_id)).all()
            assert len(lines) == 1  # same item, not duplicated

            quotes = session.exec(select(Quote).where(Quote.item_id == lines[0].id)).all()
            suppliers = {s.id: s.name for s in session.exec(select(Supplier)).all()}
            rates_by_name = {suppliers[q.supplier_id]: q.rate for q in quotes if q.rate is not None}
            assert rates_by_name == {"Acme Co": 25, "Beta Ltd": 22}
    finally:
        app.dependency_overrides.clear()


def test_reentering_same_item_updates_qty_without_duplicating_line():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client)
        item_master_id = _create_item_master(engine)

        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"item_master_id": str(item_master_id), "qty": "10", "supplier_name": "Acme Co", "rate": "25"},
            follow_redirects=False,
        )
        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"item_master_id": str(item_master_id), "qty": "15", "supplier_name": "Acme Co", "rate": "27"},
            follow_redirects=False,
        )

        with Session(engine) as session:
            lines = session.exec(select(Item).where(Item.tender_id == tender_id)).all()
            assert len(lines) == 1
            assert lines[0].qty == 15  # updated, not a second line

            quotes = session.exec(select(Quote).where(Quote.item_id == lines[0].id)).all()
            non_null = [q for q in quotes if q.rate is not None]
            assert len(non_null) == 1
            assert non_null[0].rate == 27  # updated in place, not duplicated
    finally:
        app.dependency_overrides.clear()
