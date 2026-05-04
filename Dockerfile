FROM python:3.11-slim

WORKDIR /app

# System deps for httpx/uvicorn
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx rich tiktoken

COPY . .

RUN mkdir -p logs

EXPOSE 4000 8082

CMD ["python", "opencode.py"]
