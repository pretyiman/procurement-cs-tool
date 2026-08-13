"""Title/signature-block text editable from Settings instead of being
hardcoded strings - see excel_io.py's use of these for the CS Excel
export (both the item-wise and package-basis versions)."""

from sqlmodel import Session, select

from .models import DocumentLabels


def get_document_labels(session: Session) -> DocumentLabels:
    labels = session.exec(select(DocumentLabels).where(DocumentLabels.id == 1)).first()
    if labels is None:
        labels = DocumentLabels(id=1)
        session.add(labels)
        session.commit()
        session.refresh(labels)
    return labels
