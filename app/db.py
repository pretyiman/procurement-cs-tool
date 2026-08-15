from sqlmodel import Session, SQLModel, create_engine

from .paths import user_data_dir

DB_PATH = user_data_dir() / "procurement.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    _reset_orphaned_proposal_statuses()


def _add_missing_columns() -> None:
    """No migration framework (SQLite, single-file, local-first) -
    create_all only creates brand-new tables, it won't add a column to a
    table that already exists. This adds columns introduced after a table
    already shipped, so an existing DB (with real data in it) doesn't need
    to be deleted just to pick up a schema change."""
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(custom_field)")}
        if "group_id" not in cols:
            conn.exec_driver_sql("ALTER TABLE custom_field ADD COLUMN group_id INTEGER REFERENCES custom_field_group(id)")
            # Custom Field Groups reuse the same tag_name once per group (see
            # CustomFieldGroup) - a DB created before groups existed has a
            # single-column UNIQUE index on tag_name from the old model,
            # which would wrongly block that. Swap it for a plain index.
            conn.exec_driver_sql("DROP INDEX IF EXISTS ix_custom_field_tag_name")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_custom_field_tag_name ON custom_field (tag_name)")
            conn.commit()


def _reset_orphaned_proposal_statuses() -> None:
    """ProposalSnapshot (added after status/proposal_generated/awarded
    already existed) is now the only source of truth for a tender's
    Purchase Proposal / Contract Award pages. A tender already sitting at
    proposal_generated/proposal_approved/awarded from before this feature
    shipped has no snapshot row and no way to reconstruct one after the
    fact - the safest recovery is to drop it back to draft (its item
    awards are untouched) so Generate Proposal -> Approve -> Contract
    Award can run fresh, rather than the page crashing on a missing
    snapshot every time someone opens it."""
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(tender)")}
        if "status" not in cols:
            return  # fresh DB, nothing to reconcile
        has_snapshot_table = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE name='proposal_snapshot'"
        ).fetchone()
        if not has_snapshot_table:
            return
        orphaned = conn.exec_driver_sql(
            """
            SELECT t.id FROM tender t
            LEFT JOIN proposal_snapshot ps ON ps.tender_id = t.id
            WHERE t.status IN ('proposal_generated', 'proposal_approved', 'awarded')
              AND ps.id IS NULL
            """
        ).fetchall()
        if orphaned:
            conn.exec_driver_sql(
                """
                UPDATE tender SET status = 'draft', awarded_date = NULL
                WHERE id IN ({})
                """.format(",".join(str(row[0]) for row in orphaned))
            )
            conn.commit()


def get_session():
    with Session(engine) as session:
        yield session
