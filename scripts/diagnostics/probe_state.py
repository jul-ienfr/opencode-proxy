import sqlite3, time, json

DB = 'logs/registry.db'
now = time.time()
c = sqlite3.connect(DB)
cur = c.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print('TABLES:', tables)
print()

for t in tables:
    cur.execute('PRAGMA table_info(%s)' % t)
    cols = [r[1] for r in cur.fetchall()]
    print(t, '->', cols)

print()
cur.execute('SELECT COUNT(*) FROM warm_pool WHERE expires_at > ?', (now,))
print('warm_pool alive:', cur.fetchone()[0])
cur.execute('SELECT expires_at FROM warm_pool ORDER BY expires_at')
res = [r[0] for r in cur.fetchall()]
import datetime
print('expiries (local):', [datetime.datetime.fromtimestamp(x).strftime('%H:%M') for x in res])