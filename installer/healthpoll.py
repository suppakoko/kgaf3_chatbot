#!/usr/bin/env python3
"""healthpoll.py — wait for afmm_chat to become ready.

Stdlib only (urllib). Polls http://127.0.0.1:<APP_PORT>/health/ready until the
app reports ready or a timeout elapses. /health/ready returns ready when the
SQLite jobs DB is reachable.

Exit codes: 0 ready, 4 timed out / never ready.
"""

import argparse
import json
import os
import sys
import time
from urllib import request, error


def _poll_once(url, timeout=4):
    try:
        with request.urlopen(url, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", "replace")
            return status, body
    except error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, body
    except (error.URLError, OSError):
        return None, ""


def _looks_ready(status, body):
    if status != 200:
        return False
    text = (body or "").lower()
    # Accept JSON {"status":"ready"} or a plain "ready" body.
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            for key in ("status", "state", "ready"):
                v = data.get(key)
                if isinstance(v, str) and v.lower() in ("ready", "ok", "healthy"):
                    return True
                if v is True:
                    return True
    except (ValueError, TypeError):
        pass
    return "ready" in text or "ok" in text or "healthy" in text


def main(argv=None):
    ap = argparse.ArgumentParser(description="Poll afmm_chat /health/ready.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "5013")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--timeout", type=int, default=180, help="seconds (default 180)")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    url = "http://%s:%d/health/ready" % (args.host, args.port)
    deadline = time.time() + args.timeout
    attempt = 0
    if not args.quiet:
        print("Waiting for %s (timeout %ds)..." % (url, args.timeout))

    while time.time() < deadline:
        attempt += 1
        status, body = _poll_once(url)
        if _looks_ready(status, body):
            if not args.quiet:
                print("  [ OK ] app reports ready after %d attempt(s)." % attempt)
            return 0
        if not args.quiet and attempt % 5 == 0:
            shown = status if status is not None else "no-response"
            print("  ... still waiting (status=%s)" % shown)
        time.sleep(args.interval)

    if not args.quiet:
        print("  [FAIL] app did not become ready within %ds." % args.timeout)
        print("         Check logs: docker compose logs -f kgaf3-chat")
    return 4


if __name__ == "__main__":
    sys.exit(main())
