#!/usr/bin/env python3
"""configure_parser.py — parse configure.md and emit .env.docker.

Stdlib only. Runs on the host (before containers) under python3.

Reads the FIRST ```ini fenced block of configure.md as the single source of
truth, validates required keys, maps configure.md keys to real afmm_chat env
names, auto-injects the fixed Lite-profile values, and writes a 0600
.env.docker (backing up any existing one).

Usage:
    python3 configure_parser.py [--configure PATH] [--out PATH] [--quiet]

Exit codes: 0 ok, 2 validation error, 1 unexpected error.
"""

import argparse
import os
import re
import sys
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.dirname(HERE)

# ---- key definitions --------------------------------------------------------

REQUIRED_KEYS = ("OPENROUTER_API_KEY", "AF3_MCP_URL", "AF3_OUTPUT_ROOT")

# configure.md key -> .env.docker key (1:1 names except PROFILE which is consumed
# locally for compose selection and not written verbatim).
DIRECT_MAP = {
    "OPENROUTER_API_KEY": "OPENROUTER_API_KEY",
    "AF3_MCP_URL": "AF3_MCP_URL",
    "AF3_OUTPUT_ROOT": "AF3_OUTPUT_ROOT",
    "APP_PORT": "APP_PORT",
    "LLM_DEFAULT_MODEL": "LLM_DEFAULT_MODEL",
    "AF3_MCP_AUTH_TOKEN": "AF3_MCP_AUTH_TOKEN",
    "ENABLE_NGINX": "ENABLE_NGINX",
    "BASIC_AUTH_USER": "BASIC_AUTH_USER",
    "BASIC_AUTH_PASS": "BASIC_AUTH_PASS",
    # GraphRAG (optional) — connects to the bundled graphrag-mcp SSE server.
    "GRAPHRAG_ENABLED": "GRAPHRAG_ENABLED",
    "GRAPHRAG_MCP_AUTH_TOKEN": "GRAPHRAG_MCP_AUTH_TOKEN",
    "GRAPHRAG_OPENROUTER_MODEL": "GRAPHRAG_OPENROUTER_MODEL",
}

# Recognized configure.md keys (DIRECT_MAP + locally-consumed). Anything else -> warn.
KNOWN_KEYS = set(DIRECT_MAP) | {"PROFILE"}

DEFAULTS = {
    "PROFILE": "lite",
    "APP_PORT": "5013",
    "LLM_DEFAULT_MODEL": "anthropic/claude-sonnet-4-6",
    "ENABLE_NGINX": "false",
    # GraphRAG is ON by default in kgaf3_chatbot (KG is a first-class chat mode).
    "GRAPHRAG_ENABLED": "true",
    "GRAPHRAG_OPENROUTER_MODEL": "anthropic/claude-opus-4-7",
}

# Fixed values auto-injected for the bundled Lite stack — users never set these.
FIXED_ENV = {
    "OPENMM_MCP_URL": "http://smina-mcp:8001/mcp/",
    "USE_OPENMM": "false",
    "OPENMM_WORK_ROOT": "/data/work",
    "AFMM_DB_PATH": "/data/afmm.db",
    "AFMM_LIBRARY_DIR": "/data/uploads",
    # GraphRAG MCP endpoint is the in-network compose service (graphrag profile).
    "GRAPHRAG_MCP_URL": "http://graphrag-mcp:8893/sse",
    "APP_HOST": "0.0.0.0",
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "SMINA_SCORING": "vinardo",
}

VALID_PROFILES = {"lite", "full"}


def _err(msg):
    sys.stderr.write("ERROR: %s\n" % msg)


def _warn(msg):
    sys.stderr.write("WARN:  %s\n" % msg)


def extract_ini_block(text):
    """Return the contents of the FIRST ```ini (or ```env) fenced block."""
    pattern = re.compile(r"```(?:ini|env)[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1)


def parse_block(block):
    """Parse 'key = value' lines. Strip inline '#' comments, trim, skip blanks."""
    out = {}
    for raw in block.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            _warn("ignoring malformed line (no '='): %r" % raw.strip())
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        out[key] = val
    return out


def validate(cfg):
    """Apply defaults, validate, return (env_dict, profile). Exits on hard error."""
    errors = []

    # Warn on unknown keys (pass through otherwise ignored).
    for k in cfg:
        if k not in KNOWN_KEYS:
            _warn("unknown key %r in configure.md — ignored" % k)

    # Fill defaults for absent optional keys.
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if v != ""})
    # keep explicit empty strings for optional token/auth keys
    for k, v in cfg.items():
        if v == "" and k not in merged:
            merged[k] = ""

    # Required keys must be present and non-empty / non-placeholder.
    for k in REQUIRED_KEYS:
        v = merged.get(k, "").strip()
        if not v:
            errors.append("required key %s is missing or empty" % k)
        elif k == "OPENROUTER_API_KEY" and v in ("sk-or-REPLACE_ME", "sk-or-..."):
            errors.append(
                "OPENROUTER_API_KEY is still the placeholder — set a real key "
                "(get one at https://openrouter.ai/keys)"
            )

    # PROFILE
    profile = merged.get("PROFILE", "lite").strip().lower()
    if profile not in VALID_PROFILES:
        errors.append("PROFILE must be one of %s (got %r)" % (sorted(VALID_PROFILES), profile))
    elif profile == "full":
        _warn("PROFILE=full is reserved and not functional yet; proceeding as 'lite'.")
        profile = "lite"

    # AF3_MCP_URL format
    url = merged.get("AF3_MCP_URL", "").strip()
    if url:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            errors.append("AF3_MCP_URL must be an http(s) URL like "
                          "http://host.docker.internal:8002/mcp/ (got %r)" % url)
        elif not p.path.rstrip("/").endswith("/mcp") and not p.path.rstrip("/").endswith("mcp"):
            _warn("AF3_MCP_URL path does not end in /mcp/ — confirm this is the MCP endpoint: %r" % url)

    # AF3_OUTPUT_ROOT must be absolute + exist
    out_root = merged.get("AF3_OUTPUT_ROOT", "").strip()
    if out_root:
        if not os.path.isabs(out_root):
            errors.append("AF3_OUTPUT_ROOT must be an absolute host path (got %r)" % out_root)
        elif not os.path.isdir(out_root):
            errors.append("AF3_OUTPUT_ROOT does not exist or is not a directory: %s "
                          "(create it or point at your AF3 output dir)" % out_root)

    # APP_PORT numeric
    port = merged.get("APP_PORT", "5013").strip()
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        errors.append("APP_PORT must be a number 1-65535 (got %r)" % port)

    # nginx basic auth coherence
    if str(merged.get("ENABLE_NGINX", "false")).strip().lower() in ("true", "1", "yes"):
        if not merged.get("BASIC_AUTH_USER", "").strip() or not merged.get("BASIC_AUTH_PASS", "").strip():
            _warn("ENABLE_NGINX=true but BASIC_AUTH_USER/BASIC_AUTH_PASS empty — "
                  "the proxy will have no credentials configured.")

    if errors:
        _err("configure.md validation failed:")
        for e in errors:
            sys.stderr.write("       - %s\n" % e)
        sys.exit(2)

    return merged, profile


def build_env(merged):
    """Map configure keys to env names + inject fixed values."""
    env = {}
    # Auto-injected fixed values first (so explicit user keys can't clobber them
    # for the truly-fixed ones — but FIXED_ENV here are all fixed Lite values).
    env.update(FIXED_ENV)
    # Direct user-provided / defaulted mappings.
    for cfg_key, env_key in DIRECT_MAP.items():
        if cfg_key in merged:
            env[env_key] = merged[cfg_key]
    return env


def write_env(env, out_path, quiet=False):
    # Back up existing file (idempotent re-runs).
    if os.path.exists(out_path):
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = "%s.bak.%s" % (out_path, ts)
        # Copy the existing file to a timestamped backup, then overwrite below.
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                old = f.read()
            with open(backup, "w", encoding="utf-8") as f:
                f.write(old)
            if not quiet:
                print("Backed up existing .env.docker -> %s" % os.path.basename(backup))
        except OSError as e:
            _warn("could not back up existing .env.docker: %s" % e)

    lines = [
        "# Generated by installer/configure_parser.py from configure.md.",
        "# DO NOT EDIT BY HAND — edit configure.md and re-run the installer.",
        "# Container paths below are fixed for the bundled Lite stack.",
        "",
    ]
    for k in sorted(env):
        lines.append("%s=%s" % (k, env[k]))
    content = "\n".join(lines) + "\n"

    # Write with 0600 perms.
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    finally:
        try:
            os.chmod(out_path, 0o600)
        except OSError:
            pass
    if not quiet:
        print("Wrote %s (0600) with %d variables." % (out_path, len(env)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parse configure.md -> .env.docker")
    ap.add_argument("--configure", default=os.path.join(DIST, "configure.md"))
    ap.add_argument("--out", default=os.path.join(DIST, ".env.docker"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.configure):
        _err("configure.md not found at %s" % args.configure)
        return 2

    with open(args.configure, "r", encoding="utf-8") as f:
        text = f.read()

    block = extract_ini_block(text)
    if block is None:
        _err("no ```ini fenced block found in %s — nothing to parse." % args.configure)
        return 2

    cfg = parse_block(block)
    merged, profile = validate(cfg)
    env = build_env(merged)
    write_env(env, args.out, quiet=args.quiet)

    if not args.quiet:
        print("Profile: %s" % profile)
        print("App port: %s" % env.get("APP_PORT", "5013"))
        print("AF3 MCP URL: %s" % env.get("AF3_MCP_URL", "(unset)"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        _err("unexpected: %s" % e)
        sys.exit(1)
