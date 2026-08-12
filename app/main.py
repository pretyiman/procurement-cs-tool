import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .cs_engine import build_comparative_statement
from .db import create_db_and_tables, get_session
from .excel_io import get_or_create_supplier, import_tender
from .models import Item, Quote, Supplier, Tender, TenderStatus

app = FastAPI(title="Procurement Comparative Statement & Award Tool")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()


@app.get("/health")
def health():
    return {"status": "ok", "app": "procurement-cs-tool"}


# ---------------------------------------------------------------------------
# Home / tender list
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    tenders = session.exec(select(Tender).order_by(Tender.id.desc())).all()
    return templates.TemplateResponse(request, "home.html", {"tenders": tenders})


# ---------------------------------------------------------------------------
# Create / import tenders
# NOTE: these literal routes must be registered before GET /tenders/{tender_id}
# so "new" doesn't get swallowed as a tender_id path param.
# ---------------------------------------------------------------------------


@app.get("/tenders/new", response_class=HTMLResponse)
def new_tender_form(request: Request):
    return templates.TemplateResponse(request, "tender_new.html", {})


@app.post("/tenders")
def create_tender(
    inquiry_no: str = Form(...),
    gst_percent: str = Form("18"),
    session: Session = Depends(get_session),
):
    try:
        gst = float(gst_percent)
    except ValueError:
        raise HTTPException(400, "GST % must be a number")

    tender = Tender(inquiry_no=inquiry_no.strip(), gst_percent=gst, status=TenderStatus.draft)
    session.add(tender)
    session.commit()
    session.refresh(tender)
    return RedirectResponse(f"/tenders/{tender.id}", status_code=303)


def _import_uploaded_file(file: UploadFile, session: Session) -> Tender:
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Expected an .xlsx file")
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    try:
        return import_tender(tmp_path, session)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.post("/tenders/import")
async def import_tender_api(file: UploadFile = File(...), session: Session = Depends(get_session)):
    tender = _import_uploaded_file(file, session)
    return {"tender_id": tender.id, "inquiry_no": tender.inquiry_no}


@app.post("/tenders/import-ui")
async def import_tender_ui(file: UploadFile = File(...), session: Session = Depends(get_session)):
    tender = _import_uploaded_file(file, session)
    return RedirectResponse(f"/tenders/{tender.id}", status_code=303)


# ---------------------------------------------------------------------------
# Tender detail: items, suppliers, quote grid, live CS
# ---------------------------------------------------------------------------


@app.get("/tenders/{tender_id}", response_class=HTMLResponse)
def tender_detail(tender_id: int, request: Request, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    items = session.exec(
        select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)
    ).all()
    item_ids = [i.id for i in items]

    quotes = (
        session.exec(select(Quote).where(Quote.item_id.in_(item_ids))).all() if item_ids else []
    )
    rate_matrix = {(q.item_id, q.supplier_id): q.rate for q in quotes}

    cs = build_comparative_statement(session, tender_id)
    attached_suppliers = sorted(cs.suppliers_by_id.values(), key=lambda s: s.name)
    all_supplier_names = [
        s.name for s in session.exec(select(Supplier).order_by(Supplier.name)).all()
    ]

    item_rows = []
    for r in cs.item_results:
        lowest_name = cs.suppliers_by_id[r.lowest_supplier_id].name if r.lowest_supplier_id else None
        item_rows.append(
            {
                "item": r.item,
                "lowest_supplier_id": r.lowest_supplier_id,
                "lowest_name": lowest_name,
                "lowest_rate": r.lowest_rate,
                "total_value": r.total_value,
                "inc_dec_pct": r.inc_dec_pct,
            }
        )

    next_ser = (max((i.ser for i in items), default=0)) + 1

    return templates.TemplateResponse(
        request,
        "tender_detail.html",
        {
            "tender": tender,
            "suppliers": attached_suppliers,
            "all_supplier_names": all_supplier_names,
            "rate_matrix": rate_matrix,
            "item_rows": item_rows,
            "firm_summaries": cs.firm_summaries,
            "grand_total": cs.grand_total,
            "next_ser": next_ser,
        },
    )


@app.post("/tenders/{tender_id}/items")
def add_item(
    tender_id: int,
    ser: str = Form(...),
    part_no: str = Form(""),
    description: str = Form(...),
    unit: str = Form(""),
    qty: str = Form(...),
    lpr: str = Form(""),
    session: Session = Depends(get_session),
):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    try:
        ser_val = int(ser)
        qty_val = float(qty)
        lpr_val: Optional[float] = float(lpr) if lpr.strip() else None
    except ValueError:
        raise HTTPException(400, "Ser/Qty/LPR must be numeric")

    existing_items = session.exec(select(Item).where(Item.tender_id == tender_id)).all()
    existing_item_ids = [i.id for i in existing_items]
    attached_supplier_ids = (
        {q.supplier_id for q in session.exec(select(Quote).where(Quote.item_id.in_(existing_item_ids))).all()}
        if existing_item_ids
        else set()
    )

    item = Item(
        tender_id=tender_id,
        ser=ser_val,
        part_no=part_no.strip(),
        description=description.strip(),
        unit=unit.strip(),
        qty=qty_val,
        lpr=lpr_val,
    )
    session.add(item)
    session.flush()

    for supplier_id in attached_supplier_ids:
        session.add(Quote(item_id=item.id, supplier_id=supplier_id, rate=None))

    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}", status_code=303)


@app.post("/tenders/{tender_id}/suppliers")
def attach_supplier(
    tender_id: int,
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, "Supplier name is required")

    supplier = get_or_create_supplier(session, name)

    items = session.exec(select(Item).where(Item.tender_id == tender_id)).all()
    already_quoted_item_ids = {
        q.item_id
        for q in session.exec(select(Quote).where(Quote.supplier_id == supplier.id)).all()
    }
    for item in items:
        if item.id not in already_quoted_item_ids:
            session.add(Quote(item_id=item.id, supplier_id=supplier.id, rate=None))

    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}", status_code=303)


@app.post("/tenders/{tender_id}/quotes")
async def save_quotes(tender_id: int, request: Request, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    form = await request.form()
    for key, value in form.multi_items():
        if not key.startswith("rate__"):
            continue
        _, item_id_str, supplier_id_str = key.split("__")
        item_id, supplier_id = int(item_id_str), int(supplier_id_str)

        text = str(value).strip()
        if text == "" or text.upper() == "NQ":
            rate = None
        else:
            try:
                rate = float(text)
            except ValueError:
                raise HTTPException(400, f"Invalid rate '{text}' for item {item_id}")

        quote = session.exec(
            select(Quote).where(Quote.item_id == item_id, Quote.supplier_id == supplier_id)
        ).first()
        if quote is None:
            session.add(Quote(item_id=item_id, supplier_id=supplier_id, rate=rate))
        else:
            quote.rate = rate
            session.add(quote)

    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}#cs-view", status_code=303)
