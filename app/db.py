from sqlmodel import Session, SQLModel, create_engine

from .paths import user_data_dir

DB_PATH = user_data_dir() / "procurement.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


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


def get_session():
    with Session(engine) as session:
        yield session
