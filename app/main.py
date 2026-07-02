"""afmm_chat FastAPI 앱 팩토리 + lifespan.

backend_plan §2.2 권위.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

import aiosqlite
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_database
from app.routers import health, pages, chat, library, screening, sessions, smina, system, target
from app.services.af3_client import AF3MCPClient
from app.services.graphrag_service import GraphRAGService
from app.services.history_service import HistoryService
from app.services.library_service import LibraryService
from app.services.llm_service import LLMService
from app.services.openmm_client import OpenMMMCPClient
from app.services.smina_service import SminaService
from app.services.scoring_service import ScoringService, ScoreWeights
from app.services.ranking_service import RankingService
from app.services.target_service import TargetService
from app.services.screening_service import ScreeningService
from app.utils.log import configure_logging, get_logger

log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """앱 시작/종료 lifecycle 관리."""

    # 1. 로깅 설정
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log.info("afmm_chat.startup", host=settings.app_host, port=settings.app_port)

    # 2. SQLite 초기화 (v2 마이그레이션 포함)
    await init_database(settings.afmm_db_path)

    # 3. HistoryService 초기화
    app.state.history = HistoryService(settings.afmm_db_path)
    await app.state.history.init()
    log.info("service.history.ready")

    # 4. AF3 MCP 클라이언트
    app.state.af3 = AF3MCPClient(
        base_url=settings.af3_mcp_url,
        auth_token=settings.af3_mcp_auth_token,
    )
    try:
        await app.state.af3.connect()
        log.info("service.af3.connected", url=settings.af3_mcp_url)
    except Exception as exc:
        log.warning(
            "service.af3.connect_failed",
            error=str(exc),
            url=settings.af3_mcp_url,
            note="lazy reconnect on first request 가 활성화됨 — 서버 복구 후 자동 재연결",
        )
        # MCP 연결 실패는 시작 중단 사유가 아님 — degraded mode 허용

    # 5. OpenMM MCP 클라이언트
    app.state.openmm = OpenMMMCPClient(
        base_url=settings.openmm_mcp_url,
        auth_token=settings.openmm_mcp_auth_token,
    )
    try:
        await app.state.openmm.connect()
        log.info("service.openmm.connected", url=settings.openmm_mcp_url)
    except Exception as exc:
        log.warning(
            "service.openmm.connect_failed",
            error=str(exc),
            url=settings.openmm_mcp_url,
            note="lazy reconnect on first request 가 활성화됨 — 서버 복구 후 자동 재연결",
        )

    # 6. LLM 서비스
    app.state.llm = LLMService.from_settings(settings)
    log.info("service.llm.ready", default_model=settings.llm_default_model)

    # 7. LibraryService
    app.state.library = LibraryService(
        db_path=settings.afmm_db_path,
        library_dir=settings.afmm_library_dir,
    )
    log.info("service.library.ready")

    # 8. SminaService (Stage 5b)
    app.state.smina = SminaService(
        openmm_client=app.state.openmm,
        history=app.state.history,
    )
    log.info("service.smina.ready")

    # 9. ScoringService
    weights = ScoreWeights(
        iptm=settings.score_weight_iptm,
        pae=settings.score_weight_pae,
        inter=settings.score_weight_energy,
        smina=0.0,
    )
    app.state.scoring = ScoringService(weights=weights, iptm_floor=settings.iptm_floor)
    log.info(
        "service.scoring.ready",
        iptm=weights.iptm,
        pae=weights.pae,
        inter=weights.inter,
    )

    # 10. RankingService
    app.state.ranking = RankingService(history=app.state.history)
    log.info("service.ranking.ready")

    # 11. TargetService
    app.state.target = TargetService(http_timeout=10.0)
    log.info("service.target.ready")

    # 12. ScreeningService (Stages 1-7 — Phase 2 full)
    app.state.screening = ScreeningService(
        history=app.state.history,
        smina=app.state.smina,
        library=app.state.library,
        target=app.state.target,
        af3=app.state.af3,
        llm=app.state.llm,
        openmm=app.state.openmm,
        scoring=app.state.scoring,
        ranking=app.state.ranking,
    )
    log.info("service.screening.ready", stages_wired=7)

    # 12.5 GraphRAGService — 번들된 graphrag-mcp-server(SSE :8893)에 붙는 wrapper.
    # MCP/Neo4j 미접속은 로그만 남기고 계속 진행 (degraded mode).
    app.state.graphrag = GraphRAGService(
        mcp_url=settings.graphrag_mcp_url,
        enabled=settings.graphrag_enabled,
        auth_token=settings.graphrag_mcp_auth_token,
        default_provider=settings.graphrag_default_provider,
        openrouter_model=settings.graphrag_openrouter_model,
    )
    if settings.graphrag_enabled:
        try:
            ok = await app.state.graphrag.ping()
            log.info(
                "service.graphrag.ready",
                mcp_url=settings.graphrag_mcp_url,
                mcp_connected=ok,
            )
            if not ok:
                log.warning(
                    "service.graphrag.mcp_unreachable",
                    note=(
                        "graphrag-mcp 컨테이너가 떠 있는지 확인하세요 "
                        "(docker compose --profile graphrag up -d) / GRAPHRAG_MCP_URL"
                    ),
                )
        except Exception as exc:
            log.warning("service.graphrag.init_failed", error=str(exc))
    else:
        log.info("service.graphrag.disabled")

    # 13. 좀비 잡 복구 — 서비스 재시작 시 in-process asyncio.create_task() 가
    # 휘발되어 status='queued'/'running' 잡이 DB 에 영원히 남는 문제 방지.
    # 모든 미종료 잡을 'abandoned' 로 마킹하여 사용자에게 명확히 표시.
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(settings.afmm_db_path) as db:
            cursor = await db.execute(
                """
                UPDATE screening_jobs
                   SET status = ?, ended_at = ?, error = ?
                 WHERE status IN ('queued', 'running')
                   AND ended_at IS NULL
                """,
                ("abandoned", now_iso,
                 "service_restart: in-process pipeline task lost on lifespan startup"),
            )
            abandoned = cursor.rowcount or 0
            await db.commit()
        if abandoned:
            log.warning("startup.zombie_jobs.marked_abandoned", count=abandoned)
        else:
            log.info("startup.zombie_jobs.none")
    except Exception as exc:
        log.error("startup.zombie_jobs.cleanup_failed", exc_info=exc)

    log.info("afmm_chat.ready")
    yield

    # --- Shutdown ---
    log.info("afmm_chat.shutdown.start")

    try:
        await app.state.af3.aclose()
        log.info("service.af3.closed")
    except Exception as exc:
        log.error("service.af3.close_failed", exc_info=exc)

    try:
        await app.state.openmm.aclose()
        log.info("service.openmm.closed")
    except Exception as exc:
        log.error("service.openmm.close_failed", exc_info=exc)

    try:
        await app.state.target.aclose()
        log.info("service.target.closed")
    except Exception as exc:
        log.error("service.target.close_failed", exc_info=exc)

    try:
        await app.state.graphrag.aclose()
        log.info("service.graphrag.closed")
    except Exception as exc:
        log.error("service.graphrag.close_failed", exc_info=exc)

    try:
        await app.state.history.close()
        log.info("service.history.closed")
    except Exception as exc:
        log.error("service.history.close_failed", exc_info=exc)

    log.info("afmm_chat.shutdown.done")


# ---------------------------------------------------------------------------
# 앱 인스턴스
# ---------------------------------------------------------------------------

app = FastAPI(
    title="afmm_chat",
    description="AF3 + OpenMM Virtual Screening Chatbot",
    version="0.2.0",
    lifespan=lifespan,
)

# 정적 파일
app.mount("/static", StaticFiles(directory="static"), name="static")

# 라우터 등록
app.include_router(pages.router)
app.include_router(chat.router)
app.include_router(library.router)
app.include_router(screening.router)
app.include_router(sessions.router)
app.include_router(system.router)
app.include_router(health.router)
app.include_router(target.router)   # Phase 2: POST /api/target/resolve
app.include_router(smina.router)    # Phase 2: POST /api/screening/{job_id}/ligands/{ligand_id}/smina
