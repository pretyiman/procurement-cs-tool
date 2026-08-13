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
        data={"inquiry_no": "Quote Entry Test", "tax_percent": "10"},
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


def _add_item_to_tender(client, engine, tender_id, item_master_id, qty=10) -> int:
    """Items only ever come from the RFQ page's own add-item flow now -
    Quote Entry no longer creates them."""
    resp = client.post(
        f"/tenders/{tender_id}/items",
        data={"item_master_id": str(item_master_id), "qty": str(qty)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        item = session.exec(select(Item).where(Item.tender_id == tender_id)).all()[-1]
        return item.id


def _quick_create_supplier(client, name) -> int:
    resp = client.post("/suppliers/quick-create", data={"name": name})
    assert resp.status_code == 200
    return resp.json()["id"]


def test_quote_entry_saves_rate_against_existing_item():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client)
        item_master_id = _create_item_master(engine)
        item_id = _add_item_to_tender(client, engine, tender_id, item_master_id, qty=10)
        supplier_id = _quick_create_supplier(client, "Acme Co")

        resp = client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(supplier_id), f"rate__{item_id}": "25"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with Session(engine) as session:
            quote = session.exec(
                select(Quote).where(Quote.item_id == item_id, Quote.supplier_id == supplier_id)
            ).one()
            assert quote.rate == 25
            # item itself is untouched by quote entry - qty stays whatever
            # the RFQ page set it to
            item = session.get(Item, item_id)
            assert item.qty == 10
    finally:
        app.dependency_overrides.clear()


def test_second_suppliers_quote_does_not_disturb_first():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client)
        item_master_id = _create_item_master(engine)
        item_id = _add_item_to_tender(client, engine, tender_id, item_master_id, qty=10)
        acme_id = _quick_create_supplier(client, "Acme Co")
        beta_id = _quick_create_supplier(client, "Beta Ltd")

        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(acme_id), f"rate__{item_id}": "25"},
            follow_redirects=False,
        )
        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(beta_id), f"rate__{item_id}": "22"},
            follow_redirects=False,
        )

        with Session(engine) as session:
            lines = session.exec(select(Item).where(Item.tender_id == tender_id)).all()
            assert len(lines) == 1  # quote entry never creates a second item line

            quotes = session.exec(select(Quote).where(Quote.item_id == item_id)).all()
            suppliers = {s.id: s.name for s in session.exec(select(Supplier)).all()}
            rates_by_name = {suppliers[q.supplier_id]: q.rate for q in quotes if q.rate is not None}
            assert rates_by_name == {"Acme Co": 25, "Beta Ltd": 22}
    finally:
        app.dependency_overrides.clear()


def test_reentering_same_supplier_updates_rate_in_place():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client)
        item_master_id = _create_item_master(engine)
        item_id = _add_item_to_tender(client, engine, tender_id, item_master_id, qty=10)
        supplier_id = _quick_create_supplier(client, "Acme Co")

        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(supplier_id), f"rate__{item_id}": "25"},
            follow_redirects=False,
        )
        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(supplier_id), f"rate__{item_id}": "27"},
            follow_redirects=False,
        )

        with Session(engine) as session:
            quotes = session.exec(
                select(Quote).where(Quote.item_id == item_id, Quote.supplier_id == supplier_id)
            ).all()
            assert len(quotes) == 1  # updated in place, not duplicated
            assert quotes[0].rate == 27
    finally:
        app.dependency_overrides.clear()


def test_blank_rate_clears_an_existing_quote_to_nq():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client)
        item_master_id = _create_item_master(engine)
        item_id = _add_item_to_tender(client, engine, tender_id, item_master_id, qty=10)
        supplier_id = _quick_create_supplier(client, "Acme Co")

        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(supplier_id), f"rate__{item_id}": "25"},
            follow_redirects=False,
        )
        client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={"supplier_id": str(supplier_id), f"rate__{item_id}": ""},
            follow_redirects=False,
        )

        with Session(engine) as session:
            quote = session.exec(
                select(Quote).where(Quote.item_id == item_id, Quote.supplier_id == supplier_id)
            ).one()
            assert quote.rate is None
    finally:
        app.dependency_overrides.clear()


def test_multiple_items_saved_in_one_submission():
    client, engine = _make_client()
    try:
        tender_id = _create_tender(client)
        im1 = _create_item_master(engine, "X-1", "Widget A")
        im2 = _create_item_master(engine, "X-2", "Widget B")
        item1_id = _add_item_to_tender(client, engine, tender_id, im1, qty=10)
        item2_id = _add_item_to_tender(client, engine, tender_id, im2, qty=5)
        supplier_id = _quick_create_supplier(client, "Acme Co")

        resp = client.post(
            f"/tenders/{tender_id}/quote-entry",
            data={
                "supplier_id": str(supplier_id),
                f"rate__{item1_id}": "25",
                f"rate__{item2_id}": "40",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with Session(engine) as session:
            quotes = {
                q.item_id: q.rate
                for q in session.exec(select(Quote).where(Quote.supplier_id == supplier_id)).all()
            }
            assert quotes == {item1_id: 25, item2_id: 40}
    finally:
        app.dependency_overrides.clear()
