#!/usr/bin/env bash
# bootstrap_host_dirs.sh — create host-side directories for the afmm_chat stack
# and verify the external AF3 output root.
#
# Named-volume-backed data (afmm.db, uploads, work) lives in Docker volumes and
# needs no host dirs. The one host path that must already exist is
# AF3_OUTPUT_ROOT (mounted read-only). We also create an optional local
# ./data dir for bind-mount-based setups.
#
# Reads AF3_OUTPUT_ROOT from the environment or from ../.env.docker.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${DIST_DIR}/.env.docker"

# Load AF3_OUTPUT_ROOT from .env.docker if not already in env.
if [[ -z "${AF3_OUTPUT_ROOT:-}" && -f "${ENV_FILE}" ]]; then
  AF3_OUTPUT_ROOT="$(grep -E '^AF3_OUTPUT_ROOT=' "${ENV_FILE}" | head -n1 | cut -d= -f2- || true)"
fi

echo "[bootstrap] DIST=${DIST_DIR}"

# Optional local data dir (only used if a setup chooses bind mounts).
LOCAL_DATA="${DIST_DIR}/data"
mkdir -p "${LOCAL_DATA}"
echo "[bootstrap] ensured ${LOCAL_DATA}"

# Verify AF3 output root (REQUIRED, read-only mount source).
if [[ -z "${AF3_OUTPUT_ROOT:-}" ]]; then
  echo "[bootstrap] ERROR: AF3_OUTPUT_ROOT is not set (env or .env.docker)." >&2
  exit 2
fi
if [[ ! -d "${AF3_OUTPUT_ROOT}" ]]; then
  echo "[bootstrap] ERROR: AF3_OUTPUT_ROOT does not exist: ${AF3_OUTPUT_ROOT}" >&2
  echo "[bootstrap]        Create it or point it at your AF3 output directory." >&2
  exit 2
fi
if [[ ! -r "${AF3_OUTPUT_ROOT}" ]]; then
  echo "[bootstrap] ERROR: AF3_OUTPUT_ROOT is not readable: ${AF3_OUTPUT_ROOT}" >&2
  exit 2
fi
echo "[bootstrap] AF3_OUTPUT_ROOT OK (readable): ${AF3_OUTPUT_ROOT}"

echo "[bootstrap] done."
