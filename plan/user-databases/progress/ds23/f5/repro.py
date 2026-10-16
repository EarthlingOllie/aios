"""Holds `per_proc` concurrent store_session("rw") sessions in each of `procs` processes,
against one provisioned store, and reports how many connect (role CONNECTION LIMIT default 10)."""
import multiprocessing as mp, sys, uuid, threading
from sqlalchemy import text

def worker(store_id, per_proc, barrier, out):
    from aios.datastores import store_session
    held, ok, err = [], 0, []
    for _ in range(per_proc):
        try:
            cm = store_session(uuid.UUID(store_id), "rw"); s = cm.__enter__()
            s.execute(text("SELECT 1")); held.append(cm); ok += 1
        except Exception as e:
            err.append(str(e).splitlines()[0][:160])
    barrier.wait()
    out.put((ok, err))
    barrier.wait()

if __name__ == "__main__":
    procs, per_proc = int(sys.argv[1]), int(sys.argv[2])
    from aios.db import SessionLocal
    from aios.models import User
    from aios.datastores import data_store_for, provision
    with SessionLocal() as db:
        u = User(github_id=-int(uuid.uuid4().int % 10**9), login=f"f5-{uuid.uuid4().hex[:6]}")
        db.add(u); db.commit(); uid = u.id
    sid = data_store_for(uid); provision.provision(sid)
    ctx = mp.get_context("spawn"); b = ctx.Barrier(procs); q = ctx.Queue()
    ps = [ctx.Process(target=worker, args=(str(sid), per_proc, b, q)) for _ in range(procs)]
    [p.start() for p in ps]; res = [q.get() for _ in ps]; [p.join() for p in ps]
    print(f"{procs} processes x {per_proc} sessions = {procs*per_proc} wanted;",
          f"connected {sum(r[0] for r in res)}; errors {sum(len(r[1]) for r in res)}")
    for r in res:
        for e in r[1][:1]: print("  ", e)
