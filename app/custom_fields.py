"""Admin-defined name/value text fields (Settings > Custom Fields).

Two consumers:
  - docx_export.py merges every field's tag_name/value into the PP/CA
    Word template context, so {{ tag_name }} works in the template the
    moment a field with that name exists - no code change needed.
  - excel_io.py looks up a small fixed set of recognised names (see
    SUGGESTED_CS_SIGNATURE_FIELDS) to fill in a designation/rank line
    under each CS Excel signature role, since that layout is fixed
    (openpyxl cells), not template-driven.

Real per-contract data (rates, firm names, computed totals - anything
that legitimately differs between documents) always overrides a
same-named custom field when merged, and a custom field can never be
created with a name the app itself already uses - see RESERVED_TAG_NAMES
and validate_tag_name(). Custom fields are only ever for text that's the
same on every document (a designation, a boilerplate phrase, ...).
"""

import re

from sqlmodel import Session, select

from .models import CustomField

# Every top-level (or item/firm-group nested) key docx_export.py's context
# dicts already use - conservatively reserved even though most of these
# are nested, not top-level, since a custom field named e.g. "rate" would
# be confusing even where it wouldn't technically collide.
RESERVED_TAG_NAMES = {
    "agreement_date_words", "amount_in_words", "contract_date", "contract_no",
    "contract_value", "current_month", "current_year", "date", "delivery_days",
    "description", "est_cost", "firm_address", "firm_groups", "firm_name",
    "firms_invited_count", "grand_contract_value", "grand_store_value",
    "grand_tax_amount", "indent_date", "indent_no", "issue_date", "item_count",
    "items", "offered_rates", "opening_date", "overall_inc_dec", "part_no",
    "participating_firms_count", "qty", "rate", "security_deposit", "ser",
    "stamp_duty", "store_value", "subject_department", "supplier_name",
    "tax_amount", "tax_percent", "tax_type", "tender_inquiry_no",
    "total_item_count", "total_value", "unit", "warranty_months",
}

# Names the CS Excel export specifically looks for (excel_io.py) to add a
# designation/rank line under the matching signature role. Shown as
# suggestions in the Custom Fields settings UI - not enforced, just the
# convention that makes them actually appear on the Excel export.
SUGGESTED_CS_SIGNATURE_FIELDS = {
    "prep_by_designation": "Designation shown under \"Prep By\" on the CS Excel export",
    "checked_by_designation": "Designation shown under \"Checked by\" on the CS Excel export",
    "head_qac_designation": "Designation shown under the head-of-department role on the CS Excel export",
    "fmsad_designation": "Designation shown under the final-approver role on the CS Excel export",
}

_TAG_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


def slugify_tag_name(label: str) -> str:
    """"Prep By - Designation" -> "prep_by_designation" - a starting
    suggestion the admin can still edit before saving."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    if not slug or slug[0].isdigit():
        slug = f"field_{slug}" if slug else "field"
    return slug


def validate_tag_name(tag_name: str) -> None:
    """Raises ValueError with a message fit to show the admin directly."""
    if not _TAG_NAME_PATTERN.match(tag_name):
        raise ValueError(
            "Tag name must contain only lowercase letters, numbers, and underscores, "
            "and can't start with a number (e.g. \"prep_by_designation\")."
        )
    if tag_name in RESERVED_TAG_NAMES:
        raise ValueError(
            f'"{tag_name}" is already used internally for real document data and can\'t '
            "be reused as a custom field name."
        )


def list_custom_fields(session: Session) -> list:
    return session.exec(select(CustomField).order_by(CustomField.tag_name)).all()


def custom_fields_dict(session: Session) -> dict:
    return {f.tag_name: f.value for f in list_custom_fields(session)}


def create_custom_field(session: Session, tag_name: str, label: str, value: str) -> CustomField:
    tag_name = tag_name.strip()
    validate_tag_name(tag_name)
    existing = session.exec(select(CustomField).where(CustomField.tag_name == tag_name)).first()
    if existing is not None:
        raise ValueError(f'A custom field named "{tag_name}" already exists.')
    field = CustomField(tag_name=tag_name, label=label.strip() or tag_name, value=value)
    session.add(field)
    session.commit()
    session.refresh(field)
    return field


def update_custom_field(session: Session, field_id: int, label: str, value: str) -> CustomField:
    """Tag name is deliberately not editable here - renaming it would
    silently break any template that already references the old name.
    Delete and recreate if a rename is genuinely needed."""
    field = session.get(CustomField, field_id)
    if field is None:
        raise ValueError("Custom field not found.")
    field.label = label.strip() or field.tag_name
    field.value = value
    session.add(field)
    session.commit()
    return field


def delete_custom_field(session: Session, field_id: int) -> None:
    field = session.get(CustomField, field_id)
    if field is not None:
        session.delete(field)
        session.commit()
