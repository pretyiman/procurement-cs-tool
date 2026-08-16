import datetime
import json
import shutil
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .award_engine import build_purchase_proposal, resolve_awarded_items, validate_override
from .business_rules import get_business_rules
from .cs_engine import build_comparative_statement, compute_bundle_lineup, compute_item_result
from .custom_fields import (
    SUGGESTED_CS_SIGNATURE_FIELDS,
    create_custom_field,
    create_group,
    custom_fields_dict_for_tender,
    delete_custom_field,
    delete_group,
    list_custom_fields,
    list_groups,
    update_custom_field,
    update_group,
)
from .db import create_db_and_tables, get_session
from .document_labels import get_document_labels
from .docx_export import generate_contract_award, generate_purchase_proposal_doc
from .lpr_history import get_last_purchase_rate
from .paths import resource_path
from .proposal_snapshot import (
    all_firms_have_contract_award,
    approve_proposal_snapshot,
    get_contract_award,
    get_snapshot,
    save_proposal_snapshot,
    upsert_contract_award,
)
from .template_manager import (
    TEMPLATE_NAMES,
    convert_doc_to_docx,
    list_templates,
    read_active_template,
    restore_default_template,
    save_custom_template,
    validate_template,
)
from .excel_io import (
    export_cs_xlsx,
    export_department_list_xlsx,
    export_item_catalog_xlsx,
    export_package_cs_xlsx,
    export_purchase_proposal_xlsx,
    export_rfq_item_list_xlsx,
    export_supplier_list_xlsx,
    export_working_comparison_xlsx,
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


def _items_locked(tender: Tender) -> bool:
    """Once an RFQ has moved past drafting (status) or has actually been
    published (issue_date has passed), there's no legitimate reason to
    keep adding/editing/deleting items - suppliers may already be quoting
    against a fixed list. A blank issue_date means "not yet published"
    and never locks by date alone - only a real, passed publish date
    does. No unlock/override exists for v1; start a fresh RFQ if the
    item list genuinely needs to change after this point."""
    if tender.status != TenderStatus.draft:
        return True
    if tender.issue_date is not None and tender.issue_date <= datetime.date.today():
        return True
    return False


def _package_limit_slice(package_totals: list, package_limit: str) -> list:
    if not package_limit.strip() or package_limit.strip().lower() == "all":
        return package_totals
    try:
        n = int(package_limit)
    except ValueError:
        return package_totals
    return package_totals[:n]


def _tied_package_supplier_ids(package_totals: list) -> list:
    if package_totals and package_totals[0].fully_quoted:
        top_value = package_totals[0].contract_value
        return [p.supplier_id for p in package_totals if p.fully_quoted and p.contract_value == top_value]
    return []


def _parse_bundle_sizes(raw: str, max_size: int) -> list:
    """Default sizes are always 1 through min(5, however many suppliers
    actually quoted anything) - plus whatever the admin typed into the
    "Bundle sizes" field, so it's adjustable without losing the defaults."""
    requested = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            requested.append(int(part))
    defaults = list(range(1, min(5, max_size) + 1))
    return sorted({n for n in defaults + requested if 1 <= n <= max_size})


def _phase_landing_url(session: Session, tender: Tender) -> str:
    """Where "opening" this RFQ (from a list) should land, based on how
    far along it actually is - so resuming work shows what to do next
    instead of always the item-editing page regardless of stage. Only
    picks the *default* landing; every phase is still directly reachable
    from any page via the phase nav / quick-jump links."""
    base = f"/tenders/{tender.id}"
    if tender.status in (TenderStatus.awarded, TenderStatus.proposal_approved):
        return f"{base}/contract-award"
    if tender.status == TenderStatus.proposal_generated:
        return f"{base}/proposal"

    # status == draft
    today = datetime.date.today()
    if tender.opening_date is not None and tender.opening_date <= today:
        # Quote collection has formally closed - land on Comparative
        # Summary to review/decide, even if every item already happens to
        # be resolved (awards default to lowest bidder automatically, so
        # "resolved" alone is too weak a signal to skip a human review).
        return f"{base}/comparative-summary"
    proposal = build_purchase_proposal(session, tender.id)
    if proposal.firm_groups and not proposal.unresolved_items:
        # No opening_date tracked, but every item already has a decision -
        # a reasonable nudge toward generating the proposal.
        return f"{base}/proposal"
    if tender.issue_date is not None and tender.issue_date <= today:
        return f"{base}/quote-entry"
    return base


@app.get("/health")
def health():
    return {"status": "ok", "app": "procurement-cs-tool"}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    tenders = session.exec(select(Tender).order_by(Tender.id.desc())).all()
    status_counts = {"draft": 0, "proposal_generated": 0, "proposal_approved": 0, "awarded": 0}
    for t in tenders:
        status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1

    item_count = len(session.exec(select(ItemMaster)).all())
    supplier_count = len(session.exec(select(Supplier)).all())
    recent_tenders = tenders[:8]
    recent_rows = [{"tender": t, "landing_url": _phase_landing_url(session, t)} for t in recent_tenders]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "tender_count": len(tenders),
            "status_counts": status_counts,
            "item_count": item_count,
            "supplier_count": supplier_count,
            "recent_rows": recent_rows,
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


@app.get("/items/export")
def export_items(q: str = "", session: Session = Depends(get_session)):
    catalog = session.exec(select(ItemMaster)).all()
    if q.strip():
        needle = q.strip().lower()
        catalog = [im for im in catalog if needle in im.part_no.lower() or needle in im.description.lower()]
    catalog.sort(key=lambda im: (im.part_no, im.description))
    content = export_item_catalog_xlsx(catalog)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="item-catalog.xlsx"'},
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


@app.get("/departments/export")
def export_departments(q: str = "", session: Session = Depends(get_session)):
    departments = session.exec(select(Department).order_by(Department.name)).all()
    if q.strip():
        needle = q.strip().lower()
        departments = [d for d in departments if needle in d.name.lower()]
    content = export_department_list_xlsx(departments)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="departments.xlsx"'},
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


@app.get("/suppliers/export")
def export_suppliers(q: str = "", session: Session = Depends(get_session)):
    # Registered before /suppliers/{supplier_id} - both are unconstrained
    # path segments at the routing layer, so "export" would otherwise
    # match the int supplier_id route first and 422 (order matters here,
    # not just the int type hint).
    suppliers = session.exec(select(Supplier).order_by(Supplier.name)).all()
    if q.strip():
        needle = q.strip().lower()
        suppliers = [s for s in suppliers if needle in s.name.lower()]
    content = export_supplier_list_xlsx(suppliers)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="suppliers.xlsx"'},
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
    rows = [{"tender": t, "landing_url": _phase_landing_url(session, t)} for t in tenders]
    return templates.TemplateResponse(request, "tenders_list.html", {"rows": rows})


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
    issue_date: str = Form(""),
    opening_date: str = Form(""),
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
        issue_date=datetime.date.fromisoformat(issue_date) if issue_date.strip() else None,
        opening_date=datetime.date.fromisoformat(opening_date) if opening_date.strip() else None,
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
    issue_date: str = Form(""),
    opening_date: str = Form(""),
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
        issue_date=datetime.date.fromisoformat(issue_date) if issue_date.strip() else None,
        opening_date=datetime.date.fromisoformat(opening_date) if opening_date.strip() else None,
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
            "items_locked": _items_locked(tender),
        },
    )


@app.get("/tenders/{tender_id}/export")
def export_cs(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    cs = build_comparative_statement(session, tender_id)
    labels = get_document_labels(session)
    content = export_cs_xlsx(cs, labels, custom_fields_dict_for_tender(session, tender))
    filename = f"comparative-statement-tender-{tender_id}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/tenders/{tender_id}/export-items")
def export_rfq_items(tender_id: int, session: Session = Depends(get_session)):
    """Just the RFQ's own item list - no pricing. This is what the RFQ item
    page's own Download button uses now; the full comparative statement
    (with rates/lowest-firm) lives on Quote Entry's Download CS instead,
    since that's the only page that actually has pricing data to show."""
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    items = session.exec(select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)).all()
    content = export_rfq_item_list_xlsx(tender, items)
    filename = f"rfq-item-list-{tender_id}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/tenders/{tender_id}/export-package")
def export_package_cs(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    cs = build_comparative_statement(session, tender_id)
    labels = get_document_labels(session)
    content = export_package_cs_xlsx(cs, labels, custom_fields_dict_for_tender(session, tender))
    filename = f"comparative-statement-package-tender-{tender_id}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/tenders/{tender_id}/export-working-comparison")
def export_working_comparison(
    tender_id: int,
    view: str = "item",
    suppliers: List[int] = Query([]),
    session: Session = Depends(get_session),
):
    """A user-narrowed shortlist comparison (e.g. "lowest 5 overall", or one
    Sourcing Options bundle's members) - deliberately NOT the official
    Comparative Statement, which always includes every supplier and is
    untouched by this route (see export_cs/export_package_cs above)."""
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    cs = build_comparative_statement(session, tender_id)
    quoting_suppliers = sorted(cs.suppliers_by_id.values(), key=lambda s: s.name)
    selected_ids = {sid for sid in suppliers if sid in cs.suppliers_by_id} if suppliers else {s.id for s in quoting_suppliers}
    selected_suppliers = [s for s in quoting_suppliers if s.id in selected_ids]
    if not selected_suppliers:
        raise HTTPException(400, "No suppliers selected")

    items = session.exec(select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)).all()

    if view == "package":
        filtered_package_totals = [p for p in cs.package_totals if p.supplier_id in selected_ids]
        content = export_working_comparison_xlsx(
            tender, items, selected_suppliers, "package", package_totals=filtered_package_totals
        )
    else:
        item_ids = [i.id for i in items]
        quotes = session.exec(select(Quote).where(Quote.item_id.in_(item_ids))).all() if item_ids else []
        quotes_by_item: dict = {}
        for q in quotes:
            quotes_by_item.setdefault(q.item_id, []).append(q)
        item_results = [
            compute_item_result(item, [q for q in quotes_by_item.get(item.id, []) if q.supplier_id in selected_ids])
            for item in items
        ]
        content = export_working_comparison_xlsx(tender, items, selected_suppliers, "item", item_results=item_results)

    filename = f"working-comparison-tender-{tender_id}.xlsx"
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
    if _items_locked(tender):
        raise HTTPException(400, "Items are locked - this RFQ has been published or has moved past drafting")

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
    if _items_locked(tender):
        raise HTTPException(400, "Items are locked - this RFQ has been published or has moved past drafting")

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
    if _items_locked(tender):
        raise HTTPException(400, "Items are locked - this RFQ has been published or has moved past drafting")

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
    tender_id: int,
    request: Request,
    supplier_id: str = "",
    view: str = "item",
    package_limit: str = "",
    session: Session = Depends(get_session),
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
    tied_by_item_id = {r.item.id: r.tied_supplier_ids for r in cs.item_results}
    tied_package_supplier_ids = _tied_package_supplier_ids(cs.package_totals)

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
            "tied_by_item_id": tied_by_item_id,
            "view": "package" if view == "package" else "item",
            "package_totals": _package_limit_slice(cs.package_totals, package_limit),
            "package_totals_total_count": len(cs.package_totals),
            "tied_package_supplier_ids": tied_package_supplier_ids,
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


@app.get("/tenders/{tender_id}/comparative-summary", response_class=HTMLResponse)
def comparative_summary_view(
    tender_id: int,
    request: Request,
    view: str = "item",
    package_limit: str = "",
    bundle_sizes: str = "",
    suppliers: List[int] = Query([]),
    suppliers_filter: str = "",
    session: Session = Depends(get_session),
):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    awarded_items, cs = resolve_awarded_items(session, tender_id)
    item_results_by_id = {r.item.id: r for r in cs.item_results}

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
        result = item_results_by_id[ai.item.id]
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
                "is_tied": result.is_tied,
                "tied_names": [cs.suppliers_by_id[sid].name for sid in result.tied_supplier_ids],
            }
        )

    # Same cross-check grid as Quote Entry (shared partial) - every supplier
    # who has quoted anything, lowest rate per item highlighted, plus the
    # Download Comparative Statement link, so the printed/signed copy
    # needed before drafting the Purchase Proposal is available right here.
    items = session.exec(select(Item).where(Item.tender_id == tender_id).order_by(Item.ser)).all()
    quoting_suppliers = sorted(cs.suppliers_by_id.values(), key=lambda s: s.name)
    full_rate_matrix = {(q.item_id, q.supplier_id): q.rate for q in quotes}

    # Analysis panel: coverage stats + "Sourcing Options" - the cheapest
    # combination of exactly N suppliers for a handful of adjustable sizes,
    # including partial bidders as candidates (not just single suppliers
    # who individually cover everything - see cs_engine.compute_best_bundle).
    items_unresolved_count = sum(1 for r in cs.item_results if r.lowest_supplier_id is None)
    tied_item_count = sum(1 for r in cs.item_results if r.is_tied)
    full_bidders = [p for p in cs.package_totals if p.fully_quoted]

    quotes_by_item: dict = {}
    for q in quotes:
        quotes_by_item.setdefault(q.item_id, []).append(q)
    requested_sizes = _parse_bundle_sizes(bundle_sizes, len(quoting_suppliers))
    bundles = compute_bundle_lineup(items, quotes_by_item, cs.suppliers_by_id, tender.tax_percent, requested_sizes)
    bundle_sizes_csv = ",".join(str(n) for n in requested_sizes)

    # "All Quotes" supplier narrowing (working comparison, NOT the official
    # CS - that always exports every supplier untouched, see export_cs/
    # export_package_cs above). No `suppliers` param at all = everyone, the
    # unfiltered default. `suppliers_filter` is a sentinel the checkbox form
    # always submits, so an explicit "everything unchecked" (suppliers=[])
    # is distinguishable from a fresh page load with no filter applied yet.
    if suppliers:
        selected_supplier_ids = {sid for sid in suppliers if sid in cs.suppliers_by_id}
    elif suppliers_filter:
        selected_supplier_ids = set()
    else:
        selected_supplier_ids = {s.id for s in quoting_suppliers}
    no_suppliers_selected = not selected_supplier_ids
    grid_suppliers = [s for s in quoting_suppliers if s.id in selected_supplier_ids]
    # Carries the active selection along the grid's own internal navigation
    # (item/package toggle, package Top-N paging) so switching those doesn't
    # silently reset back to "everyone" - only built when a filter is
    # actually active this request, to keep the default URLs plain.
    selection_qs = "".join(f"&suppliers={sid}" for sid in sorted(selected_supplier_ids)) if (suppliers or suppliers_filter) else ""

    working_item_results = [
        compute_item_result(item, [q for q in quotes_by_item.get(item.id, []) if q.supplier_id in selected_supplier_ids])
        for item in items
    ]
    grid_lowest_by_item_id = {r.item.id: r.lowest_supplier_id for r in working_item_results}
    grid_tied_by_item_id = {r.item.id: r.tied_supplier_ids for r in working_item_results}

    filtered_package_totals = [p for p in cs.package_totals if p.supplier_id in selected_supplier_ids]
    working_tied_package_supplier_ids = _tied_package_supplier_ids(filtered_package_totals)

    # "Lowest N overall" quick-select shortcuts - ranked the same way as
    # PackageTotal already sorts (cheapest fully-quoted first, then
    # cheapest partial), so "lowest 5" means the 5 suppliers whose own
    # package total is cheapest, not the 5 most-often-cheapest-per-item
    # (that's a different lens, already covered by the leaderboard).
    package_ranked_ids = [p.supplier_id for p in cs.package_totals]
    lowest_n_options = [
        (n, package_ranked_ids[:n]) for n in (3, 5, 10) if n < len(quoting_suppliers)
    ]

    locked = tender.status not in (TenderStatus.draft, TenderStatus.proposal_generated)
    return templates.TemplateResponse(
        request,
        "comparative_summary.html",
        {
            "tender": tender,
            "rows": rows,
            "locked": locked,
            "items": items,
            "quoting_suppliers": quoting_suppliers,
            "grid_suppliers": grid_suppliers,
            "selected_supplier_ids": selected_supplier_ids,
            "no_suppliers_selected": no_suppliers_selected,
            "supplier_selection_enabled": True,
            "selection_qs": selection_qs,
            "lowest_n_options": lowest_n_options,
            "full_rate_matrix": full_rate_matrix,
            "lowest_by_item_id": grid_lowest_by_item_id,
            "tied_by_item_id": grid_tied_by_item_id,
            "view": "package" if view == "package" else "item",
            "package_limit": package_limit,
            "package_totals": _package_limit_slice(filtered_package_totals, package_limit),
            "package_totals_total_count": len(filtered_package_totals),
            "tied_package_supplier_ids": working_tied_package_supplier_ids,
            "lowest_count_leaderboard": cs.lowest_count_leaderboard,
            "full_bidders": full_bidders,
            "items_unresolved_count": items_unresolved_count,
            "tied_item_count": tied_item_count,
            "bundles": bundles,
            "bundle_sizes_csv": bundle_sizes_csv,
        },
    )


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
    if item.tender.status not in (TenderStatus.draft, TenderStatus.proposal_generated):
        raise HTTPException(400, "Award decisions lock once the proposal is approved - generate a fresh proposal isn't possible after that point")

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
    return RedirectResponse(f"/tenders/{tender_id}/comparative-summary#tab-award", status_code=303)


@app.get("/tenders/{tender_id}/proposal", response_class=HTMLResponse)
def purchase_proposal_view(tender_id: int, request: Request, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    proposal = build_purchase_proposal(session, tender_id)
    snapshot = get_snapshot(session, tender_id)
    contract_awards_by_supplier = {}
    all_have_contract_award = False
    if snapshot is not None:
        contract_awards_by_supplier = {
            g.supplier_id: get_contract_award(session, snapshot.id, g.supplier_id) for g in snapshot.firm_groups
        }
        all_have_contract_award = all_firms_have_contract_award(session, snapshot.id)
    departments = session.exec(select(Department).order_by(Department.name)).all()
    departments_json = _json_for_script([{"id": d.id, "label": d.name} for d in departments])
    return templates.TemplateResponse(
        request,
        "purchase_proposal.html",
        {
            "tender": tender,
            "proposal": proposal,
            "snapshot": snapshot,
            "contract_awards_by_supplier": contract_awards_by_supplier,
            "all_have_contract_award": all_have_contract_award,
            "departments_json": departments_json,
        },
    )


@app.get("/tenders/{tender_id}/contract-award", response_class=HTMLResponse)
def contract_award_view(tender_id: int, request: Request, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")

    snapshot = get_snapshot(session, tender_id)
    can_issue = tender.status in (TenderStatus.proposal_approved, TenderStatus.awarded)
    contract_awards_by_supplier = {}
    if snapshot is not None and can_issue:
        contract_awards_by_supplier = {
            g.supplier_id: get_contract_award(session, snapshot.id, g.supplier_id) for g in snapshot.firm_groups
        }

    return templates.TemplateResponse(
        request,
        "contract_award.html",
        {
            "tender": tender,
            "snapshot": snapshot,
            "can_issue": can_issue,
            "contract_awards_by_supplier": contract_awards_by_supplier,
        },
    )


@app.post("/tenders/{tender_id}/generate-proposal")
def generate_proposal(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    try:
        save_proposal_snapshot(session, tender_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/tenders/{tender_id}/proposal", status_code=303)


@app.post("/tenders/{tender_id}/approve-proposal")
def approve_proposal_route(tender_id: int, session: Session = Depends(get_session)):
    try:
        approve_proposal_snapshot(session, tender_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/tenders/{tender_id}/proposal", status_code=303)


@app.post("/tenders/{tender_id}/mark-awarded")
def mark_awarded(tender_id: int, session: Session = Depends(get_session)):
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(404, "Tender not found")
    if tender.status != TenderStatus.proposal_approved:
        raise HTTPException(400, "Approve the proposal before finalizing the award")
    snapshot = get_snapshot(session, tender_id)
    if snapshot is None or not all_firms_have_contract_award(session, snapshot.id):
        raise HTTPException(400, "Every awarded firm needs a Contract Award (with a contract number) before finalizing")
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


def _require_approved_snapshot(session: Session, tender: Tender):
    """CA generation is only allowed once the proposal has been approved
    (or the tender is already fully awarded) - and always renders from
    that frozen snapshot, never from live Item/Quote/catalog state."""
    if tender.status not in (TenderStatus.proposal_approved, TenderStatus.awarded):
        raise HTTPException(400, "Approve the proposal before generating a Contract Award")
    snapshot = get_snapshot(session, tender.id)
    if snapshot is None:
        raise HTTPException(400, "No approved proposal found")
    return snapshot


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

    snapshot = _require_approved_snapshot(session, tender)
    group = next((g for g in snapshot.firm_groups if g.supplier_id == supplier_id), None)
    if group is None:
        raise HTTPException(400, "This supplier has no items awarded on this tender")

    if tender.status == TenderStatus.awarded:
        # Contract numbers lock once the RFQ is finalized - reuse whatever
        # was already issued regardless of what's submitted, rather than
        # silently overwriting an already-issued number with no audit
        # trail. (The UI hides the edit field at this stage too; this is
        # the server-side enforcement of that same rule.)
        existing = get_contract_award(session, snapshot.id, supplier_id)
        if existing is None:
            raise HTTPException(400, "No Contract Award was issued for this firm before finalizing")
        contract_no = existing.contract_no
    else:
        try:
            upsert_contract_award(session, snapshot.id, supplier_id, contract_no)
        except ValueError as e:
            raise HTTPException(400, str(e))

    rules = get_business_rules(session)
    content = generate_contract_award(
        tender, group, supplier, contract_no=contract_no, rules=rules,
        custom_fields=custom_fields_dict_for_tender(session, tender),
    )
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

    snapshot = _require_approved_snapshot(session, tender)

    rules = get_business_rules(session)
    fields = custom_fields_dict_for_tender(session, tender)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for group in snapshot.firm_groups:
            supplier = session.get(Supplier, group.supplier_id)
            # Reuse whatever contract number is already on record for this
            # firm (e.g. entered via a single-firm download); only fall
            # back to an auto-generated placeholder - and persist it, so
            # later visits (including the finalize gate) see the same
            # number this zip actually used.
            existing = get_contract_award(session, snapshot.id, group.supplier_id)
            contract_no = existing.contract_no if existing else f"{tender.inquiry_no} / {group.supplier_name}"
            upsert_contract_award(session, snapshot.id, group.supplier_id, contract_no)
            content = generate_contract_award(
                tender, group, supplier, contract_no=contract_no, rules=rules,
                custom_fields=fields,
            )
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

    snapshot = get_snapshot(session, tender_id)
    if snapshot is None:
        raise HTTPException(400, "Generate the proposal first")

    suppliers_by_id = {g.supplier_id: session.get(Supplier, g.supplier_id) for g in snapshot.firm_groups}
    content = generate_purchase_proposal_doc(
        tender, snapshot, suppliers_by_id, custom_fields=custom_fields_dict_for_tender(session, tender)
    )
    filename = f"purchase-proposal-tender-{tender_id}.docx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Settings: policy numbers used in generated documents (Contract Award),
# editable here instead of being hardcoded constants in docx_export.py
# ---------------------------------------------------------------------------


@app.get("/settings/business-rules", response_class=HTMLResponse)
def business_rules_form(request: Request, saved: str = "", session: Session = Depends(get_session)):
    rules = get_business_rules(session)
    return templates.TemplateResponse(request, "business_rules.html", {"rules": rules, "saved": bool(saved)})


@app.post("/settings/business-rules")
def update_business_rules(
    security_deposit_percent: str = Form(...),
    security_deposit_waived_below: str = Form("0"),
    stamp_duty_percent: str = Form(...),
    session: Session = Depends(get_session),
):
    rules = get_business_rules(session)
    try:
        rules.security_deposit_percent = float(security_deposit_percent)
        rules.security_deposit_waived_below = float(security_deposit_waived_below or 0)
        rules.stamp_duty_percent = float(stamp_duty_percent)
    except ValueError:
        raise HTTPException(400, "All fields must be numbers")
    session.add(rules)
    session.commit()
    return RedirectResponse("/settings/business-rules?saved=1", status_code=303)


@app.get("/settings/templates", response_class=HTMLResponse)
def document_templates_form(request: Request, error: str = "", saved: str = ""):
    return templates.TemplateResponse(
        request,
        "document_templates.html",
        {"rows": list_templates(), "error": error, "saved": bool(saved)},
    )


@app.get("/settings/templates/{name}/download")
def download_template(name: str):
    try:
        content = read_active_template(name)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Unknown template")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/settings/templates/{name}/upload")
async def upload_template(name: str, file: UploadFile = File(...)):
    if name not in TEMPLATE_NAMES:
        raise HTTPException(404, "Unknown template")
    filename = file.filename.lower()
    if not (filename.endswith(".docx") or filename.endswith(".doc")):
        return RedirectResponse(
            f"/settings/templates?error={quote('Please upload a .doc or .docx file.')}", status_code=303
        )
    content = await file.read()
    if filename.endswith(".doc"):
        try:
            content = convert_doc_to_docx(content)
        except ValueError as e:
            return RedirectResponse(f"/settings/templates?error={quote(str(e))}", status_code=303)
    try:
        validate_template(name, content)
    except ValueError as e:
        return RedirectResponse(f"/settings/templates?error={quote(str(e))}", status_code=303)
    save_custom_template(name, content)
    return RedirectResponse("/settings/templates?saved=1", status_code=303)


@app.post("/settings/templates/{name}/restore-default")
def restore_template(name: str):
    if name not in TEMPLATE_NAMES:
        raise HTTPException(404, "Unknown template")
    restore_default_template(name)
    return RedirectResponse("/settings/templates?saved=1", status_code=303)


@app.get("/settings/cs-labels", response_class=HTMLResponse)
def cs_labels_form(request: Request, saved: str = "", session: Session = Depends(get_session)):
    labels = get_document_labels(session)
    return templates.TemplateResponse(request, "cs_labels.html", {"labels": labels, "saved": bool(saved)})


@app.post("/settings/cs-labels")
def update_cs_labels(
    cs_title: str = Form(...),
    prep_by_label: str = Form(...),
    checked_by_label: str = Form(...),
    head_qac_label: str = Form(...),
    countersigned_label: str = Form(...),
    fmsad_label: str = Form(...),
    session: Session = Depends(get_session),
):
    labels = get_document_labels(session)
    labels.cs_title = cs_title.strip() or labels.cs_title
    labels.prep_by_label = prep_by_label.strip()
    labels.checked_by_label = checked_by_label.strip()
    labels.head_qac_label = head_qac_label.strip()
    labels.countersigned_label = countersigned_label.strip()
    labels.fmsad_label = fmsad_label.strip()
    session.add(labels)
    session.commit()
    return RedirectResponse("/settings/cs-labels?saved=1", status_code=303)


@app.get("/settings/custom-fields", response_class=HTMLResponse)
def custom_fields_form(request: Request, error: str = "", saved: str = "", session: Session = Depends(get_session)):
    fields = list_custom_fields(session)
    used_suggestions = {f.tag_name for f in fields}
    suggestions = [
        {"tag_name": name, "description": desc}
        for name, desc in SUGGESTED_CS_SIGNATURE_FIELDS.items()
        if name not in used_suggestions
    ]
    groups = list_groups(session)
    groups_view = [{"group": g, "fields": list_custom_fields(session, group_id=g.id)} for g in groups]
    grouped_department_ids = {g.department_id for g in groups}
    all_departments = session.exec(select(Department).order_by(Department.name)).all()
    departments_without_group = [d for d in all_departments if d.id not in grouped_department_ids]
    return templates.TemplateResponse(
        request,
        "custom_fields.html",
        {
            "fields": fields,
            "suggestions": suggestions,
            "error": error,
            "saved": bool(saved),
            "groups_view": groups_view,
            "departments_without_group": departments_without_group,
        },
    )


@app.post("/settings/custom-fields")
def create_custom_field_route(
    tag_name: str = Form(...),
    label: str = Form(...),
    value: str = Form(""),
    group_id: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        create_custom_field(session, tag_name, label, value, group_id=int(group_id) if group_id.strip() else None)
    except ValueError as e:
        return RedirectResponse(f"/settings/custom-fields?error={quote(str(e))}", status_code=303)
    return RedirectResponse("/settings/custom-fields?saved=1", status_code=303)


@app.post("/settings/custom-field-groups")
def create_custom_field_group_route(
    name: str = Form(...),
    department_id: str = Form(...),
    session: Session = Depends(get_session),
):
    if not department_id.strip():
        return RedirectResponse(
            f"/settings/custom-fields?error={quote('Pick a department for the new group.')}", status_code=303
        )
    try:
        create_group(session, name, int(department_id))
    except ValueError as e:
        return RedirectResponse(f"/settings/custom-fields?error={quote(str(e))}", status_code=303)
    return RedirectResponse("/settings/custom-fields?saved=1", status_code=303)


@app.post("/settings/custom-field-groups/{group_id}/update")
def update_custom_field_group_route(group_id: int, name: str = Form(...), session: Session = Depends(get_session)):
    try:
        update_group(session, group_id, name)
    except ValueError as e:
        return RedirectResponse(f"/settings/custom-fields?error={quote(str(e))}", status_code=303)
    return RedirectResponse("/settings/custom-fields?saved=1", status_code=303)


@app.post("/settings/custom-field-groups/{group_id}/delete")
def delete_custom_field_group_route(group_id: int, session: Session = Depends(get_session)):
    delete_group(session, group_id)
    return RedirectResponse("/settings/custom-fields?saved=1", status_code=303)


@app.post("/settings/custom-fields/{field_id}/update")
def update_custom_field_route(
    field_id: int,
    label: str = Form(...),
    value: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        update_custom_field(session, field_id, label, value)
    except ValueError as e:
        return RedirectResponse(f"/settings/custom-fields?error={quote(str(e))}", status_code=303)
    return RedirectResponse("/settings/custom-fields?saved=1", status_code=303)


@app.post("/settings/custom-fields/{field_id}/delete")
def delete_custom_field_route(field_id: int, session: Session = Depends(get_session)):
    delete_custom_field(session, field_id)
    return RedirectResponse("/settings/custom-fields?saved=1", status_code=303)
