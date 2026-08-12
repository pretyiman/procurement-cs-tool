import datetime
import json
import shutil
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .award_engine import build_purchase_proposal, resolve_awarded_items, validate_override
from .cs_engine import build_comparative_statement
from .db import create_db_and_tables, get_session
from .docx_export import generate_contract_draft
from .lpr_history import get_last_purchase_rate
from .paths import resource_path
from .excel_io import (
    export_cs_xlsx,
    export_purchase_proposal_xlsx,
    get_or_create_item_master,
    get_or_create_supplier,
    import_tender,
)
from .models import Item, ItemMaster, Quote, Supplier, TaxType, Tender, TenderStatus

app = FastAPI(title="Procurement Comparative Statement & Award Tool")

templates = Jinja2Templates(directory=str(resource_path("templates")))


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()


def _json_for_script(data) -> str:
    """json.dumps, safe to embed inside a <script> tag (escapes a literal
    "</" so an item description etc. can never accidentally close the
    surrounding script element)."""
    return json.dumps(data).replace("</", "<\\/")


@app.get("/health")
def health():
    return {"status": "ok", "app": "procurement-cs-tool"}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    tenders = session.exec(select(Tender).order_by(Tender.id.desc())).all()
    status_counts = {"draft": 0, "proposal_generated": 0, "awarded": 0}
    for t in tenders:
        status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1

    item_count = len(session.exec(select(ItemMaster)).all())
    supplier_count = len(session.exec(select(Supplier)).all())

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "tender_count": len(tenders),
            "status_counts": status_counts,
            "item_count": item_count,
            "supplier_count": supplier_count,
            "recent_tenders": tenders[:8],
        },
    )


# ---------------------------------------------------------------------------
# Item catalog (reusable across tenders)
# ---------------------------------------------------------------------------


@app.get("/items", response_class=HTMLResponse)
def items_catalog(request: Request, q: str = "", session: Session = Depends(get_session)):
    catalog = session.exec(select(ItemMaster)).all()
    if q.strip():
        needle = q.strip().lower()
        catalog = [
            im for im in catalog if needle in im.part_no.lower() or needle in im.description.lower()
        ]
    catalog.sort(key=lambda im: (im.part_no, im.description))
    return templates.TemplateResponse(request, "items.html", {"items": catalog, "q": q})


@app.post("/items")
def create_item(
    part_no: str = Form(""),
    description: str = Form(...),
    default_unit: str = Form(""),
    session: Session = Depends(get_session),
):
    if not description.strip():
        raise HTTPException(400, "Description is required")
    get_or_create_item_master(session, part_no, description, default_unit)
    session.commit()
    return RedirectResponse("/items", status_code=303)


# ---------------------------------------------------------------------------
# Supplier catalog (reusable across tenders)
# ---------------------------------------------------------------------------


@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_catalog(request: Request, q: str = "", session: Session = Depends(get_session)):
    suppliers = session.exec(select(Supplier).order_by(Supplier.name)).all()
    if q.strip():
        needle = q.strip().lower()
        suppliers = [s for s in suppliers if needle in s.name.lower()]
    return templates.TemplateResponse(request, "suppliers.html", {"suppliers": suppliers, "q": q})


@app.post("/suppliers")
def create_supplier(
    name: str = Form(...),
    address: str = Form(""),
    contact_person: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    tax_no: str = Form(""),
    session: Session = Depends(get_session),
):
    if not name.strip():
        raise HTTPException(400, "Supplier name is required")
    supplier = get_or_create_supplier(session, name)
    for field, value in (
        ("address", address),
        ("contact_person", contact_person),
        ("phone", phone),
        ("email", email),
        ("tax_no", tax_no),
    ):
        if value.strip():
            setattr(supplier, field, value.strip())
    session.add(supplier)
    session.commit()
    return RedirectResponse("/suppliers", status_code=303)


@app.get("/suppliers/{supplier_id}", response_class=HTMLResponse)
def supplier_detail(supplier_id: int, request: Request, session: Session = Depends(get_session)):
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(404, "Supplier not found")
    return templates.TemplateResponse(request, "supplier_detail.html", {"supplier": supplier})


@app.post("/suppliers/{supplier_id}")
def update_supplier(
    supplier_id: int,
    name: str = Form(...),
    address: str = Form(""),
    contact_person: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    tax_no: str = Form(""),
    session: Session = Depends(get_session),
):
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(404, "Supplier not found")
    if not name.strip():
        raise HTTPException(400, "Supplier name is required")
    supplier.name = name.strip()
    supplier.address = address.strip() or None
    supplier.contact_person = contact_person.strip() or None
    supplier.phone = phone.strip() or None
    supplier.email = email.strip() or None
    supplier.tax_no = tax_no.strip() or None
    session.add(supplier)
    session.commit()
    return RedirectResponse(f"/suppliers/{supplier_id}", status_code=303)


# ---------------------------------------------------------------------------
# Tenders: list, create, import
# NOTE: these literal routes must be registered before GET /tenders/{tender_id}
# so "new" doesn't get swallowed as a tender_id path param.
# ---------------------------------------------------------------------------


@app.get("/tenders", response_class=HTMLResponse)
def tenders_list(request: Request, session: Session = Depends(get_session)):
    tenders = session.exec(select(Tender).order_by(Tender.id.desc())).all()
    return templates.TemplateResponse(request, "tenders_list.html", {"tenders": tenders})


@app.get("/tenders/new", response_class=HTMLResponse)
def new_tender_form(request: Request):
    return templates.TemplateResponse(request, "tender_new.html", {})


@app.post("/tenders")
def create_tender(
    inquiry_no: str = Form(...),
    tax_type: str = Form("GST"),
    tax_percent: str = Form("18"),
    session: Session = Depends(get_session),
):
    try:
        tax_pct = float(tax_percent)
    except ValueError:
        raise HTTPException(400, "Tax % must be a number")
    try:
        tax_type_val = TaxType(tax_type)
    except ValueError:
        raise HTTPException(400, "Tax type must be GST or PST")

    tender = Tender(
        inquiry_no=inquiry_no.strip(),
        tax_type=tax_type_val,
        tax_percent=tax_pct,
        status=TenderStatus.draft,
    )
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

    catalog_items = session.exec(select(ItemMaster)).all()
    catalog_items.sort(key=lambda im: (im.part_no, im.description))
    catalog_items_json = _json_for_script(
        [{"id": im.id, "label": f"{im.part_no} - {im.description} ({im.default_unit})"} for im in catalog_items]
    )
    supplier_names_json = _json_for_script([{"id": n, "label": n} for n in all_supplier_names])

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
            "catalog_items_json": catalog_items_json,
            "supplier_names_json": supplier_names_json,
        },
    )


@app.get("/tenders/{tender_id}/export")
def export_cs(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    cs = build_comparative_statement(session, tender_id)
    content = export_cs_xlsx(cs)
    filename = f"comparative-statement-tender-{tender_id}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/tenders/{tender_id}/items")
def add_item(
    tender_id: int,
    item_master_id: str = Form(...),
    qty: str = Form(...),
    lpr: str = Form(""),
    session: Session = Depends(get_session),
):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    try:
        item_master_id_val = int(item_master_id)
        qty_val = float(qty)
        lpr_val: Optional[float] = float(lpr) if lpr.strip() else None
    except ValueError:
        raise HTTPException(400, "Item/Qty/LPR must be valid")

    item_master = session.get(ItemMaster, item_master_id_val)
    if item_master is None:
        raise HTTPException(400, "Unknown catalog item")

    if lpr_val is None:
        lpr_val = get_last_purchase_rate(session, item_master_id_val, exclude_tender_id=tender_id)

    existing_items = session.exec(select(Item).where(Item.tender_id == tender_id)).all()
    if any(i.item_master_id == item_master_id_val for i in existing_items):
        raise HTTPException(400, "This item is already on this tender")

    next_ser = (max((i.ser for i in existing_items), default=0)) + 1
    existing_item_ids = [i.id for i in existing_items]
    attached_supplier_ids = (
        {q.supplier_id for q in session.exec(select(Quote).where(Quote.item_id.in_(existing_item_ids))).all()}
        if existing_item_ids
        else set()
    )

    item = Item(
        tender_id=tender_id,
        item_master_id=item_master_id_val,
        ser=next_ser,
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


def _ensure_full_grid(session: Session, tender_id: int) -> None:
    """Backfill a blank (NQ) Quote row for every (item, supplier) pair that
    doesn't have one yet, so the grid view (tender_detail.html) stays
    rectangular regardless of which entry path (grid vs quote-entry) added
    data. Doesn't affect cs_engine's calculations either way - a missing
    row and a rate=None row are equivalent there."""
    items = session.exec(select(Item).where(Item.tender_id == tender_id)).all()
    if not items:
        return
    item_ids = [i.id for i in items]
    quotes = session.exec(select(Quote).where(Quote.item_id.in_(item_ids))).all()
    supplier_ids = {q.supplier_id for q in quotes}
    existing_pairs = {(q.item_id, q.supplier_id) for q in quotes}
    for item in items:
        for supplier_id in supplier_ids:
            if (item.id, supplier_id) not in existing_pairs:
                session.add(Quote(item_id=item.id, supplier_id=supplier_id, rate=None))


# ---------------------------------------------------------------------------
# Guided quotation entry: one supplier's price for one item at a time
# ---------------------------------------------------------------------------


@app.get("/tenders/{tender_id}/quote-entry", response_class=HTMLResponse)
def quote_entry_form(tender_id: int, request: Request, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    catalog_items = session.exec(select(ItemMaster)).all()
    catalog_items.sort(key=lambda im: (im.part_no, im.description))

    lines = session.exec(select(Item).where(Item.tender_id == tender_id)).all()
    qty_by_item_master = {line.item_master_id: line.qty for line in lines}
    lines_by_id = {line.id: line for line in lines}

    all_supplier_names = [
        s.name for s in session.exec(select(Supplier).order_by(Supplier.name)).all()
    ]
    suppliers_by_id = {s.id: s for s in session.exec(select(Supplier)).all()}

    line_ids = list(lines_by_id.keys())
    quotes = (
        session.exec(select(Quote).where(Quote.item_id.in_(line_ids))).all() if line_ids else []
    )
    recorded = []
    for q in quotes:
        if q.rate is None:
            continue
        line = lines_by_id[q.item_id]
        recorded.append(
            {
                "ser": line.ser,
                "part_no": line.item_master.part_no,
                "description": line.item_master.description,
                "supplier_name": suppliers_by_id[q.supplier_id].name,
                "qty": line.qty,
                "rate": q.rate,
                "total": line.qty * q.rate,
            }
        )
    recorded.sort(key=lambda r: (r["ser"], r["supplier_name"]))

    catalog_items_json = _json_for_script(
        [
            {
                "id": im.id,
                "label": f"{im.part_no} - {im.description}",
                "unit": im.default_unit,
                "qty": qty_by_item_master.get(im.id, ""),
            }
            for im in catalog_items
        ]
    )
    supplier_names_json = _json_for_script([{"id": n, "label": n} for n in all_supplier_names])

    return templates.TemplateResponse(
        request,
        "quote_entry.html",
        {
            "tender": tender,
            "catalog_items_json": catalog_items_json,
            "supplier_names_json": supplier_names_json,
            "recorded": recorded,
        },
    )


@app.post("/tenders/{tender_id}/quote-entry")
def submit_quote_entry(
    tender_id: int,
    item_master_id: str = Form(...),
    qty: str = Form(...),
    supplier_name: str = Form(...),
    rate: str = Form(...),
    session: Session = Depends(get_session),
):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    try:
        item_master_id_val = int(item_master_id)
        qty_val = float(qty)
        rate_val = float(rate)
    except ValueError:
        raise HTTPException(400, "Item/Qty/Rate must be valid")

    item_master = session.get(ItemMaster, item_master_id_val)
    if item_master is None:
        raise HTTPException(400, "Unknown catalog item")
    if not supplier_name.strip():
        raise HTTPException(400, "Supplier is required")

    existing_lines = session.exec(select(Item).where(Item.tender_id == tender_id)).all()
    line = next((i for i in existing_lines if i.item_master_id == item_master_id_val), None)
    if line is None:
        next_ser = (max((i.ser for i in existing_lines), default=0)) + 1
        lpr_val = get_last_purchase_rate(session, item_master_id_val, exclude_tender_id=tender_id)
        line = Item(
            tender_id=tender_id, item_master_id=item_master_id_val, ser=next_ser, qty=qty_val, lpr=lpr_val
        )
        session.add(line)
        session.flush()
    else:
        line.qty = qty_val
        session.add(line)

    supplier = get_or_create_supplier(session, supplier_name)

    quote = session.exec(
        select(Quote).where(Quote.item_id == line.id, Quote.supplier_id == supplier.id)
    ).first()
    if quote is None:
        session.add(Quote(item_id=line.id, supplier_id=supplier.id, rate=rate_val))
    else:
        quote.rate = rate_val
        session.add(quote)

    session.flush()
    _ensure_full_grid(session, tender_id)

    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}/quote-entry", status_code=303)


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
                "lowest_supplier_id": result.lowest_supplier_id,
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


@app.post("/tenders/{tender_id}/generate-proposal")
def generate_proposal(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    proposal = build_purchase_proposal(session, tender_id)
    if not proposal.firm_groups:
        raise HTTPException(400, "Award at least one item before generating the proposal")
    tender.status = TenderStatus.proposal_generated
    session.add(tender)
    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}/proposal", status_code=303)


@app.post("/tenders/{tender_id}/mark-awarded")
def mark_awarded(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    if tender.status != TenderStatus.proposal_generated:
        raise HTTPException(400, "Generate the proposal before finalizing the award")
    tender.status = TenderStatus.awarded
    tender.awarded_date = datetime.date.today()
    session.add(tender)
    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}/proposal", status_code=303)


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


def _safe_filename_part(name: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()


@app.get("/tenders/{tender_id}/proposal/contract/{supplier_id}")
def download_contract_draft(tender_id: int, supplier_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(404, "Supplier not found")

    proposal = build_purchase_proposal(session, tender_id)
    group = next((g for g in proposal.firm_groups if g.supplier_id == supplier_id), None)
    if group is None:
        raise HTTPException(400, "This supplier has no items awarded on this tender")

    content = generate_contract_draft(proposal.tender, group, supplier)
    filename = f"contract-draft-{_safe_filename_part(group.supplier_name)}.docx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/tenders/{tender_id}/proposal/contracts.zip")
def download_all_contract_drafts(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    proposal = build_purchase_proposal(session, tender_id)
    if not proposal.firm_groups:
        raise HTTPException(400, "No items have been awarded to any firm yet")

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for group in proposal.firm_groups:
            supplier = session.get(Supplier, group.supplier_id)
            content = generate_contract_draft(proposal.tender, group, supplier)
            zf.writestr(f"contract-draft-{_safe_filename_part(group.supplier_name)}.docx", content)

    filename = f"contract-drafts-tender-{tender_id}.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
