"""smina-only MCP server — FastMCP streamable-http.

Standalone, OpenMM-free MCP server that exposes smina rescoring/minimization
tools over HTTP. Designed to be called by afmm-chat with ZERO client changes:
the tool names, argument keys, and returned keys are identical to the original
openMM_bot smina tools.

Transport : streamable-http (FastMCP "http")
Bind      : 0.0.0.0:8001
MCP path  : /mcp/
Health    : GET /health  -> "OK"

Tools exposed (names + arg keys match afmm-chat's SminaService exactly):
  - smina_score_only(receptor_pdb, ligand_pdb_or_sdf, timeout_sec=120)
  - smina_minimize(receptor_pdb, ligand_pdb_or_sdf, out_path=None,
                   scoring="vinardo", minimize_iters=0, timeout_sec=120)
  - smina_score_batch(receptor_pdb, ligand_files, timeout_sec_per_ligand=60)

smina is invoked via subprocess on CPU. Binary path: $SMINA_BIN
(default /usr/local/bin/smina). Receptor/ligand are FILE PATHS read from the
shared work volume (/data/work); minimize writes *_minimized.sdf back there.
"""

from __future__ import annotations

import os

import structlog
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from smina_mcp import tools

log = structlog.get_logger("smina.server")

HOST = os.environ.get("SMINA_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("SMINA_MCP_PORT", "8001"))
PATH = os.environ.get("SMINA_MCP_PATH", "/mcp/")

mcp = FastMCP("smina-mcp")

# ── Tool registration (names MUST match afmm-chat's calls) ────────────────────
# Registered by reference so the implementations stay in smina_mcp/tools.py.
mcp.tool(tools.smina_score_only)
mcp.tool(tools.smina_minimize)
mcp.tool(tools.smina_score_batch)


# ── Health endpoint (used by container HEALTHCHECK) ───────────────────────────
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    """Lightweight liveness probe — does not require an MCP session."""
    return PlainTextResponse("OK")


if __name__ == "__main__":
    log.info("smina_mcp.start", host=HOST, port=PORT, path=PATH, smina_bin=tools.SMINA_BIN)
    mcp.run(transport="http", host=HOST, port=PORT, path=PATH)
