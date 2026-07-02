"""라이브러리 / 리간드 Pydantic 모델.

backend_plan.md §4 권위.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LigandEntry(BaseModel):
    """단일 리간드 엔트리 (파싱·정규화 완료 상태)."""

    ligand_id: str
    source_index: int
    name: str | None
    smiles: str
    inchi_key: str
    mw: float
    heavy_atoms: int
    metadata: dict = Field(default_factory=dict)


class Library(BaseModel):
    """파싱된 리간드 라이브러리 (SDF 또는 SMI 파일 1개 기준)."""

    library_id: str
    source_filename: str
    n_entries: int
    n_unique: int
    n_duplicates: int
    entries: list[LigandEntry]
