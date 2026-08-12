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
- **Frontend**: server-rendered (Jinja2 templates + HTMX), not a separate JS
  build. Keeps the app truly standalone (no node toolchain) and easy for an
  agent to build/modify in small increments.
- **Excel I/O**: `openpyxl` / `pandas`.
- **Word generation**: `docxtpl` (Jinja2-style placeholders inside a real
  .docx template), NOT raw `python-docx` composition. This lets non-technical
  procurement staff open the template in Word and edit letterhead / T&C /
  security-of-contract wording directly — the app only fills in data.
- **Packaging**: PyInstaller (or similar) for a standalone local launcher,
  once the app works.

## Repo layout (target — created as phases are built, not all at once)

```
CLAUDE.md
PLAN.md
docs/
  data-model.md
CS.xlsx                 # existing dummy dataset — treated as a fixture/
                         # regression-test target, do not overwrite
app/
  main.py               # FastAPI app
  models.py             # DB models
  cs_engine.py           # comparative-statement calculation
  award_engine.py         # lowest + manual override logic
  excel_io.py            # import quotes, export CS.xlsx
  docx_export.py          # purchase proposal + contract drafts
  templates/             # Jinja2/HTMX pages
  docx_templates/          # editable .docx templates (T&C, security clause, etc.)
tests/
  test_cs_engine.py       # asserts against known-good numbers from CS.xlsx
```

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

## Data sensitivity

`CS.xlsx` in this repo is a **dummy/sample** dataset used only to validate
the calculation engine. Real supplier/pricing data must never be committed
to this repo as-is — once real data is used, add the real data files/folder
to `.gitignore`.
