import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class TenderStatus(str, Enum):
    draft = "draft"
    proposal_generated = "proposal_generated"
    awarded = "awarded"


class TaxType(str, Enum):
    GST = "GST"
    PST = "PST"


class Tender(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    inquiry_no: str
    tax_type: TaxType = TaxType.GST
    tax_percent: float = 18.0
    status: TenderStatus = TenderStatus.draft
    awarded_date: Optional[datetime.date] = None  # set when marked awarded; feeds LPR history

    items: List["Item"] = Relationship(back_populates="tender")


class Supplier(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    address: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    tax_no: Optional[str] = None


class ItemMaster(SQLModel, table=True):
    """Reusable item catalog. Unique on (part_no, description) together,
    not part_no alone: non-inventory items commonly share a generic part_no
    ("NIV") but are distinct items, distinguished only by description."""

    __tablename__ = "item_master"
    __table_args__ = (UniqueConstraint("part_no", "description", name="uq_item_master_part_desc"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    part_no: str = Field(index=True, default="")
    description: str
    default_unit: str = ""


class Item(SQLModel, table=True):
    """A tender line: a catalog item (ItemMaster) required in a specific
    quantity for one tender. Part No/Description/Unit live on the catalog
    row (item_master), not here."""

    id: Optional[int] = Field(default=None, primary_key=True)
    tender_id: int = Field(foreign_key="tender.id")
    item_master_id: int = Field(foreign_key="item_master.id")
    ser: int
    qty: float
    lpr: Optional[float] = None
    awarded_supplier_id: Optional[int] = Field(default=None, foreign_key="supplier.id")
    award_reason: Optional[str] = None

    tender: Optional[Tender] = Relationship(back_populates="items")
    item_master: Optional[ItemMaster] = Relationship()
    quotes: List["Quote"] = Relationship(back_populates="item")


class Quote(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="item.id")
    supplier_id: int = Field(foreign_key="supplier.id")
    rate: Optional[float] = None  # None = NQ (not quoted)

    item: Optional[Item] = Relationship(back_populates="quotes")


class TenderTemplate(SQLModel, table=True):
    """A saved item list (part numbers + quantities) for a recurring
    tender, so a new tender can be pre-populated instead of re-adding the
    same items every time. Deliberately holds no suppliers/quotes - those
    are always entered fresh per tender."""

    __tablename__ = "tender_template"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)

    lines: List["TenderTemplateItem"] = Relationship(back_populates="template")


class TenderTemplateItem(SQLModel, table=True):
    __tablename__ = "tender_template_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    template_id: int = Field(foreign_key="tender_template.id")
    item_master_id: int = Field(foreign_key="item_master.id")
    ser: int
    qty: float

    template: Optional[TenderTemplate] = Relationship(back_populates="lines")
    item_master: Optional[ItemMaster] = Relationship()
