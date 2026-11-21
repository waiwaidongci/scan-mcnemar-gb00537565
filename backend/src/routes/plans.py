from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.config.settings import ARTIFACTS
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.sequential import Plan, Look
from src.models.source import Dataset
from src.schemas.requests import PlanRequest
from src.services import sequential
from src.services.validation import normalize_plan_params
from src.utils.time import iso
from src.utils.wakeup import wake

router = APIRouter(prefix='/plans')


def require_plan(db, plan_id, lock=False):
    item = db.get(Plan, plan_id, with_for_update=lock)
    if not item:
        raise HTTPException(404, '找不到该验收计划')
    return item


def plan_view(db, plan, detail=False):
    looks = sequential.look_ledger(db, plan)
    last = looks[-1] if looks else None
    retry = last is not None and last['status'] == 'failed' and not last['conclusion']
    next_sequence = last['sequence'] if retry else len(looks) + 1
    batch = plan.batches[next_sequence - 1] if next_sequence <= len(plan.batches) else None
    data = {
        'id': plan.id, 'dataset_id': plan.dataset_id, 'dataset_checksum': plan.dataset_checksum,
        'name': plan.name, 'alpha_total': plan.alpha_total, 'min_pairs': plan.min_pairs,
        'min_gain_pp': plan.min_gain_pp, 'status': plan.status, 'sealed_look_id': plan.sealed_look_id,
        'sealed_at': iso(plan.sealed_at), 'author': plan.author, 'created_at': iso(plan.created_at),
        'batches': plan.batches, 'alphas': plan.alphas,
        'used_alpha': round(sum(item['alpha'] for item in looks), 6),
        'next_sequence': next_sequence if batch else None, 'next_batch': batch,
        'looks': looks,
    }
    if detail:
        current = sequential.latest_look(db, plan.id)
        data['current'] = look_view(current) if current else None
    return data


def look_view(look, detail=False):
    data = {
        'id': look.id, 'plan_id': look.plan_id, 'sequence': look.sequence, 'batch_id': look.batch_id,
        'alpha': look.alpha, 'min_gain_pp': look.min_gain_pp, 'status': look.status,
        'conclusion': look.conclusion, 'summary': look.summary, 'error': look.error,
        'started_at': iso(look.started_at), 'finished_at': iso(look.finished_at),
        'published_at': iso(look.published_at), 'published_by': look.published_by,
        'has_export': bool(look.export_file), 'attempt': look.attempt,
    }
    if detail:
        data['result'] = look.result
    return data


@router.get('')
def index(db: Session = Depends(connection), actor: User = Depends(current)):
    plans = db.scalars(select(Plan).order_by(Plan.id.desc()).limit(100)).all()
    return [plan_view(db, plan) for plan in plans]


@router.post('', status_code=201)
def create(body: PlanRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    dataset = db.get(Dataset, body.dataset_id)
    if not dataset:
        raise HTTPException(404, '找不到指定的数据批次')
    params = normalize_plan_params(body.params)
    plan = sequential.lock_plan(db, dataset, body.name, params['alpha_total'],
                                params['min_pairs'], params['min_gain_pp'],
                                body.alphas, actor.username)
    db.commit()
    return plan_view(db, plan)


@router.get('/{plan_id}')
def detail(plan_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    return plan_view(db, require_plan(db, plan_id), detail=True)


@router.post('/{plan_id}/looks', status_code=202)
def start(plan_id: int, db: Session = Depends(connection),
          actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    require_plan(db, plan_id)
    look, created = sequential.start_look(db, plan_id, actor.username)
    db.commit()
    wake()
    return {'created': created, 'look': look_view(look, detail=True)}


@router.post('/{plan_id}/looks/{look_id}/publish')
def publish(plan_id: int, look_id: int, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN))):
    plan = require_plan(db, plan_id, lock=True)
    look = db.get(Look, look_id, with_for_update=True)
    if not look:
        raise HTTPException(404, '找不到该次查看')
    sequential.publish_look(db, plan, look, actor.username)
    db.commit()
    return plan_view(db, plan, detail=True)


@router.get('/{plan_id}/looks/{look_id}/export')
def export(plan_id: int, look_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    require_plan(db, plan_id)
    look = db.get(Look, look_id)
    if not look or look.plan_id != plan_id:
        raise HTTPException(404, '找不到该次查看')
    if not look.export_file:
        raise HTTPException(409, '此记录尚无可下载结果')
    path = ARTIFACTS / look.export_file
    if not path.is_file():
        raise HTTPException(410, '导出文件已不可用，请重新发起查看')
    return FileResponse(path, media_type='text/csv',
                        filename=f'{SPEC["short_name"]}-plan{plan_id}-look{look.sequence}.csv')
