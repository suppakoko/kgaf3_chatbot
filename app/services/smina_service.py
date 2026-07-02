"""Smina 리스코어링 서비스.

pipeline_design.md §6 Stage 5b 권위.
smina_score_only MCP 툴을 통해 receptor + ligand PDB/SDF 를 입력받아
binding affinity (kcal/mol) 를 추출한다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import structlog

from app.models.smina import SminaResult
from app.services.openmm_client import OpenMMMCPClient
from app.services.history_service import HistoryService

log = structlog.get_logger("service.smina")


class SminaService:
    """Stage 5b: Smina score-only rescoring 서비스.

    사용 패턴:
        svc = SminaService(openmm_client, history)
        result = await svc.score_one(receptor_pdb, ligand_pdb)
        await svc.save_to_db(job_id, ligand_id, result)
    """

    def __init__(self, openmm_client: OpenMMMCPClient, history: HistoryService) -> None:
        self._openmm = openmm_client
        self._history = history

    # ── 단일 리간드 스코어링 ──────────────────────────────────────────────────

    async def score_one(
        self,
        receptor_pdb: str,
        ligand_pdb_or_sdf: str,
        timeout: int = 60,
    ) -> SminaResult:
        """smina_score_only MCP 툴 호출 → SminaResult.

        Args:
            receptor_pdb: 수용체 PDB 파일 경로 (문자열).
            ligand_pdb_or_sdf: 리간드 PDB 또는 SDF 파일 경로 (문자열).
            timeout: 타임아웃 (초). 기본 60초.

        Returns:
            SminaResult (affinity, intramolecular energy, 또는 error).
        """
        log.debug(
            "smina.score_one.start",
            receptor=receptor_pdb,
            ligand=ligand_pdb_or_sdf,
        )
        try:
            raw = await asyncio.wait_for(
                self._openmm.call(
                    "smina_score_only",
                    {
                        "receptor_pdb": receptor_pdb,
                        "ligand_pdb_or_sdf": ligand_pdb_or_sdf,
                    },
                ),
                timeout=timeout,
            )
            # openMM_bot smina_tools.py 가 반환하는 정규 키 (smina_ prefix).
            # 호환성: prefix 없는 키도 fallback 으로 시도.
            affinity = _extract_float(raw, "smina_affinity_kcal_mol")
            if affinity is None:
                affinity = _extract_float(raw, "affinity_kcal_mol")
            intra = _extract_float(raw, "intramolecular_energy_kcal_mol")
            if intra is None:
                intra = _extract_float(raw, "intramolecular_kcal_mol")
            log.debug(
                "smina.score_one.done",
                affinity=affinity,
                intramolecular=intra,
            )
            return SminaResult(
                smina_affinity_kcal_mol=affinity,
                intramolecular_kcal_mol=intra,
            )
        except asyncio.TimeoutError:
            err = f"smina_score_only timed out after {timeout}s"
            log.warning("smina.score_one.timeout", receptor=receptor_pdb, ligand=ligand_pdb_or_sdf)
            return SminaResult(error=err)
        except Exception as exc:
            err = str(exc)
            log.error("smina.score_one.error", error=err, receptor=receptor_pdb, ligand=ligand_pdb_or_sdf)
            return SminaResult(error=err)

    # ── 단일 리간드 minimize (smina --minimize) ──────────────────────────────

    async def minimize_one(
        self,
        receptor_pdb: str,
        ligand_pdb_or_sdf: str,
        out_path: str | None = None,
        scoring: str = "vinardo",
        minimize_iters: int = 0,
        timeout: int = 180,
    ) -> SminaResult:
        """smina_minimize MCP 툴 호출 → minimized affinity SminaResult.

        OpenMM minimization 을 대체. minimized affinity 를 canonical
        smina_affinity_kcal_mol 에도 동일 기록(설계 §3.4 B-2 컬럼 규약).

        Args:
            receptor_pdb: 수용체 PDB 경로.
            ligand_pdb_or_sdf: 리간드 시작 포즈(보통 AF3 holo) 경로.
            out_path: minimized pose 출력(SDF) 경로. None 이면 서버가 ligand 옆에 생성.
            scoring: smina scoring function ("vinardo" | "vina").
            minimize_iters: steepest descent 반복 수. 0=수렴까지.
            timeout: 타임아웃(초). 기본 180초.

        Returns:
            SminaResult (minimized affinity 또는 error).
        """
        args: dict = {
            "receptor_pdb": receptor_pdb,
            "ligand_pdb_or_sdf": ligand_pdb_or_sdf,
            "scoring": scoring,
            "minimize_iters": minimize_iters,
        }
        if out_path:
            args["out_path"] = out_path
        try:
            raw = await asyncio.wait_for(
                self._openmm.call("smina_minimize", args),
                timeout=timeout,
            )
            aff = _extract_float(raw, "minimized_affinity_kcal_mol")
            intra = _extract_float(raw, "intramolecular_energy_kcal_mol")
            if intra is None:
                intra = _extract_float(raw, "intramolecular_kcal_mol")
            pose = raw.get("minimized_pose_path") if isinstance(raw, dict) else None
            sf = raw.get("scoring_function") if isinstance(raw, dict) else None
            log.debug("smina.minimize_one.done", affinity=aff, pose=pose)
            return SminaResult(
                # canonical 최종 점수 컬럼 = minimized affinity (B-2 규약)
                smina_affinity_kcal_mol=aff,
                minimized_affinity_kcal_mol=aff,
                intramolecular_kcal_mol=intra,
                minimized_pose_path=pose,
                scoring_function=sf,
            )
        except asyncio.TimeoutError:
            err = f"smina_minimize timed out after {timeout}s"
            log.warning("smina.minimize_one.timeout", receptor=receptor_pdb, ligand=ligand_pdb_or_sdf)
            return SminaResult(error=err)
        except Exception as exc:
            err = str(exc)
            log.error("smina.minimize_one.error", error=err, receptor=receptor_pdb, ligand=ligand_pdb_or_sdf)
            return SminaResult(error=err)

    # ── 배치 스코어링 ─────────────────────────────────────────────────────────

    async def score_batch(
        self,
        receptor_pdb: str,
        ligand_files: list[str],
    ) -> list[SminaResult]:
        """여러 리간드에 대한 smina score_one 병렬 실행.

        Args:
            receptor_pdb: 공통 수용체 PDB 경로.
            ligand_files: 리간드 파일 경로 목록.

        Returns:
            SminaResult 리스트 (입력 순서 유지).
        """
        log.info("smina.score_batch.start", n_ligands=len(ligand_files), receptor=receptor_pdb)
        tasks = [self.score_one(receptor_pdb, lig) for lig in ligand_files]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        n_errors = sum(1 for r in results if r.error is not None)
        log.info(
            "smina.score_batch.done",
            n_ligands=len(ligand_files),
            n_errors=n_errors,
        )
        return list(results)

    # ── DB 저장 ───────────────────────────────────────────────────────────────

    async def save_to_db(
        self,
        job_id: str,
        ligand_id: str,
        result: SminaResult,
    ) -> None:
        """SminaResult 를 screening_results 테이블에 upsert.

        smina_affinity_kcal_mol / smina_intramolecular_kcal_mol 컬럼 갱신.
        """
        db_path = self._history._db_path
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                UPDATE screening_results
                SET smina_affinity_kcal_mol = ?,
                    smina_intramolecular_kcal_mol = ?,
                    smina_minimized_affinity_kcal_mol = ?
                WHERE job_id = ? AND ligand_id = ?
                """,
                (
                    result.smina_affinity_kcal_mol,
                    result.intramolecular_kcal_mol,
                    result.minimized_affinity_kcal_mol,
                    job_id,
                    ligand_id,
                ),
            )
            await db.commit()
        log.debug(
            "smina.save_to_db.done",
            job_id=job_id,
            ligand_id=ligand_id,
            affinity=result.smina_affinity_kcal_mol,
        )


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

def _extract_float(raw: dict, key: str) -> float | None:
    """MCP 응답 dict 에서 float 값 추출. 없거나 변환 실패 시 None."""
    val = raw.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
