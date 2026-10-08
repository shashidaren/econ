"""A/B timing of the cache-warming cycle against dead upstreams.

Runs the REAL warm_all() from app.py (registry -> prefetch -> fetchers ->
breakers) against a path where every upstream fails, which is the worst case
and the exact situation the LXC hit with Yahoo 429s + dead stooq.

    python3 tools/warm_ab.py <dashboard_dir>
"""

import os
import sys
import time

dashboard_dir = sys.argv[1] if len(sys.argv) > 1 else "dashboard"
sys.path.insert(0, os.path.abspath(dashboard_dir))
os.environ["ECON_CACHE_FILE"] = f"/tmp/econ-warm-{os.path.basename(os.path.abspath(dashboard_dir))}.json"
if os.path.exists(os.environ["ECON_CACHE_FILE"]):
    os.remove(os.environ["ECON_CACHE_FILE"])

import app  # noqa: E402

t0 = time.time()
app.warm_all(force=True)
dt = time.time() - t0

with_data = sum(1 for k in app.CACHE if (app.CACHE[k] or {}).get("data") is not None)
print(f"\nRESULT warm_all(): {dt:.1f}s for {len(app.REGISTRY)} registry keys "
      f"({with_data} with data, {len(app.CACHE) - with_data} failed)")
if hasattr(app.backend, "provider_status"):
    for p in app.backend.provider_status():
        print(f"  breaker {p['provider']}: {p['state']} retry_in={p['retry_in']}s "
              f"fails={p['fails']}")
