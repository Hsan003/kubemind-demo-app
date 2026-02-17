import os
import time
import json
import random
import logging
import threading
from typing import Optional

from fastapi import FastAPI, Response, Query
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

APP_NAME = os.getenv("APP_NAME", "demo-generator")

# ---------- Logging (structured-ish JSON) ----------
logger = logging.getLogger(APP_NAME)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(message)s"))
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logger.handlers = [handler]

def log(level: str, msg: str, **fields):
    record = {
        "app": APP_NAME,
        "level": level,
        "msg": msg,
        "ts": time.time(),
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False)
    getattr(logger, level.lower(), logger.info)(line)

# ---------- Prometheus metrics ----------
REQS = Counter("demo_requests_total", "Total requests", ["endpoint", "status"])
ERRORS = Counter("demo_errors_total", "Total errors", ["endpoint", "type"])
LAT = Histogram(
    "demo_request_latency_seconds",
    "Request latency",
    ["endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
CPU_BURN = Gauge("demo_cpu_burn_level", "CPU burn intensity (0-10)")
RANDOM_GAUGE = Gauge("demo_random_gauge", "Random gauge for demos")

app = FastAPI(title="K8s Observability Demo Generator", version="1.0.0")

@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/ok")
def ok(user: Optional[str] = None):
    endpoint = "/ok"
    start = time.time()
    try:
        log("info", "Request OK", endpoint=endpoint, user=user)
        REQS.labels(endpoint, "200").inc()
        return {"ok": True, "user": user}
    finally:
        LAT.labels(endpoint).observe(time.time() - start)

@app.get("/warn")
def warn():
    endpoint = "/warn"
    start = time.time()
    try:
        log("warning", "Simulated warning", endpoint=endpoint, code="DEMO_WARN")
        REQS.labels(endpoint, "200").inc()
        return {"warn": True}
    finally:
        LAT.labels(endpoint).observe(time.time() - start)

@app.get("/error")
def error(kind: str = Query("exception", enum=["exception", "http500", "timeout"])):
    endpoint = "/error"
    start = time.time()
    try:
        log("error", "Simulated error trigger", endpoint=endpoint, kind=kind)
        if kind == "exception":
            ERRORS.labels(endpoint, "exception").inc()
            REQS.labels(endpoint, "500").inc()
            # stack trace in logs:
            try:
                1 / 0
            except Exception as e:
                log("error", "Exception occurred", endpoint=endpoint, exc=str(e))
                raise
        elif kind == "timeout":
            # pretend a downstream timeout
            ERRORS.labels(endpoint, "timeout").inc()
            time.sleep(2.5)
            REQS.labels(endpoint, "504").inc()
            return Response(content="gateway timeout (simulated)", status_code=504)
        else:
            ERRORS.labels(endpoint, "http500").inc()
            REQS.labels(endpoint, "500").inc()
            return Response(content="internal error (simulated)", status_code=500)
    finally:
        LAT.labels(endpoint).observe(time.time() - start)

@app.get("/slow")
def slow(ms: int = 800):
    endpoint = "/slow"
    start = time.time()
    try:
        time.sleep(max(ms, 0) / 1000.0)
        log("info", "Slow request served", endpoint=endpoint, ms=ms)
        REQS.labels(endpoint, "200").inc()
        return {"slow": True, "ms": ms}
    finally:
        LAT.labels(endpoint).observe(time.time() - start)

@app.get("/spam-logs")
def spam_logs(lines: int = 200, level: str = "info"):
    endpoint = "/spam-logs"
    start = time.time()
    try:
        level = level.lower()
        for i in range(min(lines, 5000)):
            log(level, "Spam log line", endpoint=endpoint, i=i, batch=lines)
        REQS.labels(endpoint, "200").inc()
        return {"spammed": lines, "level": level}
    finally:
        LAT.labels(endpoint).observe(time.time() - start)

# --- CPU burn simulation (background thread) ---
burn_level = 0
burn_lock = threading.Lock()

def cpu_burner():
    global burn_level
    while True:
        with burn_lock:
            lvl = burn_level
        CPU_BURN.set(lvl)
        RANDOM_GAUGE.set(random.random())

        if lvl <= 0:
            time.sleep(0.2)
            continue

        # Busy loop for a fraction of time; higher lvl => more burn
        # (simple, good enough for demos)
        end = time.time() + (0.02 * lvl)
        while time.time() < end:
            pass
        time.sleep(0.05)

threading.Thread(target=cpu_burner, daemon=True).start()

@app.get("/burn")
def burn(level: int = Query(0, ge=0, le=10)):
    global burn_level
    with burn_lock:
        burn_level = level
    log("warning", "CPU burn updated", endpoint="/burn", level=level)
    return {"burn_level": burn_level}
