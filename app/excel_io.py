"""Import a Comparative Statement-shaped Excel file into the DB.

Expected shape (see docs/data-model.md and CS.xlsx):
  - a header row whose first cell is "Ser"
  - the row directly below it holds supplier names, one per rate column
  - rate columns run from the "Rate Quoted by Firms..." header to (not
    including) the "Lowest" header
  - data rows follow, each starting with an integer serial number; the
    first row whose first cell is not a number ends the item list
  - "NQ" / blank / "-" in a rate cell means that supplier did not quote
"""

import re
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Union

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from sqlmodel import Session, select

from .models import Item, ItemMaster, Quote, Supplier, TaxType, Tender, TenderStatus

if TYPE_CHECKING:
    from .award_engine import PurchaseProposal
    from .cs_engine import ComparativeStatement

_TAX_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(GST|PST)", re.IGNORECASE)
_NQ_VALUES = {"NQ", "-", ""}


def _is_nq(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().upper() in _NQ_VALUES:
        return True
    return False


def _parse_tax(rows) -> tuple:
    """Existing CS.xlsx-shaped files only ever label tax as "X% GST" - PST
    detection is here so a file this app exported for a PST tender can be
    re-imported correctly too (see export_cs_xlsx)."""
    for row in rows:
        for cell in row:
            if isinstance(cell, str):
                match = _TAX_PATTERN.search(cell)
                if match:
                    return float(match.group(1)), TaxType(match.group(2).upper())
    return 18.0, TaxType.GST


def _find_inquiry_no(rows, header_idx: int) -> str:
    for row in rows[:header_idx]:
        for cell in row:
            if isinstance(cell, str) and "inquiry" in cell.lower():
                return cell.strip()
    return "UNSPECIFIED"


def get_or_create_supplier(session: Session, name: str) -> Supplier:
    name = name.strip()
    existing = session.exec(select(Supplier).where(Supplier.name == name)).first()
    if existing:
        return existing
    supplier = Supplier(name=name)
    session.add(supplier)
    session.flush()
    return supplier


def get_or_create_item_master(
    session: Session, part_no: str, description: str, default_unit: str = ""
) -> ItemMaster:
    """Reuse a catalog row when (part_no, description) already matches one
    (see docs/data-model.md - part_no alone isn't unique for "NIV" items)."""
    part_no = (part_no or "").strip()
    description = (description or "").strip()
    default_unit = (default_unit or "").strip()

    existing = session.exec(
        select(ItemMaster).where(
            ItemMaster.part_no == part_no, ItemMaster.description == description
        )
    ).first()
    if existing:
        if not existing.default_unit and default_unit:
            existing.default_unit = default_unit
            session.add(existing)
        return existing

    item_master = ItemMaster(part_no=part_no, description=description, default_unit=default_unit)
    session.add(item_master)
    session.flush()
    return item_master


def import_tender(path: Union[str, Path], session: Session) -> Tender:
    wb = load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))

    header_idx = next(
        (i for i, row in enumerate(rows) if row and isinstance(row[0], str) and row[0].strip() == "Ser"),
        None,
    )
    if header_idx is None:
        raise ValueError("Could not find header row (expected a 'Ser' column)")

    header_row = rows[header_idx]
    subheader_row = rows[header_idx + 1]

    rate_start_col = None
    lowest_col = None
    for j, val in enumerate(header_row):
        if not isinstance(val, str):
            continue
        low = val.lower()
        if rate_start_col is None and "rate quoted" in low:
            rate_start_col = j
        if rate_start_col is not None and "lowest" in low:
            lowest_col = j
            break
    if rate_start_col is None or lowest_col is None:
        raise ValueError(
            "Could not locate supplier rate columns "
            "('Rate Quoted by Firms...' / 'Lowest' headers not found)"
        )

    supplier_cols = list(range(rate_start_col, lowest_col))
    supplier_names = [subheader_row[c] for c in supplier_cols]
    if any(not isinstance(n, str) or not n.strip() for n in supplier_names):
        raise ValueError("Expected a supplier name in the row below the header, under each rate column")

    inquiry_no = _find_inquiry_no(rows, header_idx)
    tax_percent, tax_type = _parse_tax(rows)

    tender = Tender(
        inquiry_no=inquiry_no, tax_type=tax_type, tax_percent=tax_percent, status=TenderStatus.draft
    )
    session.add(tender)
    session.flush()

    suppliers = [get_or_create_supplier(session, name) for name in supplier_names]

    data_start = header_idx + 2
    started = False
    for row in rows[data_start:]:
        if not row or all(v is None for v in row):
            continue  # blank spacer row (e.g. between subheader and data)

        if not isinstance(row[0], (int, float)):
            if started:
                break  # reached the totals/summary section
            continue  # not at the data rows yet

        started = True
        item_master = get_or_create_item_master(
            session,
            part_no=str(row[1]) if row[1] is not None else "",
            description=str(row[2]) if row[2] is not None else "",
            default_unit=str(row[3]) if row[3] is not None else "",
        )
        item = Item(
            tender_id=tender.id,
            item_master_id=item_master.id,
            ser=int(row[0]),
            qty=float(row[4]) if row[4] is not None else 0.0,
        )
        session.add(item)
        session.flush()

        for col, supplier in zip(supplier_cols, suppliers):
            raw = row[col]
            rate = None if _is_nq(raw) else float(raw)
            session.add(Quote(item_id=item.id, supplier_id=supplier.id, rate=rate))

    session.commit()
    session.refresh(tender)
    return tender


def export_cs_xlsx(cs: "ComparativeStatement") -> bytes:
    """Render a ComparativeStatement (app/cs_engine.py) as an .xlsx workbook
    shaped like the original CS.xlsx: Ser/Part No/Description/A-U/Qty, one
    rate column per supplier, Lowest Firm/Rate/Total Value, LPR/Inc-Dec%,
    then totals and a per-firm summary block. Deliberately shaped so the
    app's own import_tender() can re-parse it (see
    test_reexporting_and_reimporting_round_trips_correctly) - not just a
    one-way report."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Comparative Statement"
    bold = Font(bold=True)

    suppliers = sorted(cs.suppliers_by_id.values(), key=lambda s: s.name)
    n = len(suppliers)

    rate_start_col = 6  # after Ser/Part No/Description/A-U/Qty
    lowest_col = rate_start_col + n
    lpr_col = lowest_col + 3
    incdec_col = lpr_col + 1

    ws.cell(row=1, column=1, value="COMPARATIVE STATEMENT").font = Font(bold=True, size=13)
    ws.cell(row=2, column=1, value=cs.tender.inquiry_no)

    header_row = 3
    ws.cell(row=header_row, column=1, value="Ser").font = bold
    ws.cell(row=header_row, column=2, value="Part No").font = bold
    ws.cell(row=header_row, column=3, value="Description").font = bold
    ws.cell(row=header_row, column=4, value="A/U").font = bold
    ws.cell(row=header_row, column=5, value="Qty").font = bold
    ws.cell(
        row=header_row,
        column=rate_start_col,
        value=f"Rate Quoted by Firms Excl {cs.tender.tax_percent:g}% {cs.tender.tax_type.value}",
    ).font = bold
    ws.cell(row=header_row, column=lowest_col, value="Lowest").font = bold
    ws.cell(row=header_row, column=lpr_col, value="LPR (Rs)").font = bold
    ws.cell(row=header_row, column=incdec_col, value="Inc/Dec %").font = bold
    if n > 1:
        ws.merge_cells(start_row=header_row, start_column=rate_start_col, end_row=header_row, end_column=lowest_col - 1)
    ws.merge_cells(start_row=header_row, start_column=lowest_col, end_row=header_row, end_column=lowest_col + 2)

    subheader_row = header_row + 1
    for i, supplier in enumerate(suppliers):
        ws.cell(row=subheader_row, column=rate_start_col + i, value=supplier.name)
    ws.cell(row=subheader_row, column=lowest_col, value="Firm").font = bold
    ws.cell(row=subheader_row, column=lowest_col + 1, value="Rate Rs.").font = bold
    ws.cell(row=subheader_row, column=lowest_col + 2, value="Total Value").font = bold

    # cs.item_results doesn't carry raw per-supplier rates, so pull them from
    # item.quotes (lazy-loaded via the still-open session) as we go.
    row = subheader_row + 1
    for r in cs.item_results:
        item = r.item
        ws.cell(row=row, column=1, value=item.ser)
        ws.cell(row=row, column=2, value=item.item_master.part_no)
        ws.cell(row=row, column=3, value=item.item_master.description)
        ws.cell(row=row, column=4, value=item.item_master.default_unit)
        ws.cell(row=row, column=5, value=item.qty)
        for i, supplier in enumerate(suppliers):
            rate = next((q.rate for q in item.quotes if q.supplier_id == supplier.id), None)
            ws.cell(row=row, column=rate_start_col + i, value=rate if rate is not None else "NQ")
        lowest_name = cs.suppliers_by_id[r.lowest_supplier_id].name if r.lowest_supplier_id else "NQ"
        ws.cell(row=row, column=lowest_col, value=lowest_name)
        ws.cell(row=row, column=lowest_col + 1, value=r.lowest_rate if r.lowest_rate is not None else 0)
        ws.cell(row=row, column=lowest_col + 2, value=r.total_value)
        ws.cell(row=row, column=lpr_col, value=item.lpr if item.lpr is not None else "-")
        ws.cell(row=row, column=incdec_col, value=f"{r.inc_dec_pct:.2f}" if r.inc_dec_pct is not None else "-")
        row += 1

    tax_label = cs.tender.tax_type.value
    row += 1
    ws.cell(row=row, column=1, value=f"Total Amount Excl {cs.tender.tax_percent:g}% {tax_label} (Rs)").font = bold
    ws.cell(row=row, column=lowest_col + 2, value=cs.grand_total.store_value)
    row += 1
    ws.cell(row=row, column=1, value=f"{cs.tender.tax_percent:g}% {tax_label} (Rs)").font = bold
    ws.cell(row=row, column=lowest_col + 2, value=cs.grand_total.tax_amount)
    row += 1
    ws.cell(row=row, column=1, value=f"Total Amount Incl {cs.tender.tax_percent:g}% {tax_label} (Rs)").font = bold
    ws.cell(row=row, column=lowest_col + 2, value=cs.grand_total.contract_value)

    row += 2
    ws.cell(row=row, column=4, value="SUMMARY").font = bold
    row += 1
    ws.cell(row=row, column=4, value="Firm").font = bold
    ws.cell(row=row, column=6, value="Items").font = bold
    ws.cell(row=row, column=7, value="Store Value").font = bold
    ws.cell(row=row, column=8, value=tax_label).font = bold
    ws.cell(row=row, column=9, value="Contr Value").font = bold
    row += 1
    for f in cs.firm_summaries:
        ws.cell(row=row, column=4, value=f.supplier_name)
        ws.cell(row=row, column=6, value=f.item_count)
        ws.cell(row=row, column=7, value=f.store_value)
        ws.cell(row=row, column=8, value=f.tax_amount)
        ws.cell(row=row, column=9, value=f.contract_value)
        row += 1
    ws.cell(row=row, column=4, value="G.Total").font = bold
    ws.cell(row=row, column=6, value=cs.grand_total.item_count).font = bold
    ws.cell(row=row, column=7, value=cs.grand_total.store_value).font = bold
    ws.cell(row=row, column=8, value=cs.grand_total.tax_amount).font = bold
    ws.cell(row=row, column=9, value=cs.grand_total.contract_value).font = bold

    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 36
    ws.column_dimensions["D"].width = 10
    ws.column_dimensions["E"].width = 8

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def export_purchase_proposal_xlsx(proposal: "PurchaseProposal") -> bytes:
    """Render a PurchaseProposal (app/award_engine.py) as an .xlsx workbook:
    one block per awarded firm, an unresolved-items block if any, and a
    grand total - for internal sign-off before contract drafts go out."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Purchase Proposal"

    bold = Font(bold=True)
    item_headers = ["Ser", "Part No", "Description", "Unit", "Qty", "Rate", "Total Value"]
    tax_label = proposal.tender.tax_type.value

    row = 1
    ws.cell(row=row, column=1, value=f"PURCHASE PROPOSAL - {proposal.tender.inquiry_no}").font = Font(
        bold=True, size=13
    )
    row += 2

    for group in proposal.firm_groups:
        ws.cell(row=row, column=1, value=f"Firm: {group.supplier_name}").font = bold
        row += 1

        for col, header in enumerate(item_headers, start=1):
            ws.cell(row=row, column=col, value=header).font = bold
        row += 1

        for ai in group.items:
            item = ai.item
            ws.cell(row=row, column=1, value=item.ser)
            ws.cell(row=row, column=2, value=item.item_master.part_no)
            ws.cell(row=row, column=3, value=item.item_master.description)
            ws.cell(row=row, column=4, value=item.item_master.default_unit)
            ws.cell(row=row, column=5, value=item.qty)
            ws.cell(row=row, column=6, value=ai.awarded_rate)
            ws.cell(row=row, column=7, value=ai.total_value)
            row += 1

        ws.cell(row=row, column=6, value="Store Value").font = bold
        ws.cell(row=row, column=7, value=group.store_value)
        row += 1
        ws.cell(row=row, column=6, value=tax_label).font = bold
        ws.cell(row=row, column=7, value=group.tax_amount)
        row += 1
        ws.cell(row=row, column=6, value="Contract Value").font = bold
        ws.cell(row=row, column=7, value=group.contract_value)
        row += 3

    if proposal.unresolved_items:
        ws.cell(row=row, column=1, value="Unresolved items (no valid award - not quoted / needs review)").font = bold
        row += 1
        for col, header in enumerate(["Ser", "Part No", "Description", "Unit", "Qty"], start=1):
            ws.cell(row=row, column=col, value=header).font = bold
        row += 1
        for item in proposal.unresolved_items:
            ws.cell(row=row, column=1, value=item.ser)
            ws.cell(row=row, column=2, value=item.item_master.part_no)
            ws.cell(row=row, column=3, value=item.item_master.description)
            ws.cell(row=row, column=4, value=item.item_master.default_unit)
            ws.cell(row=row, column=5, value=item.qty)
            row += 1
        row += 2

    ws.cell(row=row, column=1, value="GRAND TOTAL").font = bold
    ws.cell(row=row, column=2, value=f"Items: {proposal.grand_total.item_count}")
    ws.cell(row=row, column=6, value="Store Value").font = bold
    ws.cell(row=row, column=7, value=proposal.grand_total.store_value)
    row += 1
    ws.cell(row=row, column=6, value=tax_label).font = bold
    ws.cell(row=row, column=7, value=proposal.grand_total.tax_amount)
    row += 1
    ws.cell(row=row, column=6, value="Contract Value").font = bold
    ws.cell(row=row, column=7, value=proposal.grand_total.contract_value)

    for col_letter, width in zip("ABCDEFG", [6, 12, 36, 8, 8, 12, 14]):
        ws.column_dimensions[col_letter].width = width

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
