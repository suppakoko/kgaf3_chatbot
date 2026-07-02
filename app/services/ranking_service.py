"""랭킹 서비스 — screening_results DB 조회 + 정렬 + 순위 부여.

interface-contracts §1 GET /api/screening/{job_id}/results 권위.
"""

from __future__ import annotations

import json
from typing import Literal

import aiosqlite
import structlog
from pydantic import BaseModel

from app.services.history_service import HistoryService

log = structlog.get_logger("service.ranking")


# ── 결과 행 모델 ──────────────────────────────────────────────────────────────

class ScreeningResultRow(BaseModel):
    """단일 스크리닝 결과 행."""

    job_id: str
    ligand_id: str
    ligand_name: str | None = None
    iptm: float | None = None
    af3_ranking_score: float | None = None
    pae_mean: float | None = None
    e_interaction_kJ: float | None = None
    smina_affinity_kcal_mol: float | None = None
    composite_score: float | None = None
    rank: int | None = None


# ── 랭킹 서비스 ───────────────────────────────────────────────────────────────

_SORT_COLUMN: dict[str, str] = {
    "composite":     "composite_score",
    "smina":         "smina_affinity_kcal_mol",
    "iptm":          "af3_iptm",
    "ranking_score": "af3_ranking_score",
    "e_inter":       "om_e_interaction",
}

_SORT_ORDER: dict[str, str] = {
    "composite":     "DESC",  # 높을수록 좋음
    "smina":         "ASC",   # 낮을수록 (더 음수) 좋음 — 최종 binding affinity
    "iptm":          "DESC",
    "ranking_score": "DESC",  # AF3 best-sample confidence
    "e_inter":       "ASC",
}


class RankingService:
    """screening_results 조회 + top-N 정렬 + rank 부여.

    사용 패턴:
        svc = RankingService(history)
        rows = await svc.compute_ranking(job_id, sort_by="composite", top_n=20)
    """

    def __init__(self, history: HistoryService) -> None:
        self._history = history

    # ── 공개 메서드 ───────────────────────────────────────────────────────────

    async def compute_ranking(
        self,
        job_id: str,
        sort_by: Literal[
            "composite", "smina", "iptm", "ranking_score", "e_inter"
        ] = "smina",
        top_n: int = 20,
        ascending: bool | None = None,
    ) -> list[ScreeningResultRow]:
        """job_id 에 대해 정렬된 top-N 결과를 반환.

        Args:
            job_id: 스크리닝 잡 ID.
            sort_by: 정렬 기준 컬럼 키.
            top_n: 반환할 최대 행 수.
            ascending: None 이면 sort_by 기본 방향 사용.

        Returns:
            rank 가 부여된 ScreeningResultRow 목록.
        """
        col = _SORT_COLUMN.get(sort_by, "composite_score")
        default_order = _SORT_ORDER.get(sort_by, "DESC")

        if ascending is None:
            order = default_order
        else:
            order = "ASC" if ascending else "DESC"

        sql = f"""
            SELECT
                r.job_id,
                r.ligand_id,
                r.af3_iptm,
                r.af3_ranking_score,
                r.af3_mean_pae,
                r.om_e_interaction,
                r.smina_affinity_kcal_mol,
                r.composite_score,
                r.payload_json
            FROM screening_results r
            WHERE r.job_id = ?
            ORDER BY {col} {order} NULLS LAST
            LIMIT ?
        """

        db_path = self._history._db_path
        rows: list[ScreeningResultRow] = []

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (job_id, top_n)) as cur:
                db_rows = await cur.fetchall()

        for rank_idx, row in enumerate(db_rows, start=1):
            ligand_name = _extract_name(row["payload_json"])
            rows.append(
                ScreeningResultRow(
                    job_id=row["job_id"],
                    ligand_id=row["ligand_id"],
                    ligand_name=ligand_name,
                    iptm=_f(row["af3_iptm"]),
                    af3_ranking_score=_f(row["af3_ranking_score"]),
                    pae_mean=_f(row["af3_mean_pae"]),
                    e_interaction_kJ=_f(row["om_e_interaction"]),
                    smina_affinity_kcal_mol=_f(row["smina_affinity_kcal_mol"]),
                    composite_score=_f(row["composite_score"]),
                    rank=rank_idx,
                )
            )

        log.info(
            "ranking.compute_ranking.done",
            job_id=job_id,
            sort_by=sort_by,
            returned=len(rows),
        )
        return rows

    async def get_single(self, job_id: str, ligand_id: str) -> ScreeningResultRow | None:
        """단일 (job_id, ligand_id) 조회.

        Returns:
            ScreeningResultRow 또는 None (존재하지 않으면).
        """
        sql = """
            SELECT
                r.job_id, r.ligand_id,
                r.af3_iptm, r.af3_ranking_score,
                r.af3_mean_pae, r.om_e_interaction,
                r.smina_affinity_kcal_mol, r.composite_score,
                r.rank, r.payload_json
            FROM screening_results r
            WHERE r.job_id = ? AND r.ligand_id = ?
        """
        db_path = self._history._db_path
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (job_id, ligand_id)) as cur:
                row = await cur.fetchone()

        if row is None:
            return None

        return ScreeningResultRow(
            job_id=row["job_id"],
            ligand_id=row["ligand_id"],
            ligand_name=_extract_name(row["payload_json"]),
            iptm=_f(row["af3_iptm"]),
            af3_ranking_score=_f(row["af3_ranking_score"]),
            pae_mean=_f(row["af3_mean_pae"]),
            e_interaction_kJ=_f(row["om_e_interaction"]),
            smina_affinity_kcal_mol=_f(row["smina_affinity_kcal_mol"]),
            composite_score=_f(row["composite_score"]),
            rank=row["rank"],
        )


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

def _f(val: object) -> float | None:
    if val is None:
        return None
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _extract_name(payload_json: str | None) -> str | None:
    """payload_json 에서 ligand name 추출."""
    if not payload_json:
        return None
    try:
        payload = json.loads(payload_json)
        return payload.get("name") or payload.get("ligand_name")
    except Exception:
        return None
