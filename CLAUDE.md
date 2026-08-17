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
                           # BusinessRules, DocumentLabels, CustomField,
                           # LockSettings (singleton, see lock.py)
  db.py                    # SQLite engine/session (path from paths.user_data_dir())
  seed_demo_data.py         # seed_demo_data_if_empty(), called from main.py's
                             # startup hook right after create_db_and_tables() -
                             # a no-op unless the DB has zero Tender rows, so it
                             # never touches real data, only a brand-new install.
                             # Builds 5 tenders spanning every lifecycle stage
                             # plus a Custom Field Group with example values for
                             # all 15 PP/CA "department blank" tags, so a fresh
                             # .exe on a new machine is demo-ready on first
                             # launch - no manual data entry or file copying.
                             # Item/supplier names are generic/fictional
  paths.py                  # dev-vs-frozen resource/DB path resolution (Phase 11)
  icons.py                    # inline-SVG icon set for the UI (icon(name) is a
                               # Jinja global, {{ icon('dashboard') }}) - hand-drawn
                               # outline icons, NOT the Phosphor icon-font CDN the
                               # UI redesign reference used, to keep the app fully
                               # offline (same reasoning as the HTMX decision below)
  lock.py                       # optional local workspace passcode (Settings >
                                 # Lock, sidebar Lock button) - explicitly NOT real
                                 # security (no account system exists), just a
                                 # convenience screen-lock. In-memory unlock flag
                                 # (resets on app restart), gated by main.py's
                                 # lock_gate HTTP middleware, which goes through
                                 # app.dependency_overrides for get_session (NOT a
                                 # direct Session(engine)) so it doesn't fight the
                                 # test suite's isolated in-memory DBs. Disabled
                                 # (no passcode) is the default - a no-op for
                                 # everyone who hasn't opted in via Settings
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
                                   # CS export, purchase proposal export, + the
                                   # non-official "Working Comparison" export
                                   # (export_working_comparison_xlsx) - a
                                   # user-narrowed supplier subset, deliberately
                                   # shaped differently (no signature block, no
                                   # re-import) so it can't be mistaken for the
                                   # official CS
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
                                               # creation (RESERVED_TAG_NAMES).
                                               # SUGGESTED_PP_CA_FIELDS names 15
                                               # consignee/authority/routing-chain
                                               # blanks in ca_template.docx/
                                               # pp_template.docx that were found
                                               # hardcoded to one department's
                                               # wording (e.g. "our Organization",
                                               # "Company") when a real sample CA/PP
                                               # from a different department showed
                                               # they actually vary - each is now
                                               # {{ tag|default('original text') }},
                                               # so it renders unchanged until a
                                               # same-named field (typically inside
                                               # that department's Custom Field
                                               # Group) is created - see
                                               # docs/data-model.md
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
    base.html                 # UI shell, rebuilt on the "Nocturne" design
                               # reference (see "UI redesign" below): a
                               # collapsible icon-rail sidebar (Dashboard/RFQs/
                               # Insights/Items/Suppliers/Departments/Templates/
                               # Settings + Collapse/Lock), a light/dark theme
                               # toggle (localStorage-persisted, no
                               # flash-of-wrong-theme), and the full design-token
                               # CSS (colors/spacing/shadows as CSS custom
                               # properties, [data-theme="light"] override block)
                               # underneath every existing class name (.card,
                               # .stat-grid, .badge, .tab-nav, .search-select...)
                               # - the point being every OTHER template kept
                               # working unmodified purely by inheriting the new
                               # look, since they already styled through these
                               # shared classes rather than one-off CSS. Also
                               # still carries the shared search-select JS
                               # combobox (supports inline "+" quick-create)
    dashboard.html              # "/" — two views (Work queue / Metrics, a
                                 # `view` query param): Work queue is a
                                 # "needs you next" card list (status-sorted,
                                 # unfinished RFQs first) + a pipeline bar +
                                 # catalog stat shortcuts; Metrics is stat cards
                                 # + a contract-value-by-stage bar chart + the
                                 # original recent-RFQs table. Each queue card's
                                 # contract value is read from an already-frozen
                                 # ProposalSnapshot (cheap), never recomputed
                                 # live, so the dashboard stays fast regardless
                                 # of RFQ count. Links use the same
                                 # phase-appropriate landing page as before (see
                                 # _phase_landing_url in main.py)
    insights.html                 # "/insights" — NEW, purely derived read-only
                                   # analytics scoped to *awarded* tenders only
                                   # (not proposal-generated ones - answers "how
                                   # did procurement actually go," not mixed
                                   # with still-open decisions): total awarded
                                   # value, savings vs Last Purchase Rate, avg
                                   # bidders/RFQ, single-source item count
                                   # (needs a live per-item quote-count check
                                   # since the frozen snapshot only kept the
                                   # winner, not how many suppliers quoted -
                                   # cheap here since awarded tenders' quotes
                                   # never change again), awarded value by firm,
                                   # and a rate-movement-vs-LPR table. No new
                                   # DB writes at all
    lock.html                       # "/lock" — NEW, the standalone passcode
                                     # screen (deliberately does NOT extend
                                     # base.html - no sidebar, since every
                                     # sidebar link would just bounce right back
                                     # here while locked). See app/lock.py
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
    settings_lock.html                         # "/settings/lock" — NEW, set/change/
                                                # clear the optional local passcode
                                                # (app/lock.py). Blank "Save" is a
                                                # deliberate no-op (doesn't disable
                                                # the lock) - only "Turn lock off"
                                                # (a separate form/flag) actually
                                                # clears it, and setting/changing a
                                                # passcode never locks out the admin
                                                # who just typed it, only future
                                                # lock engagements
    _settings_nav.html                           # NEW shared partial - pill nav
                                                  # across all 5 /settings/* pages
                                                  # (replaced the old plain-text
                                                  # breadcrumb line each page had)
    tenders_list.html                # "/tenders" — list/create/import; status-
                                      # filter pills with counts; each row links
                                      # to its phase landing
    tender_new.html                   # "/tenders/new" (+ start from template)
    templates_list.html                 # "/templates" — tender template mgmt
    tender_detail.html                 # "/tenders/{id}" — add item (search-select),
                                        # add supplier, quote grid, live CS, Excel
                                        # export, save-as-template
    quote_entry.html                    # "/tenders/{id}/quote-entry" — guided entry,
                                         # includes _comparative_summary_grid.html
    comparative_summary.html              # "/tenders/{id}/comparative-summary" —
                                           # below the stats bar, split into 3 tabs
                                           # (vanilla JS showTab(), tab state kept in
                                           # a URL hash so it survives this page's
                                           # full-reload actions): "Sourcing Options"
                                           # (adjustable cheapest-N-supplier bundle
                                           # cards, partial bidders eligible, BEST
                                           # VALUE highlighted - see
                                           # cs_engine.compute_best_bundle - with the
                                           # per-item lowest-count leaderboard
                                           # collapsed as secondary detail; "Full
                                           # bidders" stat card + every bundle card
                                           # are also click-to-reveal (vanilla JS, no
                                           # AJAX) showing that card's detail table
                                           # in a shared #analysis-detail-area, incl.
                                           # items a bundle doesn't cover, above the
                                           # collapsed leaderboard, accordion-style),
                                           # "Price Comparison" (click-to-award pills,
                                           # no reason needed to pick a non-lowest
                                           # bidder - see docs/data-model.md; locks
                                           # read-only once the proposal is
                                           # approved), and "All Quotes" (the same
                                           # _comparative_summary_grid.html partial
                                           # as Quote Entry, so both stay in sync from
                                           # one shared fragment, plus a "Compare
                                           # selected suppliers" panel - checkboxes +
                                           # Lowest-N/bundle quick-selects that filter
                                           # the on-screen grid, recomputing "lowest"
                                           # among just the selection, and a "Download
                                           # Working Comparison" export scoped to it -
                                           # see docs/data-model.md. The OFFICIAL
                                           # /export and /export-package downloads
                                           # take no supplier filter and always
                                           # include everyone, untouched by this - a
                                           # deliberate split, not an oversight, since
                                           # government CS documents are generally
                                           # expected to show every bidder)
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
    _phase_nav.html                               # shared 5-pill stepper (Items ->
                                                   # Quotes -> Comparison -> Proposal
                                                   # -> Contracts) across the 5
                                                   # lifecycle pages, included
                                                   # additively alongside each page's
                                                   # own quick-jump links. "Done"
                                                   # per pill is a simple approximation
                                                   # from tender.status alone (not a
                                                   # live per-phase completion check
                                                   # re-queried in all 5 routes) -
                                                   # items/quotes/comparison read done
                                                   # once status has left draft, etc.
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
  test_seed_demo_data.py
  test_insights.py
  test_lock.py
```

## UI redesign (Nocturne)

`base.html` and every page were re-skinned onto a dark-first design
reference the user generated externally and shared as a local export
(`UI redesign/` at the repo root - a Nocturne design-system bundle plus a
`Procurement CS Tool.dc.html` interactive prototype using the design
tool's own proprietary component/binding syntax, not plain HTML - a
visual reference to re-implement, not code to copy in). That folder isn't
committed (large, derived, one-time reference) - if you need to re-check
the original look in a later session and it's missing, ask the user for
it rather than reconstructing from memory of this file.

Two genuinely new features came out of that redesign, not just a re-skin:
Insights (`app/templates/insights.html`, read-only derived analytics) and
the optional local Lock screen (`app/lock.py`). Everything else - the
icon-rail sidebar, theme toggle, phase-pill stepper, Dashboard's two
views, RFQ list filter pills, Settings sub-nav - is presentation only;
no route's underlying business logic changed. Verified end-to-end
against the actual packaged `.exe` (not just the dev server): built it,
launched it with `%LOCALAPPDATA%` pointed at an empty temp dir to
simulate a genuinely fresh machine, and confirmed the new UI, the
auto-seeded demo data, and a real Contract Award download all worked
with zero manual steps.

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

Copying `dist\ProcurementCSTool.exe` to another machine and running it is
the whole install - no other files need to travel with it, and no
manual setup step is needed on the new machine. On first launch there
(an empty `%LOCALAPPDATA%\ProcurementCSTool\`), the app auto-creates its
database AND auto-seeds it with demo tenders/custom fields (see
`app/seed_demo_data.py` / docs/data-model.md) - it only skips seeding if
that machine already has real data in it. Verified end-to-end against
the actual built exe (not just simulated): launched it with
`%LOCALAPPDATA%` redirected to an empty temp dir, confirmed demo data
appeared and a downloaded Contract Award rendered its example custom
field values correctly.

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

A later session was given a second, real-looking sample set - `CA 8127.doc`,
`PP 8127.doc`, `CST 8127.xlsx` (a real tender number, real department/
personnel names and figures, not the `CS.xlsx` dummy dataset's numbers) -
used the same way: analysing which text in them was manually highlighted
red (meaning "this varies, fill it in") against what the shipped templates
had hardcoded, which is what `custom_fields.SUGGESTED_PP_CA_FIELDS` (see
`docs/data-model.md`) came from. `.gitignore` covers this pattern
generically (`CA *.doc`, `PP *.doc`, `CST *.xlsx`) so any future sample set
following this same "type space number" naming stays out of git without
needing a new `.gitignore` entry each time - check `git status` after
adding a new sample file to confirm it's actually ignored before assuming
so, since a differently-named file wouldn't be. These files are local-only
reference material, not durable - if a later session needs to re-derive
something from them and they're missing, ask the user rather than
reconstructing content from memory of this file (which deliberately does
not reproduce the sensitive text itself, only the field-name mapping it
led to).
