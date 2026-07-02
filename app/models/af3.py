"""AF3 결과 Pydantic 모델.

pipeline_design.md §2 Stage 3, Stage 4 권위.
mcp_integration.md §3.3 참조.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AF3Result(BaseModel):
    """단일 리간드에 대한 AF3 예측 결과."""

    ligand_id: str
    af3_job_id: str
    cif_path: str | None = None
    # summary_confidences 집계값 (summary_confidences.json)
    iptm: float | None = Field(default=None, ge=0, le=1)
    ptm: float | None = None
    ranking_score: float | None = None
    fraction_disordered: float | None = None
    has_clash: bool | None = None
    # 인터페이스 PAE (confidences.json cross-block — af3_parser 계산)
    pae_min_interface: float | None = None
    pae_mean_interface: float | None = None
    # mean_plddt: AF3 v2 per-atom 평균 (summary 또는 confidences.json)
    mean_plddt: float | None = None
    # 원본 summary dict (디버깅/downstream 용)
    raw_summary: dict = Field(default_factory=dict)


class AF3BatchResult(BaseModel):
    """Stage 3 AF3 holo batch 전체 결과."""

    screening_id: str
    n_attempted: int
    n_succeeded: int
    n_failed: int
    # ligand_id → AF3Result 매핑
    results: dict[str, AF3Result] = Field(default_factory=dict)
    failed_ligands: list[dict] = Field(default_factory=list)


class AF3ParseResult(BaseModel):
    """Stage 4 AF3 결과 파싱 완료 요약."""

    screening_id: str
    parsed_count: int
    failed_count: int
    per_ligand: list[AF3Result] = Field(default_factory=list)
