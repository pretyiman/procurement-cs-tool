from sqlmodel import Session, SQLModel, create_engine

from .paths import user_data_dir

DB_PATH = user_data_dir() / "procurement.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
