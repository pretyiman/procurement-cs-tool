"""One-time demo-data seed for a brand-new install (Phase 13-ish).

A freshly packaged .exe starts with a completely empty database - fine
for real use, but not great for a live demo/presentation, which is what
this exists for. seed_demo_data_if_empty() runs from the app's startup
hook (main.py) on every launch, but does nothing at all unless the
database has zero Tender rows - it never touches a database that's
already in real use, seeded or not. Item/supplier/department names here
are deliberately generic and fictional, not derived from any real data
this project has ever handled.

Builds 5 tenders spanning every lifecycle stage (Items -> Quote Entry /
Comparative Summary -> Purchase Proposal -> Contract Award -> fully
Awarded), plus a Custom Field Group with example values for all 15 of
the PP/CA "department blank" tags (see custom_fields.SUGGESTED_PP_FIELDS /
SUGGESTED_CA_FIELDS)
so a demo download of the Purchase Proposal/Contract Award for the
Contract-Award-stage tender shows filled-in text instead of generic
defaults."""

import datetime

from sqlmodel import Session, select

from .custom_fields import create_custom_field, create_group
from .models import Department, Item, ItemMaster, Quote, Supplier, Tender, TenderStatus
from .proposal_snapshot import approve_proposal_snapshot, save_proposal_snapshot, upsert_contract_award

DEMO_ITEMS = [
    ("STL-12MM", "Steel Rod 12mm", "Kg"),
    ("RBR-GKT-50", "Rubber Gasket 50mm", "Nos"),
    ("BRG-6205", "Ball Bearing 6205", "Nos"),
    ("PNT-WHT-4L", "Paint White 4 Litre", "Tin"),
    ("CBL-4CORE", "Cable 4-Core 2.5mm", "Mtr"),
]

DEMO_SUPPLIERS = [
    ("M/s Alpha Traders", "12 Industrial Road, City Center"),
    ("M/s Beta Engineering Works", "45 Workshop Lane, East District"),
    ("M/s Gamma Hardware Co", "8 Market Street, West Zone"),
    ("M/s Delta Supplies", "21 Commerce Ave, North Town"),
]

EXAMPLE_PP_CA_FIELDS = {
    "indentor_name": "Director Procurement, Supply Chain Management",
    "cost_head": "Fund Code 1/234/56",
    "country_of_origin": "Local",
    "inspection_authority": "Chief Inspector, Supply Chain Management",
    "inspection_officer_detail": "Chief Inspector",
    "place_of_inspection": "Central Store, Supply Chain Management",
    "ca_paying_authority": "Accounts Officer, Finance Directorate",
    "secrecy_authority": "Supply Chain Management",
    "pp_paying_authority": "Accounts Officer, Finance Directorate",
    "pp_prep_officer_rank_name": "Procurement Officer (Demo Officer)",
    "pp_prep_officer_department": "Procurement Cell",
    "routing_mid_role": "Deputy Director SCM",
    "routing_md_hrf_remark": "For review and concurrence, please.",
    "routing_final_role": "Budget & Accounts Officer",
    "routing_final_remark": "For fund certification.",
}


def _get_or_create_department(session: Session, name: str) -> Department:
    dep = session.exec(select(Department).where(Department.name == name)).first()
    if dep is None:
        dep = Department(name=name)
        session.add(dep)
        session.commit()
        session.refresh(dep)
    return dep


def _get_or_create_supplier(session: Session, name: str, address: str) -> Supplier:
    supplier = session.exec(select(Supplier).where(Supplier.name == name)).first()
    if supplier is None:
        supplier = Supplier(name=name, address=address)
        session.add(supplier)
        session.commit()
        session.refresh(supplier)
    return supplier


def _get_or_create_item_master(session: Session, part_no: str, description: str, unit: str) -> ItemMaster:
    im = session.exec(
        select(ItemMaster).where(ItemMaster.part_no == part_no, ItemMaster.description == description)
    ).first()
    if im is None:
        im = ItemMaster(part_no=part_no, description=description, default_unit=unit)
        session.add(im)
        session.commit()
        session.refresh(im)
    return im


def _add_item(session: Session, tender_id: int, ser: int, item_master_id: int, qty: float) -> Item:
    item = Item(tender_id=tender_id, item_master_id=item_master_id, ser=ser, qty=qty)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _add_quote(session: Session, item_id: int, supplier_id: int, rate: float) -> None:
    session.add(Quote(item_id=item_id, supplier_id=supplier_id, rate=rate))
    session.commit()


def seed_demo_data_if_empty(session: Session) -> None:
    if session.exec(select(Tender)).first() is not None:
        return  # already has real (or already-seeded) data - never touch it

    scm_dept = _get_or_create_department(session, "Supply Chain Management")
    _get_or_create_department(session, "Technical Directorate")

    suppliers = [_get_or_create_supplier(session, name, addr) for name, addr in DEMO_SUPPLIERS]
    items_master = [_get_or_create_item_master(session, p, d, u) for p, d, u in DEMO_ITEMS]

    group = create_group(session, "Supply Chain Management - Demo", scm_dept.id)
    for tag_name, value in EXAMPLE_PP_CA_FIELDS.items():
        create_custom_field(session, tag_name, tag_name.replace("_", " ").title(), value, group_id=group.id)

    today = datetime.date.today()

    # --- DEMO/2026/001: draft, items only, no quotes - Items page ---
    t1 = Tender(
        inquiry_no="DEMO/2026/001", department_id=scm_dept.id,
        issue_date=today, opening_date=today + datetime.timedelta(days=14),
    )
    session.add(t1)
    session.commit()
    session.refresh(t1)
    for i, im in enumerate(items_master[:3], start=1):
        _add_item(session, t1.id, i, im.id, qty=10)

    # --- DEMO/2026/002: quotes entered, no proposal yet - Comparative Summary ---
    t2 = Tender(
        inquiry_no="DEMO/2026/002", department_id=scm_dept.id,
        issue_date=today - datetime.timedelta(days=10), opening_date=today - datetime.timedelta(days=3),
    )
    session.add(t2)
    session.commit()
    session.refresh(t2)
    for i, im in enumerate(items_master[:3], start=1):
        item = _add_item(session, t2.id, i, im.id, qty=10)
        for s_idx, s in enumerate(suppliers[:3]):
            _add_quote(session, item.id, s.id, rate=100 + i * 10 + s_idx * 5)

    # --- DEMO/2026/003: proposal generated, not approved - Purchase Proposal ---
    t3 = Tender(
        inquiry_no="DEMO/2026/003", department_id=scm_dept.id,
        issue_date=today - datetime.timedelta(days=20), opening_date=today - datetime.timedelta(days=13),
    )
    session.add(t3)
    session.commit()
    session.refresh(t3)
    for i, im in enumerate(items_master[:4], start=1):
        item = _add_item(session, t3.id, i, im.id, qty=5)
        for s_idx, s in enumerate(suppliers[:2]):
            _add_quote(session, item.id, s.id, rate=200 + i * 15 + s_idx * 8)
    save_proposal_snapshot(session, t3.id)

    # --- DEMO/2026/004: proposal approved - Contract Award page, ready to
    # download with the example custom field values filled in - the main
    # tender for demoing the PP/CA "department blank" tags feature. ---
    t4 = Tender(
        inquiry_no="DEMO/2026/004", department_id=scm_dept.id, indent_no="DEMO-IND-004",
        issue_date=today - datetime.timedelta(days=30), opening_date=today - datetime.timedelta(days=23),
    )
    session.add(t4)
    session.commit()
    session.refresh(t4)
    for i, im in enumerate(items_master, start=1):
        item = _add_item(session, t4.id, i, im.id, qty=8)
        for s_idx, s in enumerate(suppliers):
            _add_quote(session, item.id, s.id, rate=150 + i * 12 + s_idx * 6)
    save_proposal_snapshot(session, t4.id)
    approve_proposal_snapshot(session, t4.id)

    # --- DEMO/2026/005: fully awarded - shows the complete lifecycle ---
    t5 = Tender(
        inquiry_no="DEMO/2026/005", department_id=scm_dept.id,
        issue_date=today - datetime.timedelta(days=45), opening_date=today - datetime.timedelta(days=38),
    )
    session.add(t5)
    session.commit()
    session.refresh(t5)
    for i, im in enumerate(items_master[:3], start=1):
        item = _add_item(session, t5.id, i, im.id, qty=6)
        for s_idx, s in enumerate(suppliers[:2]):
            _add_quote(session, item.id, s.id, rate=90 + i * 9 + s_idx * 4)
    snapshot5 = save_proposal_snapshot(session, t5.id)
    approve_proposal_snapshot(session, t5.id)
    for fg in snapshot5.firm_groups:
        upsert_contract_award(
            session, snapshot5.id, fg.supplier_id,
            contract_no=f"CA-DEMO-{t5.id}-{fg.supplier_id}",
            contract_date=today - datetime.timedelta(days=5),
        )
    t5.status = TenderStatus.awarded
    t5.awarded_date = today - datetime.timedelta(days=5)
    session.add(t5)
    session.commit()
