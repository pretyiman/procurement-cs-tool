import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class TenderStatus(str, Enum):
    draft = "draft"
    proposal_generated = "proposal_generated"
    proposal_approved = "proposal_approved"
    awarded = "awarded"


class TaxType(str, Enum):
    GST = "GST"
    PST = "PST"


class Department(SQLModel, table=True):
    """Reusable department/section catalog - same pattern as Supplier and
    ItemMaster (create once, pick from a dropdown on every RFQ after)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)


class Tender(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    inquiry_no: str
    tax_type: TaxType = TaxType.GST
    tax_percent: float = 18.0
    status: TenderStatus = TenderStatus.draft
    awarded_date: Optional[datetime.date] = None  # set when marked awarded; feeds LPR history

    # Narrative/administrative details that only appear on generated PP/CA
    # documents (Phase 12) - optional, filled in on the tender detail page
    # before generating documents; sensible defaults if left blank.
    indent_no: Optional[str] = None  # defaults to inquiry_no when rendering if blank
    department_id: Optional[int] = Field(default=None, foreign_key="department.id")
    firms_invited_count: Optional[int] = None
    issue_date: Optional[datetime.date] = None
    opening_date: Optional[datetime.date] = None
    delivery_days: int = 60
    warranty_months: int = 3

    items: List["Item"] = Relationship(back_populates="tender")
    department: Optional[Department] = Relationship()


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


class ProposalSnapshot(SQLModel, table=True):
    """A frozen record of the Purchase Proposal at the moment it was
    generated - which firms won which items, at what rates, and the
    totals - so a Contract Award always renders from what was actually
    approved, never from whatever the live Item/Quote/catalog data says
    today (which could have changed since - an award override, a renamed
    Supplier, an edited ItemMaster description).

    One row per tender (unique tender_id). Overwritten in place (this row
    and all its firm_groups/items deleted and recreated) every time
    "Generate Proposal" runs while status is still proposal_generated -
    that's the revise-after-rejection cycle. Once approved_at is set
    (status -> proposal_approved) it becomes read-only: no more
    regenerating, and Contract Award downloads are only allowed from here
    on, always reading this frozen data."""

    __tablename__ = "proposal_snapshot"

    id: Optional[int] = Field(default=None, primary_key=True)
    tender_id: int = Field(foreign_key="tender.id", unique=True)
    generated_at: datetime.datetime
    approved_at: Optional[datetime.datetime] = None

    # Document-detail fields as they were at generation time (Tender's own
    # copies of these can't be edited after the item-lock work anyway, but
    # freezing them here too means this snapshot is self-contained).
    indent_no: str
    department_name: Optional[str] = None
    firms_invited_count: Optional[int] = None
    issue_date: Optional[datetime.date] = None
    opening_date: Optional[datetime.date] = None
    delivery_days: int
    warranty_months: int
    tax_type: str
    tax_percent: float
    participating_firms_count: int  # every firm that quoted, win or not - for the PP doc's "X firms invited, Y quoted"
    total_item_count: int  # every item on the RFQ, including any left unresolved (unlike grand_item_count below)

    grand_item_count: int  # awarded items only - what's actually in this proposal's firm groups
    grand_store_value: float
    grand_tax_amount: float
    grand_contract_value: float

    firm_groups: List["ProposalSnapshotFirmGroup"] = Relationship(
        back_populates="snapshot", sa_relationship_kwargs={"order_by": "ProposalSnapshotFirmGroup.supplier_name"}
    )


class ProposalSnapshotFirmGroup(SQLModel, table=True):
    """One winning firm within a ProposalSnapshot. supplier_name is a
    frozen copy (not just a live join through supplier_id) so a later
    Supplier rename doesn't retroactively change an already-approved
    proposal's history."""

    __tablename__ = "proposal_snapshot_firm_group"

    id: Optional[int] = Field(default=None, primary_key=True)
    snapshot_id: int = Field(foreign_key="proposal_snapshot.id")
    supplier_id: int = Field(foreign_key="supplier.id")
    supplier_name: str
    store_value: float
    tax_amount: float
    contract_value: float

    snapshot: Optional[ProposalSnapshot] = Relationship(back_populates="firm_groups")
    items: List["ProposalSnapshotItem"] = Relationship(
        back_populates="firm_group", sa_relationship_kwargs={"order_by": "ProposalSnapshotItem.ser"}
    )


class ProposalSnapshotItem(SQLModel, table=True):
    """One awarded line item within a ProposalSnapshotFirmGroup - part
    no./description/unit frozen as text (not a live ItemMaster join),
    same reasoning as supplier_name above."""

    __tablename__ = "proposal_snapshot_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    firm_group_id: int = Field(foreign_key="proposal_snapshot_firm_group.id")
    ser: int
    part_no: str
    description: str
    unit: str
    qty: float
    rate: float
    total_value: float
    lpr: Optional[float] = None  # frozen Last Purchase Rate, for the PP doc's Inc/Dec% - see number_words/docx_export
    is_override: bool = False
    override_reason: Optional[str] = None

    firm_group: Optional[ProposalSnapshotFirmGroup] = Relationship(back_populates="items")


class ContractAward(SQLModel, table=True):
    """A persisted contract number for one winning firm on one tender's
    approved proposal. contract_no is a different number series than the
    RFQ's inquiry_no - assigned per firm, only once a ProposalSnapshot is
    approved, so a Contract Award page can show/reuse the same number on
    every visit instead of re-asking. Finalizing a tender to `awarded`
    requires every firm in the approved snapshot to have one of these."""

    __tablename__ = "contract_award"
    __table_args__ = (UniqueConstraint("snapshot_id", "supplier_id", name="uq_contract_award_snapshot_supplier"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    snapshot_id: int = Field(foreign_key="proposal_snapshot.id")
    supplier_id: int = Field(foreign_key="supplier.id")
    contract_no: str
    contract_date: Optional[datetime.date] = None
    created_at: datetime.datetime


class BusinessRules(SQLModel, table=True):
    """Singleton settings row (always id=1) for policy numbers that
    shouldn't require a code change to adjust - e.g. the security deposit
    percentage and the contract value below which it's waived entirely.
    Previously these were hardcoded constants in docx_export.py."""

    __tablename__ = "business_rules"

    id: Optional[int] = Field(default=None, primary_key=True)
    security_deposit_percent: float = 5.0
    security_deposit_waived_below: float = 0.0  # 0 = never waived (today's behavior, unconditional)
    stamp_duty_percent: float = 0.25


class DocumentLabels(SQLModel, table=True):
    """Singleton settings row (always id=1) for the static title/
    signature-block text on the CS Excel export - previously hardcoded
    strings in excel_io.py. Same role signatures as the original CS.xlsx
    (see excel_io.py's _sig_slot), just editable without a code change."""

    __tablename__ = "document_labels"

    id: Optional[int] = Field(default=None, primary_key=True)
    cs_title: str = "COMPARATIVE STATEMENT"
    prep_by_label: str = "Prep By"
    checked_by_label: str = "Checked by"
    head_qac_label: str = "HEAD QAC (TDA)"
    countersigned_label: str = "COUNTERSIGNED"
    fmsad_label: str = "FMSAD (XDS)"


class LockSettings(SQLModel, table=True):
    """Singleton settings row (always id=1) for the optional local
    workspace lock (Settings > Lock, and the sidebar Lock button). This is
    explicitly NOT real security - a local convenience passcode so the
    screen isn't left open, matching the app's "no accounts, data stays
    local" design (see app/lock.py). passcode_hash=None means the lock is
    disabled (today's behavior, the default) - the app never gates access
    until an admin sets a passcode here."""

    __tablename__ = "lock_settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    passcode_hash: Optional[str] = None


class CustomFieldGroup(SQLModel, table=True):
    """A department's own preset of tag values (e.g. Department A's
    initiating-officer name/designation, receiving store/authority - values
    that are legitimately different for Department B). Tied 1:1 to a
    Department so it's picked up automatically from whichever department a
    tender belongs to - no manual per-document selection step. A tag not
    set in the group falls back to the plain (ungrouped) CustomField of the
    same name - see custom_fields.custom_fields_dict."""

    __tablename__ = "custom_field_group"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    department_id: Optional[int] = Field(default=None, foreign_key="department.id", unique=True)

    department: Optional[Department] = Relationship()


class CustomField(SQLModel, table=True):
    """Admin-defined name/value text pairs (NOT a singleton - there can be
    any number of these), usable as {{ tag_name }} in the PP/CA Word
    templates and, for a handful of recognised names (see
    custom_fields.SUGGESTED_CS_FIELDS), as designation lines
    under a role on the CS Excel signature block. Exists so a genuinely
    new static field (e.g. a signatory's designation/rank) never needs a
    new DB column/code change - see app/custom_fields.py.

    group_id NULL = the plain global value (today's behavior). A non-null
    group_id scopes this field to one CustomFieldGroup, overriding the
    same-named global field only for documents generated under that
    group's department."""

    __tablename__ = "custom_field"
    __table_args__ = (UniqueConstraint("group_id", "tag_name", name="uq_custom_field_group_tag"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: Optional[int] = Field(default=None, foreign_key="custom_field_group.id")
    tag_name: str = Field(index=True)
    label: str  # human-readable description shown in the settings UI
    value: str = ""

    group: Optional[CustomFieldGroup] = Relationship()


class TenderTemplateItem(SQLModel, table=True):
    __tablename__ = "tender_template_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    template_id: int = Field(foreign_key="tender_template.id")
    item_master_id: int = Field(foreign_key="item_master.id")
    ser: int
    qty: float

    template: Optional[TenderTemplate] = Relationship(back_populates="lines")
    item_master: Optional[ItemMaster] = Relationship()
