from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_session
from app.gstin import pan_of
from app.models import Entity, Gstin
from app.schemas import EntityCreate, EntityRead, EntityUpdate, GstinCreate, GstinRead, GstinUpdate

router = APIRouter(tags=["entity master"])


def _entity_or_404(session: Session, entity_id: int) -> Entity:
    entity = session.get(Entity, entity_id, options=[selectinload(Entity.gstins)])
    if not entity:
        raise HTTPException(404, f"entity {entity_id} not found")
    return entity


@router.get("/entities", response_model=list[EntityRead])
def list_entities(include_inactive: bool = False, session: Session = Depends(get_session)):
    stmt = select(Entity).options(selectinload(Entity.gstins)).order_by(Entity.name)
    if not include_inactive:
        stmt = stmt.where(Entity.active.is_(True))
    return session.scalars(stmt).all()


@router.post("/entities", response_model=EntityRead, status_code=201)
def create_entity(body: EntityCreate, session: Session = Depends(get_session)):
    if session.scalar(select(Entity).where(Entity.name == body.name)):
        raise HTTPException(409, f"entity '{body.name}' already exists")
    entity = Entity(**body.model_dump())
    session.add(entity)
    session.commit()
    return _entity_or_404(session, entity.id)


@router.get("/entities/{entity_id}", response_model=EntityRead)
def get_entity(entity_id: int, session: Session = Depends(get_session)):
    return _entity_or_404(session, entity_id)


@router.patch("/entities/{entity_id}", response_model=EntityRead)
def update_entity(entity_id: int, body: EntityUpdate, session: Session = Depends(get_session)):
    entity = _entity_or_404(session, entity_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("pan"):
        mismatched = [g.gstin for g in entity.gstins if pan_of(g.gstin) != changes["pan"]]
        if mismatched:
            raise HTTPException(422, f"PAN {changes['pan']} does not match GSTINs {', '.join(mismatched)}")
    for key, value in changes.items():
        setattr(entity, key, value)
    session.commit()
    return _entity_or_404(session, entity_id)


@router.post("/entities/{entity_id}/gstins", response_model=GstinRead, status_code=201)
def add_gstin(entity_id: int, body: GstinCreate, session: Session = Depends(get_session)):
    entity = _entity_or_404(session, entity_id)
    existing = session.scalar(select(Gstin).where(Gstin.gstin == body.gstin))
    if existing:
        raise HTTPException(409, f"GSTIN {body.gstin} is already registered to entity {existing.entity_id}")
    if entity.pan and pan_of(body.gstin) != entity.pan:
        raise HTTPException(422, f"GSTIN {body.gstin} does not belong to PAN {entity.pan}")
    gstin = Gstin(entity_id=entity.id, state_code=body.gstin[:2], **body.model_dump())
    if not entity.pan:
        entity.pan = pan_of(body.gstin)
    session.add(gstin)
    session.commit()
    return gstin


@router.get("/gstins", response_model=list[GstinRead])
def list_gstins(include_inactive: bool = False, session: Session = Depends(get_session)):
    stmt = select(Gstin).order_by(Gstin.gstin)
    if not include_inactive:
        stmt = stmt.where(Gstin.active.is_(True))
    return session.scalars(stmt).all()


@router.patch("/gstins/{gstin_id}", response_model=GstinRead)
def update_gstin(gstin_id: int, body: GstinUpdate, session: Session = Depends(get_session)):
    gstin = session.get(Gstin, gstin_id)
    if not gstin:
        raise HTTPException(404, f"GSTIN record {gstin_id} not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(gstin, key, value)
    session.commit()
    return gstin
