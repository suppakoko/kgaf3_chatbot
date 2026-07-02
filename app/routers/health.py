"""헬스체크 라우터.

GET /health   — liveness (인증 불필요, 빠른 응답)
GET /health/ready — readiness (서비스 의존성 점검)

interface-contracts §1 권위.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.routers._deps import get_af3, get_openmm
from app.services.af3_client import AF3MCPClient
from app.services.openmm_client import OpenMMMCPClient
from app.utils.log import get_logger

log = get_logger("router.health")

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_liveness(
    af3: AF3MCPClient = Depends(get_af3),
    openmm: OpenMMMCPClient = Depends(get_openmm),
) -> dict:
    """Liveness probe — 인증 없이 접근 가능. 빠른 단순 ping 체크."""
    af3_ok = False
    openmm_ok = False

    try:
        af3_ok = await af3.ping()
    except Exception:
        pass

    try:
        openmm_ok = await openmm.ping()
    except Exception:
        pass

    return {"app": True, "af3_mcp": af3_ok, "openmm_mcp": openmm_ok}


@router.get("/health/ready")
async def health_readiness(
    request: Request,
    af3: AF3MCPClient = Depends(get_af3),
    openmm: OpenMMMCPClient = Depends(get_openmm),
) -> JSONResponse:
    """Readiness probe — 전체 의존성 점검. 503 시 서비스 일부 비가용."""
    checks: dict[str, bool] = {}

    # af3 MCP
    try:
        checks["af3_mcp"] = await af3.ping()
    except Exception:
        checks["af3_mcp"] = False

    # openmm MCP
    try:
        checks["openmm_mcp"] = await openmm.ping()
    except Exception:
        checks["openmm_mcp"] = False

    # SQLite — history service 존재 여부로 확인
    try:
        history = request.app.state.history
        checks["sqlite"] = history is not None
    except Exception:
        checks["sqlite"] = False

    # LLM (OpenRouter API key 설정 여부만 확인 — 실제 호출 X)
    try:
        from app.config import settings
        checks["openrouter"] = bool(settings.openrouter_api_key)
    except Exception:
        checks["openrouter"] = False

    all_critical = checks["sqlite"]  # SQLite 는 필수
    status = "ready" if all_critical else "degraded"
    status_code = 200 if all_critical else 503

    log.info(
        "health.ready",
        status=status,
        af3_mcp=checks["af3_mcp"],
        openmm_mcp=checks["openmm_mcp"],
        sqlite=checks["sqlite"],
        openrouter=checks["openrouter"],
    )

    return JSONResponse(
        content={"status": status, "checks": checks},
        status_code=status_code,
    )
