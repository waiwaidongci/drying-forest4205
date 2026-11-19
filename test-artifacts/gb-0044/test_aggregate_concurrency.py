import os, sys, tempfile, threading
DB = tempfile.mktemp(suffix='.db')
os.environ['DATABASE_URL'] = 'sqlite:///' + DB
os.environ['SEED_DEMO'] = 'false'
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'backend'))

from sqlalchemy import select
from src.config.database import Session, Base, engine
from src.models.aggregate import AggregateRequest, DisclosureLock
from src.models.record import Record
from src.models.source import Source
from src.services.aggregates import verify_pending, PENDING, PUBLISHED, REJECTED

Base.metadata.create_all(engine)
with Session() as db:
    s = Source(key='S', name='S', filename='s.csv'); db.add(s); db.flush()
    for i in range(3):
        db.add(Record(source_id=s.id, reference=f'R{i+1}',
                      values={'moisture': 30, 'thickness': 4, 'temperature': 70,
                              'airflow': 3, 'minutes': 10 + i}))
    db.add(DisclosureLock(id=1, version=0))
    db.commit()
    ids = {r.reference: r.id for r in db.scalars(select(Record))}

with Session() as db:
    specs = [('A', ['R1', 'R2']), ('B', ['R2', 'R3']), ('C', ['R1', 'R3'])]
    pids = []
    for key, refs in specs:
        p = AggregateRequest(source_id=1, request_key=key, references=refs,
                             record_ids=[ids[r] for r in refs], total=999,
                             state=PENDING, requested_by='x', created_at='x')
        db.add(p); db.flush(); pids.append(p.id)
    db.commit()

errors = []
def worker(pid):
    try:
        verify_pending(pid)
    except Exception as e:
        errors.append(e)

threads = [threading.Thread(target=worker, args=(pid,)) for pid in pids]
for t in threads: t.start()
for t in threads: t.join()

with Session() as db:
    rows = db.scalars(select(AggregateRequest).where(AggregateRequest.id.in_(pids))).all()
    states = sorted((r.references[0] + r.references[-1], r.state) for r in rows)
print('errors:', errors)
print('states:', states)
published = [r for r in rows if r.state == PUBLISHED]
rejected = [r for r in rows if r.state == REJECTED]
# With serialization, the classic 3-way leak must not slip through: at most 2 may be
# published, and the pair {R1,R2}&{R1,R3} together with a third would reveal. Any
# pair of these three published is individually safe, so exactly 2 published + 1
# rejected is the expected outcome (the third would complete the rank-3 system).
assert not errors, errors
assert len(published) == 2 and len(rejected) == 1, states
print('CONCURRENCY TEST PASSED')
