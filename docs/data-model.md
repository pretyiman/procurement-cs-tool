# Data Model

Source of truth for entities/fields. Update this file first if the model
changes; code and `PLAN.md` verification steps should match it.

## Entities

### Tender
| field | type | notes |
|---|---|---|
| id | int, PK | |
| inquiry_no | text | e.g. "Tender Inquiry No. xxxxx" |
| date | date | |
| gst_percent | decimal | e.g. 18.0 |
| status | enum | `draft` -> `proposal_generated` -> `awarded` |

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

## Derived (never stored, always computed)

- **Lowest rate / lowest firm per item** = min(rate) across quotes where
  rate is not null. If no supplier quoted (all NQ), item has no lowest.
- **Item total value** = qty * rate of the *awarded* supplier (awarded =
  override if set, else computed-lowest).
- **Inc/Dec %** = (awarded_rate - lpr) / lpr * 100, only if lpr present.
- **Per-firm summary** (for CS bottom block and for Purchase Proposal
  grouping): for each supplier with >=1 awarded item — items count, store
  value (sum of item totals), GST amount, contract value (store value +
  GST).
- **Grand total**: sum across all firms.

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
