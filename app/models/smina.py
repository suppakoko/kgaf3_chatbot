"""Smina rescoring 결과 모델.

pipeline_design.md §6 / interface-contracts §2 권위.
"""

from __future__ import annotations

from pydantic import BaseModel


class SminaResult(BaseModel):
    """smina_score_only / smina_minimize MCP 툴 호출 결과.

    smina_affinity_kcal_mol 은 canonical 최종 점수 컬럼에 매핑되는 값이다.
    --minimize 모드에서는 minimized_affinity_kcal_mol 과 동일 값을 담는다.
    """

    ligand_id: str | None = None
    smina_affinity_kcal_mol: float | None = None
    intramolecular_kcal_mol: float | None = None
    # --minimize 모드 전용 (provenance)
    minimized_affinity_kcal_mol: float | None = None
    minimized_pose_path: str | None = None
    scoring_function: str | None = None
    error: str | None = None
