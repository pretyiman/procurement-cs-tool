"""Last Purchase Rate (LPR) history across tenders.

LPR used to be typed in by hand per tender line, which made Inc/Dec%
meaningless unless someone remembered the previous contract's rate. This
tracks it automatically: once a tender is marked awarded (Tender.awarded_date
gets set - see main.py:mark_awarded), the rate actually paid for each item
becomes that item's LPR for the *next* tender that includes it.

Self-contained (doesn't import cs_engine/award_engine) - award resolution
for a single historical item is simple enough not to need the full
per-tender ComparativeStatement machinery.
"""

import datetime
from typing import Optional

from sqlmodel import Session, select

from .models import Item, Quote, Tender, TenderStatus


def get_last_purchase_rate(
    session: Session, item_master_id: int, exclude_tender_id: Optional[int] = None
) -> Optional[float]:
    """The awarded rate for item_master_id in the most recently *awarded*
    tender that included it (by Tender.awarded_date, falling back to
    tender id when dates are equal/missing), or None if it's never been
    awarded before."""
    query = (
        select(Item, Tender)
        .join(Tender, Item.tender_id == Tender.id)
        .where(Item.item_master_id == item_master_id, Tender.status == TenderStatus.awarded)
    )
    if exclude_tender_id is not None:
        query = query.where(Tender.id != exclude_tender_id)

    candidates = session.exec(query).all()
    if not candidates:
        return None

    candidates.sort(key=lambda pair: (pair[1].awarded_date or datetime.date.min, pair[1].id), reverse=True)
    item, _tender = candidates[0]

    quotes = session.exec(select(Quote).where(Quote.item_id == item.id)).all()
    rate_by_supplier = {q.supplier_id: q.rate for q in quotes if q.rate is not None}
    if not rate_by_supplier:
        return None

    if item.awarded_supplier_id is not None and item.awarded_supplier_id in rate_by_supplier:
        return rate_by_supplier[item.awarded_supplier_id]
    return min(rate_by_supplier.values())
