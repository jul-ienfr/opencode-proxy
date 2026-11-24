import sqlite3, time, datetime, subprocess, os

DB = 'logs/registry.db'
c = sqlite3.connect(DB)

def t(x):
    try:
        return datetime.datetime.fromtimestamp(float(x)).strftime('%H:%M:%S')
    except Exception:
        return str(x)

print('NOW:', datetime.datetime.now().strftime('%H:%M:%S'))
print()
print('=== station_state ===')
for r in c.execute('SELECT station, state, current_ip, client_usable, reason, last_200_at, updated_at FROM station_state ORDER BY station'):
    print('  S%s %-10s %-16s usable=%s %-16s last200=%s upd=%s' % (r[0], r[1], r[2], r[3], r[4], t(r[5]), t(r[6])))

cur = c.execute('SELECT COUNT(*) FROM station_state WHERE client_usable=1')
print()
print('client_usable count =', cur.fetchone()[0], '/ 6')

cur = c.execute('SELECT COUNT(*) FROM warm_pool WHERE expires_at > ?', (time.time(),))
print('warm_pool alive =', cur.fetchone()[0])

print()
print('=== blocked stations quota reset ===')
ips = []
for r in c.execute("SELECT station, current_ip FROM station_state WHERE client_usable=0"):
    ips.append(r[1])
    print('  S%s ip=%s' % (r[0], r[1]))
for ip in ips:
    for r in c.execute('SELECT reset_at, last_seen_at, last_status, used FROM quota_buckets WHERE ip=?', (ip,)).fetchall():
        print('     bucket reset=%s seen=%s status=%s used=%s' % (t(r[0]), t(r[1]), r[2], r[3]))

print()
print('=== watchdog ===')
for r in c.execute('SELECT heartbeat_at, last_run_at, status, error FROM watchdog_state').fetchall():
    print('  heartbeat=%s last_run=%s status=%s err=%s' % (t(r[0]), t(r[1]), r[2], r[3]))

print()
print('=== log tail ===')
try:
    out = subprocess.run(['powershell', '-NoProfile', '-Command', 'Get-Content logs\\fresh_chain_supervisor.log -Tail 8'],
                         capture_output=True, text=True, timeout=30)
    print(out.stdout)
except Exception as e:
    print('ERR', e)