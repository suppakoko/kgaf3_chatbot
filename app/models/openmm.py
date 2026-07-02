"""OpenMM rescoring 결과 Pydantic 모델.

pipeline_design.md §2 Stage 5 권위.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class OpenMMResult(BaseModel):
    """단일 리간드에 대한 OpenMM minimization + interaction energy 결과."""

    ligand_id: str
    minimization_status: Literal["converged", "max_iter", "failed", "skipped"]
    energy_before_kJ: float | None = None
    energy_after_kJ: float | None = None
    e_interaction_kJ: float | None = None
    e_inter_lj_kJ: float | None = None
    e_inter_coul_kJ: float | None = None
    rmsd_ligand_A: float | None = None
    minimized_pdb_path: str | None = None
    error_message: str | None = None


class OpenMMRescoreResult(BaseModel):
    """Stage 5 전체 결과 (모든 리간드 집계)."""

    screening_id: str
    n_succeeded: int
    n_failed: int
    n_skipped: int
    per_ligand: list[OpenMMResult]


class RankingResult(BaseModel):
    """Stage 6 랭킹 결과."""

    screening_id: str
    n_total: int
    n_ranked: int
    top_n: list  # ScreeningResultRow from ranking_service
    weights_used: dict
