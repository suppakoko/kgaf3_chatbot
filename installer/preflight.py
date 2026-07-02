#!/usr/bin/env python3
"""preflight.py — host readiness checks before building/starting the stack.

Stdlib only. Cross-platform (Linux + Windows-via-WSL). Checks:
  - docker present and >= 20.10
  - `docker compose version` is Compose v2
  - APP_PORT is free
  - disk space (warn if low)
  - (Linux) docker group membership; SELinux note

Exit codes: 0 ok (warnings allowed), 2 a hard requirement failed.
"""

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys

MIN_DOCKER = (20, 10)


def _run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def check_docker():
    if not shutil.which("docker"):
        return False, "docker not found on PATH. Install Docker Engine 20.10+ " \
                      "(Linux) or Docker Desktop (Windows/macOS)."
    rc, out = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    if rc != 0:
        # Daemon may be down.
        rc2, out2 = _run(["docker", "version"])
        return False, "docker is installed but the daemon is not reachable. " \
                      "Start Docker (e.g. `systemctl start docker` or launch " \
                      "Docker Desktop).\n        " + out2.strip().splitlines()[-1] \
                      if out2.strip() else "docker daemon unreachable."
    m = re.search(r"(\d+)\.(\d+)", out)
    if not m:
        return True, "docker present (version string unparsed: %r)" % out.strip()
    ver = (int(m.group(1)), int(m.group(2)))
    if ver < MIN_DOCKER:
        return False, "docker %d.%d is too old; need >= 20.10." % ver
    return True, "docker %d.%d server reachable." % ver


def check_compose_v2():
    rc, out = _run(["docker", "compose", "version"])
    if rc != 0:
        return False, "`docker compose` (v2 plugin) not available. Install the " \
                      "Docker Compose v2 plugin (the legacy `docker-compose` " \
                      "binary is not used)."
    if "v2" in out or re.search(r"version v?2", out, re.I) or re.search(r"\b2\.\d+", out):
        return True, "docker compose v2 present (%s)" % out.strip().splitlines()[0]
    return True, "docker compose present (%s)" % out.strip().splitlines()[0]


def check_port(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True, "port %d is free." % port
    except OSError:
        return False, "port %d is already in use. Change APP_PORT in " \
                      "configure.md or free the port." % port
    finally:
        s.close()


def check_disk(path, warn_gb=10):
    try:
        total, used, free = shutil.disk_usage(path)
    except OSError as e:
        return True, "disk check skipped (%s)" % e
    free_gb = free / (1024 ** 3)
    if free_gb < warn_gb:
        return True, "WARNING: only %.1f GB free on %s (recommend >= %d GB for " \
                     "images + work volumes)." % (free_gb, path, warn_gb)
    return True, "disk free: %.1f GB on %s" % (free_gb, path)


def check_linux_docker_group():
    if sys.platform != "linux":
        return True, None
    # If we can talk to docker (checked elsewhere) group is effectively fine,
    # but advise if the user is not in the docker group and not root.
    if os.geteuid() == 0:
        return True, "running as root (docker socket accessible)."
    rc, out = _run(["id", "-nG"])
    groups = out.split() if rc == 0 else []
    if "docker" in groups:
        return True, "user is in the 'docker' group."
    # Not necessarily fatal (rootless / sudo), so warn only.
    return True, "WARNING: user not in 'docker' group. If docker commands fail " \
                 "with permission denied, run: sudo usermod -aG docker $USER " \
                 "(then re-login)."


def selinux_note():
    if sys.platform != "linux":
        return None
    if shutil.which("getenforce"):
        rc, out = _run(["getenforce"])
        mode = out.strip()
        if rc == 0 and mode.lower() == "enforcing":
            return "SELinux is Enforcing: install.sh will apply :Z/fcontext on " \
                   "bind mounts. If volumes show permission errors, see the " \
                   "SELinux note in docs/BRIDGE.md."
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="afmm_chat preflight checks.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "5013")))
    ap.add_argument("--profile", default=os.environ.get("PROFILE", "lite"))
    args = ap.parse_args(argv)

    print("Running preflight checks (profile=%s, port=%d)..." % (args.profile, args.port))
    hard_fail = False

    checks = [
        ("docker", check_docker()),
        ("compose", check_compose_v2()),
        ("port", check_port(args.port)),
        ("disk", check_disk(os.getcwd())),
    ]
    grp = check_linux_docker_group()
    if grp[1] is not None:
        checks.append(("docker-group", grp))

    for name, (ok, msg) in checks:
        if msg is None:
            continue
        flag = "[ OK ]" if ok else "[FAIL]"
        if "WARNING" in (msg or ""):
            flag = "[WARN]"
        print("  %s %-13s %s" % (flag, name, msg))
        if not ok:
            hard_fail = True

    note = selinux_note()
    if note:
        print("  [NOTE] selinux       %s" % note)

    if args.profile == "full":
        print("  [NOTE] profile       PROFILE=full is reserved/non-functional; "
              "the Lite stack needs no GPU.")

    if hard_fail:
        print("Preflight FAILED — fix the [FAIL] items above and re-run.")
        return 2
    print("Preflight OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
