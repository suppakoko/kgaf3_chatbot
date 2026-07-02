#!/usr/bin/env bash
# install.sh — one-click installer for afmm_chat (Lite) on Linux.
#
# Flow (per spec 06 §6.1):
#   preflight -> parse configure.md -> bootstrap host dirs ->
#   (best-effort SELinux/firewalld) -> docker compose build -> up -d ->
#   health poll -> AF3 verify -> success report.
#
# Idempotent and re-runnable. Edit configure.md, then run ./install.sh.
set -euo pipefail

# ---- locate self -----------------------------------------------------------
DIST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${DIST_DIR}"

INSTALLER="${DIST_DIR}/installer"
SCRIPTS="${DIST_DIR}/scripts"
CONFIGURE="${DIST_DIR}/configure.md"
ENV_FILE="${DIST_DIR}/.env.docker"

PY="${PYTHON:-python3}"
COMPOSE=(docker compose)

log()  { printf '\n=== %s ===\n' "$1"; }
die()  { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

command -v "${PY}" >/dev/null 2>&1 || die "python3 not found (needed to run the installer)."

# ---- 0. read APP_PORT / PROFILE early (for preflight) ----------------------
APP_PORT="5013"
PROFILE="lite"
if [[ -f "${CONFIGURE}" ]]; then
  # Extract from the ini block cheaply; the parser does the authoritative job.
  _p="$(grep -E '^[[:space:]]*APP_PORT[[:space:]]*=' "${CONFIGURE}" | head -n1 | sed 's/#.*//' | cut -d= -f2- | tr -d '[:space:]' || true)"
  [[ -n "${_p}" ]] && APP_PORT="${_p}"
  _pr="$(grep -E '^[[:space:]]*PROFILE[[:space:]]*=' "${CONFIGURE}" | head -n1 | sed 's/#.*//' | cut -d= -f2- | tr -d '[:space:]' || true)"
  [[ -n "${_pr}" ]] && PROFILE="${_pr}"
fi

# ---- 1. preflight ----------------------------------------------------------
log "1/8 Preflight"
"${PY}" "${INSTALLER}/preflight.py" --port "${APP_PORT}" --profile "${PROFILE}" \
  || die "Preflight failed. Fix the items above and re-run."

# ---- 2. parse configure.md -> .env.docker ----------------------------------
log "2/8 Parsing configure.md -> .env.docker"
"${PY}" "${INSTALLER}/configure_parser.py" --configure "${CONFIGURE}" --out "${ENV_FILE}" \
  || die "configure.md is invalid. Fix the reported keys and re-run."

# Re-read authoritative APP_PORT from the generated env.
if [[ -f "${ENV_FILE}" ]]; then
  _p2="$(grep -E '^APP_PORT=' "${ENV_FILE}" | head -n1 | cut -d= -f2- || true)"
  [[ -n "${_p2}" ]] && APP_PORT="${_p2}"
fi

# ---- 3. bootstrap host dirs + verify AF3_OUTPUT_ROOT -----------------------
log "3/8 Bootstrapping host directories"
bash "${SCRIPTS}/bootstrap_host_dirs.sh" || die "Host directory bootstrap failed."

# ---- 4. best-effort SELinux + firewalld (Linux) ----------------------------
log "4/8 SELinux / firewalld (best-effort)"
AF3_OUTPUT_ROOT="$(grep -E '^AF3_OUTPUT_ROOT=' "${ENV_FILE}" | head -n1 | cut -d= -f2- || true)"
if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce 2>/dev/null)" == "Enforcing" ]]; then
  if [[ -n "${AF3_OUTPUT_ROOT}" ]] && command -v semanage >/dev/null 2>&1 && command -v restorecon >/dev/null 2>&1; then
    echo "  SELinux Enforcing: labeling ${AF3_OUTPUT_ROOT} for container read access."
    sudo semanage fcontext -a -t container_file_t "${AF3_OUTPUT_ROOT}(/.*)?" 2>/dev/null || \
      echo "  (semanage fcontext skipped — may need sudo or already set)"
    sudo restorecon -R "${AF3_OUTPUT_ROOT}" 2>/dev/null || \
      echo "  (restorecon skipped — compose also uses :Z/:ro labels)"
  else
    echo "  SELinux Enforcing but semanage/restorecon unavailable — relying on compose :Z/:ro labels."
  fi
else
  echo "  SELinux not enforcing (or not present) — nothing to do."
fi
if command -v firewall-cmd >/dev/null 2>&1; then
  if firewall-cmd --state >/dev/null 2>&1; then
    echo "  firewalld active: marking docker0 as trusted (best-effort)."
    sudo firewall-cmd --zone=trusted --add-interface=docker0 2>/dev/null || \
      echo "  (firewalld docker0 trusted skipped — may need sudo or already set)"
  fi
else
  echo "  firewalld not present — nothing to do."
fi

# ---- 5. build images -------------------------------------------------------
log "5/8 Building images (docker compose build)"
"${COMPOSE[@]}" build || die "docker compose build failed. See output above."

# ---- 6. start stack --------------------------------------------------------
# GraphRAG (neo4j + graphrag-mcp) is ON BY DEFAULT in kgaf3_chatbot, so a plain
# `up -d` starts the whole stack and pulls the pre-built KG/MCP images. When the
# user opts out (GRAPHRAG_ENABLED=false), start only the screening services so
# the heavy KG images are never pulled.
GRAPHRAG_ENABLED="$(grep -E '^GRAPHRAG_ENABLED=' "${ENV_FILE}" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
if [[ "${GRAPHRAG_ENABLED,,}" == "false" ]]; then
  log "6/8 Starting stack (docker compose up -d kgaf3-chat smina-mcp — GraphRAG opted out)"
  "${COMPOSE[@]}" up -d kgaf3-chat smina-mcp || die "docker compose up failed. See output above."
else
  log "6/8 Starting full stack incl. GraphRAG (docker compose up -d)"
  "${COMPOSE[@]}" up -d || die "docker compose up failed. See output above."
fi

# ---- 7. health poll --------------------------------------------------------
log "7/8 Waiting for afmm_chat to become ready"
HEALTH_OK=1
"${PY}" "${INSTALLER}/healthpoll.py" --port "${APP_PORT}" --timeout 180 || HEALTH_OK=0

# ---- 8. verify external AF3 ------------------------------------------------
log "8/8 Verifying external AF3 connection"
AF3_OK=1
"${PY}" "${INSTALLER}/verify_af3.py" --env-file "${ENV_FILE}" || AF3_OK=0

# ---- success report --------------------------------------------------------
get() { grep -E "^$1=" "${ENV_FILE}" 2>/dev/null | head -n1 | cut -d= -f2-; }
LLM_MODEL="$(get LLM_DEFAULT_MODEL)"; LLM_MODEL="${LLM_MODEL:-anthropic/claude-sonnet-4-6}"
AF3_URL="$(get AF3_MCP_URL)"
AF3_OUT="$(get AF3_OUTPUT_ROOT)"
ENABLE_NGINX="$(get ENABLE_NGINX)"; ENABLE_NGINX="${ENABLE_NGINX:-false}"

if [[ "${ENABLE_NGINX}" == "true" ]]; then NGINX_NOTE="(nginx reverse proxy enabled)"; else NGINX_NOTE="(nginx disabled)"; fi
if [[ "${AF3_OK}" -eq 1 ]]; then AF3_STATUS="[connected, batch tools verified]"; else AF3_STATUS="[NOT reachable — see remediation above]"; fi
if [[ "${HEALTH_OK}" -eq 1 ]]; then READY_NOTE="ready"; else READY_NOTE="NOT ready (check: docker compose logs -f kgaf3-chat)"; fi

echo
echo "============================================================"
if [[ "${HEALTH_OK}" -eq 1 && "${AF3_OK}" -eq 1 ]]; then
  echo " afmm_chat install COMPLETE"
elif [[ "${HEALTH_OK}" -eq 1 ]]; then
  echo " afmm_chat install COMPLETE (DEGRADED: AF3 not verified)"
else
  echo " afmm_chat install FINISHED (app not yet ready)"
fi
echo "============================================================"
printf "   - URL:          http://localhost:%s   %s\n" "${APP_PORT}" "${NGINX_NOTE}"
printf "   - App status:    %s\n" "${READY_NOTE}"
printf "   - Profile:       lite (no GPU)\n"
printf "   - LLM:           %s\n" "${LLM_MODEL}"
printf "   - Bundled MCP:   smina-mcp:8001 (internal, no config needed)\n"
if [[ "${GRAPHRAG_ENABLED,,}" == "false" ]]; then
  printf "   - GraphRAG:      disabled (set GRAPHRAG_ENABLED=true in configure.md to enable)\n"
else
  printf "   - GraphRAG:      ENABLED (default) — neo4j + graphrag-mcp:8893\n"
fi
printf "   - External AF3:  %s  %s\n" "${AF3_URL}" "${AF3_STATUS}"
printf "   - AF3 output:    %s  (mounted read-only)\n" "${AF3_OUT}"
echo "   Next steps:"
echo "     1. Open http://localhost:${APP_PORT} in a browser."
echo "     2. Paste a protein sequence + a SMILES string into the chat."
if [[ "${AF3_OK}" -ne 1 ]]; then
  echo "     ! Fix the AF3 connection (see remediation above and docs/BRIDGE.md)"
  echo "       before running a screening — Stage 3 will fail until AF3 is reachable."
fi
echo "============================================================"

# Exit 0 even when degraded: the stack is up; AF3 issues are advisory.
exit 0
