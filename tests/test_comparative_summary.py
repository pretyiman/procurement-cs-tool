import re

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.main import app
from app.models import Item, ItemMaster

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


def _quote(client, tender_id, supplier_name, rates_by_item_id):
    supplier_id = client.post("/suppliers/quick-create", data={"name": supplier_name}).json()["id"]
    data = {"supplier_id": str(supplier_id)}
    for item_id, rate in rates_by_item_id.items():
        data[f"rate__{item_id}"] = str(rate)
    client.post(f"/tenders/{tender_id}/quote-entry", data=data)
    return supplier_id


def _tender_with_items(client, engine, inquiry_no, part_nos):
    resp = client.post("/tenders", data={"inquiry_no": inquiry_no}, follow_redirects=False)
    tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
    for part_no in part_nos:
        with Session(engine) as session:
            im = ItemMaster(part_no=part_no, description=f"Widget {part_no}", default_unit="Nos")
            session.add(im)
            session.commit()
            session.refresh(im)
            item_master_id = im.id
        client.post(f"/tenders/{tender_id}/items", data={"item_master_id": str(item_master_id), "qty": "1"})
    with Session(engine) as session:
        item_ids = [
            i.id for i in session.exec(select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)).all()
        ]
    return tender_id, item_ids


def test_package_view_does_not_falsely_flag_a_tie_for_the_sole_top_supplier():
    """Regression test: tied_package_supplier_ids naturally contains the
    single top supplier even when nobody else matches its value - the
    per-row TIE badge must check list length, not just membership, or
    every package leader gets wrongly flagged as tied with itself."""
    client, engine = _make_client()
    try:
        tender_id, item_ids = _tender_with_items(client, engine, "No Tie Test", ["A-1"])
        _quote(client, tender_id, "Cheaper Firm", {item_ids[0]: 50})
        _quote(client, tender_id, "Pricier Firm", {item_ids[0]: 60})

        resp = client.get(f"/tenders/{tender_id}/comparative-summary?view=package")
        assert resp.status_code == 200
        assert "LOWEST PACKAGE" in resp.text
        # Baseline of 1 is unavoidable and not evidence of a real tie: the
        # leaderboard's own explanatory copy has one literal example badge.
        # More than that means a package row was actually (wrongly) flagged.
        assert resp.text.count('<span class="badge badge-tie">TIE</span>') == 1
    finally:
        app.dependency_overrides.clear()


def test_package_view_flags_a_genuine_tie_on_both_suppliers():
    client, engine = _make_client()
    try:
        tender_id, item_ids = _tender_with_items(client, engine, "Real Tie Test", ["A-1"])
        _quote(client, tender_id, "Firm A", {item_ids[0]: 50})
        _quote(client, tender_id, "Firm B", {item_ids[0]: 50})

        resp = client.get(f"/tenders/{tender_id}/comparative-summary?view=package")
        assert resp.status_code == 200
        # Baseline of 1 (leaderboard's explanatory copy) + 2 for the tied firms' rows.
        assert resp.text.count('<span class="badge badge-tie">TIE</span>') >= 3
    finally:
        app.dependency_overrides.clear()


def test_item_view_tie_badge_and_stats_bar():
    client, engine = _make_client()
    try:
        tender_id, item_ids = _tender_with_items(client, engine, "Item Tie Test", ["A-1", "A-2"])
        _quote(client, tender_id, "Firm A", {item_ids[0]: 100, item_ids[1]: 100})
        _quote(client, tender_id, "Firm B", {item_ids[0]: 100, item_ids[1]: 200})

        resp = client.get(f"/tenders/{tender_id}/comparative-summary")
        assert resp.status_code == 200
        assert "TIE" in resp.text
        # Stats bar: 2 items, 2 suppliers, both full bidders, 0 unresolved, 1 tied item.
        assert '<div class="value">2</div><div class="label">Items</div>' in resp.text
        assert '<div class="value">2</div><div class="label">Suppliers</div>' in resp.text
        assert resp.text.count('<div class="label" title="Suppliers who quoted every item on this RFQ">Full bidders</div>') == 1
        assert '<div class="value">2</div><div class="label" title="Suppliers who quoted every item on this RFQ">Full bidders</div>' in resp.text
        assert '<div class="value">1</div><div class="label">Tied items</div>' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_leaderboard_partitions_items_correctly():
    client, engine = _make_client()
    try:
        tender_id, item_ids = _tender_with_items(client, engine, "Leaderboard Test", ["A-1", "A-2", "A-3"])
        _quote(client, tender_id, "Firm A", {item_ids[0]: 10, item_ids[1]: 10, item_ids[2]: 100})
        _quote(client, tender_id, "Firm B", {item_ids[0]: 20, item_ids[1]: 20, item_ids[2]: 5})

        resp = client.get(f"/tenders/{tender_id}/comparative-summary")
        assert resp.status_code == 200

        # Leaderboard: Firm A wins 2 items, Firm B wins 1.
        assert re.search(r"Firm A.*?<td class=\"num\">2</td>", resp.text, re.S)
        assert re.search(r"Firm B.*?<td class=\"num\">1</td>", resp.text, re.S)
    finally:
        app.dependency_overrides.clear()


def test_sourcing_options_bundle_cards_render_and_pick_the_best_value():
    """Firm A is cheaper on 2 items, Firm B on 1 - the size-1 bundle is
    whichever of them is cheapest alone (Firm B: 20+20+5=45), but the
    size-2 bundle (both combined, cheapest-per-item within just the two)
    is far cheaper still (10+10+5=25) and should be marked BEST VALUE."""
    client, engine = _make_client()
    try:
        tender_id, item_ids = _tender_with_items(client, engine, "Bundle Card Test", ["A-1", "A-2", "A-3"])
        _quote(client, tender_id, "Firm A", {item_ids[0]: 10, item_ids[1]: 10, item_ids[2]: 100})
        _quote(client, tender_id, "Firm B", {item_ids[0]: 20, item_ids[1]: 20, item_ids[2]: 5})

        resp = client.get(f"/tenders/{tender_id}/comparative-summary")
        assert resp.status_code == 200
        assert "Sourcing Options" in resp.text
        assert "1 Supplier" in resp.text
        assert "2 Suppliers" in resp.text
        assert "Covers all 3 items" in resp.text
        # Size 1 (Firm B alone): store 45 * 1.18 tax = 53.10. Size 2 (A+B
        # combined, cheapest per item within the pair): store 25 * 1.18 = 29.50.
        assert "53.10" in resp.text
        assert "29.50" in resp.text
        assert "BEST VALUE" in resp.text
        assert re.search(r"BEST VALUE.*?29\.50", resp.text, re.S)

        # Adjustable: a custom size beyond the default 1-5 range works too.
        resp = client.get(f"/tenders/{tender_id}/comparative-summary?bundle_sizes=1,2")
        assert resp.status_code == 200
        assert "29.50" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_bundle_size_input_is_prefilled_and_adjustable():
    client, engine = _make_client()
    try:
        tender_id, item_ids = _tender_with_items(client, engine, "Adjustable Bundle Test", ["A-1"])
        _quote(client, tender_id, "Firm A", {item_ids[0]: 10})
        _quote(client, tender_id, "Firm B", {item_ids[0]: 20})

        resp = client.get(f"/tenders/{tender_id}/comparative-summary")
        assert resp.status_code == 200
        assert 'name="bundle_sizes"' in resp.text
        assert 'value="1,2"' in resp.text  # only 2 quoting suppliers - defaults cap there
    finally:
        app.dependency_overrides.clear()


def test_package_top_n_links_only_appear_when_more_than_five_suppliers():
    client, engine = _make_client()
    try:
        tender_id, item_ids = _tender_with_items(client, engine, "Small Supplier Set", ["A-1"])
        _quote(client, tender_id, "Firm A", {item_ids[0]: 10})
        _quote(client, tender_id, "Firm B", {item_ids[0]: 20})

        resp = client.get(f"/tenders/{tender_id}/comparative-summary?view=package")
        assert "Top 5" not in resp.text

        for i in range(6):
            _quote(client, tender_id, f"Firm {i}", {item_ids[0]: 30 + i})

        resp = client.get(f"/tenders/{tender_id}/comparative-summary?view=package")
        assert "Top 5" in resp.text
        assert "package_limit=5" in resp.text

        resp = client.get(f"/tenders/{tender_id}/comparative-summary?view=package&package_limit=5")
        # 8 fully-quoting suppliers total (2 + 6) - each row's "Items Quoted"
        # column reads "1/1" for this single-item tender, so counting that
        # cell directly verifies only 5 rows rendered, not all 8.
        assert resp.text.count("1/1") == 5
        assert "Showing 5 of 8" in resp.text
    finally:
        app.dependency_overrides.clear()
