"""Target 라우터 — UniProt/PDB/이름/직접 서열 해석.

POST /api/target/resolve  — TargetInput → TargetPrepResult wrap

interface-contracts §2 (Phase 2 신규 router) 권위.
TargetService.resolve() 우선순위: sequence > uniprot_id > pdb_id > protein_name.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.models.target import TargetResolveResponse
from app.routers._deps import get_target
from app.services.target_service import TargetInput, TargetService
from app.utils.log import get_logger
from app.utils.validators import ValidationError

log = get_logger("router.target")

router = APIRouter(prefix="/api", tags=["target"])


@router.post("/target/resolve", response_model=TargetResolveResponse)
async def resolve_target(
    body: TargetInput,
    target: TargetService = Depends(get_target),
) -> TargetResolveResponse:
    """단백질 타겟을 4가지 경로 중 하나로 해석한다.

    Body 예시:
        {"uniprot_id":"Q13464"}
        {"pdb_id":"4WB6"}
        {"protein_name":"ROCK1"}
        {"sequence":"MST...END"}

    우선순위(TargetService.resolve): sequence > uniprot_id > pdb_id > protein_name.
    여러 필드 동시 제공 시 우선순위에 따라 하나만 사용된다.

    Returns:
        TargetResolveResponse: ok + TargetPrepResult (target_name, sequence,
        sequence_length, source, json_template, warnings).

    Errors:
        400: ValidationError (서열 검증 실패) 또는 외부 API 실패.
        422: Pydantic — 모든 필드가 None (TargetInput._at_least_one).
    """
    log.info(
        "target.resolve.request",
        has_sequence=bool(body.sequence),
        has_uniprot=bool(body.uniprot_id),
        has_pdb=bool(body.pdb_id),
        has_name=bool(body.protein_name),
    )

    try:
        result = await target.resolve(body)
    except ValidationError as exc:
        log.warning("target.resolve.validation_error", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        # 외부 API(UniProt/PDB) 실패 — _fetch_with_retry 의 최종 실패
        log.warning("target.resolve.external_error", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))

    return TargetResolveResponse(target=result)
