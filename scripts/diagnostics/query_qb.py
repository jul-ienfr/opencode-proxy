import sqlite3, datetime

c = sqlite3.connect('logs/registry.db')
c.row_factory = sqlite3.Row
print("quota_buckets cols:", [r[1] for r in c.execute('pragma table_info(quota_buckets)')])
rows = c.execute("select * from quota_buckets order by rowid desc limit 12").fetchall()
for r in rows:
    d = dict(r)
    out = {}
    for k, v in d.items():
        if isinstance(v, float) and v > 1.7e9:
            out[k] = datetime.datetime.fromtimestamp(v, datetime.timezone.utc).strftime('%H:%M:%S')
        else:
            out[k] = v
    print(out)
