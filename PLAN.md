# MVP Plan — Procurement Comparative Statement & Award Tool

**All 11 phases are Done — the MVP is complete.** Import/enter quotes,
compare, award (with override), generate a Purchase Proposal, download
per-firm Contract Award Drafts, export a CS matching the original
`CS.xlsx` template, and run it all as a standalone packaged app. See
"Deferred to v2" at the bottom for what's deliberately out of scope.

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
**Status: Done**

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

**Confirmed:** `pytest tests/` (7 tests) passes. `app/cs_engine.py` exactly
reproduces the fixture's firm summaries (SNS 10/209,655/37,737.90/
247,392.90; Awan 11/211,134/38,004.12/249,138.12) and grand total
(21/420,789/75,742.02/496,531.02); M/s Libra Enterprises (won 0 items)
correctly excluded from the summary; Ser 1 & 21 (NQ by all) correctly
excluded from totals but present in `item_results`.

---

## Phase 3 — Quote-entry UI
**Status: Done**

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
  grid renders all 23 items x 3 suppliers correctly, including NQ cells.

**Confirmed (manual, via curl against a running server):**
- Created a tender by hand, added 3 items (one with an LPR) and 2
  suppliers, saved a quote grid with one NQ per item. Rendered page showed
  correct lowest-firm highlighting per item, correct totals (item3: LPR
  100, awarded rate 90 -> Inc/Dec -10.0%), and a firm summary/grand total
  that hand-recomputation confirmed exactly (Supplier X: 2/1720/172/1892,
  Supplier Y: 1/250/25/275, grand total 3/1970/197/2167).
- Imported the real `CS.xlsx` through `POST /tenders/import-ui` (the HTML
  form path, not the import script) - rendered grid has 69 rate inputs
  (23 items x 3 suppliers) and the firm summary block matches the known-
  good numbers exactly (e.g. M/s Awan Tech: 11/211,134.00/38,004.12/
  249,138.12).
- `pytest tests/` (7 tests) still passes unchanged.

---

## Phase 4 — Award engine + Purchase Proposal
**Status: Done**

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

**Confirmed:** `pytest tests/` (13 tests) passes, including that Purchase
Proposal groupings exactly equal `cs_engine`'s firm summaries when no
overrides exist, that an override moves an item and updates both firms'
subtotals by precisely the item's value, that an override without a
reason (when not the lowest bidder) is rejected, and that awarding to a
supplier who didn't quote the item is rejected. Also manually verified
against a running server: overrode Ser 3 (fixture item, Awan 150 -> SNS
350 via Award Review with a reason) and confirmed the Purchase Proposal
page, its Excel export, and the underlying numbers all agreed exactly
(Awan 10 items/210,384; SNS 11 items/211,405; grand total 421,789),
including the unresolved-items block (Ser 1 & 21) and the override reason
shown inline.

---

## Phase 5 — Reusable item catalog + supplier catalog UI
**Status: Done**

**Why now:** user feedback after Phase 4 was that the UI doesn't look
professional and, critically, that **items should be reusable** across
tenders (a standing catalog like Part No "A-2394" recurring tender after
tender), not re-typed as free text per tender. This is a schema change:
`Item` (tender line) currently *stores* part_no/description/unit directly;
it needs to instead reference a new `ItemMaster` catalog table. No real
data exists yet (still fixture-only per `CLAUDE.md`), so this is a clean
schema rewrite, not a migration-with-data-preservation problem.

**Goal:**
- New `ItemMaster` table: `id, part_no, description, default_unit`,
  unique on `(part_no, description)` together (not `part_no` alone -
  the fixture has multiple genuinely different items sharing part_no
  "NIV" for non-inventory items, distinguished only by description).
- `Item` (tender line) becomes `id, tender_id, item_master_id, ser, qty,
  lpr, awarded_supplier_id, award_reason` - part_no/description/unit are
  read via `item.item_master.*`, not stored on Item itself.
- Left sidebar layout (Dashboard / Items / Suppliers / Tenders) replacing
  the current top-nav-only `base.html`.
- `/items`: catalog list with a search box (part_no/description) +
  create form. This is where new catalog items get defined now - the old
  tender-detail free-text "Add item" form is removed.
- `/suppliers` (list + create) and `/suppliers/{id}` (detail: address,
  contact, phone, email, tax_no, editable) - supplier records already
  existed as reusable rows since Phase 1, this just gives them a proper
  UI instead of only being creatable via the "attach to tender" flow.

**Outputs:** `app/models.py` (ItemMaster + Item rewrite), `app/excel_io.py`
(`get_or_create_item_master`, import updated to populate it), updated
`app/cs_engine.py`/`award_engine.py` call sites if any touch item fields
directly, `app/templates/base.html` (sidebar), `app/templates/items.html`,
`app/templates/suppliers.html`, `app/templates/supplier_detail.html`,
routes in `app/main.py`, `docs/data-model.md` updated to match.

**Verification:**
- `pytest tests/` still passes (existing tests don't assert on
  part_no/description/unit directly per a repo-wide grep, but the schema
  change touches every table via FKs, so a full green run is the real
  gate here).
- Re-importing `CS.xlsx` twice into the same DB does not duplicate
  catalog rows - the second import's items resolve to the same
  `ItemMaster` rows as the first (proves reusability actually works).
- `/items` search filters correctly; `/suppliers/{id}` shows the right
  supplier's detail and survives an edit.

**Confirmed:** `pytest tests/` (14 tests, 1 new) passes, including a new
regression test that re-importing `CS.xlsx` twice into the same DB
produces 23 catalog rows (not 46) and both tenders' lines point at the
same `ItemMaster` rows - including confirming NIV-part-numbered items
stayed distinct by description rather than colliding. Manually verified
on a running server: imported the fixture (23 catalog items appeared),
added a new tender by hand and added an existing catalog item to it via
the new dropdown (unit/part_no/description flowed through correctly),
confirmed adding the same catalog item twice to one tender is rejected
(400), created and edited a supplier via `/suppliers` and
`/suppliers/{id}` and confirmed the edit persisted, and confirmed the
Purchase Proposal Excel export still works against the new schema
(`item.item_master.*`).

---

## Phase 6 — Dashboard home page
**Status: Done**

**Goal:** Replace the current home page (tender list + create/import
forms bolted onto it) with an actual dashboard: stat cards (tenders by
status, catalog item count, supplier count) and a recent-tenders list.
Tender create/import moves to a dedicated `/tenders` page (list + create
+ import), consistent with Items/Suppliers now having their own section.

**Verification:** Home page loads with correct live counts against a
populated DB (spot-check against known fixture-derived numbers); creating
a tender via `/tenders` still works exactly as before (same underlying
routes, just relocated).

**Confirmed:** Built alongside Phase 5 since the sidebar's "Tenders" nav
link needed somewhere real to point at. Manually verified: dashboard at
`/` shows correct live counts (2 tenders both "draft", 23 catalog items,
4 suppliers) matching the DB state at the time; `/tenders` (moved from
the old `/`) still lists/creates/imports tenders exactly as before.

---

## Phase 7 — Guided quotation-entry page
**Status: Done**

**Goal:** A single-line entry workflow matching how a procurement officer
actually receives a quotation (one supplier's price for one item at a
time), instead of only the big grid: `/tenders/{id}/quote-entry` -
- Item dropdown, sourced from the catalog (Phase 5), not free text.
  Selecting an item auto-displays its unit (from `ItemMaster.default_unit`,
  no extra request - plain JS reading a data attribute already in the
  page).
- Qty: prefilled from the existing tender line if this item is already on
  the tender; otherwise required and creates the line.
- Supplier dropdown (existing suppliers, reused from Phase 5's catalog).
- Rate input, with a live-computed Total Value (qty x rate) via inline
  JS - cosmetic only, the server recomputes authoritatively on submit.
- Submitting upserts the tender line (`Item`, by item_master_id) and the
  `Quote` (by item+supplier) - same underlying tables as the existing
  grid, this is an additional entry path, not a new data model.
- A running table below the form lists quotations entered so far this
  session, for confirmation.

**Verification:** Entering a quotation for a brand-new item on a tender
creates both the line and the quote correctly (qty and rate both
persisted); entering a second supplier's rate for an already-added item
does not duplicate or disturb the first supplier's quote; the resulting
comparative statement/lowest calculation is unaffected by which entry
path (grid vs this page) was used to get the data in.

**Confirmed:** `pytest tests/test_quote_entry.py` (3 new tests, first use
of FastAPI's `TestClient` with a `get_session` dependency override rather
than calling engine functions directly) passes: a new item creates both
the line and quote, a second supplier's quote on the same item doesn't
touch the first, and re-submitting the same item updates qty/rate in
place rather than duplicating. `pytest tests/` (17 total) all green.
Manually verified on a running server against the imported fixture:
selecting an existing item auto-filled its unit and current qty (data
attributes confirmed in the rendered HTML); adding a brand-new 4th
supplier's quote (Rs 300, undercutting SNS's 350) through this page
immediately changed the lowest-cell highlight and total on the grid page
(`/tenders/{id}`) to that new supplier - confirming both entry paths
write through the same tables and cs_engine picks it up either way.

---

## Phase 8 — Visual award comparison (Award Review redesign)
**Status: Done**

**Goal:** Per user feedback, replace the dropdown-based override control
with a side-by-side price comparison: for each item, show every quoting
supplier as a clickable price pill (lowest visually highlighted/badged).
Clicking a pill submits the award directly - no dropdown needed. This is
a UI change on top of the existing `award_engine.py` (`validate_override`
still enforces "reason required unless lowest" and "must have quoted"
server-side, unchanged) - implemented as multiple submit buttons sharing
one `name` in a single per-item `<form>`, no JS required for the award
action itself (only the optional reason text field needs to accompany
whichever button was clicked, which plain HTML forms already do).

**Verification:** Clicking the lowest-priced pill for an item awards it
immediately with no reason prompt; clicking a non-lowest pill without
first typing a reason is rejected with the same error `validate_override`
already produces; the resulting Purchase Proposal reflects the click
exactly as it did the old dropdown-based override in Phase 4's tests.

**Confirmed:** No `award_engine.py` changes needed - `validate_override`/
`resolve_award` were already correct, this was purely `award_review.html`
+ passing `lowest_supplier_id` through in `main.py`. `pytest tests/` (17,
unchanged) still passes. Manually verified on a fully-restarted server
(see "Known environment quirk" in `CLAUDE.md` - a `--reload` artifact
briefly looked like a real bug here before a clean restart proved
otherwise): the lowest-priced pill renders with the green "Lowest" badge
and `.awarded` outline by default; clicking a non-lowest pill without a
reason is rejected (400); clicking it with a reason succeeds, moves the
`.awarded` outline to that pill while the `.lowest` badge correctly stays
on the actual cheapest option, and shows a "Reset to lowest" button which
correctly clears the override when clicked.

---

## Phase 9 — Contract Award Draft generator
**Status: Done**

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

**Confirmed:** `pytest tests/` (19 tests, 2 new) passes, including a
regression test that the rendered item-schedule table exactly matches the
firm group's awarded items (ser/part_no/description/unit/qty/rate/total,
row-for-row) with no leftover `{%tr%}` marker rows, and no unrendered
`{{`/`{%` tags anywhere in the output.

Hit and fixed a real docxtpl bug along the way: its XML patching does an
"unescape html entities" pass on the rendered output, which means
un-escaped `&` in a context value (e.g. a firm name like "M/s Zafar &
Sons" - a completely realistic firm name) produced malformed intermediate
XML that silently corrupted *nearby* static template text too (headings
like "Terms & Conditions" lost their ampersand even though that text was
never touched by any Jinja variable). Fixed by HTML-escaping every
free-text context value before it reaches docxtpl
(`docx_export._esc()`), with a regression test
(`test_ampersand_in_firm_name_survives_rendering`) covering exactly this.

Also had to reverse-engineer the correct `{%tr %}` row-loop syntax:
docxtpl's own docs don't show a worked example, and the intuitive
approach (put `{%tr for %}` in the first cell and `{%tr endfor %}` in the
last cell of the *same* row) fails outright ("Encountered unknown tag
'endfor'") because docxtpl disallows two `{%tr` tags in one row. The
working pattern is three separate rows: one containing only
`{%tr for item in items %}`, the literal data row in between (repeated
per item), and one containing only `{%tr endfor %}` - the two marker rows
get deleted entirely. The committed artifact is the resulting
`contract_template.docx` itself (meant to be hand-edited in Word from
here on); the one-off script that built it wasn't committed.

Manually verified end-to-end against the seeded dummy tender (4 real
winning firms, one with "&" in its name): downloaded each firm's
`.docx` individually and as a combined `.zip` via the actual HTTP
endpoints, re-opened every file with python-docx, and confirmed table row
counts and values matched the Purchase Proposal exactly for all four
firms (5, 3, 5, and 1 awarded items respectively).

---

## Phase 10 — CS Excel export matching existing template
**Status: Done**

**Goal:** "Export to Excel" produces a file matching the layout/formatting
of the existing `CS.xlsx` (same columns, lowest firm/rate/total, summary
block) so it can drop into existing approval paperwork unchanged.

**Verification:** Exported file for the fixture tender, opened in Excel,
matches `CS.xlsx` cell-for-cell in the numeric columns (formatting close
enough to be presentable, not necessarily byte-identical).

**Confirmed:** Went further than static cell comparison - a genuine
round-trip test (`test_exported_cs_round_trips_through_the_apps_own_importer`)
exports the fixture tender's CS, re-imports that exported file with the
app's own `import_tender()` into a completely fresh DB, and asserts the
recomputed CS reproduces the known-good numbers exactly (SNS 10/209,655/
247,392.90; Awan 11/211,134/249,138.12; grand total 21/420,789/496,531.02;
Ser 1 & 21 still NQ). This proves the export is genuinely CS.xlsx-shaped,
not just presentable. `pytest tests/` (20, 1 new) passes. Manually
verified via the actual `/tenders/{id}/export` HTTP endpoint against the
seeded dummy tender: header/subheader/lowest-firm/rate/total/LPR/Inc-Dec%
all correct per row, "M/s Zafar & Sons" ampersand intact, and the totals
row matches the known grand total (2,300,860) exactly.

---

## Phase 11 — Packaging
**Status: Done**

**Goal:** Standalone local launcher (no manual Python install) that starts
the app and opens the browser to it.

**Verification:** A clean machine (or clean venv) can run the packaged
app end-to-end: import fixture, view CS, generate proposal, generate
contract drafts, without installing anything manually beyond the
installer/launcher itself.

**Confirmed:** Added `app/paths.py` (`resource_path()` for
templates/docx_templates, `user_data_dir()` for the SQLite DB) so dev mode
is unchanged (repo root, as always) but a frozen build uses
`sys._MEIPASS` for bundled resources and `%LOCALAPPDATA%\ProcurementCSTool\`
for the DB - never tries to write next to a possibly read-only installed
executable. `run.py` is the packaged entry point (starts uvicorn, opens
the browser after a short delay via `threading.Timer`) - kept separate
from `app/main.py` so "how this app is launched" doesn't leak into the
FastAPI app itself.

Built with `pyinstaller ProcurementCSTool.spec` (or the equivalent
`pyinstaller run.py --name ProcurementCSTool --onefile --add-data
"app/templates;app/templates" --add-data
"app/docx_templates;app/docx_templates"` - the committed `.spec` is the
reproducible source of truth). Requires `pip install -r
requirements-dev.txt` first (adds `pyinstaller` on top of the normal
runtime deps).

Manually verified against the actual built `dist/ProcurementCSTool.exe`
(not just the dev-mode launcher): ran it standalone, imported `CS.xlsx`
through the real UI, confirmed `/tenders/1`, `/tenders/1/award`,
`/tenders/1/proposal`, `/items`, `/suppliers` all return 200 with correct
data (Purchase Proposal: Awan 11/SNS 10, grand total 420,789 - matches
the known-good fixture numbers exactly), downloaded a contract draft
(12-row table, no unrendered tags - proves the bundled docx template
resolved correctly under `sys._MEIPASS`), and confirmed the DB landed at
`%LOCALAPPDATA%\ProcurementCSTool\procurement.db`, fully separate from
the dev repo's `procurement.db`. `pytest tests/` (20, unchanged) still
passes after the path refactor.

---

## Post-MVP round 1 (user-requested changes after MVP sign-off)

Requested together in one message: GST/PST tax type selection, searchable
item/supplier fields (replacing dropdown/datalist), cross-tender LPR
auto-tracking, tender templates (save/reuse item lists), and - the
explicitly flagged "most important" one - real Purchase Proposal (PP) and
Contract Award (CA) Word documents matching two sample files the user
supplied (`CA.doc`/`PP.doc`, kept local-only, gitignored - see "Data
sensitivity" in `CLAUDE.md`), replacing Phase 9's generic
`contract_template.docx` (deleted) with `ca_template.docx`/
`pp_template.docx` built by *surgically editing* the real converted
documents (collapsing multi-run text spans into single Jinja-tagged runs,
restructuring per-firm/per-item tables into `{%tr for/endfor%}` loops)
rather than recreating them from scratch - preserves the real legal/
procedural wording verbatim, only genuinely dynamic values became tags.
Added `app/number_words.py` (amount-in-words, matches the sample's exact
phrasing) and a handful of new optional `Tender` fields (indent_no,
subject_department, firms_invited_count, issue_date, opening_date,
delivery_days, warranty_months) that only these two documents need.

All five committed individually (see git log for the detailed verification
notes per change); `pytest tests/` (39 tests) passes as of the last of
these commits. Not tracked as new numbered MVP phases since the MVP itself
was already complete - this is iteration on top of it, same rigor
(tested, verified against a running server, committed with a clear why).

---

## Post-MVP round 2 (in-app document control + Purchase Proposal approval workflow)

Three threads, each requested and discussed before building:

**In-app document control** (so non-technical procurement staff never need
filesystem/code access to tweak PP/CA wording or numbers): Business Rules
settings (security deposit %/waiver threshold, stamp duty %, previously
hardcoded in `docx_export.py`) → Document Templates manager (download/
edit-in-Word/upload PP/CA `.docx`, `.doc` accepted via Word COM conversion,
validated by rendering against synthetic sample data before accepting) →
CS Excel label/signature-role settings → Custom Fields (admin-defined
`{{ tag }}` text, reserved-name collision protection) → Custom Field
Groups (the same tag can have a different value per department, resolved
automatically from a tender's `department_id`, no manual selection step).
Also fixed a real business-rule bug found along the way: stamp duty was
being calculated on contract value instead of store value.

**Purchase Proposal approval workflow**: added a `proposal_approved`
status between `proposal_generated` and `awarded`. Generating a proposal
now freezes the current award state into a `ProposalSnapshot` (+ per-firm
`ProposalSnapshotFirmGroup`/`Item` rows) - freely regenerable while still
`proposal_generated` (the revise-after-rejection cycle), but approving
locks it: award decisions lock, and every CA/PP Word document renders only
from that frozen snapshot from then on, never live Item/Quote/catalog
state. Contract numbers are now persisted per (snapshot, firm) via a new
`ContractAward` table instead of being re-typed on every download;
finalizing to `awarded` requires every winning firm to have one.

**Page restructuring + item lock**: Award Review renamed to Comparative
Summary (route `/comparative-summary`), now including the same
comparative-grid partial Quote Entry uses (one shared fragment) plus a
Download Comparative Statement link. Contract Award downloads moved off
the Purchase Proposal page onto their own page (`/contract-award`), one
card per winning firm. All 5 lifecycle pages (Items → Quote Entry →
Comparative Summary → Purchase Proposal → Contract Award) got a Prev/Next
nav pair. Finally, item add/edit/delete now locks once an RFQ's
`issue_date` has passed (not just once status leaves `draft`) - a blank
issue_date never locks by date alone; no unlock override exists yet
(start a fresh RFQ if requirements genuinely change after publishing).

Dev DB reseeded with 7 fresh demo RFQs (`PROC/2026/301`-`307`) spanning
every stage above - see `git log`/commit messages for verification detail
per change. All committed individually; `pytest tests/` (106 tests) passes
as of the last of these commits. Not tracked as new numbered MVP phases,
same reasoning as round 1.

---

## Deferred to v2 (explicitly out of MVP scope)

- Multi-user / online hosting, login & roles
- Supplier self-service quote submission (portal/email intake)
- Historical LPR auto-tracking across tenders
- Formal approval-routing workflow / audit trail / e-signatures (Phase 9
  produces documents *for* multi-person review, but does not itself route
  approvals or track sign-off status)
- Cross-tender analytics (e.g., which supplier is consistently cheapest)
