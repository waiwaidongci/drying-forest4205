"""同来源试件合计时长的公开申请与泄露核验。

规则：
- 一份申请只属于一个来源，名单是该来源内的试件编号。
- 已公开合计与本次合计都是“若干试件时长之和”的 0/1 系数方程。若这些方程
  能线性确定任意一个单件的时长（含单件名单、叠加、相减），则拒绝。
- 待公开的合计先落库再核验；中断后恢复时沿用入库的名单与合计继续核验，
  补录新样本也不会改写已受理申请的旧合计。
- 核验通过全局锁行的乐观版本号串行化：并发申请只有一方能基于同一版已公开
  集合落定，另一方必须重读，不会同时公开合起来会泄露单件的结果。
- 每份申请只会从 pending 迁移一次；相同申请标识的重试只生效一次。
"""
import hashlib
import logging
import time
from fractions import Fraction

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from ..config.database import Session
from ..models.aggregate import AggregateRequest, DisclosureLock
from ..models.record import Record
from ..utils.serialization import now
from .audit import log
from .sources import source_or_404

PENDING = 'pending'
PUBLISHED = 'published'
REJECTED = 'rejected'
LOCK_ID = 1
MAX_ATTEMPTS = 8


def _normalize_references(references):
    out, seen = [], set()
    for value in references:
        reference = str(value).strip()
        if not reference:
            continue
        if reference in seen:
            raise HTTPException(422, '同一申请内的试件编号不能重复：' + reference)
        seen.add(reference)
        out.append(reference)
    if not out:
        raise HTTPException(422, '至少需要一个同来源的试件编号')
    return sorted(out)


def _auto_key(source_id, references):
    payload = str(source_id) + '|' + ','.join(references)
    return 'auto-' + hashlib.sha256(payload.encode()).hexdigest()


def _load_items(db, source_id, references):
    records = db.scalars(
        select(Record).where(Record.source_id == source_id, Record.reference.in_(references))
    ).all()
    by_reference = {r.reference: r for r in records}
    missing = [r for r in references if r not in by_reference]
    if missing:
        raise HTTPException(422, '来源中没有这些试件编号：' + '、'.join(missing[:10]))
    ordered = [by_reference[r] for r in references]
    try:
        total = float(sum(float(r.values['minutes']) for r in ordered))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, '试件缺少可用的实测时长，无法形成合计')
    return ordered, total


def public_row(request):
    """未公开的合计一律不返回；页面只能看到待核验/已公开/拒绝状态。"""
    return {
        'id': request.id,
        'source_id': request.source_id,
        'references': request.references,
        'item_count': len(request.references),
        'state': request.state,
        'total': request.total if request.state == PUBLISHED else None,
        'reason': request.reason,
        'requested_by': request.requested_by,
        'created_at': request.created_at,
        'decided_at': request.decided_at,
    }


def _same_application(request, source_id, references):
    return request.source_id == source_id and sorted(request.references) == references


def _ensure_lock_row():
    with Session() as db:
        if db.get(DisclosureLock, LOCK_ID) is not None:
            return
        db.add(DisclosureLock(id=LOCK_ID, version=0))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()


def _rref(matrix, width):
    rows = [[Fraction(value) for value in row] for row in matrix]
    rank, pivots = 0, []
    for column in range(width):
        pivot = None
        for index in range(rank, len(rows)):
            if rows[index][column] != 0:
                pivot = index
                break
        if pivot is None:
            continue
        rows[rank], rows[pivot] = rows[pivot], rows[rank]
        scale = rows[rank][column]
        rows[rank] = [value / scale for value in rows[rank]]
        for index in range(len(rows)):
            if index != rank and rows[index][column] != 0:
                factor = rows[index][column]
                rows[index] = [a - factor * b for a, b in zip(rows[index], rows[rank])]
        pivots.append(column)
        rank += 1
        if rank == len(rows):
            break
    return rows[:rank], pivots


def _leaking_record(requests):
    """线性方程组能确定的第一个单件 record id；没有则返回 None。

    对 0/1 系数矩阵做精确有理数 RREF：单位向量 e_i 属于行空间，当且仅当 i 是
    主元列，且该主元行在所有非主元列上的系数均为 0。
    """
    record_ids, index = [], {}
    for request in requests:
        for record_id in request.record_ids:
            if record_id not in index:
                index[record_id] = len(record_ids)
                record_ids.append(record_id)
    if not record_ids:
        return None
    matrix = []
    for request in requests:
        coefficients = [0] * len(record_ids)
        for record_id in request.record_ids:
            coefficients[index[record_id]] = 1
        matrix.append(coefficients)
    reduced, pivots = _rref(matrix, len(record_ids))
    pivot_columns = set(pivots)
    for row, pivot in zip(reduced, pivots):
        if all(row[column] == 0 for column in range(len(record_ids)) if column not in pivot_columns):
            return record_ids[pivot]
    return None


def _reference_of(requests, record_id):
    for request in requests:
        for reference, candidate in zip(request.references, request.record_ids):
            if candidate == record_id:
                return reference
    return str(record_id)


def _decide(published, request):
    checked = published + [request]
    leaked = _leaking_record(checked)
    if leaked is None:
        return PUBLISHED, ''
    return REJECTED, '与历次已公开合计叠加/相减可推出单件「' + _reference_of(checked, leaked) + '」的时长'


def verify_pending(request_id):
    """核验并落定一份 pending 申请；已在终态则原样返回，不重复生效。"""
    for attempt in range(MAX_ATTEMPTS):
        with Session() as db:
            try:
                request = db.get(AggregateRequest, request_id)
                if request is None:
                    return None
                if request.state != PENDING:
                    return public_row(request)
                db.rollback()

                if attempt == 0:
                    _ensure_lock_row()

                lock = db.get(DisclosureLock, LOCK_ID)
                if lock is None:
                    db.rollback()
                    _ensure_lock_row()
                    continue
                version = lock.version

                # 重读 pending 申请，避免与并发的重试重复搬迁状态。
                request = db.get(AggregateRequest, request_id)
                if request is None:
                    return None
                if request.state != PENDING:
                    return public_row(request)

                published = list(db.scalars(select(AggregateRequest).where(
                    AggregateRequest.state == PUBLISHED)))
                state, reason = _decide(published, request)

                # 乐观版本号：并发申请若基于同一版已公开集合，只有一方能提交。
                claimed = db.execute(
                    update(DisclosureLock)
                    .where(DisclosureLock.id == LOCK_ID, DisclosureLock.version == version)
                    .values(version=version + 1))
                if claimed.rowcount != 1:
                    db.rollback()
                    time.sleep(0.01 * (attempt + 1))
                    continue

                request.state = state
                request.reason = reason
                request.decided_at = now()
                log(db, request.requested_by, 'aggregate.' + state, request.id,
                    ('拒绝：' + reason) if state == REJECTED
                    else '公开：' + str(len(request.references)) + ' 件合计')
                db.commit()
                return public_row(request)
            except OperationalError:
                db.rollback()
                time.sleep(0.02 * (attempt + 1))
            except IntegrityError:
                db.rollback()
                time.sleep(0.01 * (attempt + 1))
    raise HTTPException(503, '合计核验正在被其他申请占用，请稍后重试')


def _find_by_key(db, key):
    return db.scalar(select(AggregateRequest).where(AggregateRequest.request_key == key))


def _existing_result(db, existing):
    state, request_id = existing.state, existing.id
    snapshot = None if state == PENDING else public_row(existing)
    db.rollback()
    if state == PENDING:
        return verify_pending(request_id)
    return snapshot


def submit(db, user, data):
    source_id = data.source_id
    source_or_404(db, source_id)
    references = _normalize_references(data.references)
    supplied = (data.request_key or '').strip()
    key = supplied or _auto_key(source_id, references)

    # 先按申请标识查重：重试沿用已入库的名单与合计，不在补录后重算。
    existing = _find_by_key(db, key)
    if existing is not None:
        if not _same_application(existing, source_id, references):
            raise HTTPException(409, '该申请标识已用于另一份合计申请')
        return _existing_result(db, existing)

    records, total = _load_items(db, source_id, references)
    request = AggregateRequest(
        source_id=source_id, request_key=key, references=references,
        record_ids=[r.id for r in records], total=total, state=PENDING,
        requested_by=user.email, created_at=now())
    try:
        db.add(request)
        db.flush()
        log(db, user, 'aggregate.submitted', request.id, str(len(references)) + ' 件')
        # 先把待公开的合计持久化；核验可能在不同进程或重启后继续。
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _find_by_key(db, key)
        if existing is None:
            raise HTTPException(503, '合计申请暂时无法保存，请稍后重试')
        if not _same_application(existing, source_id, references):
            raise HTTPException(409, '该申请标识已用于另一份合计申请')
        return _existing_result(db, existing)

    return verify_pending(request.id)


def retry(db, user, request_id):
    request = db.get(AggregateRequest, request_id)
    if request is None:
        raise HTTPException(404, '未找到合计申请')
    if request.state != PENDING:
        return public_row(request)
    db.rollback()
    return verify_pending(request_id)


def list_requests(db):
    return [public_row(r) for r in db.scalars(
        select(AggregateRequest).order_by(AggregateRequest.id.desc()))]


def resume_pending():
    """启动时接着核验已保存但尚未完成核验的合计申请。"""
    with Session() as db:
        ids = list(db.scalars(select(AggregateRequest.id).where(
            AggregateRequest.state == PENDING).order_by(AggregateRequest.id)))
    for request_id in ids:
        try:
            verify_pending(request_id)
        except Exception:
            logging.exception('pending aggregate verification failed: %s', request_id)
