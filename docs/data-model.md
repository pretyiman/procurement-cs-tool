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
| status | enum | `draft` -> `proposal_generated` -> `awarded` |
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
| award_reason | text, nullable | required if awarded_supplier != lowest |

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

## Derived (never stored, always computed)

- **Lowest rate / lowest firm per item** = min(rate) across quotes where
  rate is not null. If no supplier quoted (all NQ), item has no lowest.
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
