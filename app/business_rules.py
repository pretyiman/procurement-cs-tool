"""Policy numbers editable from Settings instead of being hardcoded
constants - see docx_export.py's use of these for Contract Award
generation (security deposit / stamp duty)."""

from sqlmodel import Session, select

from .models import BusinessRules


def get_business_rules(session: Session) -> BusinessRules:
    rules = session.exec(select(BusinessRules).where(BusinessRules.id == 1)).first()
    if rules is None:
        rules = BusinessRules(id=1)
        session.add(rules)
        session.commit()
        session.refresh(rules)
    return rules
