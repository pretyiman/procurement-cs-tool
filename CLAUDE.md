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

## Repo layout (current, as of Phase 11 — MVP complete)

```
CLAUDE.md
PLAN.md
docs/
  data-model.md
CS.xlsx                    # existing dummy dataset — treated as a fixture/
                            # regression-test target, do not overwrite
requirements.txt           # runtime deps
requirements-dev.txt        # + pyinstaller, for building the standalone exe
run.py                      # standalone launcher entry point (Phase 11)
ProcurementCSTool.spec      # PyInstaller build spec (committed, reproducible)
app/
  main.py                # FastAPI app + all routes
  models.py               # DB models: Tender, Supplier, ItemMaster, Item, Quote,
                           # Department, TenderTemplate/TenderTemplateItem,
                           # BusinessRules, DocumentLabels, CustomField
  db.py                    # SQLite engine/session (path from paths.user_data_dir())
  paths.py                  # dev-vs-frozen resource/DB path resolution (Phase 11)
  cs_engine.py                # comparative-statement calculation (pure)
  award_engine.py               # lowest + manual override logic, live Purchase
                                 # Proposal (draft-stage preview only - the
                                 # generated/approved proposal is frozen, see
                                 # proposal_snapshot.py)
  proposal_snapshot.py            # freezes the Purchase Proposal on Generate
                                   # (ProposalSnapshot/FirmGroup/Item - draft ->
                                   # proposal_generated -> proposal_approved ->
                                   # awarded) and the persisted per-firm
                                   # ContractAward.contract_no; CA/PP docs render
                                   # only from this frozen data, never live state
  excel_io.py                     # import CS.xlsx, catalog/supplier get-or-create,
                                   # CS export, purchase proposal export
  docx_export.py                    # PP/CA document generation (docxtpl),
                                     # renders from ProposalSnapshot, not live
                                     # award_engine data
  number_words.py                     # amount-in-words, ordinal (for PP/CA)
  lpr_history.py                        # cross-tender Last Purchase Rate lookup
  business_rules.py                       # get-or-create singleton BusinessRules
                                           # row (security deposit/stamp duty %),
                                           # editable at /settings/business-rules
                                           # instead of hardcoded in docx_export.py
  document_labels.py                        # get-or-create singleton
                                             # DocumentLabels row (CS export title +
                                             # signature-block role names), editable
                                             # at /settings/cs-labels instead of
                                             # hardcoded in excel_io.py
  custom_fields.py                            # admin-defined name/value text
                                               # fields (/settings/custom-fields) -
                                               # merged into PP/CA Word template
                                               # context as {{ tag_name }}, and a
                                               # recognised few picked up as CS
                                               # Excel signature-block designation
                                               # lines. Real per-contract data
                                               # always overrides a same-named
                                               # field; reserved names blocked at
                                               # creation (RESERVED_TAG_NAMES)
  template_manager.py                       # Settings > Document Templates:
                                             # download/upload/restore pp_template.docx
                                             # and ca_template.docx from the browser,
                                             # validated by rendering against
                                             # synthetic sample data before accepting.
                                             # Uploads land in paths.
                                             # custom_docx_templates_dir() (under
                                             # user_data_dir(), NOT resource_path() -
                                             # the latter is sys._MEIPASS when frozen,
                                             # wiped every launch)
  docx_templates/
    ca_template.docx                      # Contract Award - built by surgically
                                           # editing the user's real CA.doc (see
                                           # "Data sensitivity"), not from scratch
    pp_template.docx                        # Purchase Proposal - same approach,
                                             # from PP.doc
  templates/
    base.html                 # sidebar shell (Dashboard/Items/Suppliers/
                               # Departments/RFQs/Settings) + shared
                               # search-select JS combobox (supports inline
                               # "+" quick-create)
    dashboard.html              # "/" — stat cards + recent tenders, each
                                 # linking to its phase-appropriate landing
                                 # page (see _phase_landing_url in main.py)
    items.html                   # "/items" — catalog list/search/create
    suppliers.html                 # "/suppliers" — list/search/create
    supplier_detail.html            # "/suppliers/{id}" — view/edit
    departments.html                 # "/departments" — catalog list/search/create
    business_rules.html                # "/settings/business-rules" — deposit/
                                        # stamp-duty % used in Contract Award docs
    document_templates.html              # "/settings/templates" — download/upload/
                                          # restore PP/CA docx templates
    cs_labels.html                         # "/settings/cs-labels" — CS export title/
                                            # signature-block role names
    custom_fields.html                       # "/settings/custom-fields" — admin-
                                              # defined {{ tag }} text fields
    tenders_list.html                # "/tenders" — list/create/import;
                                      # each row links to its phase landing
    tender_new.html                   # "/tenders/new" (+ start from template)
    templates_list.html                 # "/templates" — tender template mgmt
    tender_detail.html                 # "/tenders/{id}" — add item (search-select),
                                        # add supplier, quote grid, live CS, Excel
                                        # export, save-as-template
    quote_entry.html                    # "/tenders/{id}/quote-entry" — guided entry,
                                         # includes _comparative_summary_grid.html
    comparative_summary.html              # "/tenders/{id}/comparative-summary" —
                                           # click-to-award pills (locks read-only
                                           # once the proposal is approved) + the
                                           # same _comparative_summary_grid.html
                                           # partial as Quote Entry, so both stay in
                                           # sync from one shared fragment + stats
                                           # bar + "Sourcing Options" (adjustable
                                           # cheapest-N-supplier bundle cards, partial
                                           # bidders eligible, BEST VALUE highlighted -
                                           # see cs_engine.compute_best_bundle) with
                                           # the per-item lowest-count leaderboard
                                           # collapsed as secondary detail - in-app
                                           # only, Excel export untouched
    purchase_proposal.html                  # "/tenders/{id}/proposal" — Generate/
                                             # Approve/Finalize actions, Excel/PP-doc
                                             # download, document-details form (CA
                                             # downloads live on contract_award.html)
    contract_award.html                       # "/tenders/{id}/contract-award" — one
                                               # card per winning firm (single- or
                                               # multi-party), persisted contract
                                               # number, only reachable once the
                                               # proposal is approved
    _comparative_summary_grid.html              # shared partial: view toggle (item/
                                                 # package) + grid (tie-aware
                                                 # highlighting) + Download
                                                 # Comparative Statement link + package
                                                 # Top-N/tie flagging - included by
                                                 # quote_entry.html and
                                                 # comparative_summary.html
    _phase_nav.html                               # shared Prev/Next partial across
                                                   # the 5 lifecycle pages (Items ->
                                                   # Quote Entry -> Comparative
                                                   # Summary -> Purchase Proposal ->
                                                   # Contract Award), included
                                                   # additively alongside each page's
                                                   # own quick-jump links
tests/
  test_excel_io.py
  test_cs_engine.py
  test_award_engine.py
  test_quote_entry.py
  test_docx_export.py
  test_proposal_snapshot.py
  test_item_lock.py
  test_phase_landing.py
  test_comparative_summary.py
  test_tender_status.py
  test_lpr_history.py
  test_tender_templates.py
  test_number_words.py
  test_quote_entry.py
  test_docx_export.py
```

## Building the standalone package (Phase 11)

```
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m PyInstaller ProcurementCSTool.spec --noconfirm
dist\ProcurementCSTool.exe
```

The `.spec` (not the raw CLI flags) is the source of truth for the build -
edit it directly if bundled data files or hidden imports need to change,
rather than re-running `pyinstaller run.py ...` from scratch. `dist/` and
`build/` are gitignored (regenerate, don't commit).

`.doc`-upload support (Settings > Document Templates, `template_manager.
convert_doc_to_docx()`) added a `pywin32` dependency (Windows-only, see
`requirements.txt`) that drives Word via COM automation - only reachable
when a `.doc` is uploaded (`.docx` uploads never touch it) and only
actually usable on a machine with Word installed; without Word it
degrades to a friendly error asking the admin to save as `.docx`
manually, it doesn't crash. Not yet verified inside a frozen build - the
lazy `import win32com.client` happens inside the function, which
PyInstaller's static analysis should still pick up, but this hasn't
actually been tested with a rebuilt `dist/ProcurementCSTool.exe`.

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

`CA.doc` and `PP.doc` at the repo root (gitignored, local-only) are
user-supplied sample Contract Award / Purchase Proposal Word documents
used as the reference for Phase 12's `ca_template.docx`/`pp_template.docx`.
Their dollar figures are dummy (they match the `CS.xlsx` fixture), but the
surrounding legal/procedural text names real department roles and
process details, so - unlike `CS.xlsx` - they're kept out of git rather
than committed. If you need to re-derive the templates from them again in
a later session, they should still be sitting at the repo root; if they're
missing, ask the user for them rather than reconstructing content from
memory of this file.
