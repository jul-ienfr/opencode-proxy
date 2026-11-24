import sqlite3, time, datetime

DB = 'logs/registry.db'
now = time.time()
c = sqlite3.connect(DB)
cur = c.cursor()

print('=== station_state ===')
cur.execute('SELECT station, state, current_ip, client_usable, last_probe_at, last_200_at, reason, updated_at FROM station_state ORDER BY station')
for r in cur.fetchall():
    print(r)

print()
print('=== egress_ips: state + reset_at (non-ok) ===')
cur.execute("SELECT ip, state, successes, failures, reset_at, last_429_at FROM egress_ips WHERE reset_at IS NOT NULL ORDER BY reset_at DESC LIMIT 15")
for r in cur.fetchall():
    rt = r[4]
    try:
        rtl = datetime.datetime.fromtimestamp(float(rt)).strftime('%H:%M') if rt else None
    except Exception:
        rtl = rt
    print(r[0], r[1], 'succ=%s fail=%s' % (r[2], r[3]), 'reset=', rtl, 'last429=', r[5])

print()
print('=== quota_buckets recent ===')
cur.execute("SELECT ip, model, used, last_status, reset_at, last_seen_at FROM quota_buckets ORDER BY last_seen_at DESC LIMIT 12")
for r in cur.fetchall():
    print(r)