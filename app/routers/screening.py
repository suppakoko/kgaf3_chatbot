"""스크리닝 라우터 — Phase 2 + Phase 3 (CIF 엔드포인트 추가).

POST /api/screening                          → 잡 생성 + job_id 반환
GET  /api/screening/{job_id}/results         → RankingService.compute_ranking
GET  /api/results/{job_id}/{ligand_id}       → 단일 결과 상세
GET  /api/results/{job_id}/{ligand_id}/cif   → CIF 파일 (Mol* 뷰어용)

interface-contracts §1 권위.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.routers._deps import get_history, get_ranking, get_screening
from app.services.history_service import HistoryService
from app.services.ranking_service import RankingService, ScreeningResultRow
from app.services.screening_service import ScreeningJobContext, ScreeningService
from app.services.target_service import TargetInput
from app.utils.ids import ulid_str
from app.utils.log import get_logger

log = get_logger("router.screening")

router = APIRouter(prefix="/api", tags=["screening"])


# ── 요청/응답 모델 ────────────────────────────────────────────────────────────

class StartScreeningRequest(BaseModel):
    """POST /api/screening 요청 바디."""

    library_id: str
    target_sequence: str | None = None
    target_pdb_path: str | None = None
    config: dict | None = None
    session_id: str | None = None  # 채팅 세션 연동 시 stage 이벤트 라우팅 키로 사용


class StartScreeningResponse(BaseModel):
    """POST /api/screening 응답."""

    ok: bool = True
    job_id: str


class ResultsResponse(BaseModel):
    """GET /api/screening/{job_id}/results 응답."""

    ok: bool = True
    job_id: str
    sort_by: str
    top_n: int
    results: list[ScreeningResultRow]


# ── 엔드포인트 ────────────────────────────────────────────────────────────────

@router.post("/screening", response_model=StartScreeningResponse, status_code=202)
async def start_screening(
    body: StartScreeningRequest,
    history: Annotated[HistoryService, Depends(get_history)],
    screening: Annotated[ScreeningService, Depends(get_screening)],
) -> StartScreeningResponse:
    """스크리닝 잡 생성 → job_id 반환 + 백그라운드 파이프라인 시작.

    Phase 2 full: 잡 레코드 INSERT → asyncio.create_task 로 run_pipeline_full() 시작.
    응답은 즉시 (202 Accepted), 실제 stages 1-7 은 백그라운드.
    GPU 시간 ~5h (AF3 dominant). WS 또는 GET /api/screening/{job_id}/results 로 진행 모니터링.
    """
    import aiosqlite
    from datetime import datetime, timezone

    job_id = ulid_str()
    now = datetime.now(timezone.utc).isoformat()

    log.info(
        "screening.start",
        job_id=job_id,
        library_id=body.library_id,
    )

    async with aiosqlite.connect(history._db_path) as db:
        await db.execute(
            """
            INSERT INTO screening_jobs
                (job_id, library_id, target_json, config_json,
                 status, stage, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                body.library_id,
                body.target_sequence or body.target_pdb_path,
                str(body.config) if body.config else None,
                "queued",
                "ingest",
                now,
            ),
        )
        await db.commit()

    log.info("screening.job_created", job_id=job_id, status="queued")

    # Build context + start background pipeline
    target_input = TargetInput(sequence=body.target_sequence)

    # 채팅 세션과 연결되어 있으면 event_bus 로 stage 이벤트 발행 (chat WS 가 forward)
    from app.services.event_bus import bus as _event_bus
    _bcast = None
    if body.session_id:
        _event_bus.link_job(job_id, body.session_id)
        _sid = body.session_id

        async def _bcast(event: dict) -> None:
            await _event_bus.publish(_sid, event)

    ctx = ScreeningJobContext(
        job_id=job_id,
        session_id=body.session_id,
        library_id=body.library_id,
        target_input=target_input,
        config=body.config or {},
        ws_broadcast=_bcast,
    )

    async def _run_with_status_update() -> None:
        """파이프라인 실행 + 종료 시 DB status 업데이트."""
        try:
            log.info("screening.pipeline_full.spawn", job_id=job_id)
            result = await screening.run_pipeline_full(ctx)
            final_status = result.get("status", "unknown")
            log.info("screening.pipeline_full.finished", job_id=job_id, final_status=final_status)
            async with aiosqlite.connect(history._db_path) as db:
                await db.execute(
                    "UPDATE screening_jobs SET status = ?, ended_at = ?, error = ? WHERE job_id = ?",
                    (
                        final_status,
                        datetime.now(timezone.utc).isoformat(),
                        ", ".join(result.get("errors", [])) or None,
                        job_id,
                    ),
                )
                await db.commit()
        except Exception as exc:
            log.error("screening.pipeline_full.crash", job_id=job_id, error=str(exc), exc_info=True)
            try:
                async with aiosqlite.connect(history._db_path) as db:
                    await db.execute(
                        "UPDATE screening_jobs SET status = ?, ended_at = ?, error = ? WHERE job_id = ?",
                        ("failed", datetime.now(timezone.utc).isoformat(), str(exc)[:500], job_id),
                    )
                    await db.commit()
            except Exception:
                pass

    asyncio.create_task(_run_with_status_update())

    return StartScreeningResponse(job_id=job_id)


@router.get(
    "/screening/{job_id}/results",
    response_model=ResultsResponse,
)
async def get_results(
    job_id: str,
    ranking: Annotated[RankingService, Depends(get_ranking)],
    sort_by: Annotated[
        Literal["composite", "smina", "iptm", "ranking_score", "e_inter"],
        Query(description="정렬 기준"),
    ] = "smina",
    top_n: Annotated[int, Query(ge=1, le=200, description="반환 최대 행 수")] = 20,
) -> ResultsResponse:
    """스크리닝 결과 조회 (정렬 + top-N).

    sort_by:
        - smina: smina affinity 오름차순 (더 음수 = 더 강한 결합) — **기본값, 최종 binding affinity**
        - composite: 복합 스코어 내림차순 (AF3 ranking_score + PAE + OpenMM E_inter, smina blend 가능)
        - ranking_score: AF3 best-sample ranking_score 내림차순 (AF3 confidence 단독)
        - iptm: ipTM 내림차순 (legacy AF3 confidence)
        - e_inter: OpenMM interaction energy 오름차순
    """
    log.info(
        "screening.results.request",
        job_id=job_id,
        sort_by=sort_by,
        top_n=top_n,
    )
    rows = await ranking.compute_ranking(job_id, sort_by=sort_by, top_n=top_n)
    return ResultsResponse(
        job_id=job_id,
        sort_by=sort_by,
        top_n=top_n,
        results=rows,
    )


@router.get(
    "/results/{job_id}/{ligand_id}",
    response_model=ScreeningResultRow,
)
async def get_result_detail(
    job_id: str,
    ligand_id: str,
    ranking: Annotated[RankingService, Depends(get_ranking)],
) -> ScreeningResultRow:
    """단일 (job_id, ligand_id) 결과 상세 조회."""
    log.info(
        "screening.result_detail.request",
        job_id=job_id,
        ligand_id=ligand_id,
    )
    row = await ranking.get_single(job_id, ligand_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Result not found: job_id={job_id}, ligand_id={ligand_id}",
        )
    return row


@router.get("/results/{job_id}/{ligand_id}/cif")
async def get_cif(job_id: str, ligand_id: str) -> FileResponse:
    """AF3 결과 CIF 파일을 반환한다 (Mol* 3D 뷰어용).

    탐색 순서:
    1. af3_output_root/{job_id}_{ligand_id}*/**/*model*.cif
    2. af3_output_root/{job_id}/**/{ligand_id}*model*.cif
    경로 탈출 방지: resolved path가 af3_output_root 하위인지 검증.
    """
    import re

    log.info("screening.cif.request", job_id=job_id, ligand_id=ligand_id)

    output_root: Path = settings.af3_output_root.resolve()

    # 안전한 식별자 검증 (디렉토리 탈출 방지)
    _safe = re.compile(r'^[A-Za-z0-9_\-]+$')
    if not _safe.match(job_id) or not _safe.match(ligand_id):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id 또는 ligand_id")

    # 후보 glob 패턴 두 가지 시도
    patterns = [
        output_root / f"{job_id}_{ligand_id}*" / "**" / "*model*.cif",
        output_root / job_id / "**" / f"{ligand_id}*model*.cif",
    ]

    cif_path: Path | None = None
    for pattern in patterns:
        matches = list(output_root.glob(
            str(pattern.relative_to(output_root))
        ))
        for m in matches:
            resolved = m.resolve()
            # 경로 탈출 방지
            try:
                resolved.relative_to(output_root)
            except ValueError:
                continue
            if resolved.is_file() and resolved.suffix in {".cif", ".mmcif"}:
                cif_path = resolved
                break
        if cif_path:
            break

    if cif_path is None:
        log.warning(
            "screening.cif.not_found",
            job_id=job_id,
            ligand_id=ligand_id,
            output_root=str(output_root),
        )
        raise HTTPException(
            status_code=404,
            detail=f"CIF 파일을 찾을 수 없음: job_id={job_id}, ligand_id={ligand_id}",
        )

    log.info("screening.cif.found", path=str(cif_path))
    return FileResponse(
        path=cif_path,
        media_type="application/x-cif",
        filename=f"{job_id}_{ligand_id}_model.cif",
    )
