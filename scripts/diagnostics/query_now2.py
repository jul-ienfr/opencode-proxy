import sqlite3, datetime

c = sqlite3.connect('logs/registry.db')
c.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc).timestamp()

print("== STATION STATE ==")
rows = c.execute("select station,state,current_ip,current_host,client_usable,last_probe_at,last_200_at,reason,updated_at,transport_effective,country from station_state order by station").fetchall()
for r in rows:
    d = dict(r)
    lp = d.get('last_probe_at'); l2 = d.get('last_200_at'); up = d.get('updated_at')
    def fmt(x):
        try:
            x = float(x)
            return datetime.datetime.fromtimestamp(x, datetime.timezone.utc).strftime('%H:%M:%S')
        except Exception:
            return str(x)
    print(f"S{d['station']} {d['state']:<10} usable={d['client_usable']} ip={d['current_ip']} probe={fmt(lp)} last200={fmt(l2)} upd={fmt(up)} reason={str(d['reason'])[:90]}")

n = c.execute("select count(*) from warm_pool where expires_at > ?", (now,)).fetchone()[0]
tot = c.execute("select count(*) from warm_pool").fetchone()[0]
print(f"\nwarm_pool alive={n} total={tot}")

print("\n== recent alive (5) ==")
for r in c.execute("select ip,hostname,verified_at,expires_at from warm_pool where expires_at > ? order by verified_at desc limit 5", (now,)):
    d = dict(r)
    print(f"{d['ip']:<18} {d['hostname']:<32} ver={datetime.datetime.fromtimestamp(float(d['verified_at']),datetime.timezone.utc).strftime('%m-%d %H:%M')} exp={datetime.datetime.fromtimestamp(float(d['expires_at']),datetime.timezone.utc).strftime('%m-%d %H:%M')}")

print("\n== open incidents ==")
for r in c.execute("select * from incidents order by rowid desc limit 5"):
    d = dict(r)
    print({k: (str(v)[:60]) for k, v in list(d.items())[:6]})
print("open incidents:", c.execute("select count(*) from incidents where resolved_at is null").fetchone()[0] if 'resolved_at' in [x[1] for x in c.execute('pragma table_info(incidents)')] else 'n/a')
