#!/usr/bin/env python3
"""verify_af3.py — actively verify the external AlphaFold3 MCP connection.

Stdlib only (urllib). Implements the 04 §4.4 install-time verification:
  1. POST MCP `initialize` to AF3_MCP_URL (Streamable-HTTP / JSON-RPC).
  2. POST `tools/list`.
  3. Confirm HTTP 200 and the presence of the Stage-3 batch tools.
  4. Check AF3_OUTPUT_ROOT is readable.

Handles both plain JSON and SSE (text/event-stream) responses.

Reads AF3_MCP_URL / AF3_MCP_AUTH_TOKEN / AF3_OUTPUT_ROOT from the environment,
or from a .env.docker file via --env-file.

Exit codes: 0 connection OK, 3 AF3 unreachable/degraded, 2 bad input.
A non-zero exit marks the install "degraded" but does not delete the stack.
"""

import argparse
import json
import os
import sys
import uuid
from urllib import request, error

REQUIRED_BATCH_TOOLS = (
    "af3_create_batch_job",
    "af3_get_batch_status",
    "af3_get_batch_results",
    "af3_get_batch_ligand_result",
)

BRIDGE_DOC = "docs/BRIDGE.md"


def _load_env_file(path, env):
    if not path or not os.path.isfile(path):
        return env
    for raw in open(path, "r", encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env.setdefault(k.strip(), v.strip())
    return env


def _post_jsonrpc(url, payload, token, timeout=15):
    """POST a JSON-RPC message to a Streamable-HTTP MCP endpoint.

    Returns (status_code, parsed_result_or_None, raw_text).
    Accepts both application/json and text/event-stream (SSE) responses.
    """
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        # Streamable-HTTP servers require the client to accept both.
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", "replace")
            sid = resp.headers.get("Mcp-Session-Id")
            return status, _parse_body(body), body, sid
    except error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, _parse_body(body), body, None


def _parse_body(body):
    """Parse a JSON-RPC response that may be plain JSON or SSE framed."""
    body = body.strip()
    if not body:
        return None
    # SSE: lines like "event: message" / "data: {...}". Concatenate data lines.
    if body.startswith("event:") or "\ndata:" in body or body.startswith("data:"):
        chunks = []
        for line in body.splitlines():
            if line.startswith("data:"):
                chunks.append(line[len("data:"):].strip())
        joined = "".join(chunks) if chunks else body
        try:
            return json.loads(joined)
        except ValueError:
            return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _remediation(url):
    return (
        "\n  How to fix the AF3 connection:\n"
        "    1. Make sure the AF3 MCP server is running on the AF3 host\n"
        "       (e.g. `systemctl status af3-mcp.service` or start it manually).\n"
        "    2. Confirm AF3_MCP_URL in configure.md is correct and reachable\n"
        "       from this machine: %s\n"
        "    3. KNOWN ISSUE (B3): the AF3 MCP may bind to 127.0.0.1 only, so a\n"
        "       container or remote host cannot reach it. Apply the host-binding\n"
        "       patch or use an SSH tunnel:\n"
        "         ssh -L 8002:127.0.0.1:8002 user@af3-host\n"
        "       then set AF3_MCP_URL to the tunnelled address.\n"
        "    4. If AF3 requires auth, set AF3_MCP_AUTH_TOKEN in configure.md.\n"
        "    See %s for the full bridge setup.\n" % (url, BRIDGE_DOC)
    )


def verify(url, token, output_root, quiet=False):
    def say(m):
        if not quiet:
            print(m)

    ok = True

    # --- 1. initialize ---
    init_payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "afmm-installer-verify", "version": "1.0"},
        },
    }
    try:
        status, parsed, raw, sid = _post_jsonrpc(url, init_payload, token)
    except (error.URLError, OSError) as e:
        say("  [FAIL] AF3 MCP unreachable at %s (%s)" % (url, e))
        sys.stderr.write(_remediation(url))
        return False

    if status != 200:
        say("  [FAIL] AF3 MCP initialize returned HTTP %s" % status)
        if raw:
            sys.stderr.write("         response: %s\n" % raw[:300])
        sys.stderr.write(_remediation(url))
        return False
    if not parsed or "result" not in parsed:
        say("  [FAIL] AF3 MCP initialize returned no JSON-RPC result.")
        sys.stderr.write("         response: %s\n" % (raw[:300] if raw else "(empty)"))
        sys.stderr.write(_remediation(url))
        return False
    say("  [ OK ] AF3 MCP initialize -> HTTP 200")

    # --- 2. tools/list (carry session id if the server issued one) ---
    list_payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/list",
        "params": {},
    }
    headers_token = token
    # Re-POST; include session header by temporarily wrapping if provided.
    try:
        if sid:
            data = json.dumps(list_payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": sid,
            }
            if headers_token:
                headers["Authorization"] = "Bearer %s" % headers_token
            req = request.Request(url, data=data, headers=headers, method="POST")
            with request.urlopen(req, timeout=15) as resp:
                status = resp.getcode()
                raw = resp.read().decode("utf-8", "replace")
                parsed = _parse_body(raw)
        else:
            status, parsed, raw, _ = _post_jsonrpc(url, list_payload, token)
    except (error.URLError, OSError) as e:
        say("  [FAIL] tools/list request failed (%s)" % e)
        sys.stderr.write(_remediation(url))
        return False

    if status != 200 or not parsed or "result" not in parsed:
        say("  [FAIL] tools/list did not return a tool list (HTTP %s)." % status)
        sys.stderr.write(_remediation(url))
        return False

    tools = parsed["result"].get("tools", [])
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    say("  [ OK ] tools/list -> %d tools advertised" % len(names))

    missing = [t for t in REQUIRED_BATCH_TOOLS if t not in names]
    if missing:
        ok = False
        say("  [FAIL] AF3 MCP is missing required batch tools: %s" % ", ".join(missing))
        sys.stderr.write(
            "         This AF3 MCP does not expose the Stage-3 batch API afmm_chat\n"
            "         needs. Confirm it is the af3_chatbot HTTP MCP (af3_mcp_http).\n"
            "         See %s.\n" % BRIDGE_DOC
        )
    else:
        say("  [ OK ] all batch tools present (%s)" % ", ".join(REQUIRED_BATCH_TOOLS))

    # --- 3. AF3_OUTPUT_ROOT readable (host-side check) ---
    if output_root:
        if not os.path.isdir(output_root):
            ok = False
            say("  [WARN] AF3_OUTPUT_ROOT not found on this host: %s" % output_root)
            sys.stderr.write(
                "         Stage-4 interface-PAE parsing reads AF3 output from disk.\n"
                "         Ensure this path exists and is mounted read-only.\n"
            )
        elif not os.access(output_root, os.R_OK):
            ok = False
            say("  [WARN] AF3_OUTPUT_ROOT exists but is not readable: %s" % output_root)
        else:
            say("  [ OK ] AF3_OUTPUT_ROOT readable: %s" % output_root)

    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify external AF3 MCP connection.")
    ap.add_argument("--url", default=os.environ.get("AF3_MCP_URL"))
    ap.add_argument("--token", default=os.environ.get("AF3_MCP_AUTH_TOKEN", ""))
    ap.add_argument("--output-root", default=os.environ.get("AF3_OUTPUT_ROOT"))
    ap.add_argument("--env-file", default=None,
                    help="Read AF3_* values from a .env.docker file if not in env.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    env = {}
    if args.env_file:
        _load_env_file(args.env_file, env)
    url = args.url or env.get("AF3_MCP_URL")
    token = args.token or env.get("AF3_MCP_AUTH_TOKEN", "")
    output_root = args.output_root or env.get("AF3_OUTPUT_ROOT")

    if not url:
        sys.stderr.write("ERROR: AF3_MCP_URL not provided (env, --url, or --env-file).\n")
        return 2

    if not args.quiet:
        print("Verifying external AF3 MCP at %s ..." % url)
    ok = verify(url, token, output_root, quiet=args.quiet)
    if ok:
        if not args.quiet:
            print("AF3 connection verified.")
        return 0
    if not args.quiet:
        print("AF3 connection NOT fully verified — install continues in DEGRADED mode.")
    return 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
