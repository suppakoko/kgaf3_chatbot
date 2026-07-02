"""시스템 정보 라우터 — 하단 상태바 표시용.

GET /api/system/info  — 연결정보 + GPU 사용률 + MCP 상태 + 활성 잡 수
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers._deps import get_af3, get_graphrag, get_history, get_openmm
from app.services.af3_client import AF3MCPClient
from app.services.graphrag_service import GraphRAGService
from app.services.history_service import HistoryService
from app.services.openmm_client import OpenMMMCPClient
from app.utils.log import get_logger

log = get_logger("router.system")

router = APIRouter(prefix="/api/system", tags=["system"])


_NVIDIA_SMI_QUERY = (
    "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
)


async def _query_nvidia_smi() -> list[dict[str, Any]]:
    """nvidia-smi 비동기 호출 → GPU 리스트 반환. 실패 시 [].

    출력 예시 (CSV):
        0, NVIDIA RTX A6000, 97, 20083, 49140, 72
    """
    nvsmi = shutil.which("nvidia-smi")
    if not nvsmi:
        return []

    try:
        proc = await asyncio.create_subprocess_exec(
            nvsmi,
            f"--query-gpu={_NVIDIA_SMI_QUERY}",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return []
    except Exception as exc:
        log.warning("system.nvidia_smi.exec_failed", error=str(exc))
        return []

    if proc.returncode != 0:
        return []

    gpus: list[dict[str, Any]] = []
    for line in stdout.decode("utf-8", errors="replace").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "util_pct": int(parts[2]),
                "mem_used_mb": int(parts[3]),
                "mem_total_mb": int(parts[4]),
                "temp_c": int(parts[5]),
            })
        except ValueError:
            continue
    return gpus


async def _count_active_jobs(history: HistoryService) -> int:
    """status in (queued, running) 잡 개수."""
    try:
        async with aiosqlite.connect(history._db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM screening_jobs WHERE status IN ('queued','running')"
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0
    except Exception:
        return 0


@router.get("/info")
async def system_info(
    request: Request,
    af3: Annotated[AF3MCPClient, Depends(get_af3)],
    openmm: Annotated[OpenMMMCPClient, Depends(get_openmm)],
    history: Annotated[HistoryService, Depends(get_history)],
    graphrag: Annotated[GraphRAGService, Depends(get_graphrag)],
) -> JSONResponse:
    """하단 status bar 표시용 통합 시스템 정보."""

    # MCP ping (비차단·짧은 timeout)
    async def _safe_ping(client: Any) -> bool:
        try:
            return bool(await asyncio.wait_for(client.ping(), timeout=1.5))
        except Exception:
            return False

    af3_ok, openmm_ok, graphrag_ok, gpus, active_jobs = await asyncio.gather(
        _safe_ping(af3),
        _safe_ping(openmm),
        _safe_ping(graphrag),
        _query_nvidia_smi(),
        _count_active_jobs(history),
    )

    payload = {
        "ok": True,
        "app": {
            "host": settings.app_host,
            "port": settings.app_port,
            "version": "0.2.0",
            "pid": os.getpid(),
        },
        "mcp": {
            "af3": {"url": settings.af3_mcp_url, "connected": af3_ok},
            "openmm": {"url": settings.openmm_mcp_url, "connected": openmm_ok},
        },
        "graphrag": {
            "enabled": graphrag.enabled,
            "mcp_url": settings.graphrag_mcp_url,
            "connected": graphrag_ok,
            "model": settings.graphrag_openrouter_model,
        },
        "openrouter": {
            "configured": bool(settings.openrouter_api_key)
            and settings.openrouter_api_key != "sk-or-...",
            "default_model": settings.llm_default_model,
        },
        "gpus": gpus,
        "jobs": {"active": active_jobs},
    }
    return JSONResponse(payload)
