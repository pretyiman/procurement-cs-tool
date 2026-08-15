"""Freezing the Purchase Proposal, and the persisted Contract Award number
that comes after it.

Lifecycle: draft -> proposal_generated -> proposal_approved -> awarded.

save_proposal_snapshot() is called every time "Generate Proposal" runs. It
always overwrites the tender's existing ProposalSnapshot (delete + recreate,
including its firm_groups/items) from the *live* award_engine result -
that's the revise-after-rejection cycle: while still proposal_generated,
regenerating is expected and cheap. Once approve_proposal_snapshot() has
run (status -> proposal_approved), the snapshot is locked - the routes in
main.py must stop calling save_proposal_snapshot() at that point, and
docx_export.py's CA/PP generation reads only from these frozen rows from
then on, never from live Item/Quote/catalog state.

ContractAward is deliberately a separate table, not a column on
ProposalSnapshotFirmGroup: it's assigned per firm only after the snapshot
is approved, entered by a human (not derived from anything), and finalizing
a tender to `awarded` requires one to exist for every firm in the approved
snapshot (see all_firms_have_contract_award).
"""

import datetime
from typing import Optional

from sqlmodel import Session, select

from .award_engine import build_purchase_proposal
from .cs_engine import build_comparative_statement
from .models import (
    ContractAward,
    ProposalSnapshot,
    ProposalSnapshotFirmGroup,
    ProposalSnapshotItem,
    Tender,
    TenderStatus,
)


def get_snapshot(session: Session, tender_id: int) -> Optional[ProposalSnapshot]:
    return session.exec(select(ProposalSnapshot).where(ProposalSnapshot.tender_id == tender_id)).first()


def save_proposal_snapshot(session: Session, tender_id: int) -> ProposalSnapshot:
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise ValueError("Tender not found.")
    if tender.status not in (TenderStatus.draft, TenderStatus.proposal_generated):
        raise ValueError("The proposal is already approved and can't be regenerated.")

    proposal = build_purchase_proposal(session, tender_id)
    if not proposal.firm_groups:
        raise ValueError("Award at least one item before generating the proposal.")
    participating_firms_count = len(build_comparative_statement(session, tender_id).suppliers_by_id)

    existing = get_snapshot(session, tender_id)
    if existing is not None:
        for group in existing.firm_groups:
            for item in group.items:
                session.delete(item)
            session.delete(group)
        session.delete(existing)
        session.flush()

    tender.status = TenderStatus.proposal_generated
    session.add(tender)

    snapshot = ProposalSnapshot(
        tender_id=tender_id,
        generated_at=datetime.datetime.utcnow(),
        indent_no=tender.indent_no or tender.inquiry_no,
        department_name=tender.department.name if tender.department else None,
        firms_invited_count=tender.firms_invited_count,
        issue_date=tender.issue_date,
        opening_date=tender.opening_date,
        delivery_days=tender.delivery_days,
        warranty_months=tender.warranty_months,
        tax_type=tender.tax_type.value,
        tax_percent=tender.tax_percent,
        participating_firms_count=participating_firms_count,
        total_item_count=proposal.grand_total.item_count + len(proposal.unresolved_items),
        grand_item_count=proposal.grand_total.item_count,
        grand_store_value=proposal.grand_total.store_value,
        grand_tax_amount=proposal.grand_total.tax_amount,
        grand_contract_value=proposal.grand_total.contract_value,
    )
    session.add(snapshot)
    session.flush()

    for group in proposal.firm_groups:
        db_group = ProposalSnapshotFirmGroup(
            snapshot_id=snapshot.id,
            supplier_id=group.supplier_id,
            supplier_name=group.supplier_name,
            store_value=group.store_value,
            tax_amount=group.tax_amount,
            contract_value=group.contract_value,
        )
        session.add(db_group)
        session.flush()
        for ai in group.items:
            session.add(
                ProposalSnapshotItem(
                    firm_group_id=db_group.id,
                    ser=ai.item.ser,
                    part_no=ai.item.item_master.part_no,
                    description=ai.item.item_master.description,
                    unit=ai.item.item_master.default_unit,
                    qty=ai.item.qty,
                    rate=ai.awarded_rate,
                    total_value=ai.total_value,
                    lpr=ai.item.lpr,
                    is_override=ai.is_override,
                    override_reason=ai.override_reason,
                )
            )

    session.commit()
    session.refresh(snapshot)
    return snapshot


def approve_proposal_snapshot(session: Session, tender_id: int) -> ProposalSnapshot:
    tender = session.get(Tender, tender_id)
    if tender is None:
        raise ValueError("Tender not found.")
    if tender.status != TenderStatus.proposal_generated:
        raise ValueError("Generate the proposal before approving it.")

    snapshot = get_snapshot(session, tender_id)
    if snapshot is None:
        raise ValueError("No proposal has been generated yet.")

    snapshot.approved_at = datetime.datetime.utcnow()
    tender.status = TenderStatus.proposal_approved
    session.add(snapshot)
    session.add(tender)
    session.commit()
    session.refresh(snapshot)
    return snapshot


def get_contract_award(session: Session, snapshot_id: int, supplier_id: int) -> Optional[ContractAward]:
    return session.exec(
        select(ContractAward).where(
            ContractAward.snapshot_id == snapshot_id, ContractAward.supplier_id == supplier_id
        )
    ).first()


def upsert_contract_award(
    session: Session,
    snapshot_id: int,
    supplier_id: int,
    contract_no: str,
    contract_date: Optional[datetime.date] = None,
) -> ContractAward:
    contract_no = contract_no.strip()
    if not contract_no:
        raise ValueError("Contract number is required.")

    award = get_contract_award(session, snapshot_id, supplier_id)
    if award is None:
        award = ContractAward(
            snapshot_id=snapshot_id,
            supplier_id=supplier_id,
            contract_no=contract_no,
            contract_date=contract_date,
            created_at=datetime.datetime.utcnow(),
        )
    else:
        award.contract_no = contract_no
        if contract_date is not None:
            award.contract_date = contract_date
    session.add(award)
    session.commit()
    session.refresh(award)
    return award


def all_firms_have_contract_award(session: Session, snapshot_id: int) -> bool:
    groups = session.exec(
        select(ProposalSnapshotFirmGroup).where(ProposalSnapshotFirmGroup.snapshot_id == snapshot_id)
    ).all()
    if not groups:
        return False
    awarded_supplier_ids = {
        a.supplier_id
        for a in session.exec(select(ContractAward).where(ContractAward.snapshot_id == snapshot_id)).all()
    }
    return all(g.supplier_id in awarded_supplier_ids for g in groups)
