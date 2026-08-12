# Procurement Comparative Statement & Award Tool

## What this project is

A local-first tool for a procurement department that replaces a manual Excel
workflow. Multiple suppliers quote prices for a list of items (not every
supplier quotes every item). The tool must:

1. Consolidate quotes from multiple suppliers per tender.
2. Produce a **Comparative Statement (CS)** — per item, find the lowest
   quoting firm and rate, compute totals, summarize by firm. Must match the
   existing `CS.xlsx` format (see `docs/data-model.md`).
3. Produce a **Purchase Proposal** — the CS regrouped by firm ("Firm A gets
   3 of 10 items, Firm B gets 5, Firm C gets 2"), for internal approval.
4. Produce **Contract Award Drafts** — one Word (.docx) document per winning
   firm, editable before sending, built from a template with multiple
   reviewable sections (item schedule, terms & conditions, security of
   contract, signatures) since different people approve different sections.

Full phase breakdown, current status, and Definition-of-Done per phase live
in `PLAN.md`. Data model lives in `docs/data-model.md`. **Read both before
writing code.**

## Frozen tech decisions (do not re-litigate without discussing with the user)

- **Backend**: Python 3.13, FastAPI, SQLAlchemy/SQLModel over **SQLite**
  (single file DB, no server to install). Chosen so a later "online" version
  is a DB swap + deploy, not a rewrite.
- **Frontend**: server-rendered (Jinja2 templates), not a separate JS
  build. Keeps the app truly standalone (no node toolchain) and easy for an
  agent to build/modify in small increments.
  **Deviation (Phase 3):** built with plain HTML forms (full-page
  POST + 303 redirect back to the tender page) instead of HTMX. HTMX would
  need a vendored local copy of htmx.min.js to keep the app offline-safe,
  which wasn't worth it for MVP - plain forms are simpler, equally
  "no JS build tooling," and every action already round-trips through the
  server. If per-cell save-without-reload becomes a real usability
  complaint, add HTMX as progressive enhancement on top of the existing
  forms (they'll keep working without it) rather than rewriting them.
- **Excel I/O**: `openpyxl` / `pandas`.
- **Word generation**: `docxtpl` (Jinja2-style placeholders inside a real
  .docx template), NOT raw `python-docx` composition. This lets non-technical
  procurement staff open the template in Word and edit letterhead / T&C /
  security-of-contract wording directly — the app only fills in data.
- **Packaging**: PyInstaller (or similar) for a standalone local launcher,
  once the app works.

## Repo layout (current, as of Phase 6)

```
CLAUDE.md
PLAN.md
docs/
  data-model.md
CS.xlsx                 # existing dummy dataset — treated as a fixture/
                         # regression-test target, do not overwrite
requirements.txt
app/
  main.py                # FastAPI app + all routes
  models.py               # DB models: Tender, Supplier, ItemMaster, Item, Quote
  db.py                    # SQLite engine/session
  cs_engine.py              # comparative-statement calculation (pure)
  award_engine.py            # lowest + manual override logic, Purchase Proposal
  excel_io.py                 # import CS.xlsx, catalog/supplier get-or-create, proposal export
  templates/
    base.html                 # sidebar shell (Dashboard/Items/Suppliers/Tenders)
    dashboard.html              # "/" — stat cards + recent tenders
    items.html                   # "/items" — catalog list/search/create
    suppliers.html                 # "/suppliers" — list/search/create
    supplier_detail.html            # "/suppliers/{id}" — view/edit
    tenders_list.html                # "/tenders" — list/create/import
    tender_new.html                   # "/tenders/new"
    tender_detail.html                 # "/tenders/{id}" — add item (from catalog),
                                        # add supplier, quote grid, live CS
    award_review.html                   # "/tenders/{id}/award"
    purchase_proposal.html                # "/tenders/{id}/proposal" + Excel export
docx_export.py (Phase 9, not yet built)   # purchase proposal + contract drafts
docx_templates/ (Phase 9, not yet built)   # editable .docx templates
tests/
  test_excel_io.py
  test_cs_engine.py
  test_award_engine.py
```

Items are **reusable catalog data** (`ItemMaster`, unique on
`part_no + description` together — see `docs/data-model.md` for why part_no
alone isn't unique, e.g. "NIV" non-inventory items). A tender's `Item` rows
are just `(tender_id, item_master_id, qty, ...)` — quantity/LPR/award are
per-tender, everything else comes from the catalog via `item.item_master`.

## Session protocol (read this every session)

1. Run `git log --oneline -10` and read `PLAN.md`'s Status column to see
   what's already done.
2. Work on the first phase that is `Not Started` or `In Progress`. Don't
   jump ahead — later phases depend on earlier ones being correct.
3. A phase is only `Done` when its **Verification** steps in `PLAN.md`
   actually pass — not when the code merely exists.
4. Commit at meaningful checkpoints (end of a phase, or a working
   sub-piece) with a message naming the phase. Small, reviewable commits —
   this is how a *different* session later understands what happened.
5. Before ending a session, update the Status column in `PLAN.md` for
   anything that changed.
6. Never invent new architecture/tech choices mid-phase — if something in
   "Frozen tech decisions" seems wrong once you're in the code, stop and
   flag it instead of silently switching.

## Known environment quirk

`uvicorn --reload` (WatchFiles) on this Windows setup has, twice, silently
failed to pick up a `main.py` edit - it logged no reload event and kept
serving stale code, which looked exactly like a real bug in the new code
until a full process restart (kill + relaunch, no `--reload`) proved the
code was correct. If a manual verification against a running `--reload`
server shows a result that contradicts what a direct DB/engine check
confirms is correct, restart the server fully before concluding there's an
actual bug.

## Data sensitivity

`CS.xlsx` in this repo is a **dummy/sample** dataset used only to validate
the calculation engine. Real supplier/pricing data must never be committed
to this repo as-is — once real data is used, add the real data files/folder
to `.gitignore`.
