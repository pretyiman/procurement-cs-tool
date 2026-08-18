"""Admin-defined name/value text fields (Settings > Custom Fields).

Two consumers:
  - docx_export.py merges every field's tag_name/value into the PP/CA
    Word template context, so {{ tag_name }} works in the template the
    moment a field with that name exists - no code change needed.
  - excel_io.py looks up a small fixed set of recognised names (see
    SUGGESTED_CS_FIELDS) to fill in a designation/rank line under each CS
    Excel signature role, since that layout is fixed (openpyxl cells),
    not template-driven.

Real per-contract data (rates, firm names, computed totals - anything
that legitimately differs between documents) always overrides a
same-named custom field when merged, and a custom field can never be
created with a name the app itself already uses - see RESERVED_TAG_NAMES
and validate_tag_name(). Custom fields are only ever for text that's the
same on every document for a given department (a designation, a
boilerplate phrase, ...) - for text that's the same everywhere regardless
of department, just don't put it in a group (group_id=None).

A CustomFieldGroup lets the *value* behind a tag vary by which department
initiated the RFQ (e.g. Department A's initiating officer name/designation
differs from Department B's). custom_fields_dict_for_tender() resolves a
tender's department to its group automatically - no manual selection step.

**Document scoping.** Every suggested tag name is organized by which
document it fills a blank in - SUGGESTED_CS_FIELDS / SUGGESTED_PP_FIELDS /
SUGGESTED_CA_FIELDS - purely so the Settings UI can show "which document
does this affect" instead of one undifferentiated pile. A tag name present
in more than one of these dicts is "shared"; present in *all* of them is
"Global" (deliberately reserved for that - not a synonym for "not
department-scoped", see tag_document_scope()). None of the 19 built-in
tags are shared/Global today (every CS field is CS-only, every PP field
PP-only, every CA field CA-only) - the dicts just make it trivial to
declare one that legitimately is later (add the same tag name to more
than one dict). This classification is purely a *display* label: it
cannot be derived for a genuinely ad-hoc tag name an admin types in that
isn't one of these 19 (we have no way to know which document(s) they put
{{ that_tag }} into themselves) - those show up under "Other" instead.
"""

import re
from typing import Optional

from sqlmodel import Session, select

from .models import CustomField, CustomFieldGroup, Tender

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
# designation/rank line under the matching signature role. These represent
# the procurement/SCM office's own staff who prepare/check/approve the
# Comparative Statement itself - not the requesting department - so unlike
# the PP/CA fields below, they're not meant to vary per department; set
# them once, org-wide, as global fields (group_id=None).
SUGGESTED_CS_FIELDS = {
    "prep_by_designation": "Designation shown under \"Prep By\"",
    "checked_by_designation": "Designation shown under \"Checked by\"",
    "head_qac_designation": "Designation shown under the head-of-department role",
    "fmsad_designation": "Designation shown under the final-approver role",
}

# Blanks in pp_template.docx that were hardcoded generic placeholder text
# (e.g. "our Organization") until a real department's sample Purchase
# Proposal showed they actually vary per department - each is now
# {{ tag|default('...') }} in the template, so it renders exactly as
# before until a same-named custom field is created (typically inside a
# department's CustomFieldGroup - see the "Manage Tags" popup).
SUGGESTED_PP_FIELDS = {
    "pp_paying_authority": "Terms of Payment - who makes payment",
    "pp_prep_officer_rank_name": "Signature block - preparing officer's rank & name",
    "pp_prep_officer_department": "Signature block - preparing officer's department",
    "routing_mid_role": "Routing chain - the mid-level approver's role/title",
    "routing_md_hrf_remark": "Routing chain - remark shown next to the mid-level approver",
    "routing_final_role": "Routing chain - the final approver's role/title",
    "routing_final_remark": "Routing chain - remark shown next to the final approver",
}

# Same idea, for ca_template.docx.
SUGGESTED_CA_FIELDS = {
    "indentor_name": "\"Name of Indentor\" - who's requesting the stores",
    "cost_head": "\"Cost Debitable to Head\" - the fund/budget code",
    "country_of_origin": "\"Country of Origin\" line",
    "inspection_authority": "\"Inspection authority\" line",
    "inspection_officer_detail": "\"Inspection Officer\" - who the officer is detailed by",
    "place_of_inspection": "\"Place of Inspection\" line",
    "ca_paying_authority": "Terms of Payment - who makes payment",
    "secrecy_authority": "\"Secrecy\" clause - the authority who can release information",
}

# name -> which suggested dict it lives in, for tag_document_scope() below.
DOCUMENT_FIELD_SETS = {
    "CS": SUGGESTED_CS_FIELDS,
    "PP": SUGGESTED_PP_FIELDS,
    "CA": SUGGESTED_CA_FIELDS,
}


def tag_document_scope(tag_name: str) -> tuple:
    """Which suggested document(s) a tag name belongs to - e.g. ("CA",),
    or ("PP", "CA") for one shared across two (none exist today, but nothing
    stops a tag name from being added to more than one dict above). Empty
    tuple means it's not one of the built-in suggested names at all - a
    genuinely ad-hoc tag whose document we have no way to know."""
    return tuple(doc for doc, fields in DOCUMENT_FIELD_SETS.items() if tag_name in fields)


def classify_fields_by_scope(fields: list) -> dict:
    """Buckets a list of CustomField rows for display: "CS"/"PP"/"CA" for
    a tag used in exactly one document, "shared" for 2 (of today's 3),
    "global" for all 3 (every known document - reserved for that, not a
    stand-in for "not department-scoped"), "other" for anything not in
    any suggested list."""
    buckets: dict = {"CS": [], "PP": [], "CA": [], "shared": [], "global": [], "other": []}
    total_docs = len(DOCUMENT_FIELD_SETS)
    for f in fields:
        scope = tag_document_scope(f.tag_name)
        if not scope:
            buckets["other"].append(f)
        elif len(scope) == total_docs:
            buckets["global"].append(f)
        elif len(scope) > 1:
            buckets["shared"].append(f)
        else:
            buckets[scope[0]].append(f)
    return buckets

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


def list_custom_fields(session: Session, group_id: Optional[int] = None) -> list:
    """group_id=None (default) lists the plain global fields. Pass a
    CustomFieldGroup id to list that group's own fields instead."""
    return session.exec(
        select(CustomField).where(CustomField.group_id == group_id).order_by(CustomField.tag_name)
    ).all()


def custom_fields_dict(session: Session, group_id: Optional[int] = None) -> dict:
    """Global field values, with the given group's own values (if any)
    overlaid on top - a group only needs to define what's different for
    its department, everything else still falls back to the global value."""
    result = {f.tag_name: f.value for f in list_custom_fields(session, group_id=None)}
    if group_id is not None:
        result.update({f.tag_name: f.value for f in list_custom_fields(session, group_id=group_id)})
    return result


def custom_fields_dict_for_tender(session: Session, tender: Optional[Tender]) -> dict:
    """Resolves the tender's department to its CustomFieldGroup (if one
    exists) and returns the merged tag dict - what every document-
    generation route should call instead of custom_fields_dict directly."""
    group = resolve_group_for_department(session, tender.department_id if tender else None)
    return custom_fields_dict(session, group.id if group else None)


def create_custom_field(
    session: Session, tag_name: str, label: str, value: str, group_id: Optional[int] = None
) -> CustomField:
    tag_name = tag_name.strip()
    validate_tag_name(tag_name)
    existing = session.exec(
        select(CustomField).where(CustomField.group_id == group_id, CustomField.tag_name == tag_name)
    ).first()
    if existing is not None:
        raise ValueError(f'A custom field named "{tag_name}" already exists {"in this group" if group_id else ""}.')
    field = CustomField(group_id=group_id, tag_name=tag_name, label=label.strip() or tag_name, value=value)
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


# --- Custom Field Groups (per-department presets) ----------------------------


def list_groups(session: Session) -> list:
    return session.exec(select(CustomFieldGroup).order_by(CustomFieldGroup.name)).all()


def resolve_group_for_department(session: Session, department_id: Optional[int]) -> Optional[CustomFieldGroup]:
    if department_id is None:
        return None
    return session.exec(
        select(CustomFieldGroup).where(CustomFieldGroup.department_id == department_id)
    ).first()


def create_group(session: Session, name: str, department_id: Optional[int]) -> CustomFieldGroup:
    name = name.strip()
    if not name:
        raise ValueError("Group name is required.")
    if department_id is None:
        raise ValueError("A department is required.")
    if session.exec(select(CustomFieldGroup).where(CustomFieldGroup.name == name)).first() is not None:
        raise ValueError(f'A group named "{name}" already exists.')
    if resolve_group_for_department(session, department_id) is not None:
        raise ValueError("That department already has a Custom Field Group - edit it instead of creating another.")
    group = CustomFieldGroup(name=name, department_id=department_id)
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


def delete_group(session: Session, group_id: int) -> None:
    group = session.get(CustomFieldGroup, group_id)
    if group is None:
        return
    for field in list_custom_fields(session, group_id=group_id):
        session.delete(field)
    session.delete(group)
    session.commit()


def bulk_set_fields(session: Session, group_id: Optional[int], values: dict) -> None:
    """The "Manage Tags" popup (Settings > Custom Fields) saves every tag
    for one scope - organization-wide (group_id=None) or one department's
    profile - in a single submit, rather than the one-at-a-time add/edit
    flow. `values` is {tag_name: value} for every field the popup showed,
    edited or not - a blank value *deletes* that field (falling back to
    the next level up - a department profile falls back to the
    organization-wide value if any, which falls back to nothing) instead
    of saving an empty string, matching how clearing a field already
    works in the one-at-a-time flow.

    Every tag_name here must already be known-safe before this is called
    - either one of the built-in SUGGESTED_*_FIELDS constants, or an
    existing CustomField's tag_name (created earlier through
    create_custom_field(), which already validated it) - so this
    deliberately skips validate_tag_name() itself. The caller is
    responsible for validating any genuinely new tag name (e.g. from the
    popup's "+ Add another field" rows) before it ends up in `values`."""
    existing = {f.tag_name: f for f in list_custom_fields(session, group_id=group_id)}
    for tag_name, raw_value in values.items():
        value = (raw_value or "").strip()
        current = existing.get(tag_name)
        if not value:
            if current is not None:
                session.delete(current)
            continue
        if current is not None:
            current.value = value
            session.add(current)
        else:
            session.add(
                CustomField(group_id=group_id, tag_name=tag_name, label=tag_name.replace("_", " ").title(), value=value)
            )
    session.commit()
