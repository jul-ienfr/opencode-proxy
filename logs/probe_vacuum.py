"""Probe: _db_cleanup_old_bodies + _db_vacuum_if_needed on a temp DB.

Never touches the live logs/requests.db - the module-level _conn is
monkeypatched to a temp file for this process only.
"""
import os, sys, sqlite3, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import opencode as oc

def ts(days_ago, i=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400 + i))

def build(dbp):
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE requests (timestamp TEXT PRIMARY KEY, request_body TEXT, response_body TEXT)")
    conn.execute("CREATE TABLE free_model_usage (timestamp TEXT PRIMARY KEY, status TEXT)")
    body = "x" * 50000
    for i in range(50):  # 40 days old -> DELETE phase
        conn.execute("INSERT INTO requests VALUES (?,?,?)", (ts(40, i), body, body))
        conn.execute("INSERT INTO free_model_usage VALUES (?,?)", (ts(40, i), "ok"))
    for i in range(20):  # 10 days old -> NULLIFY phase
        conn.execute("INSERT INTO requests VALUES (?,?,?)", (ts(10, i), body, body))
    for i in range(5):  # 1 day old -> untouched
        conn.execute("INSERT INTO requests VALUES (?,?,?)", (ts(1, i), "small", "small"))
    conn.commit()
    return conn

# --- Test 1: delete + NULLIFY + VACUUM shrinks the file ---------------------
tmp = tempfile.mkdtemp()
dbp = os.path.join(tmp, "t1.db")
conn = build(dbp)
size_before = os.path.getsize(dbp)
old_conn, old_log, old_debug = oc._conn, oc._log, oc._debug
oc._conn, oc._log, oc._debug = conn, print, print
try:
    deleted = oc._db_cleanup_old_bodies(retention_days=7, delete_after_days=30)
finally:
    oc._conn, oc._log, oc._debug = old_conn, old_log, old_debug
size_after = os.path.getsize(dbp)
rows = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
assert deleted == 120, f"expected 50+50 deleted + 20 nullified = 120, got {deleted}"
assert rows == 25, f"expected 25 rows left, got {rows}"
assert size_after < size_before // 10, f"file did not shrink: {size_before} -> {size_after}"
print(f"PASS delete+NULLIFY+VACUUM: {size_before} -> {size_after} bytes, {rows} rows left")

# --- Test 2: no deletes -> no VACUUM, no error ------------------------------
dbp2 = os.path.join(tmp, "t2.db")
conn2 = sqlite3.connect(dbp2)
conn2.execute("CREATE TABLE requests (timestamp TEXT PRIMARY KEY, request_body TEXT, response_body TEXT)")
conn2.execute("CREATE TABLE free_model_usage (timestamp TEXT PRIMARY KEY, status TEXT)")
for i in range(5):  # all fresh (1 day old) -> nothing to delete/nullify
    conn2.execute("INSERT INTO requests VALUES (?,?,?)", (ts(1, i), "small", "small"))
conn2.commit()
size2 = os.path.getsize(dbp2)
old_conn, old_log, old_debug = oc._conn, oc._log, oc._debug
oc._conn, oc._log, oc._debug = conn2, print, print
try:
    deleted2 = oc._db_cleanup_old_bodies(retention_days=7, delete_after_days=30)
    assert deleted2 == 0, f"expected 0 (all rows fresh), got {deleted2}"
finally:
    oc._conn, oc._log, oc._debug = old_conn, old_log, old_debug
assert os.path.getsize(dbp2) == size2, "file changed with 0 deletes"
print("PASS no-deletes -> no VACUUM, file untouched")

# --- Test 3: open batched-insert transaction before VACUUM -> no error ------
dbp3 = os.path.join(tmp, "t3.db")
conn3 = build(dbp3)
conn3.execute("INSERT INTO requests VALUES (?,?,?)", (ts(0.1, i), "pending", "pending"))  # leave UNCOMMITTED
assert conn3.in_transaction
old_conn, old_log, old_debug = oc._conn, oc._log, oc._debug
oc._conn, oc._log, oc._debug = conn3, print, print
try:
    deleted3 = oc._db_cleanup_old_bodies(retention_days=7, delete_after_days=30)
    assert deleted3 == 120, f"expected 120, got {deleted3}"
finally:
    oc._conn, oc._log, oc._debug = old_conn, old_log, old_debug
assert not conn3.in_transaction, "transaction still open after VACUUM"
rows3 = conn3.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
assert rows3 == 26, f"expected 26 (25 + pending), got {rows3}"
print("PASS open-transaction guard: VACUUM committed pending batch first, no error")

print("ALL VACUUM PROBES PASSED")
