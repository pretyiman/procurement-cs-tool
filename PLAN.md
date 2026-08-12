# MVP Plan — Procurement Comparative Statement & Award Tool

Status legend: `Not Started` / `In Progress` / `Done`. A phase is `Done`
only when its Verification steps pass, not just when code exists. See
`CLAUDE.md` for session protocol and `docs/data-model.md` for schema.

## Phase 0 — Repo scaffolding
**Status: Done**
Git repo initialized, `CLAUDE.md`, `PLAN.md`, `docs/data-model.md`,
`.gitignore` created. `CS.xlsx` kept at repo root as the regression fixture.

---

## Phase 1 — Project skeleton, DB models, Excel import
**Status: Done**

**Goal:** A runnable (empty) FastAPI app with the DB schema from
`docs/data-model.md`, plus a script/endpoint to import a tender's items and
per-supplier quotes from an Excel file shaped like `CS.xlsx`.

**Outputs:**
- `app/main.py`, `app/models.py` (SQLModel or SQLAlchemy models matching
  `docs/data-model.md` exactly)
- `app/excel_io.py` with an `import_tender(path) -> Tender` function that
  parses a CS-shaped sheet: reads the dynamic list of supplier columns
  between "Qty" and "Lowest", treats `"NQ"`/blank as no-quote
- SQLite file created on first run, tables auto-created

**Verification:**
- Running the import against `CS.xlsx` produces: 1 tender, **23 items**
  (Ser 1-23, including the 2 that were NQ by every firm), 3 suppliers, and
  23*3 = 69 Quote rows (some null for NQ cells). See `docs/data-model.md`
  for why 23 total items yields 21 *awarded* items downstream.
- `pytest tests/test_excel_io.py` passes.

**Confirmed:** `pytest tests/` (3 tests) passes, and a live run through the
actual HTTP API (`POST /tenders/import` then `GET /tenders/1`) returned
`item_count: 23, supplier_count: 3, quote_count: 69` against the real
`CS.xlsx` — matches exactly.

---

## Phase 2 — Comparative Statement calculation engine
**Status: Not Started**

**Goal:** Pure functions that take a Tender's items/quotes and produce the
derived values in `docs/data-model.md` ("Derived" section): lowest
rate/firm per item, item totals, per-firm summary, grand totals.

**Outputs:**
- `app/cs_engine.py`
- `tests/test_cs_engine.py`

**Verification (hard gate — must match the fixture exactly):**
- Running the engine on the imported `CS.xlsx` data reproduces the table in
  `docs/data-model.md` under "Regression fixture" exactly: SNS 10 items /
  209,655 / 37,737.90 / 247,392.90; Awan 11 items / 211,134 / 38,004.12 /
  249,138.12; Grand total 21 / 420,789 / 75,742.02 / 496,531.02.
- Items NQ by all firms are excluded from totals but still present in
  output with lowest = none.
- A supplier who quoted but won zero items still exists in the supplier
  list but contributes 0 / is omitted from the summary block (matches
  M/s Libra Enterprises in the fixture).

Do not proceed to Phase 3 until this phase's numbers match exactly —
everything downstream (proposal, contract values) depends on this being
correct.

---

## Phase 3 — Quote-entry UI
**Status: Not Started**

**Goal:** Browser UI (Jinja2 + HTMX, served by FastAPI) to: create a
tender, add/import items, manage a reusable supplier list, and enter quotes
in a grid (rows = items, columns = suppliers, blank = NQ). View the live
CS (from Phase 2's engine) for the tender.

**Outputs:** `app/templates/*.html`, routes in `app/main.py`.

**Verification:**
- Manually create a tender, add 3 items, add 2 suppliers, enter quotes
  including at least one NQ, confirm the CS view matches expected lowest
  firm/rate/totals.
- Import `CS.xlsx` through the UI (not just the script) and confirm the
  grid renders all 21 items x 3 suppliers correctly, including NQ cells.

---

## Phase 4 — Award engine + Purchase Proposal
**Status: Not Started**

**Goal:** Let an officer override an item's awarded supplier (with
required reason) instead of always taking computed-lowest. Generate the
Purchase Proposal: items grouped by awarded firm, each firm's item list +
subtotal + GST + total, grand total, unresolved (no-lowest / NQ-by-all)
items called out separately.

**Outputs:**
- `app/award_engine.py` (default-to-lowest + override resolution)
- Award Review screen (per-item override control + reason field)
- Purchase Proposal screen + Excel export
  (`app/excel_io.py: export_purchase_proposal`)

**Verification:**
- With no overrides, Purchase Proposal firm groupings exactly match Phase
  2's per-firm summary (same items, same totals) — proposal is a
  regrouping of the same numbers, not a separate calculation.
- Setting an override on one item moves it to the new firm's group, updates
  both firms' subtotals correctly, and requires a reason (rejected without
  one).

---

## Phase 5 — Contract Award Draft generator
**Status: Not Started**

**Goal:** One Word (.docx) draft per winning firm, generated from an
editable template (`docxtpl`), built from **separate reviewable sections**
so different approvers can review their part independently:
1. Cover / firm & tender details
2. Item schedule (awarded items, rates, qty, values, total incl. GST)
3. Terms & conditions
4. Security of contract
5. Signature/approval block

**Outputs:**
- `app/docx_templates/contract_template.docx` — a real Word file with
  Jinja placeholders (`{{ firm_name }}`, `{% for item in items %}` table
  row, etc.) covering all 5 sections above, editable directly in Word by
  non-technical staff
- `app/docx_export.py: generate_contract_draft(tender, supplier) -> bytes`
- "Generate Contract Drafts" action on the Purchase Proposal screen —
  produces one .docx per firm with awarded items (individually or all at
  once)

**Verification:**
- Generated .docx for each firm in the fixture opens correctly in Word,
  item schedule table matches that firm's proposal exactly (items, rates,
  qty, values, total).
- Editing the template's T&C/security-of-contract text in Word and
  re-running generation reflects the edit without any code change —
  confirms non-technical staff can maintain that content.

---

## Phase 6 — CS Excel export matching existing template
**Status: Not Started**

**Goal:** "Export to Excel" produces a file matching the layout/formatting
of the existing `CS.xlsx` (same columns, lowest firm/rate/total, summary
block) so it can drop into existing approval paperwork unchanged.

**Verification:** Exported file for the fixture tender, opened in Excel,
matches `CS.xlsx` cell-for-cell in the numeric columns (formatting close
enough to be presentable, not necessarily byte-identical).

---

## Phase 7 — Packaging
**Status: Not Started**

**Goal:** Standalone local launcher (no manual Python install) that starts
the app and opens the browser to it.

**Verification:** A clean machine (or clean venv) can run the packaged
app end-to-end: import fixture, view CS, generate proposal, generate
contract drafts, without installing anything manually beyond the
installer/launcher itself.

---

## Deferred to v2 (explicitly out of MVP scope)

- Multi-user / online hosting, login & roles
- Supplier self-service quote submission (portal/email intake)
- Historical LPR auto-tracking across tenders
- Formal approval-routing workflow / audit trail / e-signatures (Phase 5
  produces documents *for* multi-person review, but does not itself route
  approvals or track sign-off status)
- Cross-tender analytics (e.g., which supplier is consistently cheapest)
