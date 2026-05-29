import urllib.request, json

# Check history API on port 4000
r = urllib.request.urlopen('http://localhost:4000/api/history?limit=3', timeout=5)
h = json.loads(r.read())
print('History API (port 4000):')
for log in h['logs']:
    print(f"  {log['timestamp']} | {log['model']} | ok={log['success']}")
print(f"Total: {h['total']}")

# Check dashboard HTML on port 4000
r2 = urllib.request.urlopen('http://localhost:4000/', timeout=5)
html = r2.read().decode()
print(f'\nDashboard (port 4000): {len(html)} bytes')
print(f'  contient refreshAll: {"refreshAll" in html}')
print(f'  contient apiFetch: {"apiFetch" in html}')

# Check app.js on port 4000
r3 = urllib.request.urlopen('http://localhost:4000/static/app.js', timeout=5)
js = r3.read().decode()
print(f'app.js (port 4000): {len(js)} bytes')
print(f'  contient escHtml: {"escHtml" in js}')
print(f'  contient startPolling: {"startPolling" in js}')
print(f'  contient connectSSE: {"connectSSE" in js}')

# Try port 8082
try:
    r4 = urllib.request.urlopen('http://localhost:8082/api/history?limit=1', timeout=3)
    h4 = json.loads(r4.read())
    print(f'\nHistory API (port 8082): {h4["total"]} entries')
except Exception as e:
    print(f'\nPort 8082: {e}')
