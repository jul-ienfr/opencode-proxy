import sqlite3, time

c = sqlite3.connect("logs/registry.db")
now = time.time()
ips = [r[0] for r in c.execute("select current_ip from station_state")]
print("now UTC:", time.strftime("%H:%M:%SZ", time.gmtime(now)))
for ip in ips:
    rows = c.execute(
        "select model,used,last_status,reset_at,source from quota_buckets "
        "where ip=? order by last_seen_at desc limit 2", (ip,)
    ).fetchall()
    for m, used, st, ra, src in rows:
        d = (float(ra) - now) / 60 if ra else None
        print(f"  {ip:16s} {m[:22]:22s} used={used} status={st} src={src} reset_in={d:+.0f}m" if d is not None
              else f"  {ip:16s} {m[:22]:22s} used={used} status={st} src={src} reset_in=-")
