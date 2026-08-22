# Gunicorn 4 workers uvicorn — contourne GIL, 4x rps
import multiprocessing

bind = "0.0.0.0:4000"
workers = multiprocessing.cpu_count()  # 4 sur 4c, 8 sur 8c
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 2000
keepalive = 15  # aligne opencode
timeout = 600  # long streams
graceful_timeout = 5
max_requests = 10000
max_requests_jitter = 1000
preload_app = False  # chaque worker a son pool httpx + DB queue
accesslog = None  # opencode log déjà
errorlog = "-"
loglevel = "info"

# Env : REDIS_URL=redis://redis:6379/0 pour cache distribué
# Lancer : gunicorn -c gunicorn.conf.py opencode:app
# ou docker: gunicorn -c gunicorn.conf.py opencode:app --bind 0.0.0.0:4000
