"""Poll /harness at the phone's rate and report status codes and latency.

Usage: uv run python scripts/load_harness_poll.py --url http://127.0.0.1:7080 \
          --token-file <path> --clients 4 --interval 0.25 --seconds 120
"""

from __future__ import annotations

import argparse
import math
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--clients", type=int, default=4)
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--seconds", type=float, default=120)
    args = ap.parse_args()
    token = open(args.token_file).read().strip()
    codes: Counter[int] = Counter()
    latencies: list[float] = []
    lock = threading.Lock()
    deadline = time.monotonic() + args.seconds

    def client() -> None:
        while time.monotonic() < deadline:
            req = urllib.request.Request(
                f"{args.url}/harness", headers={"Authorization": f"Bearer {token}"}
            )
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                    code = resp.status
            except urllib.error.HTTPError as exc:
                code = exc.code
            except Exception:  # noqa: BLE001
                code = 0
            with lock:
                codes[code] += 1
                latencies.append(time.monotonic() - started)
            time.sleep(args.interval)

    threads = [threading.Thread(target=client) for _ in range(args.clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    latencies.sort()
    p99 = (
        latencies[max(0, math.ceil(len(latencies) * 0.99) - 1)]
        if latencies
        else float("nan")
    )
    p50 = statistics.median(latencies) if latencies else float("nan")
    print(f"requests={sum(codes.values())} codes={dict(codes)}")
    print(f"p50={p50:.3f}s p99={p99:.3f}s")
    return 0 if set(codes) == {200} else 1


if __name__ == "__main__":
    raise SystemExit(main())
