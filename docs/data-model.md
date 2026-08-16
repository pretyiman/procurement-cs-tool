# Data Model

Source of truth for entities/fields. Update this file first if the model
changes; code and `PLAN.md` verification steps should match it.

## Entities

### Tender
| field | type | notes |
|---|---|---|
| id | int, PK | |
| inquiry_no | text | e.g. "Tender Inquiry No. xxxxx" |
| tax_type | enum | `GST` or `PST` - user-selected per tender |
| tax_percent | decimal | e.g. 18.0; applies to whichever tax_type is selected |
| status | enum | `draft` -> `proposal_generated` -> `proposal_approved` -> `awarded` (see ProposalSnapshot below) |
| awarded_date | date, nullable | set when status -> `awarded`; feeds LPR history (see below) |
| indent_no | text, nullable | for PP/CA documents; defaults to inquiry_no when rendering if blank |
| department_id | int, FK -> Department, nullable | for PP; picked from the reusable Department catalog (see below) |
| firms_invited_count | int, nullable | for PP para - how many firms the inquiry was sent to |
| issue_date | date, nullable | for PP - when the tender inquiry was issued |
| opening_date | date, nullable | for PP - tender opening date |
| delivery_days | int, default 60 | for CA delivery clause |
| warranty_months | int, default 3 | for CA warranty clause |

### ItemMaster (catalog — reusable across tenders)
| field | type | notes |
|---|---|---|
| id | int, PK | |
| part_no | text | e.g. "A-2394"; can be "NIV" (non-inventory) - not globally unique by itself |
| description | text | |
| default_unit | text | "A/U", e.g. Kg, Nos, Lit |

Unique on `(part_no, description)` together. Non-inventory items in the
source data reuse part_no "NIV" for genuinely different items, so part_no
alone can't be the dedup key - see `get_or_create_item_master` in
`app/excel_io.py`.

### Item (a tender line — qty of one catalog item needed for one tender)
| field | type | notes |
|---|---|---|
| id | int, PK | |
| tender_id | FK | |
| item_master_id | FK -> ItemMaster | part_no/description/unit come from here |
| ser | int | display order / serial no. |
| qty | decimal | |
| lpr | decimal, nullable | Last Purchase Rate, for Inc/Dec% |
| awarded_supplier_id | FK, nullable | overrides computed-lowest when set |
| award_reason | text, nullable | optional free-text note; no longer collected via the UI (see Derived section) but the field/display remain for any legacy data |

### Supplier
| field | type | notes |
|---|---|---|
| id | int, PK | |
| name | text | e.g. "M/s Awan Tech" — reusable across tenders |
| address | text, nullable | needed for contract draft |
| contact_person | text, nullable | |
| phone | text, nullable | |
| email | text, nullable | |
| tax_no | text, nullable | NTN/STRN, needed for contract draft |

### Quote
| field | type | notes |
|---|---|---|
| id | int, PK | |
| item_id | FK | |
| supplier_id | FK | |
| rate | decimal, nullable | null/absent = "NQ" (not quoted) |

### Department (catalog — reusable across tenders)
| field | type | notes |
|---|---|---|
| id | int, PK | |
| name | text, unique | e.g. "Procurement Section" - picked via dropdown on RFQ creation, same pattern as ItemMaster/Supplier |

### TenderTemplate / TenderTemplateItem (saved item lists, for recurring tenders)
| field | type | notes |
|---|---|---|
| TenderTemplate.id | int, PK | |
| TenderTemplate.name | text, unique | |
| TenderTemplateItem.id | int, PK | |
| TenderTemplateItem.template_id | FK -> TenderTemplate | |
| TenderTemplateItem.item_master_id | FK -> ItemMaster | |
| TenderTemplateItem.ser | int | |
| TenderTemplateItem.qty | decimal | |

Deliberately holds no suppliers/quotes/tax fields - only the item list, so
starting a new tender "from template" copies item lines (with qty) but
always requires fresh quotes and a fresh GST/PST choice.

### BusinessRules (singleton settings row, always id=1)
| field | type | notes |
|---|---|---|
| id | int, PK | always 1 - get_business_rules() (app/business_rules.py) get-or-creates this row |
| security_deposit_percent | decimal, default 5.0 | % of store value, Contract Award's security deposit clause |
| security_deposit_waived_below | decimal, default 0.0 | contract value below which the deposit is skipped entirely (0 = never waived) |
| stamp_duty_percent | decimal, default 0.25 | % of contract value, Contract Award's stamp duty clause |

Editable via `/settings/business-rules` instead of being hardcoded
constants in `docx_export.py` - these are policy numbers a procurement
office may legitimately need to change (e.g. a deposit-waiver threshold)
without a code deploy.

### DocumentLabels (singleton settings row, always id=1)
| field | type | notes |
|---|---|---|
| id | int, PK | always 1 - get_document_labels() (app/document_labels.py) get-or-creates this row |
| cs_title | text, default "COMPARATIVE STATEMENT" | CS Excel export title banner (package export appends " (PACKAGE BASIS)") |
| prep_by_label | text, default "Prep By" | CS export signature block |
| checked_by_label | text, default "Checked by" | CS export signature block |
| head_qac_label | text, default "HEAD QAC (TDA)" | CS export signature block |
| countersigned_label | text, default "COUNTERSIGNED" | CS export signature block |
| fmsad_label | text, default "FMSAD (XDS)" | CS export signature block |

Editable via `/settings/cs-labels` instead of being hardcoded strings in
`excel_io.py` - both `export_cs_xlsx` and `export_package_cs_xlsx` take
these as a required `labels` argument, so a role/title change never
needs a code change.

### CustomFieldGroup (multiple rows - a department's own tag preset)
| field | type | notes |
|---|---|---|
| id | int, PK | |
| name | text, unique | e.g. "Department A" - admin-chosen label shown in Settings |
| department_id | int, FK -> Department, unique, nullable | which department this group applies to; unique so a department has at most one group |

Editable via `/settings/custom-fields`. Exists because a flat global
`CustomField` value can't represent "Department A's initiating officer
name/designation differs from Department B's" - see `app/custom_fields.py`.
`custom_fields_dict_for_tender()` resolves a tender's `department_id` to
its group automatically (no manual per-document selection); a tender with
no department, or a department with no group, just uses global
`CustomField` rows as-is.

### CustomField (multiple rows - not a singleton)
| field | type | notes |
|---|---|---|
| id | int, PK | |
| group_id | int, FK -> CustomFieldGroup, nullable | NULL = the plain global value (original behavior); non-null scopes this field to one department's group, overriding the same-named global field only for that department's documents |
| tag_name | text | valid Jinja identifier (lowercase/digits/underscore, no leading digit); reserved names (real computed context keys - see `custom_fields.RESERVED_TAG_NAMES`) are rejected. Unique per `group_id` (enforced in `app/custom_fields.py`; also a DB constraint `uq_custom_field_group_tag` for the non-null-group case) - the same tag_name can exist once globally *and* once per group, with the group's value winning for that department |
| label | text | human-readable description shown in the Settings UI |
| value | text | the actual text substituted wherever this tag is used |

Editable via `/settings/custom-fields`. Every field's `tag_name`/`value` is
merged into the PP/CA Word template context (`docx_export.py`) and the CS
Excel export context, so `{{ tag_name }}` works in
`pp_template.docx`/`ca_template.docx` the moment a field with that name
exists - no code change needed. A document's context is the global fields
with its tender's department-group fields (if any) overlaid on top - see
`custom_fields.custom_fields_dict_for_tender()`. Real per-contract data
always overrides a same-named custom field if merged (defense in depth on
top of the reserved-name check at creation time). A handful of recognised
names (`prep_by_designation`, `checked_by_designation`,
`head_qac_designation`, `fmsad_designation` - see
`custom_fields.SUGGESTED_CS_SIGNATURE_FIELDS`) are also picked up by the
CS Excel export to show a designation/rank line under the matching
signature role, since that layout is fixed cells (openpyxl), not
template-tag-driven like the Word docs.

### ProposalSnapshot (frozen Purchase Proposal, one per tender)
| field | type | notes |
|---|---|---|
| id | int, PK | |
| tender_id | int, FK -> Tender, unique | one row per tender |
| generated_at | datetime | set every time "Generate Proposal" runs |
| approved_at | datetime, nullable | set by "Approve Proposal" - once set, the snapshot is read-only |
| indent_no, department_name, firms_invited_count, issue_date, opening_date, delivery_days, warranty_months, tax_type, tax_percent, participating_firms_count, total_item_count | copies | Tender's document-detail fields as they were at generation time |
| grand_item_count, grand_store_value, grand_tax_amount, grand_contract_value | numbers | awarded-items-only totals (unlike `total_item_count`, which includes unresolved items) |

The lifecycle is `draft` -> `proposal_generated` -> `proposal_approved` ->
`awarded` (see Tender.status above). `app/proposal_snapshot.py`'s
`save_proposal_snapshot()` builds this (and its firm_groups/items below)
from the *live* award state every time "Generate Proposal" runs, deleting
and recreating the whole snapshot - that's the intended revise-after-
rejection cycle while still `proposal_generated`. `approve_proposal_snapshot()`
sets `approved_at` and moves status to `proposal_approved`, after which
`save_proposal_snapshot()` refuses to run again. Both `generate_contract_award()`
and `generate_purchase_proposal_doc()` (`docx_export.py`) render only from
this frozen data from `proposal_generated` onward, never from live
Item/Quote/catalog state - see ProposalSnapshotFirmGroup/Item below for why.

### ProposalSnapshotFirmGroup / ProposalSnapshotItem
| field | type | notes |
|---|---|---|
| ProposalSnapshotFirmGroup.snapshot_id | int, FK -> ProposalSnapshot | |
| ProposalSnapshotFirmGroup.supplier_id | int, FK -> Supplier | |
| ProposalSnapshotFirmGroup.supplier_name | text | **frozen copy**, not just a live join - a later Supplier rename can't retroactively change an approved proposal's history |
| ProposalSnapshotFirmGroup.store_value / tax_amount / contract_value | decimal | this firm's totals, frozen |
| ProposalSnapshotItem.firm_group_id | int, FK -> ProposalSnapshotFirmGroup | |
| ProposalSnapshotItem.ser / part_no / description / unit / qty / rate / total_value | frozen copies | same reasoning as supplier_name - protects against a later ItemMaster edit |
| ProposalSnapshotItem.lpr | decimal, nullable | frozen Last Purchase Rate, for the PP doc's Inc/Dec% |
| ProposalSnapshotItem.is_override / override_reason | | frozen copy of the award override, if any |

One winning firm per ProposalSnapshotFirmGroup, one awarded line item per
ProposalSnapshotItem - together they're a full frozen copy of one firm's
slice of the proposal, everything a Contract Award document needs.
Supplier's *address* is the one deliberate exception left un-frozen (looked
up live at render time) - see the docx_export.py module docstring.

### ContractAward (persisted contract number, one per snapshot+firm)
| field | type | notes |
|---|---|---|
| id | int, PK | |
| snapshot_id | int, FK -> ProposalSnapshot | |
| supplier_id | int, FK -> Supplier | |
| contract_no | text | a **different number series than `Tender.inquiry_no`** - the RFQ/inquiry number identifies the solicitation, the contract number identifies one specific awarded contract with one firm, assigned later |
| contract_date | date, nullable | |
| created_at | datetime | |

Unique on `(snapshot_id, supplier_id)`. Created/updated by
`proposal_snapshot.upsert_contract_award()` the first time a firm's
Contract Award is downloaded (`/tenders/{id}/proposal/contract/{supplier_id}`),
so re-downloading later reuses the same number instead of asking again.
Finalizing a tender to `awarded` (`mark_awarded` in `main.py`) requires
every firm in the approved snapshot to have one of these -
`proposal_snapshot.all_firms_have_contract_award()`.

## Derived (never stored, always computed)

- **Lowest rate / lowest firm per item** = min(rate) across quotes where
  rate is not null. If no supplier quoted (all NQ), item has no lowest.
  When multiple suppliers quote the exact same minimum, every one of them
  is recorded (`ItemResult.tied_supplier_ids`, `is_tied` when len > 1) -
  the actual pick (`lowest_supplier_id`) is the lowest supplier_id among
  the tied ones, a disclosed/deterministic rule, not whichever quote row
  the database happened to return first. Surfaced as a "TIE" badge in the
  Comparative Summary UI rather than resolved silently.
- **Lowest-count leaderboard** (`compute_lowest_count_leaderboard` in
  `cs_engine.py`): how many items each supplier is the resolved lowest
  bidder on, and how much value that represents - shown as secondary
  ("Per-item breakdown", collapsed by default) detail on the Comparative
  Summary page. Excel export is deliberately untouched by any of this -
  it's an in-app-only view.
- **Sourcing Options / supplier bundles** (`compute_best_bundle`,
  `compute_bundle_lineup` in `cs_engine.py`) - the *primary* content of
  the Comparative Summary page's analysis panel: for an adjustable set of
  bundle sizes (default 1 through min(5, quoting supplier count), plus
  whatever else the admin types in), the cheapest combination of exactly
  that many suppliers that covers the most items, cost broken second
  among ties on coverage. Partial bidders are eligible for bundle
  membership, not just suppliers who individually cover everything (that
  was the whole point - "these 3 suppliers together cover everything for
  Rs X" vs. paying more for one supplier who covers it alone). Brute-force
  search over `itertools.combinations`, capped at
  `MAX_BUNDLE_BRUTE_FORCE_COMBINATIONS` (200,000) past which it falls back
  to a greedy (not-necessarily-optimal) approximation - flagged
  `approximate=True` when that happens. The cheapest fully-covering bundle
  across the shown sizes is marked "BEST VALUE" in the UI - when a larger
  bundle ties that same value (an extra member who never actually
  undercuts anyone), only the smallest tied size gets "BEST VALUE"; larger
  ties get a muted "Same value, N is enough" note instead
  (`bundle.bundle_size` compared, not raw `contract_value` equality, since
  float equality on cost alone would flag every tied size).
  `SupplierBundle.items` (`BundleItemAssignment`, computed once per winning
  combo via `_bundle_item_assignments()`, not during the search) gives the
  item-by-item detail - which member supplies each item and at what rate,
  or `supplier_id=None`/`rate=None` for items outside that bundle's
  coverage (rendered as "not covered by this bundle"). On the Comparative
  Summary page, the "Full bidders" stat card and every bundle card are
  click-to-reveal: clicking shows that card's detail table (full-bidder
  list, or a bundle's per-item breakdown) in a shared
  `#analysis-detail-area`, positioned right after the bundle-card row and
  above the collapsed "Per-item breakdown" leaderboard. All panels are
  server-rendered upfront (consistent with the project's no-AJAX
  architecture) - vanilla JS (`showAnalysisDetail`/`hideAnalysisDetail` in
  `comparative_summary.html`) only toggles `display:none`, accordion-style
  (opening one hides any other), with a Close button per panel.
- **Comparative Summary page tabs**: below the stats bar, the page is
  split into three tabs - "Sourcing Options" (bundle cards, their detail
  panels, and the collapsed leaderboard), "Price Comparison" (the
  click-to-award pills table; internal id `tab-award` is unchanged from
  when the tab was labeled "Award Decisions" - only the visible label and
  `<h2>` changed, not the hash/id, to avoid touching the redirect/anchor
  wiring described below), and "All Quotes" (the shared
  `_comparative_summary_grid.html` item/package grid + Download link).
  Same server-rendered/vanilla-JS approach as the detail panels
  (`showTab()`), except tab state also survives the page's several
  full-reload actions (switching item/package view, updating bundle
  sizes, awarding an item) via a URL hash (`#tab-sourcing` /
  `#tab-award` / `#tab-quotes`) - each control is statically anchored to
  its own tab's hash (its GET form action, its `location.href` navigation,
  or its POST route's 303 redirect in `main.py`), since each control only
  ever lives inside one specific tab, so no dynamic "current tab" state
  needs to be threaded through the server.
- **Awarding to a non-lowest bidder needs no reason.** Earlier,
  `award_engine.validate_override()` raised a ValueError unless
  `Item.award_reason` was set whenever the chosen supplier wasn't the
  computed-lowest one, and the Comparative Summary award form had a text
  box for it. Both were removed - the officer can click any quoting
  supplier's price pill and it's awarded immediately, no justification
  required or possible via the UI. `Item.award_reason` /
  `ProposalSnapshotItem.override_reason` remain in the schema (still
  displayed read-only next to an override, when present) purely so any
  reason text entered before this change keeps displaying correctly - no
  new reason text can be entered going forward.
- **Package total tie** - same idea, for the "lowest total from one
  supplier" ranking (`compute_package_totals`): the sort key includes
  `supplier_id` as a final tie-break so ordering is deterministic (not
  Python set iteration order), and the route separately flags when 2+
  fully-quoted suppliers share the exact top contract_value.
- **Item total value** = qty * rate of the *awarded* supplier (awarded =
  override if set, else computed-lowest).
- **Inc/Dec %** = (awarded_rate - lpr) / lpr * 100, only if lpr present.
- **Per-firm summary** (for CS bottom block and for Purchase Proposal
  grouping): for each supplier with >=1 awarded item — items count, store
  value (sum of item totals), tax amount (store value * tender.tax_percent
  / 100, labeled GST or PST per tender.tax_type), contract value (store
  value + tax amount).
- **Grand total**: sum across all firms.
- **Last Purchase Rate (LPR) history**: when a new tender line is created
  for a catalog item (ItemMaster), if no LPR is explicitly given, it's
  auto-filled from the awarded rate of that same item_master_id in the
  most recently **awarded** tender (by `Tender.awarded_date`), if any such
  tender exists. This is what makes Inc/Dec% meaningful without manual
  LPR entry every time - see `get_last_purchase_rate` in
  `app/lpr_history.py`.

## Regression fixture: CS.xlsx (dummy data, repo root)

This file is the ground truth for the calculation engine (Phase 2). It has
**23 item rows** (Ser 1-23), 3 suppliers (M/s Awan Tech, M/s SNS
Enterprises, M/s Libra Enterprises). Of those 23 items, **2 were NQ by all
three firms** (Ser 1 "Powder green silicon w-20" and Ser 21 "Apexior
Compound Paint No-3") and are excluded from totals, leaving **21 awarded
items** — which is where the "21" in the summary block below comes from.
Don't confuse total item rows (23) with awarded items (21).

Known-good aggregate numbers the engine must reproduce exactly:

| Firm | Items | Store Value (Rs) | GST 18% | Contract Value |
|---|---|---|---|---|
| M/s SNS | 10 | 209,655 | 37,737.90 | 247,392.90 |
| M/s Awan | 11 | 211,134 | 38,004.12 | 249,138.12 |
| **Grand Total** | **21** | **420,789** | **75,742.02** | **496,531.02** |

(M/s Libra Enterprises quoted but won 0 items — every one of their rates
was beaten by Awan or SNS — so they don't appear in the firm summary. The
tool must handle a "quoted but won nothing" supplier correctly, i.e. they
still exist as a Supplier record, just contribute 0 to the summary.)

Ser 1 and Ser 21 (NQ by all three firms, see above) must be excluded from
totals but still shown in the CS with a lowest rate/value of 0 or blank,
matching the source file's behavior.
