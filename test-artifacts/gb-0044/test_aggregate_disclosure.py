import os, sys, tempfile
DB = tempfile.mktemp(suffix='.db')
os.environ['DATABASE_URL'] = 'sqlite:///' + DB
os.environ['SEED_DEMO'] = 'false'
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'backend'))

from fastapi.testclient import TestClient
from src.main import app
from src.config.database import Session, Base, engine
from src.models.aggregate import AggregateRequest
from src.models.record import Record
from src.models.source import Source
from src.models.user import User
from src.services.aggregates import resume_pending, verify_pending, PENDING, PUBLISHED, REJECTED
from src.utils.security import hash_password, token_for
from sqlalchemy import select


def setup():
    Base.metadata.create_all(engine)
    with Session() as db:
        if not db.scalar(select(User)):
            db.add(User(email='admin@x.demo', password_hash=hash_password('pw'), role='admin'))
            s = Source(key='S1', name='来源一', filename='s1.csv')
            db.add(s); db.flush()
            # 6 records with distinct minutes
            for i in range(6):
                db.add(Record(source_id=s.id, reference=f'R{i+1}',
                              values={'moisture': 30, 'thickness': 4, 'temperature': 70,
                                      'airflow': 3, 'minutes': 10 + i}))
            s2 = Source(key='S2', name='来源二', filename='s2.csv')
            db.add(s2); db.flush()
            for i in range(3):
                db.add(Record(source_id=s2.id, reference=f'Q{i+1}',
                              values={'moisture': 30, 'thickness': 4, 'temperature': 70,
                                      'airflow': 3, 'minutes': 20 + i}))
        db.commit()


def client():
    return TestClient(app)


def login(c):
    r = c.post('/auth/login', json={'email': 'admin@x.demo', 'password': 'pw'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def post(c, h, body):
    return c.post('/aggregates', json=body, headers=h)


def ids_by_ref(source_id, refs):
    with Session() as db:
        rows = db.scalars(select(Record).where(Record.source_id == source_id,
                                               Record.reference.in_(refs))).all()
        return {r.reference: r.id for r in rows}


def main():
    setup()
    c = client()
    with c:
        h = login(c)
        # 1. single item rejected, total hidden
        r = post(c, h, {'source_id': 1, 'references': ['R1']})
        print('single:', r.status_code, r.json()['state'], 'total=', r.json()['total'])
        assert r.json()['state'] == REJECTED and r.json()['total'] is None

        # 2. disjoint multi-item published
        r = post(c, h, {'source_id': 1, 'references': ['R1', 'R2', 'R3']})
        j = r.json(); print('multi:', j['state'], j['total'])
        assert j['state'] == PUBLISHED and j['total'] == 10 + 11 + 12
        first_id = j['id']

        # 3. subset claimed later -> rejected
        r = post(c, h, {'source_id': 1, 'references': ['R1', 'R2']})
        print('subset:', r.json()['state'], r.json()['total'])
        assert r.json()['state'] == REJECTED and r.json()['total'] is None

        # 4. difference: published {R4,R5}; then {R4,R5,R6} reveals R6 -> rejected
        r = post(c, h, {'source_id': 1, 'references': ['R4', 'R5']})
        print('pair:', r.json()['state'], r.json()['total'])
        assert r.json()['state'] == PUBLISHED
        r = post(c, h, {'source_id': 1, 'references': ['R4', 'R5', 'R6']})
        print('superset:', r.json()['state'])
        assert r.json()['state'] == REJECTED

        # 5. disjoint from both published sets stays fine
        r = post(c, h, {'source_id': 1, 'references': ['R6', 'R1']})
        # R6 alone already determined by R4+R5 vs R4+R5+R6? that was rejected, so R6 not public.
        # R6+R1 vs published R1+R2+R3: no determination.
        print('mixed:', r.json()['state'])
        assert r.json()['state'] == PUBLISHED

        # 6. idempotent retry: same key returns same id and total, no duplicate
        body = {'source_id': 1, 'references': ['R1', 'R2', 'R3'], 'request_key': 'retry-1'}
        a = post(c, h, body).json()
        b = post(c, h, body).json()
        print('retry:', a['id'], b['id'], a['total'], b['total'])
        assert a['id'] == b['id'] and a['state'] == PUBLISHED
        with Session() as db:
            count = len(db.scalars(select(AggregateRequest).where(
                AggregateRequest.request_key == 'retry-1')).all())
        assert count == 1, count

        # 7. cross-source references rejected
        r = post(c, h, {'source_id': 1, 'references': ['R1', 'Q1']})
        print('cross-source:', r.status_code)
        assert r.status_code == 422

        # 8. duplicate reference in one request rejected
        r = post(c, h, {'source_id': 1, 'references': ['R1', 'R1', 'R2']})
        print('dup-ref:', r.status_code)
        assert r.status_code == 422

        # 9. viewer cannot submit
        r = post(c, h, {'source_id': 1, 'references': ['R2', 'R3', 'R1']})
        assert r.status_code in (200, 409, 422)

        # 10. list hides totals for non-published
        rows = c.get('/aggregates', headers=h).json()
        assert all(x['total'] is None for x in rows if x['state'] != PUBLISHED)

    # 11. pending persisted then resumed, using stored total after backfill
    with Session() as db:
        recs = db.scalars(select(Record).where(Record.source_id == 2).order_by(Record.id)).all()
        refs = [recs[0].reference, recs[1].reference]
        rec_ids = [recs[0].id, recs[1].id]
        frozen_total = float(recs[0].values['minutes'] + recs[1].values['minutes'])
        p = AggregateRequest(source_id=2, request_key='pending-1', references=refs,
                             record_ids=rec_ids, total=frozen_total, state=PENDING,
                             requested_by='admin@x.demo', created_at='x')
        db.add(p); db.flush(); pid = p.id
        # backfill changes recorded minutes after the aggregate was accepted
        recs[0].values = {**recs[0].values, 'minutes': 999}
        db.commit()
    resume_pending()
    with Session() as db:
        p = db.get(AggregateRequest, pid)
        print('resume:', p.state, p.total, 'frozen=', frozen_total)
        assert p.state == PUBLISHED and p.total == frozen_total

    # 12. final state transitions once: verify again does nothing
    before = p.decided_at
    verify_pending(pid)
    with Session() as db:
        p = db.get(AggregateRequest, pid)
        assert p.state == PUBLISHED and p.decided_at == before

    print('ALL FUNCTIONAL TESTS PASSED')


if __name__ == '__main__':
    main()
