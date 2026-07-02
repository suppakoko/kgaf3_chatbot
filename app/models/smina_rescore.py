"""Smina rescore REST endpoint 모델.

interface-contracts §2 (Phase 2 신규 router 의존) 권위.
요청은 모두 옵션(서버가 job_id/ligand_id 로 자동 탐색). 응답은 SminaResult 핵심 필드 + 사용된 경로.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SminaRescoreRequest(BaseModel):
    """POST /api/screening/{job_id}/ligands/{ligand_id}/smina 요청 body.

    둘 다 None 이면 서버가 af3_output_root 에서 자동 탐색.
    명시 경로는 디버깅용 — 경로 탈출 방어 후 사용.
    """

    receptor_pdb: str | None = Field(
        default=None,
        description="수용체 PDB 파일의 절대 경로 (선택). 미지정 시 자동 탐색.",
    )
    ligand_pdb: str | None = Field(
        default=None,
        description="리간드 PDB/SDF 파일의 절대 경로 (선택). 미지정 시 자동 탐색.",
    )


class SminaRescoreResponse(BaseModel):
    """POST /api/screening/{job_id}/ligands/{ligand_id}/smina 응답."""

    ok: bool = True
    job_id: str
    ligand_id: str
    smina_affinity_kcal_mol: float | None = None
    intramolecular_kcal_mol: float | None = None
    receptor_pdb_used: str
    ligand_pdb_used: str
    saved_to_db: bool
