from enum import Enum
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel


class TenderStatus(str, Enum):
    draft = "draft"
    proposal_generated = "proposal_generated"
    awarded = "awarded"


class Tender(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    inquiry_no: str
    gst_percent: float = 18.0
    status: TenderStatus = TenderStatus.draft

    items: List["Item"] = Relationship(back_populates="tender")


class Supplier(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    address: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    tax_no: Optional[str] = None


class Item(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tender_id: int = Field(foreign_key="tender.id")
    ser: int
    part_no: str
    description: str
    unit: str
    qty: float
    lpr: Optional[float] = None
    awarded_supplier_id: Optional[int] = Field(default=None, foreign_key="supplier.id")
    award_reason: Optional[str] = None

    tender: Optional[Tender] = Relationship(back_populates="items")
    quotes: List["Quote"] = Relationship(back_populates="item")


class Quote(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="item.id")
    supplier_id: int = Field(foreign_key="supplier.id")
    rate: Optional[float] = None  # None = NQ (not quoted)

    item: Optional[Item] = Relationship(back_populates="quotes")
