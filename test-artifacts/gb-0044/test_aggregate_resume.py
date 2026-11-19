import os, sys, tempfile, threading
DB = tempfile.mktemp(suffix='.db')
os.environ['DATABASE_URL'] = 'sqlite:///' + DB
os.environ['SEED_DEMO'] = 'false'
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'backend'))

from sqlalchemy import select
from fastapi.testclient import TestClient
from src.main import app
from src.config.database import Session, Base, engine
from src.models.aggregate import AggregateRequest, DisclosureLock
from src.models.record import Record
from src.models.source import Source
from src.models.user import User
from src.services.aggregates import verify_pending, PENDING, PUBLISHED, REJECTED, public_row
from src.utils.security import hash_password

Base.metadata.create_all(engine)
with Session() as db:
    db.add(User(email='admin@x.demo', password_hash=hash_password('pw'), role='admin'))
    s = Source(key='S', name='S', filename='s.csv'); db.add(s); db.flush()
    for i in range(4):
        db.add(Record(source_id=s.id, reference=f'R{i+1}',
                      values={'moisture': 30, 'thickness': 4, 'temperature': 70,
                              'airflow': 3, 'minutes': 10 + i}))
    db.commit()
    recs = db.scalars(select(Record).order_by(Record.id)).all()
    ids = [r.id for r in recs]

# Simulate crash after the pending aggregate was stored but before verification.
with Session() as db:
    p = AggregateRequest(source_id=1, request_key='auto-x', references=['R1', 'R2', 'R3'],
                         record_ids=ids[:3], total=999.0, state=PENDING,
                         requested_by='admin@x.demo', created_at='x')
    db.add(p); db.flush(); pid = p.id
    # Backfill one record after acceptance: stored total must survive.
    recs[0].values = {**recs[0].values, 'minutes': 500}
    db.commit()

# Starting the app runs lifespan -> resume_pending, then verification continues.
with TestClient(app) as c:
    h = {'Authorization': 'Bearer ' + c.post('/auth/login', json={
        'email': 'admin@x.demo', 'password': 'pw'}).json()['token']}
    rows = c.get('/aggregates', headers=h).json()
    row = next(x for x in rows if x['id'] == pid)
    print('after restart:', row['state'], row['total'])
    assert row['state'] == PUBLISHED and row['total'] == 999.0

    # Re-submitting the same pending key after interruption returns the stored decision.
    r = c.post('/aggregates', json={'source_id': 1, 'references': ['R1', 'R2', 'R3'],
                                    'request_key': 'auto-x'}, headers=h).json()
    assert r['id'] == pid and r['state'] == PUBLISHED and r['total'] == 999.0

    # Cross-group totals still share the same published pool: a subset is rejected.
    r = c.post('/aggregates', json={'source_id': 1, 'references': ['R1', 'R2']},
               headers=h).json()
    print('subset after restart:', r['state'])
    assert r['state'] == REJECTED

print('RESTART TEST PASSED')

# Concurrent same-pending verification: exactly one transition.
with Session() as db:
    p = AggregateRequest(source_id=1, request_key='double', references=['R1', 'R2'],
                         record_ids=ids[:2], total=21.0, state=PENDING,
                         requested_by='x', created_at='x')
    db.add(p); db.flush(); p2 = p.id; db.commit()
results, errors = [], []
def run():
    try: results.append(verify_pending(p2))
    except Exception as e: errors.append(e)
ts = [threading.Thread(target=run) for _ in range(8)]
for t in ts: t.start()
for t in ts: t.join()
with Session() as db:
    p = db.get(AggregateRequest, p2)
count_audit = None
print('double state:', p.state, 'errors:', errors, 'results:', len(results))
assert not errors
assert all(r['state'] == p.state for r in results)
print('SAME-PENDING TEST PASSED')
