#!/usr/bin/env bash
# check_dependencies.sh — standalone dependency check for afmm_chat.
# Callable on its own:  ./scripts/check_dependencies.sh
# Verifies the host has everything install.sh needs. Non-zero exit on failure.
set -uo pipefail

fail=0
ok()   { printf '  [ OK ] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; fail=1; }

echo "Checking afmm_chat dependencies..."

# python3 (installer logic runs under python3, stdlib only)
if command -v python3 >/dev/null 2>&1; then
  ok "python3: $(python3 --version 2>&1)"
else
  bad "python3 not found (required to run the installer scripts)."
fi

# docker
if command -v docker >/dev/null 2>&1; then
  if docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    ok "docker daemon reachable: $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  else
    bad "docker installed but daemon not reachable (start Docker / check permissions)."
  fi
else
  bad "docker not found (install Docker Engine 20.10+ or Docker Desktop)."
fi

# docker compose v2
if docker compose version >/dev/null 2>&1; then
  ok "docker compose v2: $(docker compose version 2>/dev/null | head -n1)"
else
  bad "docker compose v2 plugin not found."
fi

# curl (optional convenience)
if command -v curl >/dev/null 2>&1; then
  ok "curl present"
else
  warn "curl not found (optional; installer uses python urllib for health checks)."
fi

# Linux extras (best-effort, advisory only)
if [[ "$(uname -s)" == "Linux" ]]; then
  if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce 2>/dev/null)" == "Enforcing" ]]; then
    warn "SELinux Enforcing — install.sh will relabel bind mounts (:Z)."
  fi
  if [[ "$(id -u)" != "0" ]] && ! id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    warn "user not in 'docker' group (sudo usermod -aG docker \$USER, then re-login)."
  fi
fi

if [[ "$fail" -ne 0 ]]; then
  echo "Dependency check FAILED."
  exit 1
fi
echo "All required dependencies present."
exit 0
