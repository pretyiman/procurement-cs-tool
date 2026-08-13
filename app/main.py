import datetime
import json
import shutil
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .award_engine import build_purchase_proposal, resolve_awarded_items, validate_override
from .cs_engine import build_comparative_statement
from .db import create_db_and_tables, get_session
from .docx_export import generate_contract_award, generate_purchase_proposal_doc
from .lpr_history import get_last_purchase_rate
from .paths import resource_path
from .excel_io import (
    export_cs_xlsx,
    export_purchase_proposal_xlsx,
    get_or_create_department,
    get_or_create_item_master,
    get_or_create_supplier,
    import_tender,
)
from .models import (
    Department,
    Item,
    ItemMaster,
    Quote,
    Supplier,
    TaxType,
    Tender,
    TenderStatus,
    TenderTemplate,
    TenderTemplateItem,
)

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
def items_catalog(
    request: Request, q: str = "", notice: str = "", name: str = "", session: Session = Depends(get_session)
):
    catalog = session.exec(select(ItemMaster)).all()
    if q.strip():
        needle = q.strip().lower()
        catalog = [
            im for im in catalog if needle in im.part_no.lower() or needle in im.description.lower()
        ]
    catalog.sort(key=lambda im: (im.part_no, im.description))
    return templates.TemplateResponse(
        request, "items.html", {"items": catalog, "q": q, "notice": notice, "notice_name": name}
    )


@app.post("/items")
def create_item(
    part_no: str = Form(""),
    description: str = Form(...),
    default_unit: str = Form(""),
    session: Session = Depends(get_session),
):
    if not description.strip():
        raise HTTPException(400, "Description is required")
    item_master, created = get_or_create_item_master(session, part_no, description, default_unit)
    session.commit()
    if not created:
        label = f"{item_master.part_no} - {item_master.description}" if item_master.part_no else item_master.description
        return RedirectResponse(f"/items?notice=exists&name={quote(label)}", status_code=303)
    return RedirectResponse("/items", status_code=303)


@app.post("/items/quick-create")
def quick_create_item(
    part_no: str = Form(""),
    description: str = Form(...),
    default_unit: str = Form(""),
    session: Session = Depends(get_session),
):
    """Same as create_item, but returns JSON instead of redirecting - used
    by the item search-select's "+" button so a page mid-way through
    filling out an RFQ/quote form doesn't lose its state to a navigation."""
    if not description.strip():
        raise HTTPException(400, "Description is required")
    item_master, created = get_or_create_item_master(session, part_no, description, default_unit)
    session.commit()
    return {
        "id": item_master.id,
        "existed": not created,
        "part_no": item_master.part_no,
        "description": item_master.description,
        "unit": item_master.default_unit,
    }


# ---------------------------------------------------------------------------
# Department catalog (reusable across tenders)
# ---------------------------------------------------------------------------


@app.get("/departments", response_class=HTMLResponse)
def departments_catalog(
    request: Request, q: str = "", notice: str = "", name: str = "", session: Session = Depends(get_session)
):
    departments = session.exec(select(Department).order_by(Department.name)).all()
    if q.strip():
        needle = q.strip().lower()
        departments = [d for d in departments if needle in d.name.lower()]
    return templates.TemplateResponse(
        request, "departments.html", {"departments": departments, "q": q, "notice": notice, "notice_name": name}
    )


@app.post("/departments")
def create_department(name: str = Form(...), session: Session = Depends(get_session)):
    if not name.strip():
        raise HTTPException(400, "Name is required")
    department, created = get_or_create_department(session, name)
    session.commit()
    if not created:
        return RedirectResponse(f"/departments?notice=exists&name={quote(department.name)}", status_code=303)
    return RedirectResponse("/departments", status_code=303)


@app.post("/departments/quick-create")
def quick_create_department(name: str = Form(...), session: Session = Depends(get_session)):
    """Same as create_department, but returns JSON - used by the department
    search-select's "+" button so the RFQ form doesn't lose its state."""
    if not name.strip():
        raise HTTPException(400, "Name is required")
    department, created = get_or_create_department(session, name)
    session.commit()
    return {"id": department.id, "name": department.name, "existed": not created}


# ---------------------------------------------------------------------------
# Supplier catalog (reusable across tenders)
# ---------------------------------------------------------------------------


@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_catalog(
    request: Request, q: str = "", notice: str = "", name: str = "", session: Session = Depends(get_session)
):
    suppliers = session.exec(select(Supplier).order_by(Supplier.name)).all()
    if q.strip():
        needle = q.strip().lower()
        suppliers = [s for s in suppliers if needle in s.name.lower()]
    return templates.TemplateResponse(
        request, "suppliers.html", {"suppliers": suppliers, "q": q, "notice": notice, "notice_name": name}
    )


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
    supplier, created = get_or_create_supplier(session, name)
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
    if not created:
        return RedirectResponse(f"/suppliers?notice=exists&name={quote(supplier.name)}", status_code=303)
    return RedirectResponse("/suppliers", status_code=303)


@app.post("/suppliers/quick-create")
def quick_create_supplier(name: str = Form(...), session: Session = Depends(get_session)):
    """Same as create_supplier, but returns JSON - used by the supplier
    search-select's "+" button on Quote Entry."""
    if not name.strip():
        raise HTTPException(400, "Supplier name is required")
    supplier, created = get_or_create_supplier(session, name)
    session.commit()
    return {"id": supplier.id, "name": supplier.name, "existed": not created}


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


# ---------------------------------------------------------------------------
# Tender templates: save a recurring tender's item list, reuse it later
# ---------------------------------------------------------------------------


@app.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request, session: Session = Depends(get_session)):
    tender_templates = session.exec(select(TenderTemplate).order_by(TenderTemplate.name)).all()
    rows = []
    for t in tender_templates:
        lines = session.exec(
            select(TenderTemplateItem).where(TenderTemplateItem.template_id == t.id)
        ).all()
        rows.append({"template": t, "item_count": len(lines)})
    return templates.TemplateResponse(request, "templates_list.html", {"rows": rows})


@app.post("/tenders/{tender_id}/save-as-template")
def save_as_template(tender_id: int, name: str = Form(...), session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    if not name.strip():
        raise HTTPException(400, "Template name is required")

    items = session.exec(
        select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)
    ).all()
    if not items:
        raise HTTPException(400, "This tender has no items to save")

    existing = session.exec(select(TenderTemplate).where(TenderTemplate.name == name.strip())).first()
    if existing is not None:
        raise HTTPException(400, "A template with that name already exists")

    template = TenderTemplate(name=name.strip())
    session.add(template)
    session.flush()
    for item in items:
        session.add(
            TenderTemplateItem(
                template_id=template.id, item_master_id=item.item_master_id, ser=item.ser, qty=item.qty
            )
        )
    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}", status_code=303)


@app.post("/templates/create-tender")
def create_tender_from_template(
    template_id: str = Form(...),
    inquiry_no: str = Form(...),
    department_id: str = Form(""),
    tax_type: str = Form("GST"),
    tax_percent: str = Form("18"),
    session: Session = Depends(get_session),
):
    try:
        template_id_val = int(template_id)
    except ValueError:
        raise HTTPException(400, "Pick a template")
    template = session.get(TenderTemplate, template_id_val)
    if template is None:
        raise HTTPException(404, "Template not found")
    try:
        tax_pct = float(tax_percent)
        tax_type_val = TaxType(tax_type)
    except ValueError:
        raise HTTPException(400, "Invalid tax type/percent")

    tender = Tender(
        inquiry_no=inquiry_no.strip(),
        department_id=int(department_id) if department_id.strip() else None,
        tax_type=tax_type_val,
        tax_percent=tax_pct,
        status=TenderStatus.draft,
    )
    session.add(tender)
    session.flush()

    lines = session.exec(
        select(TenderTemplateItem)
        .where(TenderTemplateItem.template_id == template_id_val)
        .order_by(TenderTemplateItem.ser)
    ).all()
    for line in lines:
        session.add(
            Item(tender_id=tender.id, item_master_id=line.item_master_id, ser=line.ser, qty=line.qty)
        )
    session.commit()
    session.refresh(tender)
    return RedirectResponse(f"/tenders/{tender.id}", status_code=303)


@app.post("/templates/{template_id}/delete")
def delete_template(template_id: int, session: Session = Depends(get_session)):
    template = session.get(TenderTemplate, template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    lines = session.exec(select(TenderTemplateItem).where(TenderTemplateItem.template_id == template_id)).all()
    for line in lines:
        session.delete(line)
    session.delete(template)
    session.commit()
    return RedirectResponse("/templates", status_code=303)


@app.get("/tenders/new", response_class=HTMLResponse)
def new_tender_form(request: Request, session: Session = Depends(get_session)):
    tender_templates = session.exec(select(TenderTemplate).order_by(TenderTemplate.name)).all()
    departments = session.exec(select(Department).order_by(Department.name)).all()
    departments_json = _json_for_script([{"id": d.id, "label": d.name} for d in departments])
    return templates.TemplateResponse(
        request,
        "tender_new.html",
        {
            "tender_templates": tender_templates,
            "departments_json": departments_json,
        },
    )


@app.post("/tenders")
def create_tender(
    inquiry_no: str = Form(...),
    department_id: str = Form(""),
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
        department_id=int(department_id) if department_id.strip() else None,
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

    catalog_items = session.exec(select(ItemMaster)).all()
    catalog_items.sort(key=lambda im: (im.part_no, im.description))
    catalog_items_json = _json_for_script(
        [{"id": im.id, "label": f"{im.part_no} - {im.description} ({im.default_unit})"} for im in catalog_items]
    )

    return templates.TemplateResponse(
        request,
        "tender_detail.html",
        {
            "tender": tender,
            "items": items,
            "catalog_items_json": catalog_items_json,
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


@app.post("/tenders/{tender_id}/items/{item_id}/delete")
def delete_item(tender_id: int, item_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    item = session.get(Item, item_id)
    if item is None or item.tender_id != tender_id:
        raise HTTPException(404, "Item not found on this RFQ")

    for quote in session.exec(select(Quote).where(Quote.item_id == item_id)).all():
        session.delete(quote)
    session.delete(item)
    session.flush()

    remaining = session.exec(
        select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)
    ).all()
    for new_ser, remaining_item in enumerate(remaining, start=1):
        if remaining_item.ser != new_ser:
            remaining_item.ser = new_ser
            session.add(remaining_item)

    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}#items", status_code=303)


@app.post("/tenders/{tender_id}/items/save-quantities")
async def save_item_quantities(tender_id: int, request: Request, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    form = await request.form()
    for key, value in form.multi_items():
        if not key.startswith("qty__"):
            continue
        _, item_id_str = key.split("__")
        item_id = int(item_id_str)

        try:
            qty = float(str(value).strip())
        except ValueError:
            raise HTTPException(400, f"Invalid quantity '{value}' for item {item_id}")

        item = session.get(Item, item_id)
        if item is not None and item.tender_id == tender_id:
            item.qty = qty
            session.add(item)

    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}#items", status_code=303)


# ---------------------------------------------------------------------------
# Quotation entry: pick a supplier, enter their rate against every item
# already on this RFQ (the item list itself is fixed here - add/edit/
# delete items happens on the RFQ page, not here)
# ---------------------------------------------------------------------------


@app.get("/tenders/{tender_id}/quote-entry", response_class=HTMLResponse)
def quote_entry_form(
    tender_id: int, request: Request, supplier_id: str = "", session: Session = Depends(get_session)
):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    items = session.exec(select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)).all()
    item_ids = [i.id for i in items]

    all_suppliers = session.exec(select(Supplier).order_by(Supplier.name)).all()
    supplier_names_json = _json_for_script([{"id": s.id, "label": s.name} for s in all_suppliers])

    selected_supplier = None
    if supplier_id.strip():
        try:
            selected_supplier = session.get(Supplier, int(supplier_id))
        except ValueError:
            selected_supplier = None

    rate_map = {}
    if selected_supplier is not None and item_ids:
        quotes = session.exec(
            select(Quote).where(Quote.item_id.in_(item_ids), Quote.supplier_id == selected_supplier.id)
        ).all()
        rate_map = {q.item_id: q.rate for q in quotes}

    # Cross-check grid: every supplier who has quoted anything on this RFQ,
    # with the lowest rate per item highlighted - same idea as the old
    # tender_detail.html grid, now living here instead since this is where
    # pricing actually belongs.
    cs = build_comparative_statement(session, tender_id)
    quoting_suppliers = sorted(cs.suppliers_by_id.values(), key=lambda s: s.name)
    all_quotes = session.exec(select(Quote).where(Quote.item_id.in_(item_ids))).all() if item_ids else []
    full_rate_matrix = {(q.item_id, q.supplier_id): q.rate for q in all_quotes}
    lowest_by_item_id = {r.item.id: r.lowest_supplier_id for r in cs.item_results}

    return templates.TemplateResponse(
        request,
        "quote_entry.html",
        {
            "tender": tender,
            "items": items,
            "supplier_names_json": supplier_names_json,
            "selected_supplier": selected_supplier,
            "rate_map": rate_map,
            "quoting_suppliers": quoting_suppliers,
            "full_rate_matrix": full_rate_matrix,
            "lowest_by_item_id": lowest_by_item_id,
        },
    )


@app.post("/tenders/{tender_id}/quote-entry")
async def submit_quote_entry(tender_id: int, request: Request, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    form = await request.form()
    try:
        supplier_id = int(form.get("supplier_id", ""))
    except (TypeError, ValueError):
        raise HTTPException(400, "Supplier is required")
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(404, "Supplier not found")

    for key, value in form.multi_items():
        if not key.startswith("rate__"):
            continue
        _, item_id_str = key.split("__")
        item_id = int(item_id_str)
        item = session.get(Item, item_id)
        if item is None or item.tender_id != tender_id:
            continue

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
            if rate is not None:
                session.add(Quote(item_id=item_id, supplier_id=supplier_id, rate=rate))
        else:
            quote.rate = rate
            session.add(quote)

    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}/quote-entry?supplier_id={supplier_id}", status_code=303)


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
    departments = session.exec(select(Department).order_by(Department.name)).all()
    departments_json = _json_for_script([{"id": d.id, "label": d.name} for d in departments])
    return templates.TemplateResponse(
        request,
        "purchase_proposal.html",
        {"tender": tender, "proposal": proposal, "departments_json": departments_json},
    )


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


@app.post("/tenders/{tender_id}/proposal/contract/{supplier_id}")
def download_contract_draft(
    tender_id: int,
    supplier_id: int,
    contract_no: str = Form(...),
    session: Session = Depends(get_session),
):
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

    content = generate_contract_award(proposal.tender, group, supplier, contract_no=contract_no)
    filename = f"contract-award-{_safe_filename_part(group.supplier_name)}.docx"
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
            # No per-firm contract no. collected in a bulk download - a
            # reasonable auto-generated placeholder, editable in Word after.
            auto_contract_no = f"{tender.inquiry_no} / {group.supplier_name}"
            content = generate_contract_award(proposal.tender, group, supplier, contract_no=auto_contract_no)
            zf.writestr(f"contract-award-{_safe_filename_part(group.supplier_name)}.docx", content)

    filename = f"contract-drafts-tender-{tender_id}.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/tenders/{tender_id}/document-details")
def update_document_details(
    tender_id: int,
    indent_no: str = Form(""),
    department_id: str = Form(""),
    firms_invited_count: str = Form(""),
    issue_date: str = Form(""),
    opening_date: str = Form(""),
    delivery_days: str = Form("60"),
    warranty_months: str = Form("3"),
    session: Session = Depends(get_session),
):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    def _parse_date(s: str):
        return datetime.date.fromisoformat(s) if s.strip() else None

    try:
        tender.indent_no = indent_no.strip() or None
        tender.department_id = int(department_id) if department_id.strip() else None
        tender.firms_invited_count = int(firms_invited_count) if firms_invited_count.strip() else None
        tender.issue_date = _parse_date(issue_date)
        tender.opening_date = _parse_date(opening_date)
        tender.delivery_days = int(delivery_days)
        tender.warranty_months = int(warranty_months)
    except ValueError:
        raise HTTPException(400, "Invalid document details")

    session.add(tender)
    session.commit()
    return RedirectResponse(f"/tenders/{tender_id}/proposal", status_code=303)


@app.get("/tenders/{tender_id}/proposal/pp-document")
def download_pp_document(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    proposal = build_purchase_proposal(session, tender_id)
    cs = build_comparative_statement(session, tender_id)
    if not proposal.firm_groups:
        raise HTTPException(400, "No items have been awarded to any firm yet")

    content = generate_purchase_proposal_doc(proposal.tender, proposal, cs.suppliers_by_id)
    filename = f"purchase-proposal-tender-{tender_id}.docx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
