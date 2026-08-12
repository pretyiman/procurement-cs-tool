import shutil
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from sqlmodel import Session, select

from .db import create_db_and_tables, get_session
from .excel_io import import_tender
from .models import Item, Quote, Supplier, Tender

app = FastAPI(title="Procurement Comparative Statement & Award Tool")


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()


@app.get("/")
def root():
    return {"status": "ok", "app": "procurement-cs-tool"}


@app.post("/tenders/import")
async def import_tender_endpoint(
    file: UploadFile = File(...), session: Session = Depends(get_session)
):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Expected an .xlsx file")

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        tender = import_tender(tmp_path, session)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return {"tender_id": tender.id, "inquiry_no": tender.inquiry_no}


@app.get("/tenders/{tender_id}")
def get_tender(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "Tender not found")

    items = session.exec(select(Item).where(Item.tender_id == tender.id)).all()
    item_ids = [i.id for i in items]
    quotes = (
        session.exec(select(Quote).where(Quote.item_id.in_(item_ids))).all()
        if item_ids
        else []
    )
    supplier_ids = {q.supplier_id for q in quotes}
    suppliers = (
        session.exec(select(Supplier).where(Supplier.id.in_(supplier_ids))).all()
        if supplier_ids
        else []
    )

    return {
        "id": tender.id,
        "inquiry_no": tender.inquiry_no,
        "gst_percent": tender.gst_percent,
        "status": tender.status,
        "item_count": len(items),
        "supplier_count": len(suppliers),
        "quote_count": len(quotes),
        "suppliers": [s.name for s in suppliers],
    }
