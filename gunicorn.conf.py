# Gunicorn + uvicorn worker — SINGLE-WORKER OBLIGATOIRE
# [plan v10 §14.0.6] Le process est 100% étatful en mémoire : piles VPN par
# station (managers/watchdogs), ring buffers traffic_capture, EventManager SSE,
# caches quotas, shared_state VPN, connexion SQLite partagée. Multi-workers =
# ops dashboard appliquées à 1/N, événements SSE perdus, courses read-modify-
# write sur .env/shared_rotation.json. Ne PAS remonter `workers` sans avoir
# déporté cet état vers un store partagé (cf. plan §14 audit).
bind = "0.0.0.0:4000"
workers = 1
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 2000
keepalive = 15  # aligne opencode
timeout = 600  # long streams
graceful_timeout = 5
max_requests = 10000
max_requests_jitter = 1000
preload_app = False  # pool httpx + DB queue du process lui-même
accesslog = None  # opencode log déjà
errorlog = "-"
loglevel = "info"

# Lancer : gunicorn -c gunicorn.conf.py opencode:app   (Linux uniquement —
# sous Windows lancer `python opencode.py` directement, gunicorn non supporté).
