"""分批验收的锁计划、发起查看、后台判定与发布闸门。"""

from datetime import timedelta

from sqlalchemy import and_, or_, select

from src.config.database import Session
from src.constants.enums import Conclusion, LookStatus, PlanStatus
from src.domain.sequential import analyze_look, build_schedule
from src.models.sequential import Plan, Look
from src.models.source import Dataset
from src.services.audit import record
from src.utils.exports import write_report
from src.utils.time import now

LOCKED_PARAMS = ('alpha_total', 'min_pairs', 'min_gain_pp')


def lock_plan(db, dataset, name, alpha_total, min_pairs, min_gain_pp, alphas, actor):
    """首查前锁定批次顺序、各次额度和净增门槛。"""
    schedule = build_schedule(dataset.rows, alphas, alpha_total, min_gain_pp)
    plan = Plan(
        dataset_id=dataset.id, dataset_checksum=dataset.checksum, name=name,
        alpha_total=float(alpha_total), min_pairs=int(min_pairs), min_gain_pp=float(min_gain_pp),
        batches=[entry['batch_id'] for entry in schedule],
        alphas=[entry['alpha'] for entry in schedule],
        author=actor,
    )
    db.add(plan)
    db.flush()
    record(db, actor, 'plan.locked', plan.id,
           dataset_id=dataset.id, batches=plan.batches, alphas=plan.alphas,
           alpha_total=plan.alpha_total, min_pairs=plan.min_pairs, min_gain_pp=plan.min_gain_pp)
    return plan


def latest_look(db, plan_id):
    return db.scalar(select(Look).where(Look.plan_id == plan_id).order_by(Look.sequence.desc()).limit(1))


def look_ledger(db, plan):
    """已用过的批次、额度和结论，供页面和导出核对。"""
    looks = db.scalars(select(Look).where(Look.plan_id == plan.id).order_by(Look.sequence)).all()
    ledger = []
    used = 0.0
    for item in looks:
        summary = item.summary or {}
        used += float(item.alpha)
        ledger.append({
            'id': item.id,
            'sequence': item.sequence,
            'batch_id': item.batch_id,
            'alpha': item.alpha,
            'min_gain_pp': item.min_gain_pp,
            'status': item.status,
            'conclusion': item.conclusion,
            'pairs': summary.get('pairs'),
            'batch_pairs': summary.get('batch_pairs'),
            'p_value': summary.get('p_value'),
            'gain_pp': summary.get('gain_pp'),
            'used_alpha': round(used, 6),
            'published_at': item.published_at,
            'published_by': item.published_by,
            'attempt': item.attempt,
            'has_export': bool(item.export_file),
            'error': item.error,
        })
    return ledger


def start_look(db, plan_id, actor):
    """查看下一批；并发发起同一批只占一次额度（唯一约束 + 行锁）。"""
    plan = db.get(Plan, plan_id, with_for_update=True)
    if not plan:
        raise LookupError('找不到该验收计划')
    if plan.status != PlanStatus.ACTIVE:
        raise ValueError('该计划已封存结论，不能继续查看。')
    existing = db.scalars(select(Look).where(Look.plan_id == plan.id).order_by(Look.sequence)).all()
    if existing:
        last = existing[-1]
        if last.status in (LookStatus.PENDING, LookStatus.RUNNING):
            # 并发发起同一批：复用已占用的额度，不再新增。
            return last, False
        if last.status == LookStatus.FAILED and not last.summary:
            # 计算失败沿用该批次的锁定样本和额度重跑，不额外占用额度。
            last.status = LookStatus.PENDING
            last.error = None
            last.attempt = 0
            record(db, actor, 'look.retried', last.id, plan_id=plan.id, sequence=last.sequence,
                   batch_id=last.batch_id, alpha=last.alpha)
            return last, False
        if last.conclusion == Conclusion.PASS:
            raise ValueError('该计划已达标封存，不能继续查看。')
        if not last.published_at:
            raise ValueError('请先发布上一批未达标的结果，再查看下一批。')
    sequence = len(existing) + 1
    if sequence > len(plan.batches):
        raise ValueError('该计划的批次已全部查看完毕。')
    look = Look(
        plan_id=plan.id, sequence=sequence, batch_id=plan.batches[sequence - 1],
        alpha=plan.alphas[sequence - 1], min_gain_pp=plan.min_gain_pp,
    )
    db.add(look)
    db.flush()
    record(db, actor, 'look.queued', look.id, plan_id=plan.id, sequence=look.sequence,
           batch_id=look.batch_id, alpha=look.alpha)
    return look, True


def publish_look(db, plan, look, actor):
    if look.plan_id != plan.id:
        raise LookupError('找不到该次查看')
    if look.status != LookStatus.COMPLETE:
        raise ValueError('只有已完成的查看结果可以发布。')
    if look.conclusion == Conclusion.PASS:
        raise ValueError('达标结果已封存，无需再发布。')
    if look.published_at:
        return look
    before = db.scalar(select(Look).where(Look.plan_id == plan.id, Look.sequence < look.sequence)
                       .order_by(Look.sequence.desc()).limit(1))
    if before and not before.published_at:
        raise ValueError('请按批次顺序发布结果。')
    look.published_at = now()
    look.published_by = actor
    record(db, actor, 'look.published', look.id, plan_id=plan.id, sequence=look.sequence,
           batch_id=look.batch_id, alpha=look.alpha, conclusion=look.conclusion)
    return look


def claim_look():
    """领取待查看记录；同一计划同一时刻只允许一个查看占用租约。"""
    stamp = now()
    with Session.begin() as db:
        query = select(Look).where(
            or_(Look.status == LookStatus.PENDING,
                and_(Look.status == LookStatus.RUNNING, Look.lease_until < stamp))
        ).order_by(Look.id).limit(1).with_for_update(skip_locked=True)
        look = db.scalar(query)
        if look is None:
            return None
        plan = db.get(Plan, look.plan_id, with_for_update=True)
        if plan is None or plan.status != PlanStatus.ACTIVE or look.conclusion == Conclusion.PASS:
            look.status = LookStatus.FAILED
            look.error = '计划已封存，本次查看不再判定'
            look.finished_at = stamp
            return None
        if look.attempt >= 3:
            look.status = LookStatus.FAILED
            look.error = '后台进程多次中断，请重新发起查看'
            look.finished_at = stamp
            record(db, 'worker', 'look.failed', look.id, reason=look.error)
            return None
        look.status = LookStatus.RUNNING
        look.attempt += 1
        look.lease_until = stamp + timedelta(minutes=3)
        if look.started_at is None:
            look.started_at = stamp
        rows = db.get(Dataset, plan.dataset_id).rows
        return {
            'look_id': look.id, 'attempt': look.attempt, 'rows': rows,
            'batches': list(plan.batches[:look.sequence]),
            'entry': {'sequence': look.sequence, 'batch_id': look.batch_id,
                      'alpha': look.alpha, 'min_gain_pp': look.min_gain_pp},
            'min_pairs': plan.min_pairs, 'alpha_total': plan.alpha_total,
        }


def _prior_entries(db, plan, look):
    previous = db.scalars(select(Look).where(Look.plan_id == plan.id, Look.sequence < look.sequence)
                          .order_by(Look.sequence)).all()
    entries = []
    for item in previous:
        summary = item.summary or {}
        entries.append({
            'sequence': item.sequence, 'batch_id': item.batch_id,
            'pairs': summary.get('pairs'), 'alpha': item.alpha, 'min_gain_pp': item.min_gain_pp,
            'conclusion': item.conclusion or 'continue',
            'published_at': item.published_at.isoformat() if item.published_at else None,
        })
    return entries


def execute_look(lease):
    """用锁定的累计样本和额度判定；旧尝试晚到不能改写结论。"""
    look_id, attempt = lease['look_id'], lease['attempt']
    result = export = failure = None
    try:
        with Session() as db:
            look = db.get(Look, look_id)
            plan = db.get(Plan, look.plan_id)
            prior = _prior_entries(db, plan, look)
        result, summary = analyze_look(
            lease['rows'], lease['batches'], lease['entry'], lease['min_pairs'],
            lease['alpha_total'], prior,
        )
        export = write_report(look_id, attempt, result, prefix='look')
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Look %s failed', look_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        look = db.get(Look, look_id, with_for_update=True)
        plan = db.get(Plan, look.plan_id, with_for_update=True)
        # 只有仍持有该次租约的尝试才能落结论，旧尝试晚到直接丢弃。
        if look.attempt != attempt or look.status != LookStatus.RUNNING:
            return
        if plan.status != PlanStatus.ACTIVE:
            return
        look.lease_until = None
        look.finished_at = now()
        if failure:
            look.status = LookStatus.FAILED
            look.error = failure
            record(db, 'worker', 'look.failed', look.id, attempt=attempt, reason=failure)
            return
        look.status = LookStatus.COMPLETE
        look.result = result
        look.summary = summary
        look.conclusion = summary['conclusion']
        look.export_file = export
        record(db, 'worker', 'look.complete', look.id, attempt=attempt,
               sequence=look.sequence, batch_id=look.batch_id, alpha=look.alpha,
               conclusion=look.conclusion, pairs=summary['pairs'])
        if summary['conclusion'] == Conclusion.PASS:
            plan.status = PlanStatus.SEALED
            plan.sealed_look_id = look.id
            plan.sealed_at = now()
            record(db, 'worker', 'plan.sealed', plan.id, look_id=look.id, sequence=look.sequence)
