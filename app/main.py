import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .award_engine import build_purchase_proposal, resolve_awarded_items, validate_override
from .cs_engine import build_comparative_statement
from .db import create_db_and_tables, get_session
from .excel_io import export_purchase_proposal_xlsx, get_or_create_supplier, import_tender
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


# ---------------------------------------------------------------------------
# Award review (default-to-lowest + manual override) and Purchase Proposal
# ---------------------------------------------------------------------------


@app.get("/tenders/{tender_id}/award", response_class=HTMLResponse)
def award_review(tender_id: int, request: Request, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    awarded_items, cs = resolve_awarded_items(session, tender_id)
    lowest_by_item_id = {r.item.id: r for r in cs.item_results}

    item_ids = [ai.item.id for ai in awarded_items]
    quotes = (
        session.exec(select(Quote).where(Quote.item_id.in_(item_ids))).all() if item_ids else []
    )
    options_by_item: dict = {}
    for q in quotes:
        if q.rate is None:
            continue
        supplier = cs.suppliers_by_id.get(q.supplier_id)
        name = supplier.name if supplier else f"Supplier {q.supplier_id}"
        options_by_item.setdefault(q.item_id, []).append((q.supplier_id, name, q.rate))
    for opts in options_by_item.values():
        opts.sort(key=lambda o: o[2])

    rows = []
    for ai in awarded_items:
        result = lowest_by_item_id[ai.item.id]
        rows.append(
            {
                "item": ai.item,
                "lowest_name": cs.suppliers_by_id[result.lowest_supplier_id].name
                if result.lowest_supplier_id
                else None,
                "lowest_rate": result.lowest_rate,
                "awarded_supplier_id": ai.awarded_supplier_id,
                "awarded_name": cs.suppliers_by_id[ai.awarded_supplier_id].name
                if ai.awarded_supplier_id
                else None,
                "awarded_rate": ai.awarded_rate,
                "is_override": ai.is_override,
                "invalid_override": ai.invalid_override,
                "override_reason": ai.override_reason,
                "options": options_by_item.get(ai.item.id, []),
            }
        )

    return templates.TemplateResponse(request, "award_review.html", {"tender": tender, "rows": rows})


@app.post("/tenders/{tender_id}/items/{item_id}/award")
def set_award_override(
    tender_id: int,
    item_id: int,
    awarded_supplier_id: str = Form(""),
    award_reason: str = Form(""),
    session: Session = Depends(get_session),
):
    item = session.get(Item, item_id)
    if item is None or item.tender_id != tender_id:
        raise HTTPException(404, "Item not found")

    cs = build_comparative_statement(session, tender_id)
    result = next((r for r in cs.item_results if r.item.id == item_id), None)
    if result is None:
        raise HTTPException(404, "Item not found in this tender's comparative statement")

    quotes = session.exec(select(Quote).where(Quote.item_id == item_id)).all()
    rate_map = {q.supplier_id: q.rate for q in quotes if q.rate is not None}

    item.awarded_supplier_id = int(awarded_supplier_id) if awarded_supplier_id.strip() else None
    item.award_reason = award_reason.strip() or None

    try:
        validate_override(item, result, rate_map)
    except ValueError as e:
        session.rollback()  # discard the in-memory attribute changes above
        raise HTTPException(400, str(e))

    session.add(item)
    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}/award", status_code=303)


@app.get("/tenders/{tender_id}/proposal", response_class=HTMLResponse)
def purchase_proposal_view(tender_id: int, request: Request, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    proposal = build_purchase_proposal(session, tender_id)
    return templates.TemplateResponse(request, "purchase_proposal.html", {"tender": tender, "proposal": proposal})


@app.get("/tenders/{tender_id}/proposal/export")
def export_proposal(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    proposal = build_purchase_proposal(session, tender_id)
    content = export_purchase_proposal_xlsx(proposal)
    filename = f"purchase-proposal-tender-{tender_id}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
