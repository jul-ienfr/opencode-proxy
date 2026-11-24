import sqlite3, datetime, json

c = sqlite3.connect('logs/registry.db')
c.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
print("NOW_UTC:", now.isoformat())

tables = [r[0] for r in c.execute("select name from sqlite_master where type='table' order by name")]
print("TABLES:", tables)

def cols(t):
    try:
        return [r[1] for r in c.execute("pragma table_info(%s)" % t)]
    except Exception as e:
        return ["ERR:" + str(e)]

for t in ("warm_pool", "stations", "station_state", "clients", "quota_ledger"):
    if t in tables:
        print("\n== %s ==" % t, cols(t))
        try:
            n = c.execute("select count(*) from %s" % t).fetchone()[0]
            print("count:", n)
        except Exception as e:
            print("count err", e)

if "warm_pool" in tables:
    print("\n-- warm_pool sample --")
    for r in c.execute("select * from warm_pool order by rowid desc limit 3"):
        print(dict(r))
