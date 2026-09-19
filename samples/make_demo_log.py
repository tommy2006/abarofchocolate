"""Writes samples/demo_log.csv: a synthetic web-service event log (third domain for the adaptability check) and
samples/demo_log_truth.json with what was planted. Deterministic (seed 7)."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
rng = np.random.default_rng(7)
N = 20000
t0 = pd.Timestamp("2026-03-01 00:00:00")
ts = t0 + pd.to_timedelta(np.cumsum(rng.uniform(1.5, 7.5, N)), unit="s")
services = np.array(["api", "auth", "db", "cache", "worker"])
svc = rng.choice(services, N, p=[0.4, 0.15, 0.2, 0.15, 0.1])
base_lat = {"api": 120, "auth": 60, "db": 35, "cache": 4, "worker": 400}
lat = np.array([rng.lognormal(np.log(base_lat[s]), 0.35) for s in svc])
level = rng.choice(["INFO", "WARN", "ERROR"], N, p=[0.9, 0.07, 0.03])
status = np.where(level == "ERROR", rng.choice([500, 503], N), np.where(rng.random(N) < 0.04, 404, 200))
nbytes = rng.lognormal(7.5, 0.6, N).round()
user = rng.integers(1000, 1300, N)
truth = {"incidents": []}
# 1. database incident: errors and timeouts, slow db and api
a, b = 8000, 8600
m = (np.arange(N) >= a) & (np.arange(N) < b)
hit = m & (rng.random(N) < 0.3)
level[hit] = "ERROR"
status[hit] = 500
lat[m & np.isin(svc, ["db", "api"])] *= 4.0
truth["incidents"].append({"type": "process", "what": "database incident: 30 % errors, db and api 4x slower", "rows": [a, b - 1]})
# 2. logging bug: latency written as 0 and a block of repeated rows
a, b = 13000, 13300
lat[a:b] = 0.0
truth["incidents"].append({"type": "data", "what": "latency logged as 0 (logging bug)", "rows": [a, b - 1]})
# 3. slow drift: a memory leak makes the worker slower and slower
a, b = 16000, 17500
ramp = np.linspace(1.0, 3.0, b - a)
w = (svc[a:b] == "worker")
lat[a:b][w] *= ramp[w]
truth["incidents"].append({"type": "process", "what": "gradual drift: worker latency grows to 3x (memory leak)", "rows": [a, b - 1]})
tmpl = {"api": "GET /api/orders/{u} completed", "auth": "login for user {u}", "db": "query on table orders", "cache": "cache lookup for key k{u}", "worker": "job {u} processed"}
msg = []
for i in range(N):
    base = tmpl[svc[i]].format(u=user[i])
    if level[i] == "ERROR":
        base += " failed: " + ("query timeout after 30000 ms" if svc[i] in ("db", "api") else "upstream error")
    elif level[i] == "WARN":
        base += " (slow response)"
    msg.append(base)
df = pd.DataFrame({"timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "service": svc, "level": level, "status_code": status, "latency_ms": lat.round(1), "bytes": nbytes, "user_id": user, "message": msg})
dup = df.iloc[13100:13200]
df = pd.concat([df.iloc[:13200], dup, df.iloc[13200:]], ignore_index=True)
truth["incidents"].append({"type": "data", "what": "100 rows written twice (duplicated export)", "rows": [13200, 13299]})
df.to_csv(HERE / "demo_log.csv", index=False)
(HERE / "demo_log_truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
print("wrote", HERE / "demo_log.csv", len(df), "rows")
