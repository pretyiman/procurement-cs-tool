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
from pathlib import Path
from typing import Union

from openpyxl import load_workbook
from sqlmodel import Session, select

from .models import Item, Quote, Supplier, Tender, TenderStatus

_GST_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*GST", re.IGNORECASE)
_NQ_VALUES = {"NQ", "-", ""}


def _is_nq(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().upper() in _NQ_VALUES:
        return True
    return False


def _parse_gst_percent(rows) -> float:
    for row in rows:
        for cell in row:
            if isinstance(cell, str):
                match = _GST_PATTERN.search(cell)
                if match:
                    return float(match.group(1))
    return 18.0


def _find_inquiry_no(rows, header_idx: int) -> str:
    for row in rows[:header_idx]:
        for cell in row:
            if isinstance(cell, str) and "inquiry" in cell.lower():
                return cell.strip()
    return "UNSPECIFIED"


def _get_or_create_supplier(session: Session, name: str) -> Supplier:
    name = name.strip()
    existing = session.exec(select(Supplier).where(Supplier.name == name)).first()
    if existing:
        return existing
    supplier = Supplier(name=name)
    session.add(supplier)
    session.flush()
    return supplier


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
    gst_percent = _parse_gst_percent(rows)

    tender = Tender(inquiry_no=inquiry_no, gst_percent=gst_percent, status=TenderStatus.draft)
    session.add(tender)
    session.flush()

    suppliers = [_get_or_create_supplier(session, name) for name in supplier_names]

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
        item = Item(
            tender_id=tender.id,
            ser=int(row[0]),
            part_no=str(row[1]) if row[1] is not None else "",
            description=str(row[2]) if row[2] is not None else "",
            unit=str(row[3]) if row[3] is not None else "",
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
