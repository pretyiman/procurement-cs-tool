from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.excel_io import get_or_create_item_master
from app.main import app
from app.models import Item, TenderTemplate, TenderTemplateItem

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


def test_save_as_template_then_create_tender_copies_items_not_quotes():
    client, engine = _make_client()
    try:
        with Session(engine) as session:
            im1 = get_or_create_item_master(session, "X-1", "Widget A", "Nos")[0]
            im2 = get_or_create_item_master(session, "X-2", "Widget B", "Kg")[0]
            session.commit()
            im1_id, im2_id = im1.id, im2.id

        resp = client.post("/tenders", data={"inquiry_no": "Source Tender"}, follow_redirects=False)
        source_id = int(resp.headers["location"].rsplit("/", 1)[-1])

        client.post(f"/tenders/{source_id}/items", data={"item_master_id": str(im1_id), "qty": "10"})
        client.post(f"/tenders/{source_id}/items", data={"item_master_id": str(im2_id), "qty": "25"})
        # A quote too, to prove quotes do NOT get copied into the template.
        client.post(
            f"/tenders/{source_id}/quote-entry",
            data={"item_master_id": str(im1_id), "qty": "10", "supplier_name": "Acme", "rate": "50"},
        )

        resp = client.post(
            f"/tenders/{source_id}/save-as-template", data={"name": "Quarterly Supplies"}, follow_redirects=False
        )
        assert resp.status_code == 303

        with Session(engine) as session:
            template = session.exec(select(TenderTemplate).where(TenderTemplate.name == "Quarterly Supplies")).one()
            lines = session.exec(select(TenderTemplateItem).where(TenderTemplateItem.template_id == template.id)).all()
            assert len(lines) == 2
            assert {line.item_master_id for line in lines} == {im1_id, im2_id}
            qty_by_item = {line.item_master_id: line.qty for line in lines}
            assert qty_by_item[im1_id] == 10
            assert qty_by_item[im2_id] == 25

        resp = client.post(
            "/templates/create-tender",
            data={"template_id": str(template.id), "inquiry_no": "New Tender From Template", "tax_percent": "18"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        new_tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])

        with Session(engine) as session:
            new_items = session.exec(select(Item).where(Item.tender_id == new_tender_id)).all()
            assert len(new_items) == 2
            assert {i.item_master_id for i in new_items} == {im1_id, im2_id}
            qty_by_item = {i.item_master_id: i.qty for i in new_items}
            assert qty_by_item[im1_id] == 10
            assert qty_by_item[im2_id] == 25
            # No quotes carried over - the new tender's items start un-quoted.
            for item in new_items:
                assert item.quotes == []
    finally:
        app.dependency_overrides.clear()


def test_delete_template():
    client, engine = _make_client()
    try:
        with Session(engine) as session:
            im = get_or_create_item_master(session, "X-1", "Widget", "Nos")[0]
            session.commit()
            im_id = im.id

        resp = client.post("/tenders", data={"inquiry_no": "T"}, follow_redirects=False)
        tender_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        client.post(f"/tenders/{tender_id}/items", data={"item_master_id": str(im_id), "qty": "5"})
        client.post(f"/tenders/{tender_id}/save-as-template", data={"name": "To Delete"})

        with Session(engine) as session:
            template = session.exec(select(TenderTemplate).where(TenderTemplate.name == "To Delete")).one()
            template_id = template.id

        resp = client.post(f"/templates/{template_id}/delete", follow_redirects=False)
        assert resp.status_code == 303

        with Session(engine) as session:
            assert session.get(TenderTemplate, template_id) is None
            remaining_lines = session.exec(
                select(TenderTemplateItem).where(TenderTemplateItem.template_id == template_id)
            ).all()
            assert remaining_lines == []
    finally:
        app.dependency_overrides.clear()
