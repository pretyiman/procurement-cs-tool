from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.custom_fields import SUGGESTED_CA_FIELDS, SUGGESTED_PP_FIELDS
from app.models import CustomField, CustomFieldGroup, Item, Quote, Tender, TenderStatus
from app.seed_demo_data import seed_demo_data_if_empty


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_seeds_tenders_spanning_every_lifecycle_stage_on_an_empty_db():
    with _fresh_session() as session:
        seed_demo_data_if_empty(session)

        tenders = session.exec(select(Tender)).all()
        statuses = {t.inquiry_no: t.status for t in tenders}
        assert statuses == {
            "DEMO/2026/001": TenderStatus.draft,
            "DEMO/2026/002": TenderStatus.draft,
            "DEMO/2026/003": TenderStatus.proposal_generated,
            "DEMO/2026/004": TenderStatus.proposal_approved,
            "DEMO/2026/005": TenderStatus.awarded,
        }
        assert session.exec(select(Item)).first() is not None
        assert session.exec(select(Quote)).first() is not None


def test_seeds_a_custom_field_group_with_every_suggested_pp_ca_tag():
    with _fresh_session() as session:
        seed_demo_data_if_empty(session)

        groups = session.exec(select(CustomFieldGroup)).all()
        assert len(groups) == 1

        fields = session.exec(select(CustomField).where(CustomField.group_id == groups[0].id)).all()
        seeded_tags = {f.tag_name for f in fields}
        assert seeded_tags == set(SUGGESTED_PP_FIELDS) | set(SUGGESTED_CA_FIELDS)
        assert all(f.value.strip() for f in fields)  # every example value is non-blank


def test_does_not_touch_a_database_that_already_has_a_tender():
    with _fresh_session() as session:
        session.add(Tender(inquiry_no="REAL/2026/001"))
        session.commit()

        seed_demo_data_if_empty(session)

        tenders = session.exec(select(Tender)).all()
        assert len(tenders) == 1
        assert tenders[0].inquiry_no == "REAL/2026/001"
        assert session.exec(select(CustomFieldGroup)).first() is None


def test_calling_twice_on_a_freshly_seeded_db_is_a_no_op():
    with _fresh_session() as session:
        seed_demo_data_if_empty(session)
        first_count = len(session.exec(select(Tender)).all())

        seed_demo_data_if_empty(session)
        second_count = len(session.exec(select(Tender)).all())

        assert first_count == second_count == 5
