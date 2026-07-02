"""Smina 리스코어링 라우터 — 단일 (job_id, ligand_id) 보충 계산.

POST /api/screening/{job_id}/ligands/{ligand_id}/smina

interface-contracts §2 (Phase 2 신규 router) 권위.
스크리닝 파이프라인에서 일부 리간드의 smina 결과가 누락되거나 재계산이
필요할 때 사용한다. 자동 파일 탐색 우선(자전거 자물쇠), 명시 경로는 옵션.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.models.smina_rescore import SminaRescoreRequest, SminaRescoreResponse
from app.routers._deps import get_smina
from app.services.smina_service import SminaService
from app.utils.log import get_logger

log = get_logger("router.smina")

router = APIRouter(prefix="/api", tags=["smina"])

# 디렉토리 탈출 방지용 ID 정규식 (screening.cif 와 동일 패턴)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")


def _validate_id(value: str, name: str) -> None:
    if not _SAFE_ID.match(value):
        raise HTTPException(status_code=400, detail=f"유효하지 않은 {name}")


def _safe_within(path: Path, root: Path) -> bool:
    """resolved path 가 root 하위인지 확인 (경로 탈출 방어)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _auto_discover(job_id: str, ligand_id: str, root: Path) -> tuple[Path, Path]:
    """af3_output_root 에서 receptor + ligand 파일 자동 탐색.

    탐색 순서 (screening.cif 패턴과 정렬):
      1. {root}/{job_id}_{ligand_id}*/**/receptor*.pdb
         {root}/{job_id}_{ligand_id}*/**/{ligand_id}*minimized*.pdb
      2. {root}/{job_id}/**/{ligand_id}*receptor*.pdb
         {root}/{job_id}/**/{ligand_id}*minimized*.pdb

    Returns:
        (receptor_path, ligand_path) — 둘 다 root 하위로 resolve 됨.

    Raises:
        HTTPException 404: 후보가 하나도 없을 때.
    """
    rcp: Path | None = None
    lgd: Path | None = None

    rcp_patterns = [
        f"{job_id}_{ligand_id}*/**/receptor*.pdb",
        f"{job_id}/**/{ligand_id}*receptor*.pdb",
        f"{job_id}_{ligand_id}*/**/protein*.pdb",
    ]
    lgd_patterns = [
        f"{job_id}_{ligand_id}*/**/{ligand_id}*minimized*.pdb",
        f"{job_id}/**/{ligand_id}*minimized*.pdb",
        f"{job_id}_{ligand_id}*/**/ligand*.pdb",
        f"{job_id}_{ligand_id}*/**/{ligand_id}*.sdf",
    ]

    for pat in rcp_patterns:
        for m in root.glob(pat):
            if m.is_file() and _safe_within(m, root):
                rcp = m.resolve()
                break
        if rcp:
            break

    for pat in lgd_patterns:
        for m in root.glob(pat):
            if m.is_file() and _safe_within(m, root):
                lgd = m.resolve()
                break
        if lgd:
            break

    if rcp is None or lgd is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"receptor 또는 ligand 파일을 자동 탐색하지 못함: "
                f"job_id={job_id}, ligand_id={ligand_id}"
            ),
        )

    return rcp, lgd


def _validate_explicit(p: Path, root: Path, name: str) -> Path:
    """사용자 명시 경로의 안전성 검증."""
    if not _safe_within(p, root):
        raise HTTPException(status_code=400, detail=f"{name} path traversal 차단")
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"{name} 파일이 없음: {p.name}")
    return p.resolve()


@router.post(
    "/screening/{job_id}/ligands/{ligand_id}/smina",
    response_model=SminaRescoreResponse,
)
async def smina_rescore(
    job_id: str,
    ligand_id: str,
    body: SminaRescoreRequest | None = None,
    smina: SminaService = Depends(get_smina),
) -> SminaRescoreResponse:
    """단일 ligand 의 Smina binding affinity 를 재계산하고 DB 에 저장한다.

    경로 결정:
      - body 의 receptor_pdb / ligand_pdb 가 모두 제공되면 사용 (절대 경로,
        af3_output_root 하위 검증 통과 필수).
      - 미제공 시 (job_id, ligand_id) 기반으로 af3_output_root 에서 자동 탐색.

    Returns:
        SminaRescoreResponse: smina_affinity / intramolecular + 사용 경로 + DB 저장 여부.

    Errors:
        400: job_id/ligand_id 형식 또는 path traversal.
        404: 파일 자동 탐색 실패 또는 명시 경로 파일 없음.
        502: smina_score_only 호출 자체가 timeout/MCP 오류.
    """
    _validate_id(job_id, "job_id")
    _validate_id(ligand_id, "ligand_id")

    root: Path = settings.af3_output_root.resolve()

    if body is not None and body.receptor_pdb and body.ligand_pdb:
        rcp = _validate_explicit(Path(body.receptor_pdb), root, "receptor_pdb")
        lgd = _validate_explicit(Path(body.ligand_pdb), root, "ligand_pdb")
        log.info("smina.rescore.explicit", job_id=job_id, ligand_id=ligand_id,
                 receptor=str(rcp), ligand=str(lgd))
    else:
        rcp, lgd = _auto_discover(job_id, ligand_id, root)
        log.info("smina.rescore.auto", job_id=job_id, ligand_id=ligand_id,
                 receptor=str(rcp), ligand=str(lgd))

    result = await smina.score_one(str(rcp), str(lgd), timeout=90)
    if result.error:
        log.warning("smina.rescore.failed", job_id=job_id, ligand_id=ligand_id,
                    error=result.error)
        raise HTTPException(status_code=502, detail=f"smina_error: {result.error}")

    # DB 저장은 best-effort — 실패해도 사용자에게 결과는 반환
    saved = False
    try:
        await smina.save_to_db(job_id, ligand_id, result)
        saved = True
    except Exception as exc:
        log.warning(
            "smina.rescore.db_save_failed",
            job_id=job_id, ligand_id=ligand_id, error=str(exc),
        )

    return SminaRescoreResponse(
        job_id=job_id,
        ligand_id=ligand_id,
        smina_affinity_kcal_mol=result.smina_affinity_kcal_mol,
        intramolecular_kcal_mol=result.intramolecular_kcal_mol,
        receptor_pdb_used=str(rcp),
        ligand_pdb_used=str(lgd),
        saved_to_db=saved,
    )
