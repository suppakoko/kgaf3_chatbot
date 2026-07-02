"""스크리닝 오케스트레이터 — 7-stage VS 파이프라인.

pipeline_design.md 권위.

Stage 1: Ingest (library DB 확인 + LigandEntry 목록 반환)
Stage 2: Target (타겟 단백질 준비 — UniProt / PDB / 직접 서열)
Stage 3: AF3 (holo 예측)
Stage 4: Parse (AF3 결과 파싱)
Stage 5: OpenMM (rescoring)
Stage 5b: Smina (score-only, Phase 2 partial 구현)
Stage 6: Rank (composite scoring + sort)
Stage 7: LLM (결과 해석)
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable, TYPE_CHECKING

import aiosqlite
import structlog
from pydantic import BaseModel

from app.models.af3 import AF3BatchResult, AF3ParseResult, AF3Result
from app.models.library import LigandEntry
from app.models.llm import ScreeningSummary
from app.models.openmm import OpenMMResult, OpenMMRescoreResult, RankingResult
from app.models.smina import SminaResult
from app.services.af3_client import AF3MCPClient, MCPError
from app.services.af3_parser import parse_af3_folder
from app.services.history_service import HistoryService
from app.services.library_service import LibraryService
from app.services.openmm_client import OpenMMMCPClient
from app.services.ranking_service import RankingService, ScreeningResultRow
from app.services.scoring_service import ScoringService
from app.services.smina_service import SminaService
from app.services.target_service import TargetInput, TargetPrepResult, TargetService

if TYPE_CHECKING:
    from app.services.llm_service import LLMService

# 스크리닝 요약 프롬프트 경로
_PROMPT_DIR = Path(__file__).parent.parent / "prompts"

# GPU 공유 — OpenMM CUDA 직렬 실행 보장
_OPENMM_SEMAPHORE = asyncio.Semaphore(1)


def _load_prompt(name: str) -> str:
    """prompts/ 디렉토리에서 텍스트 파일 로드."""
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")

log = structlog.get_logger("service.screening")


# ── 공유 데이터 모델 ───────────────────────────────────────────────────────────

class StageResult(BaseModel):
    """단일 스테이지 실행 결과 (성공/실패 공통)."""

    stage: str
    ok: bool
    message: str = ""
    data: dict[str, Any] = {}


@dataclass
class ScreeningJobContext:
    """스크리닝 파이프라인 실행 컨텍스트 (스테이지 간 공유)."""

    job_id: str
    session_id: str | None
    library_id: str
    target_input: TargetInput
    config: dict[str, Any] = field(default_factory=dict)
    ws_broadcast: Callable[[dict], Awaitable[None]] | Callable[[dict], None] | None = None

    # Stage 5 OpenMM 옵션
    af3_folder: str | None = None
    output_dir: str | None = None
    ligand_ff: str = "gaff2"
    staged: bool = True
    platform: str = "CUDA"

    # Stage 6 랭킹 옵션
    # 기본 정렬: smina affinity 오름차순 (binding affinity = 최종 score)
    # AF3 ranking_score, composite, iptm 등은 명시 요청 시 사용.
    sort_by: str = "smina"
    top_n: int = 20


# ── 서비스 ────────────────────────────────────────────────────────────────────

class ScreeningService:
    """VS 파이프라인 오케스트레이터.

    Phase 2: Stage 1–7 전체 구현 (Stage 3/4/7 Agent A/B/D 담당, Stage 5/6 이 모듈).
    """

    def __init__(
        self,
        history: HistoryService,
        smina: SminaService,
        library: LibraryService,
        target: TargetService,
        af3: AF3MCPClient | None = None,
        llm: "LLMService | None" = None,
        openmm: OpenMMMCPClient | None = None,
        scoring: ScoringService | None = None,
        ranking: RankingService | None = None,
    ) -> None:
        self._history = history
        self._smina = smina
        self._library = library
        self._target = target
        self._af3 = af3
        self._llm = llm
        self._openmm = openmm
        self._scoring = scoring
        self._ranking = ranking

    # ── Stage 1 — Library Ingest ──────────────────────────────────────────────

    async def _stage_1_ingest(
        self,
        job: ScreeningJobContext,
    ) -> StageResult:
        """Stage 1: Library 가 이미 DB 에 저장되어 있는지 확인 후 LigandEntry list 반환.

        job.library_id 로 LibraryService.get() 호출 → entries 검증.
        WS broadcast: screening.stage_change {to=ingest} + screening.ligand_started 카운트.
        """
        log.info(
            "screening.stage_1.start",
            job_id=job.job_id,
            library_id=job.library_id,
        )

        # WS: INIT → INGEST 전환
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.stage_change",
                "job_id": job.job_id,
                "from_stage": "INIT",
                "to_stage": "INGEST",
            },
        )

        lib = await self._library.get(job.library_id)
        if lib is None:
            msg = f"library_id={job.library_id} 가 DB 에 존재하지 않습니다."
            log.error("screening.stage_1.library_not_found", job_id=job.job_id, library_id=job.library_id)
            return StageResult(stage="ingest", ok=False, message=msg)

        entries: list[LigandEntry] = lib.entries
        n_entries = len(entries)

        if n_entries == 0:
            msg = f"library_id={job.library_id} 에 유효한 리간드가 없습니다."
            log.warning("screening.stage_1.empty_library", job_id=job.job_id)
            return StageResult(stage="ingest", ok=False, message=msg)

        # WS: 리간드 수 브로드캐스트
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.ligand_count",
                "job_id": job.job_id,
                "n_ligands": n_entries,
                "library_id": job.library_id,
                "source_filename": lib.source_filename,
            },
        )

        log.info(
            "screening.stage_1.done",
            job_id=job.job_id,
            library_id=job.library_id,
            n_ligands=n_entries,
        )

        return StageResult(
            stage="ingest",
            ok=True,
            message=f"{n_entries}개 리간드 로드 완료 (library_id={job.library_id})",
            data={
                "n_ligands": n_entries,
                "library_id": job.library_id,
                "source_filename": lib.source_filename,
                "entries": [e.model_dump() for e in entries],
            },
        )

    # ── Stage 2 — Target Prep ─────────────────────────────────────────────────

    async def _stage_2_target(
        self,
        job: ScreeningJobContext,
        target_input: TargetInput,
    ) -> TargetPrepResult:
        """Stage 2: 단백질 정보 검증 → 서열 + AF3 input JSON template.

        target_input 우선순위: sequence > uniprot_id > pdb_id > protein_name.
        UniProt REST API 호출 시 in-process LRU 캐시 사용.
        """
        log.info(
            "screening.stage_2.start",
            job_id=job.job_id,
            has_sequence=bool(target_input.sequence),
            has_uniprot=bool(target_input.uniprot_id),
            has_pdb=bool(target_input.pdb_id),
            has_name=bool(target_input.protein_name),
        )

        # WS: INGEST → TARGET 전환
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.stage_change",
                "job_id": job.job_id,
                "from_stage": "INGEST",
                "to_stage": "TARGET",
            },
        )

        result = await self._target.resolve(target_input)

        # WS: 타겟 해석 완료
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.target_resolved",
                "job_id": job.job_id,
                "target_name": result.target_name,
                "sequence_length": result.sequence_length,
                "source": result.source,
                "uniprot_id": result.uniprot_id,
                "warnings": result.warnings,
            },
        )

        log.info(
            "screening.stage_2.done",
            job_id=job.job_id,
            target_name=result.target_name,
            sequence_length=result.sequence_length,
            source=result.source,
        )

        return result

    # ── Stage 3 — AF3 holo Batch ──────────────────────────────────────────────

    async def _stage_3_af3(
        self,
        job: ScreeningJobContext,
        target: TargetPrepResult,
        ligands: list[LigandEntry],
    ) -> AF3BatchResult:
        """Stage 3: 리간드별 AF3 holo prediction.

        GPU 단독 점유 → asyncio.Semaphore(1) 로 직렬 실행.
        Per-ligand 실패 격리: try/except → af3_failed 마킹 후 continue.
        WS broadcast: screening.ligand_af3_done {ligand_id, iptm, mean_pae, mean_plddt}.
        """
        screening_id = job.job_id
        ws = job.ws_broadcast
        af3 = self._af3

        if af3 is None:
            raise RuntimeError("AF3MCPClient 가 주입되지 않았습니다 (af3=None).")

        log.info(
            "screening.stage_3.start",
            screening_id=screening_id,
            n_ligands=len(ligands),
        )

        # WS: TARGET → AF3 전환
        await _broadcast(
            ws,
            {
                "event": "screening.stage_change",
                "job_id": screening_id,
                "from_stage": "TARGET",
                "to_stage": "AF3",
            },
        )

        results: dict[str, AF3Result] = {}
        failed_ligands: list[dict] = []
        n_attempted = len(ligands)

        # ── Batch 모드 — 단백질 MSA 1회 + N inference (MSA 재사용) ──────────────
        # ligand 인덱스 ↔ ligand_id 매핑 (결과 매칭용)
        ligand_id_by_index: dict[int, str] = {i: lig.ligand_id for i, lig in enumerate(ligands)}

        # 모든 ligand 의 시작을 즉시 알림 (UI 진행 패널 호환)
        for lig_index, lig in enumerate(ligands):
            await _broadcast(
                ws,
                {
                    "event": "screening.ligand_af3_started",
                    "job_id": screening_id,
                    "ligand_id": lig.ligand_id,
                    "ligand_index": lig_index,
                    "total_ligands": n_attempted,
                    "sub_stage": "queued",
                },
            )

        try:
            batch_id = await af3.create_batch_job(
                protein_name=target.target_name or "user_target",
                target_seq=target.sequence,
                ligands=[(lig.smiles, lig.ligand_id) for lig in ligands],
                num_seeds=1,
            )
            log.info(
                "screening.stage_3.batch_created",
                screening_id=screening_id,
                batch_id=batch_id,
                n_ligands=n_attempted,
            )

            # Polling 콜백 — completed_count 변화 시 ligand_af3_done 송출,
            # 그 외에는 heartbeat (sub_stage, msa_reused 등) 만 송출.
            poll_state = {"prev_completed": 0, "logged_msa_reuse": False}

            async def _batch_poll_cb(st: dict, elapsed_s: float) -> None:
                msa_reused = bool(st.get("msa_reused"))
                completed_count = int(st.get("completed_count", 0) or 0)
                total_count = int(st.get("total_count", n_attempted) or n_attempted)
                current_phase = st.get("current_phase") or st.get("status") or "running"
                ligand_progress = st.get("ligand_progress") or []

                # MSA 재사용 첫 감지 시 1회만 명시 로그
                if msa_reused and not poll_state["logged_msa_reuse"]:
                    poll_state["logged_msa_reuse"] = True
                    log.info(
                        "screening.stage_3.msa_reused",
                        screening_id=screening_id,
                        batch_id=batch_id,
                        reference_job_id=st.get("reference_job_id"),
                    )

                # 새로 완료된 ligand 마다 done 이벤트
                for done_idx in range(poll_state["prev_completed"], completed_count):
                    lig_id = ligand_id_by_index.get(done_idx, f"ligand_{done_idx}")
                    # ligand_progress 에서 해당 ligand 의 job_id / iptm 메타 추출
                    lig_meta = next(
                        (lp for lp in ligand_progress if lp.get("index") == done_idx),
                        {},
                    )
                    await _broadcast(
                        ws,
                        {
                            "event": "screening.ligand_af3_done",
                            "job_id": screening_id,
                            "ligand_id": lig_id,
                            "ligand_index": done_idx,
                            "total_ligands": total_count,
                            "af3_job_id": lig_meta.get("job_id"),
                            "msa_reused": msa_reused,
                        },
                    )
                poll_state["prev_completed"] = completed_count

                # 전체 batch heartbeat (UI 의 AF3 stage 가 살아있음 표시)
                await _broadcast(
                    ws,
                    {
                        "event": "screening.ligand_af3_progress",
                        "job_id": screening_id,
                        "ligand_index": completed_count,
                        "total_ligands": total_count,
                        "sub_stage": str(current_phase),
                        "elapsed_s": round(elapsed_s, 1),
                        "msa_reused": msa_reused,
                        "raw_status": st.get("status"),
                    },
                )

            await af3.wait_batch_done(
                batch_id,
                poll_s=30.0,
                timeout_s=14400.0,
                on_poll=_batch_poll_cb,
            )

            # 결과 수집 — get_batch_results 로 ligand별 메타, 그 후 ligand 별 상세
            batch_res = await af3.get_batch_results(batch_id)
            ligands_meta = batch_res.get("ligands", []) or []
            for lig_meta in ligands_meta:
                lig_index = int(lig_meta.get("index", -1))
                if lig_index < 0 or lig_index >= n_attempted:
                    continue
                lig_id = ligand_id_by_index[lig_index]
                status = str(lig_meta.get("status", "unknown"))

                if status != "completed":
                    err_msg = lig_meta.get("error_message") or f"AF3 status={status}"
                    failed_ligands.append({
                        "ligand_id": lig_id,
                        "af3_job_id": lig_meta.get("job_id"),
                        "error": err_msg,
                        "status": "af3_failed",
                    })
                    await _broadcast(
                        ws,
                        {
                            "event": "screening.ligand_af3_failed",
                            "job_id": screening_id,
                            "ligand_id": lig_id,
                            "error": err_msg,
                        },
                    )
                    continue

                # 상세 결과 조회 (cif_path / summary_confidences)
                try:
                    detail = await af3.get_batch_ligand_result(batch_id, lig_index)
                except Exception as exc:
                    err_msg = f"get_batch_ligand_result failed: {exc}"
                    log.warning(
                        "screening.stage_3.ligand_detail_failed",
                        screening_id=screening_id,
                        ligand_id=lig_id,
                        error=err_msg,
                    )
                    failed_ligands.append({
                        "ligand_id": lig_id,
                        "af3_job_id": lig_meta.get("job_id"),
                        "error": err_msg,
                        "status": "af3_failed",
                    })
                    continue

                if isinstance(detail, dict) and "error" in detail and "summary_confidences" not in detail:
                    err_msg = str(detail["error"])
                    failed_ligands.append({
                        "ligand_id": lig_id,
                        "af3_job_id": lig_meta.get("job_id"),
                        "error": err_msg,
                        "status": "af3_failed",
                    })
                    continue

                sc = detail.get("summary_confidences") or {}
                if isinstance(sc, str):
                    try:
                        sc = json.loads(sc)
                    except Exception:
                        sc = {}

                structure_paths = detail.get("structure_paths") or []
                cif_path = structure_paths[0] if structure_paths else None

                af3_result = AF3Result(
                    ligand_id=lig_id,
                    af3_job_id=lig_meta.get("job_id"),
                    cif_path=cif_path,
                    iptm=sc.get("iptm"),
                    ptm=sc.get("ptm"),
                    ranking_score=sc.get("ranking_score"),
                    fraction_disordered=sc.get("fraction_disordered"),
                    has_clash=sc.get("has_clash"),
                    mean_plddt=sc.get("mean_plddt"),
                    raw_summary=sc,
                )
                results[lig_id] = af3_result

                log.info(
                    "screening.stage_3.ligand_done",
                    screening_id=screening_id,
                    ligand_id=lig_id,
                    af3_job_id=af3_result.af3_job_id,
                    iptm=af3_result.iptm,
                )

        except (MCPError, Exception) as exc:
            # Batch 자체 실패 — 모든 ligand 를 failed 로 마킹
            err_msg = str(exc)
            log.error(
                "screening.stage_3.batch_failed",
                screening_id=screening_id,
                error=err_msg,
                exc_info=True,
            )
            for lig_index, lig in enumerate(ligands):
                if lig.ligand_id not in results:
                    failed_ligands.append({
                        "ligand_id": lig.ligand_id,
                        "af3_job_id": None,
                        "error": err_msg,
                        "status": "af3_failed",
                    })
                    await _broadcast(
                        ws,
                        {
                            "event": "screening.ligand_af3_failed",
                            "job_id": screening_id,
                            "ligand_id": lig.ligand_id,
                            "error": err_msg,
                        },
                    )

        n_succeeded = len(results)
        n_failed = len(failed_ligands)

        log.info(
            "screening.stage_3.done",
            screening_id=screening_id,
            n_attempted=n_attempted,
            n_succeeded=n_succeeded,
            n_failed=n_failed,
        )

        return AF3BatchResult(
            screening_id=screening_id,
            n_attempted=n_attempted,
            n_succeeded=n_succeeded,
            n_failed=n_failed,
            results=results,
            failed_ligands=failed_ligands,
        )

    # ── Stage 4 — AF3 Result Parse ────────────────────────────────────────────

    async def _stage_4_parse(
        self,
        job: ScreeningJobContext,
        af3_batch: AF3BatchResult,
        entries: list[LigandEntry] | None = None,
    ) -> AF3ParseResult:
        """Stage 4: AF3 결과 폴더 파싱 → AF3Result 갱신 (interface PAE 추가).

        per-ligand:
          - af3_batch.results[ligand_id].cif_path 로 폴더 특정
          - summary_confidences.json → iptm, ptm, ranking_score, mean_plddt 재확인
          - confidences.json → protein-ligand interface PAE (cross-block 추출)
          - AF3Result 갱신 (pae_min_interface, pae_mean_interface)

        실패한 ligand (af3_batch.failed_ligands) 는 건너뜀.
        파싱 실패 → 로그 후 continue (stage 3 값 유지).
        """
        screening_id = af3_batch.screening_id
        ws = job.ws_broadcast

        log.info(
            "screening.stage_4.start",
            screening_id=screening_id,
            n_results=len(af3_batch.results),
        )

        # WS: AF3 → PARSE 전환
        await _broadcast(
            ws,
            {
                "event": "screening.stage_change",
                "job_id": screening_id,
                "from_stage": "AF3",
                "to_stage": "PARSE",
            },
        )

        per_ligand: list[AF3Result] = []
        failed_count = 0

        # AF3 결과 디스크 fallback 검색용 root
        from app.config import settings as _settings
        af3_root = pathlib.Path(_settings.af3_output_root)

        for ligand_id, af3_res in af3_batch.results.items():
            # cif_path 로 폴더 결정
            folder: pathlib.Path | None = None
            if af3_res.cif_path:
                folder = pathlib.Path(af3_res.cif_path).parent

            # Fallback: 디스크에서 {screening_id}_{ligand_id} 패턴 폴더 직접 검색
            # (Stage 3 가 PermissionError 등으로 cif_path 를 못 받아도 디스크에는 결과가 있음)
            if folder is None or not folder.exists():
                candidates = list(af3_root.glob(f"{screening_id}_{ligand_id}*"))
                if candidates:
                    folder = candidates[0]
                    log.info(
                        "screening.stage_4.disk_fallback",
                        screening_id=screening_id,
                        ligand_id=ligand_id,
                        folder=str(folder),
                    )

            if folder is None or not folder.exists():
                log.warning(
                    "screening.stage_4.no_folder",
                    screening_id=screening_id,
                    ligand_id=ligand_id,
                    cif_path=af3_res.cif_path,
                )
                per_ligand.append(af3_res)
                continue

            # cif_path 가 None 이면 폴더에서 직접 발견
            if not af3_res.cif_path:
                cif_files = list(folder.glob("*.cif"))
                if cif_files:
                    af3_res = af3_res.model_copy(update={"cif_path": str(cif_files[0])})

            try:
                parsed = parse_af3_folder(
                    folder=folder,
                    ligand_id=ligand_id,
                    af3_job_id=af3_res.af3_job_id,
                    protein_chain="A",
                    ligand_chain="B",
                )

                if parsed.get("error"):
                    log.warning(
                        "screening.stage_4.parse_error",
                        screening_id=screening_id,
                        ligand_id=ligand_id,
                        error=parsed["error"],
                    )
                    per_ligand.append(af3_res)
                    failed_count += 1
                    continue

                # AF3Result 갱신 (interface PAE, cif_path 보완)
                updated = AF3Result(
                    ligand_id=ligand_id,
                    af3_job_id=af3_res.af3_job_id,
                    cif_path=parsed.get("cif_path") or af3_res.cif_path,
                    iptm=(
                        parsed.get("iptm")
                        if parsed.get("iptm") is not None
                        else af3_res.iptm
                    ),
                    ptm=(
                        parsed.get("ptm")
                        if parsed.get("ptm") is not None
                        else af3_res.ptm
                    ),
                    ranking_score=(
                        parsed.get("ranking_score")
                        if parsed.get("ranking_score") is not None
                        else af3_res.ranking_score
                    ),
                    fraction_disordered=parsed.get("fraction_disordered"),
                    has_clash=parsed.get("has_clash"),
                    pae_min_interface=parsed.get("pae_min_interface"),
                    pae_mean_interface=parsed.get("pae_mean_interface"),
                    mean_plddt=(
                        parsed.get("mean_plddt")
                        if parsed.get("mean_plddt") is not None
                        else af3_res.mean_plddt
                    ),
                    raw_summary=parsed.get("raw_summary") or af3_res.raw_summary,
                )
                per_ligand.append(updated)

                log.info(
                    "screening.stage_4.ligand_parsed",
                    screening_id=screening_id,
                    ligand_id=ligand_id,
                    iptm=updated.iptm,
                    pae_min_interface=updated.pae_min_interface,
                    pae_mean_interface=updated.pae_mean_interface,
                )

            except Exception as exc:
                log.warning(
                    "screening.stage_4.ligand_exception",
                    screening_id=screening_id,
                    ligand_id=ligand_id,
                    error=str(exc),
                )
                per_ligand.append(af3_res)
                failed_count += 1
                continue

        parsed_count = len(per_ligand) - failed_count

        # ── DB persist: 모든 ligand 의 AF3 결과를 screening_results 에 INSERT ──
        # Stage 5 (OpenMM), Stage 6 (Ranking) 은 UPDATE 만 하므로 row 가 미리 존재해야 함.
        # payload_json: ligand_name + smiles 같은 메타데이터 (ranking_service _extract_name 이 사용).
        lig_id_to_meta: dict[str, dict] = {}
        if entries:
            for e in entries:
                meta: dict = {}
                if getattr(e, "name", None):
                    meta["name"] = e.name
                if getattr(e, "smiles", None):
                    meta["smiles"] = e.smiles
                if meta:
                    lig_id_to_meta[e.ligand_id] = meta
        try:
            import aiosqlite
            async with aiosqlite.connect(self._history._db_path) as db:
                for af3 in per_ligand:
                    cif_folder = ""
                    if af3.cif_path:
                        cif_folder = str(pathlib.Path(af3.cif_path).parent)
                    meta = lig_id_to_meta.get(af3.ligand_id)
                    payload_json_str = json.dumps(meta, ensure_ascii=False) if meta else None
                    await db.execute(
                        """
                        INSERT OR REPLACE INTO screening_results
                            (job_id, ligand_id, af3_iptm, af3_ranking_score,
                             af3_mean_pae, af3_mean_plddt,
                             af3_folder, om_e_interaction, om_e_complex,
                             composite_score, rank, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
                        """,
                        (
                            screening_id,
                            af3.ligand_id,
                            af3.iptm,
                            af3.ranking_score,
                            af3.pae_mean_interface,  # canonical: interface PAE used downstream
                            af3.mean_plddt,
                            cif_folder,
                            payload_json_str,
                        ),
                    )
                await db.commit()
            log.info(
                "screening.stage_4.db_persisted",
                screening_id=screening_id,
                rows=len(per_ligand),
            )
        except Exception as db_exc:
            log.error(
                "screening.stage_4.db_persist_failed",
                screening_id=screening_id,
                error=str(db_exc),
                exc_info=True,
            )

        log.info(
            "screening.stage_4.done",
            screening_id=screening_id,
            parsed_count=parsed_count,
            failed_count=failed_count,
        )

        # WS: PARSE → OPENMM 전환 예고
        await _broadcast(
            ws,
            {
                "event": "screening.stage_change",
                "job_id": screening_id,
                "from_stage": "PARSE",
                "to_stage": "OPENMM",
                "parsed_count": parsed_count,
                "failed_count": failed_count,
            },
        )

        return AF3ParseResult(
            screening_id=screening_id,
            parsed_count=parsed_count,
            failed_count=failed_count,
            per_ligand=per_ligand,
        )

    # ── Stage 5 — OpenMM Rescoring ───────────────────────────────────────────

    async def _stage_5_openmm(
        self,
        job: ScreeningJobContext,
        af3_parsed: AF3ParseResult,
    ) -> OpenMMRescoreResult:
        """Stage 5: AF3 output 폴더 → OpenMM minimize + interaction energy.

        Option 1 (권장): run_af3_full_pipeline 1회 호출 → per-ligand minimized_pdb_path.
        이후 per-ligand compute_interaction_energy.

        iptm_floor 체크는 ScoringService에서 담당 — 여기서는 모든 리간드 처리.
        GPU CUDA 공유: _OPENMM_SEMAPHORE(1)로 직렬 실행 보장.
        실패 per-ligand → status="skipped", error 기록, continue.
        """
        if self._openmm is None:
            raise RuntimeError("OpenMMMCPClient 가 ScreeningService에 주입되지 않았습니다.")

        screening_id = job.job_id
        per_ligand_list: list[dict] = []
        for r in getattr(af3_parsed, "per_ligand", []):
            if isinstance(r, dict):
                per_ligand_list.append(r)
            else:
                # AF3Result 객체인 경우 dict-like 접근을 위해 변환
                per_ligand_list.append(
                    {
                        "ligand_id": getattr(r, "ligand_id", "unknown"),
                        "job_name": getattr(r, "af3_job_id", None) or getattr(r, "ligand_id", "unknown"),
                        "ligand_resname": "LIG",
                    }
                )

        log.info(
            "screening.stage_5.start",
            screening_id=screening_id,
            n_ligands=len(per_ligand_list),
        )

        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.stage_change",
                "job_id": job.job_id,
                "from_stage": "af3_parse",
                "to_stage": "openmm",
            },
        )

        # ── Stage 5 reshape: openMM_bot 의 run_af3_full_pipeline 가 폴더 패턴
        # `{protein}_data_{chem}` 을 기대 (af3-chatbot 의 `{job_id}_{ligand_id}` 와 mismatch).
        # 해결: temp folder 에 패턴 맞춰 sub-folders 생성 + AF3 출력 symlink.
        import tempfile, shutil
        from app.config import settings as _settings

        # 1) Build reshaped temp folder
        temp_root = pathlib.Path(tempfile.gettempdir()) / f"afmm_pipeline_{screening_id}"
        if temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)
        temp_root.mkdir(parents=True, exist_ok=True)

        ligand_to_jobname: dict[str, str] = {}  # ligand_id → AF3 job_name (for matching pipeline result)
        for r in getattr(af3_parsed, "per_ligand", []):
            cif = getattr(r, "cif_path", None)
            ligand_id = getattr(r, "ligand_id", None)
            if not cif or not ligand_id:
                continue
            src_folder = pathlib.Path(cif).parent
            if not src_folder.exists():
                continue
            sub = temp_root / f"target_data_{ligand_id[:8]}"
            sub.mkdir(exist_ok=True)
            for f in src_folder.iterdir():
                dst = sub / f.name
                if not dst.exists():
                    try:
                        dst.symlink_to(f)
                    except OSError:
                        shutil.copy(f, dst)
            # job_name pattern: {cif_basename without extension}
            cif_basename = pathlib.Path(cif).stem
            ligand_to_jobname[ligand_id] = cif_basename

        af3_folder = str(temp_root)
        output_dir = str(temp_root / "pipeline_output")
        log.info(
            "screening.stage_5.af3_folder_reshaped",
            screening_id=screening_id,
            temp_root=af3_folder,
            n_subfolders=len(ligand_to_jobname),
        )

        # 2) Call run_af3_full_pipeline with valid options:
        #    ligand_ff="openff" (권장, schema 명시) — "gaff2" 는 unsupported.
        #    platform="auto" (CUDA 자동 감지)
        pipeline_by_job: dict[str, dict] = {}

        # 하트비트: openMM pipeline 은 단일 batch 호출이라 응답 전엔 진행 정보 없음.
        # 30s 마다 elapsed_s 만 송출하여 UI 에 "작동 중" 신호 제공.
        _hb_n_lig = len(ligand_to_jobname)
        _hb_start = asyncio.get_running_loop().time()
        _hb_cancel = asyncio.Event()

        async def _openmm_heartbeat() -> None:
            try:
                while not _hb_cancel.is_set():
                    try:
                        await asyncio.wait_for(_hb_cancel.wait(), timeout=30.0)
                        return  # cancelled
                    except asyncio.TimeoutError:
                        pass
                    elapsed = asyncio.get_running_loop().time() - _hb_start
                    await _broadcast(
                        job.ws_broadcast,
                        {
                            "event": "screening.openmm_pipeline_heartbeat",
                            "job_id": job.job_id,
                            "n_ligands": _hb_n_lig,
                            "elapsed_s": round(elapsed, 1),
                            "sub_stage": "minimize+rescore_batch",
                        },
                    )
            except asyncio.CancelledError:
                return

        hb_task = asyncio.create_task(_openmm_heartbeat())

        # 시작 알림 (1회) — 사용자가 OpenMM 단계로 진입했음을 즉시 확인 가능
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.openmm_pipeline_started",
                "job_id": job.job_id,
                "n_ligands": _hb_n_lig,
            },
        )

        try:
            async with _OPENMM_SEMAPHORE:
                pipeline_raw = await self._openmm.run_af3_full_pipeline(
                    af3_folder=af3_folder,
                    output_dir=output_dir,
                    ligand_ff="openff",
                    staged=True,
                    platform="auto",
                )
            # None-safe access (pipeline 이 null 인 경우 graceful)
            pipeline_data = pipeline_raw.get("pipeline") if isinstance(pipeline_raw, dict) else None
            for entry in (pipeline_data or {}).get("results", []) or []:
                jname = entry.get("job_name", "")
                if jname:
                    pipeline_by_job[jname] = entry
            log.info(
                "screening.stage_5.pipeline_done",
                screening_id=screening_id,
                n_results=len(pipeline_by_job),
                pipeline_status=(pipeline_data or {}).get("status"),
            )
        except Exception as exc:
            log.error(
                "screening.stage_5.pipeline_failed",
                screening_id=screening_id,
                error=str(exc),
            )
            # heartbeat 종료 (실패 경로)
            _hb_cancel.set()
            try:
                await asyncio.wait_for(hb_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                hb_task.cancel()
            return OpenMMRescoreResult(
                screening_id=screening_id,
                n_succeeded=0,
                n_failed=0,
                n_skipped=len(per_ligand_list),
                per_ligand=[
                    OpenMMResult(
                        ligand_id=lig.get("ligand_id", "unknown"),
                        minimization_status="skipped",
                        error_message=f"pipeline failed: {exc}",
                    )
                    for lig in per_ligand_list
                ],
            )

        # heartbeat 종료 (성공 경로) — 이후 per-ligand 루프는 별도 가시화 사용
        _hb_cancel.set()
        try:
            await asyncio.wait_for(hb_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            hb_task.cancel()
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.openmm_pipeline_done",
                "job_id": job.job_id,
                "n_results": len(pipeline_by_job),
                "n_ligands": _hb_n_lig,
            },
        )

        # ── Per-ligand: compute_interaction_energy ────────────────────────────
        per_ligand_results: list[OpenMMResult] = []
        db_path = self._history._db_path

        _per_lig_total = len(per_ligand_list)
        for _lig_idx, lig in enumerate(per_ligand_list):
            ligand_id: str = lig.get("ligand_id", "unknown")
            # AF3 cif basename 패턴 (ligand_to_jobname 에서 매핑) 우선 사용
            mapped_jobname = ligand_to_jobname.get(ligand_id) if ligand_id in ligand_to_jobname else None
            job_name: str = mapped_jobname or lig.get("job_name", ligand_id)
            ligand_resname: str = lig.get("ligand_resname", "LIG")

            # OpenMM interaction-energy 시작 알림
            await _broadcast(
                job.ws_broadcast,
                {
                    "event": "screening.ligand_openmm_started",
                    "job_id": job.job_id,
                    "ligand_id": ligand_id,
                    "ligand_index": _lig_idx,
                    "total_ligands": _per_lig_total,
                    "sub_stage": "interaction_energy",
                },
            )

            pipe_entry = pipeline_by_job.get(job_name) or pipeline_by_job.get(ligand_id)
            pipe_status = pipe_entry.get("status", "failed") if pipe_entry else "failed"
            energy_before = pipe_entry.get("energy_before") if pipe_entry else None
            energy_after = pipe_entry.get("energy_after") if pipe_entry else None
            rmsd_ligand = pipe_entry.get("rmsd_ligand") if pipe_entry else None
            pipe_error = pipe_entry.get("error") if pipe_entry else "no pipeline entry"

            # API 응답이 minimized_pdb_path 키를 포함하지 않으므로 path 패턴으로 추론.
            # openMM_bot pipeline 출력 구조: {output_dir}/04_minimized/{job_name}_minimized.pdb
            minimized_pdb = pipe_entry.get("minimized_pdb_path") if pipe_entry else None
            separated_protein_pdb: str | None = None
            separated_ligand_sdf: str | None = None
            if pipe_entry and pipe_status == "success":
                if not minimized_pdb:
                    minimized_pdb = f"{output_dir}/04_minimized/{job_name}_minimized.pdb"
                separated_protein_pdb = f"{output_dir}/03_separated/{job_name}_protein.pdb"
                separated_ligand_sdf = f"{output_dir}/03_separated/{job_name}_ligand.sdf"

            # 파일 존재 검증 (path 추론 후)
            if minimized_pdb and not pathlib.Path(minimized_pdb).exists():
                log.warning(
                    "screening.stage_5.minimized_pdb_missing",
                    screening_id=screening_id, ligand_id=ligand_id, path=minimized_pdb,
                )
                minimized_pdb = None

            if not minimized_pdb or pipe_status not in ("success", "converged", "max_iter"):
                om_result = OpenMMResult(
                    ligand_id=ligand_id,
                    minimization_status="skipped",
                    energy_before_kJ=_f(energy_before),
                    energy_after_kJ=_f(energy_after),
                    rmsd_ligand_A=_f(rmsd_ligand),
                    error_message=pipe_error or "minimized_pdb missing",
                )
                per_ligand_results.append(om_result)
                await _broadcast(
                    job.ws_broadcast,
                    {
                        "event": "screening.ligand_openmm_done",
                        "job_id": job.job_id,
                        "ligand_id": ligand_id,
                        "e_interaction": None,
                        "e_complex": None,
                        "status": "skipped",
                    },
                )
                continue

            e_interaction: float | None = None
            e_complex: float | None = None
            e_lj: float | None = None
            e_coul: float | None = None
            inter_error: str | None = None

            try:
                async with _OPENMM_SEMAPHORE:
                    # ligand_sdf 전달 — openMM_bot 의 SystemGenerator 가 ligand template 을
                    # 자동 등록할 수 있도록. 미전달 시 "No template found" 로 실패.
                    inter_raw = await self._openmm.compute_interaction_energy(
                        pdb_file=minimized_pdb,
                        ligand_resname=ligand_resname,
                        ligand_ff="openff",  # gaff2 → openff (openMM_bot 권장)
                        decompose=True,
                        ligand_sdf=separated_ligand_sdf,
                    )
                e_interaction = _f(inter_raw.get("e_interaction"))
                e_complex = _f(inter_raw.get("e_full"))
                e_lj = _f(inter_raw.get("e_inter_lj"))
                e_coul = _f(inter_raw.get("e_inter_coul"))
            except Exception as exc:
                inter_error = str(exc)
                log.warning(
                    "screening.stage_5.inter_energy_failed",
                    screening_id=screening_id,
                    ligand_id=ligand_id,
                    error=inter_error,
                )

            # ── Smina rescoring (Stage 5b inline) — separated protein.pdb + ligand.sdf ──
            smina_affinity: float | None = None
            smina_error: str | None = None
            if separated_protein_pdb and separated_ligand_sdf:
                # Smina 시작 알림 — UI 의 smina 단계 row 활성화
                await _broadcast(
                    job.ws_broadcast,
                    {
                        "event": "screening.ligand_smina_started",
                        "job_id": job.job_id,
                        "ligand_id": ligand_id,
                        "ligand_index": _lig_idx,
                        "total_ligands": _per_lig_total,
                        "sub_stage": "score_only",
                    },
                )
                try:
                    if not pathlib.Path(separated_protein_pdb).exists():
                        raise FileNotFoundError(f"protein pdb missing: {separated_protein_pdb}")
                    if not pathlib.Path(separated_ligand_sdf).exists():
                        raise FileNotFoundError(f"ligand sdf missing: {separated_ligand_sdf}")
                    raw = await self._openmm.call("smina_score_only", {
                        "receptor_pdb": separated_protein_pdb,
                        "ligand_pdb_or_sdf": separated_ligand_sdf,
                    })
                    smina_affinity = _f((raw or {}).get("smina_affinity_kcal_mol")) \
                                     if isinstance(raw, dict) else None
                    log.info(
                        "screening.stage_5b.smina_done",
                        ligand_id=ligand_id, smina_affinity=smina_affinity,
                    )
                except Exception as exc:
                    smina_error = str(exc)
                    log.warning(
                        "screening.stage_5b.smina_failed",
                        ligand_id=ligand_id, error=smina_error,
                    )

            if pipe_status in ("converged", "max_iter"):
                min_status = pipe_status
            elif pipe_status == "success":
                min_status = "converged"
            elif inter_error and e_interaction is None:
                min_status = "failed"
            else:
                min_status = "converged"

            om_result = OpenMMResult(
                ligand_id=ligand_id,
                minimization_status=min_status,
                energy_before_kJ=_f(energy_before),
                energy_after_kJ=_f(energy_after),
                e_interaction_kJ=e_interaction,
                e_inter_lj_kJ=e_lj,
                e_inter_coul_kJ=e_coul,
                rmsd_ligand_A=_f(rmsd_ligand),
                minimized_pdb_path=minimized_pdb,
                error_message=inter_error,
            )
            per_ligand_results.append(om_result)

            # DB 저장: om_e_interaction, om_e_complex, smina_affinity (Stage 5b inline)
            try:
                async with aiosqlite.connect(db_path) as db:
                    await db.execute(
                        """
                        UPDATE screening_results
                        SET om_e_interaction = ?,
                            om_e_complex     = ?,
                            smina_affinity_kcal_mol = ?
                        WHERE job_id = ? AND ligand_id = ?
                        """,
                        (e_interaction, e_complex, smina_affinity, job.job_id, ligand_id),
                    )
                    await db.commit()
            except Exception as db_exc:
                log.warning(
                    "screening.stage_5.db_save_failed",
                    ligand_id=ligand_id,
                    error=str(db_exc),
                )

            await _broadcast(
                job.ws_broadcast,
                {
                    "event": "screening.ligand_openmm_done",
                    "job_id": job.job_id,
                    "ligand_id": ligand_id,
                    "e_interaction": e_interaction,
                    "e_complex": e_complex,
                    "status": min_status,
                },
            )

        n_succeeded = sum(
            1 for r in per_ligand_results
            if r.minimization_status in ("converged", "max_iter") and r.e_interaction_kJ is not None
        )
        n_failed = sum(1 for r in per_ligand_results if r.minimization_status == "failed")
        n_skipped = sum(1 for r in per_ligand_results if r.minimization_status == "skipped")

        log.info(
            "screening.stage_5.done",
            screening_id=screening_id,
            n_succeeded=n_succeeded,
            n_failed=n_failed,
            n_skipped=n_skipped,
        )

        return OpenMMRescoreResult(
            screening_id=screening_id,
            n_succeeded=n_succeeded,
            n_failed=n_failed,
            n_skipped=n_skipped,
            per_ligand=per_ligand_results,
        )

    # ── Stage 5b — Smina 리스코어링 ──────────────────────────────────────────

    async def _stage_5b_smina_rescore(
        self,
        job_id: str,
        openmm_results: list[dict[str, Any]],
        ws_broadcast: Any | None = None,
    ) -> list[SminaResult]:
        """Stage 5b: 각 OpenMM-minimized 리간드에 대해 smina_score_only 실행.

        Args:
            job_id: 스크리닝 잡 ID.
            openmm_results: OpenMM Stage 5 결과 리스트.
                각 항목: {
                    "ligand_id": str,
                    "minimized_pdb_path": str,   # smina 입력
                    "receptor_pdb_path": str,     # 수용체 PDB
                }
            ws_broadcast: (선택) WebSocket 브로드캐스트 callable.

        Returns:
            SminaResult 리스트 (입력 순서 유지).
        """
        log.info(
            "screening.stage_5b.start",
            job_id=job_id,
            n_ligands=len(openmm_results),
        )

        # WS: 스테이지 전환 이벤트
        await _broadcast(
            ws_broadcast,
            {
                "event": "screening.stage_change",
                "job_id": job_id,
                "from_stage": "openmm",
                "to_stage": "smina",
            },
        )

        smina_results: list[SminaResult] = []

        for item in openmm_results:
            ligand_id: str = item.get("ligand_id", "unknown")
            receptor_pdb: str = item.get("receptor_pdb_path", "")
            ligand_pdb: str = item.get("minimized_pdb_path", "")

            if not receptor_pdb or not ligand_pdb:
                log.warning(
                    "screening.stage_5b.missing_paths",
                    job_id=job_id,
                    ligand_id=ligand_id,
                )
                result = SminaResult(
                    ligand_id=ligand_id,
                    error="missing receptor_pdb_path or minimized_pdb_path",
                )
            else:
                result = await self._smina.score_one(receptor_pdb, ligand_pdb)
                result = result.model_copy(update={"ligand_id": ligand_id})

                if result.error is None:
                    # DB 저장
                    await self._smina.save_to_db(job_id, ligand_id, result)

            smina_results.append(result)

            # WS: 리간드 개별 완료 이벤트
            await _broadcast(
                ws_broadcast,
                {
                    "event": "screening.ligand_smina_done",
                    "job_id": job_id,
                    "ligand_id": ligand_id,
                    "smina_affinity": result.smina_affinity_kcal_mol,
                    "error": result.error,
                },
            )

        n_ok = sum(1 for r in smina_results if r.error is None)
        log.info(
            "screening.stage_5b.done",
            job_id=job_id,
            n_ok=n_ok,
            n_error=len(smina_results) - n_ok,
        )
        return smina_results

    # ── Stage 5 (신규) — Separate + Smina minimize (OpenMM 대체) ───────────────

    async def _stage_5_smina_minimize(
        self,
        job: ScreeningJobContext,
        af3_parsed: AF3ParseResult,
        ligand_smiles: dict[str, str],
    ) -> list[SminaResult]:
        """Stage 5 (use_openmm=False): RDKit 분리 → smina --minimize per-ligand.

        OpenMM minimize+rescore 를 대체한다(설계 §3.4 B-2).
        - RDKit `complex_splitter.split_complex` 로 AF3 holo CIF → receptor.pdb + ligand.sdf
        - openMM_bot `smina_minimize` 호출 → minimized affinity
        - canonical `smina_affinity_kcal_mol`(+provenance) DB 저장
        - iptm_floor 사전 필터로 저신뢰 포즈 스킵
        - CPU 병렬(세마포어) — GPU 직렬 제약 없음
        - per-ligand 실패 격리
        """
        from app.config import settings as _settings
        from app.services.complex_splitter import split_complex, ComplexSplitError

        screening_id = job.job_id
        db_path = self._history._db_path
        floor = self._scoring.iptm_floor if self._scoring is not None else _settings.iptm_floor

        per_ligand = list(getattr(af3_parsed, "per_ligand", []))
        log.info(
            "screening.stage_5.start",
            screening_id=screening_id,
            mode="smina_minimize",
            n_ligands=len(per_ligand),
        )
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.stage_change",
                "job_id": job.job_id,
                "from_stage": "af3_parse",
                "to_stage": "smina",
            },
        )

        # Split receptor.pdb/ligand.sdf are written under OPENMM_WORK_ROOT so the
        # bundled smina-mcp (shared "afmm_work" volume, /data/work) can read the
        # exact same absolute paths. Lite profile must NOT use a container-local
        # tmpdir here — that path is invisible to the smina-mcp container.
        out_root = pathlib.Path(_settings.openmm_work_root) / f"afmm_smina_{screening_id}"
        out_root.mkdir(parents=True, exist_ok=True)

        sem = asyncio.Semaphore(max(1, int(getattr(_settings, "smina_max_concurrency", 4))))
        total = len(per_ligand)

        async def _process(idx: int, r: AF3Result) -> SminaResult:
            ligand_id = getattr(r, "ligand_id", None) or f"lig_{idx}"
            cif_path = getattr(r, "cif_path", None)
            iptm = getattr(r, "iptm", None)

            # iptm_floor 사전 필터
            if iptm is not None and iptm < floor:
                log.info(
                    "screening.stage_5.iptm_floor_skip",
                    ligand_id=ligand_id, iptm=iptm, floor=floor,
                )
                return SminaResult(ligand_id=ligand_id, error=f"iptm<{floor} (skipped)")

            if not cif_path or not pathlib.Path(cif_path).exists():
                return SminaResult(ligand_id=ligand_id, error="cif_path missing")

            smiles = ligand_smiles.get(ligand_id)
            if not smiles:
                return SminaResult(ligand_id=ligand_id, error="smiles missing for ligand")

            await _broadcast(
                job.ws_broadcast,
                {
                    "event": "screening.ligand_smina_started",
                    "job_id": job.job_id,
                    "ligand_id": ligand_id,
                    "ligand_index": idx,
                    "total_ligands": total,
                    "sub_stage": "minimize",
                },
            )

            async with sem:
                try:
                    receptor_pdb, ligand_sdf = await asyncio.to_thread(
                        split_complex,
                        cif_path,
                        smiles,
                        str(out_root),
                        ligand_id,
                    )
                except ComplexSplitError as exc:
                    log.warning("screening.stage_5.split_failed", ligand_id=ligand_id, error=str(exc))
                    res = SminaResult(ligand_id=ligand_id, error=f"split_failed: {exc}")
                    await _broadcast(
                        job.ws_broadcast,
                        {"event": "screening.ligand_smina_done", "job_id": job.job_id,
                         "ligand_id": ligand_id, "smina_affinity": None, "error": res.error},
                    )
                    return res

                out_pose = str(out_root / f"{ligand_id}_minimized.sdf")
                res = await self._smina.minimize_one(
                    receptor_pdb=receptor_pdb,
                    ligand_pdb_or_sdf=ligand_sdf,
                    out_path=out_pose,
                    scoring=getattr(_settings, "smina_scoring", "vinardo"),
                    minimize_iters=int(getattr(_settings, "smina_minimize_iters", 0)),
                )
                res = res.model_copy(update={"ligand_id": ligand_id})

            if res.error is None:
                try:
                    await self._smina.save_to_db(job.job_id, ligand_id, res)
                except Exception as db_exc:  # noqa: BLE001
                    log.warning("screening.stage_5.db_save_failed", ligand_id=ligand_id, error=str(db_exc))

            await _broadcast(
                job.ws_broadcast,
                {
                    "event": "screening.ligand_smina_done",
                    "job_id": job.job_id,
                    "ligand_id": ligand_id,
                    "smina_affinity": res.smina_affinity_kcal_mol,
                    "error": res.error,
                },
            )
            return res

        results = await asyncio.gather(
            *[_process(i, r) for i, r in enumerate(per_ligand)],
            return_exceptions=False,
        )

        n_ok = sum(1 for r in results if r.error is None)
        log.info(
            "screening.stage_5.done",
            screening_id=screening_id,
            mode="smina_minimize",
            n_ok=n_ok,
            n_error=len(results) - n_ok,
        )
        return list(results)

    # ── Stage 6 — Composite Ranking ───────────────────────────────────────────

    async def _stage_6_rank(
        self,
        job: ScreeningJobContext,
    ) -> RankingResult:
        """Stage 6: 모든 ligand 결과 JOIN → composite_score 계산 → rank 부여.

        1. SQL: SELECT * FROM screening_results WHERE job_id = ?
        2. for each row: composite = scoring_service.composite_score(af3, openmm, smina)
        3. UPDATE screening_results SET composite_score=?, rank=?
        4. WS broadcast: screening.ranking_done {n_ranked, top1}
        5. RankingService.compute_ranking → 최종 정렬 top_n 반환

        sort_by 기본값 "composite". API 에서 사용자가 변경 가능.
        """
        if self._scoring is None:
            raise RuntimeError("ScoringService 가 ScreeningService에 주입되지 않았습니다.")
        if self._ranking is None:
            raise RuntimeError("RankingService 가 ScreeningService에 주입되지 않았습니다.")

        screening_id = job.job_id
        log.info("screening.stage_6.start", screening_id=screening_id)

        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.stage_change",
                "job_id": job.job_id,
                "from_stage": "openmm",
                "to_stage": "ranking",
            },
        )

        db_path = self._history._db_path

        # 1. 모든 결과 행 조회
        sql_select = """
            SELECT
                ligand_id,
                af3_iptm,
                af3_ranking_score,
                af3_mean_pae,
                om_e_interaction,
                smina_affinity_kcal_mol
            FROM screening_results
            WHERE job_id = ?
        """
        rows: list[dict] = []
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql_select, (job.job_id,)) as cur:
                raw_rows = await cur.fetchall()
            rows = [dict(r) for r in raw_rows]

        n_total = len(rows)
        n_ranked = 0
        weights_used = {
            "iptm": self._scoring.weights.iptm,
            "pae": self._scoring.weights.pae,
            "inter": self._scoring.weights.inter,
            "smina": self._scoring.weights.smina,
        }

        # 2+3. composite_score 계산 + DB UPDATE
        ranked_pairs: list[tuple[str, float | None]] = []

        async with aiosqlite.connect(db_path) as db:
            for row in rows:
                ligand_id = row["ligand_id"]
                af3_dict = {
                    "iptm": row["af3_iptm"],
                    "ranking_score": row["af3_ranking_score"],
                    "mean_pae": row["af3_mean_pae"],
                }
                openmm_dict = {
                    "e_interaction": row["om_e_interaction"],
                }
                smina_dict: dict | None = None
                if row["smina_affinity_kcal_mol"] is not None:
                    smina_dict = {"affinity_kcal_mol": row["smina_affinity_kcal_mol"]}

                score = self._scoring.composite_score(
                    af3_dict, openmm_dict, smina_dict, mode="canonical"
                )
                ranked_pairs.append((ligand_id, score))

                if score is not None:
                    n_ranked += 1

                await db.execute(
                    """
                    UPDATE screening_results
                    SET composite_score = ?
                    WHERE job_id = ? AND ligand_id = ?
                    """,
                    (score, job.job_id, ligand_id),
                )

            # 임시 rank 부여: composite DESC (None 후순위)
            ranked_pairs_sorted = sorted(
                ranked_pairs,
                key=lambda x: (x[1] is None, -(x[1] or 0.0)),
            )
            for rank_idx, (lid, _) in enumerate(ranked_pairs_sorted, start=1):
                await db.execute(
                    """
                    UPDATE screening_results
                    SET rank = ?
                    WHERE job_id = ? AND ligand_id = ?
                    """,
                    (rank_idx, job.job_id, lid),
                )

            await db.commit()

        # 4. RankingService.compute_ranking으로 최종 top-N 반환
        top_n_rows: list[ScreeningResultRow] = await self._ranking.compute_ranking(
            job_id=job.job_id,
            sort_by=job.sort_by,  # type: ignore[arg-type]
            top_n=job.top_n,
        )

        # WS 브로드캐스트
        top1 = None
        if top_n_rows:
            t = top_n_rows[0]
            top1 = {
                "ligand_id": t.ligand_id,
                "composite": t.composite_score,
                "smina": t.smina_affinity_kcal_mol,
            }

        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.ranking_done",
                "job_id": job.job_id,
                "n_ranked": n_ranked,
                "top1": top1,
            },
        )

        log.info(
            "screening.stage_6.done",
            screening_id=screening_id,
            n_total=n_total,
            n_ranked=n_ranked,
        )

        return RankingResult(
            screening_id=screening_id,
            n_total=n_total,
            n_ranked=n_ranked,
            top_n=top_n_rows,
            weights_used=weights_used,
        )

    # ── Stage 7 — LLM Interpret ───────────────────────────────────────────────

    async def _stage_7_llm(
        self,
        job: ScreeningJobContext,
        ranking: "list[ScreeningResultRow] | RankingResult",
        target: TargetPrepResult,
        library_meta: dict[str, Any],
    ) -> ScreeningSummary:
        """Stage 7: ranking 결과를 LLM 으로 해석 → ScreeningSummary 반환.

        1. context dict 빌드 ({target, library, config, stats, top_k, warnings})
        2. prompts/screening_summary.txt 로 user message 빌드
        3. llm.chat_json(messages, model, ScreeningSummary) — JSON Schema 강제
        4. WS broadcast 완료 후 chat.assistant_chunk + chat.assistant_done 으로 자연어 응답
        5. SQLite chat_messages 에 LLM 응답 저장 (history_service)

        LLM 실패 (API 키 placeholder, OpenRouter 429 등) → Caveat 포함된 fallback ScreeningSummary 반환.
        """
        # RankingResult 객체이면 top_n 리스트 추출
        if hasattr(ranking, "top_n") and not isinstance(ranking, list):
            ranking_list: list = ranking.top_n  # type: ignore[union-attr]
        else:
            ranking_list = ranking  # type: ignore[assignment]

        log.info(
            "screening.stage_7.start",
            job_id=job.job_id,
            n_ranked=len(ranking_list),
        )

        # WS: RANK → LLM 전환
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.stage_change",
                "job_id": job.job_id,
                "from_stage": "rank",
                "to_stage": "llm",
            },
        )

        # ── 1. 컨텍스트 빌드 ──────────────────────────────────────────────────
        n_total = library_meta.get("n_entries", len(ranking_list))
        source_filename = library_meta.get("source_filename", "unknown")
        n_ranked = sum(1 for r in ranking_list if r.composite_score is not None)
        n_af3_only = sum(1 for r in ranking_list if r.composite_score is None and r.iptm is not None)
        n_failed = n_total - n_ranked - n_af3_only

        # top-K 표 (최대 10개)
        top_k_rows = ranking_list[:10]
        top_k_table_lines = [
            "| rank | ligand | ipTM | PAE(Å) | E_inter(kJ/mol) | smina(kcal/mol) | composite |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in top_k_rows:
            top_k_table_lines.append(
                f"| {row.rank or '-'} "
                f"| {row.ligand_name or row.ligand_id} "
                f"| {_fmt(row.iptm)} "
                f"| {_fmt(row.pae_mean)} "
                f"| {_fmt(row.e_interaction_kJ)} "
                f"| {_fmt(row.smina_affinity_kcal_mol)} "
                f"| {_fmt(row.composite_score)} |"
            )
        top_k_table = "\n".join(top_k_table_lines)

        # 분포 통계 — ranking 가 RankingResult 면 Pydantic __iter__ 가 (key,value) tuple yield 하므로
        # 반드시 ranking_list (list[ScreeningResultRow]) 사용.
        iptm_vals = [r.iptm for r in ranking_list if r.iptm is not None]
        comp_vals = [r.composite_score for r in ranking_list if r.composite_score is not None]
        distribution_stats = json.dumps(
            {
                "iptm": _quartile_stats(iptm_vals),
                "composite": _quartile_stats(comp_vals),
            },
            ensure_ascii=False,
        )

        weights = json.dumps(job.config.get("weights", {}), ensure_ascii=False)
        warnings = json.dumps(job.config.get("warnings", []), ensure_ascii=False)

        # ── 2. 프롬프트 빌드 ─────────────────────────────────────────────────
        try:
            prompt_template = _load_prompt("screening_summary.txt")
        except FileNotFoundError:
            log.error("screening.stage_7.prompt_not_found")
            prompt_template = (
                "당신은 가상 스크리닝 결과 해석가입니다. 아래 컨텍스트로 JSON 요약을 생성하세요.\n"
                "반드시 headline, highlights, table_md, caveats, next_actions 키를 포함하세요.\n"
                "컨텍스트: {top_k_table}"
            )

        user_content = prompt_template.format(
            target_name=target.target_name,
            target_length=target.sequence_length,
            n_total=n_total,
            n_ranked=n_ranked,
            n_af3_only=n_af3_only,
            n_failed=n_failed,
            weights=weights,
            top_k_table=top_k_table,
            distribution_stats=distribution_stats,
            warnings=warnings,
            source_filename=source_filename,
        )

        # system.txt + user 컨텍스트 메시지 조합
        try:
            system_text = _load_prompt("system.txt")
        except FileNotFoundError:
            system_text = "당신은 가상 스크리닝 결과 해석가입니다."

        messages: list[dict] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_content},
        ]

        # ── 3. LLM 호출 (chat_json) ──────────────────────────────────────────
        summary: ScreeningSummary
        if self._llm is None:
            log.warning("screening.stage_7.llm_not_injected", job_id=job.job_id)
            summary = _fallback_summary(target.target_name, len(ranking_list))
        else:
            try:
                result_dict = await self._llm.chat_json(
                    messages=messages,
                    model=job.config.get("model"),
                    schema=ScreeningSummary,
                    # 한글 토큰 밀도 (1.5-2x EN) + table_md + 5 highlights + 3 caveats
                    # + 2 next_actions 합산 시 2000 으로 truncation 발생 사례 있음 → 3000 으로 상향
                    max_tokens=3000,
                    max_retries=1,
                )
                summary = ScreeningSummary.model_validate(result_dict)
            except Exception as exc:
                log.warning(
                    "screening.stage_7.llm_failed",
                    job_id=job.job_id,
                    error=str(exc),
                )
                summary = _fallback_summary(target.target_name, len(ranking_list))

        # ── 4. WS broadcast: 완료 이벤트 + 자연어 응답 ───────────────────────
        top1 = ranking[0] if ranking else None
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.llm_summary_done",
                "job_id": job.job_id,
                "headline": summary.headline,
                "top1_ligand_id": top1.ligand_id if top1 else None,
            },
        )

        # 마크다운 전체 응답을 스트리밍 형태로 WS에 전달
        full_md = summary.headline + "\n\n" + summary.table_md
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "chat.assistant_chunk",
                "job_id": job.job_id,
                "session_id": job.session_id,
                "delta": full_md,
            },
        )
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "chat.assistant_done",
                "job_id": job.job_id,
                "session_id": job.session_id,
            },
        )

        # ── 5. SQLite 저장 (history_service) ────────────────────────────────
        if job.session_id:
            persist_content = summary.headline + "\n\n" + summary.table_md
            try:
                await self._history.save_message(
                    session_id=job.session_id,
                    role="assistant",
                    content=persist_content,
                    meta={"source": "stage_7", "screening_id": job.job_id},
                )
            except Exception as exc:
                log.warning(
                    "screening.stage_7.history_save_failed",
                    job_id=job.job_id,
                    error=str(exc),
                )

        log.info(
            "screening.stage_7.done",
            job_id=job.job_id,
            headline=summary.headline[:60],
        )
        return summary

    # ── run_pipeline — Stage 1 + 2 연결 ──────────────────────────────────────

    async def run_pipeline(
        self,
        job: ScreeningJobContext,
    ) -> dict[str, Any]:
        """Stage 1 → Stage 2 실행 (Phase 2 구현 범위).

        Stage 3+ 는 Phase 3에서 연결.

        Returns:
            {
                "stage_1": StageResult,
                "stage_2": TargetPrepResult | None,
                "entries": list[LigandEntry],
            }
        """
        log.info("screening.pipeline.start", job_id=job.job_id)

        # Stage 1: Library Ingest
        stage1 = await self._stage_1_ingest(job)
        if not stage1.ok:
            log.error(
                "screening.pipeline.stage_1_failed",
                job_id=job.job_id,
                message=stage1.message,
            )
            return {"stage_1": stage1, "stage_2": None, "entries": []}

        entries: list[LigandEntry] = [
            LigandEntry.model_validate(e) for e in stage1.data.get("entries", [])
        ]

        # Stage 2: Target Prep
        try:
            stage2_result = await self._stage_2_target(job, job.target_input)
        except (ValueError, RuntimeError) as exc:
            log.error(
                "screening.pipeline.stage_2_failed",
                job_id=job.job_id,
                error=str(exc),
            )
            return {
                "stage_1": stage1,
                "stage_2": None,
                "entries": entries,
                "error": str(exc),
            }

        log.info(
            "screening.pipeline.stages_1_2_done",
            job_id=job.job_id,
            n_ligands=len(entries),
            target_name=stage2_result.target_name,
        )

        return {
            "stage_1": stage1,
            "stage_2": stage2_result,
            "entries": entries,
        }

    async def run_pipeline_full(
        self,
        job: ScreeningJobContext,
    ) -> dict[str, Any]:
        """Stage 1 → 2 → 3 → 4 → 5 → 5b → 6 → 7 전체 파이프라인.

        모든 stage 실패 시 graceful degradation (해당 stage 부터 스킵).
        Returns: {job_id, status, ranking, errors[]}
        """
        log.info("screening.pipeline_full.start", job_id=job.job_id)
        errors: list[str] = []

        # Stage 1: Ingest
        s1 = await self._stage_1_ingest(job)
        if not s1.ok:
            return {"job_id": job.job_id, "status": "failed", "stage": "ingest", "error": s1.message}
        entries: list[LigandEntry] = [
            LigandEntry.model_validate(e) for e in s1.data.get("entries", [])
        ]

        # Stage 2: Target
        try:
            target = await self._stage_2_target(job, job.target_input)
        except Exception as exc:
            log.error("screening.pipeline.stage_2_error", error=str(exc), exc_info=True)
            return {"job_id": job.job_id, "status": "failed", "stage": "target", "error": str(exc)}

        # Stage 3: AF3 holo (GPU 5h+ — most expensive)
        if self._af3 is None:
            log.warning("screening.stage_3.skipped", reason="af3_client_missing")
            errors.append("stage_3_af3_skipped")
            af3_batch = None
        else:
            try:
                af3_batch = await self._stage_3_af3(job, target, entries)
            except Exception as exc:
                log.error("screening.pipeline.stage_3_error", error=str(exc), exc_info=True)
                errors.append(f"stage_3_af3_failed: {exc}")
                af3_batch = None

        # Stage 4: Parse
        af3_parsed = None
        if af3_batch is not None:
            try:
                af3_parsed = await self._stage_4_parse(job, af3_batch, entries=entries)
            except Exception as exc:
                log.error("screening.pipeline.stage_4_error", error=str(exc), exc_info=True)
                errors.append(f"stage_4_parse_failed: {exc}")

        # Stage 5: rescoring — use_openmm 토글로 경로 선택
        #   False(기본): smina --minimize (OpenMM 제거, 신규 경로)
        #   True       : 기존 OpenMM minimize+rescore + Stage 5b smina (롤백/비교용)
        from app.config import settings as _settings
        om_result = None
        smina_results = None
        if af3_parsed is not None:
            if _settings.use_openmm and self._openmm is not None:
                # ── 기존 OpenMM 경로 ──
                try:
                    om_result = await self._stage_5_openmm(job, af3_parsed)
                except Exception as exc:
                    log.error("screening.pipeline.stage_5_error", error=str(exc), exc_info=True)
                    errors.append(f"stage_5_openmm_failed: {exc}")
                if om_result is not None:
                    try:
                        openmm_dicts = [r.model_dump() if hasattr(r, "model_dump") else r for r in om_result.per_ligand]
                        smina_results = await self._stage_5b_smina_rescore(job, openmm_dicts)
                    except Exception as exc:
                        log.error("screening.pipeline.stage_5b_error", error=str(exc), exc_info=True)
                        errors.append(f"stage_5b_smina_failed: {exc}")
            else:
                # ── 신규 smina --minimize 경로 ──
                try:
                    ligand_smiles = {e.ligand_id: e.smiles for e in entries}
                    smina_results = await self._stage_5_smina_minimize(job, af3_parsed, ligand_smiles)
                except Exception as exc:
                    log.error("screening.pipeline.stage_5_smina_error", error=str(exc), exc_info=True)
                    errors.append(f"stage_5_smina_minimize_failed: {exc}")

        # Stage 6: Composite Ranking
        ranking_dict = None
        if self._scoring is not None and self._ranking is not None:
            try:
                ranking_dict = await self._stage_6_rank(job)
            except Exception as exc:
                log.error("screening.pipeline.stage_6_error", error=str(exc), exc_info=True)
                errors.append(f"stage_6_rank_failed: {exc}")

        # Stage 7: LLM Interpret (deterministic fallback if no API key)
        summary = None
        if self._llm is not None and ranking_dict is not None:
            try:
                summary = await self._stage_7_llm(
                    job,
                    ranking_dict,
                    target,
                    library_meta={"n_entries": len(entries), "source_filename": ""},
                )
            except Exception as exc:
                log.error("screening.pipeline.stage_7_error", error=str(exc), exc_info=True)
                errors.append(f"stage_7_llm_failed: {exc}")

        # Final WS event
        await _broadcast(
            job.ws_broadcast,
            {
                "event": "screening.complete",
                "job_id": job.job_id,
                "n_entries": len(entries),
                "errors": errors,
            },
        )

        log.info(
            "screening.pipeline_full.done",
            job_id=job.job_id,
            n_entries=len(entries),
            n_errors=len(errors),
        )

        return {
            "job_id": job.job_id,
            "status": "completed" if not errors else "partial",
            "n_entries": len(entries),
            "ranking": ranking_dict,
            "summary": summary,
            "errors": errors,
        }

    async def _run_pipeline_stages_5_6(
        self,
        job: ScreeningJobContext,
        af3_parsed: AF3ParseResult,
        openmm_results_for_smina: list[dict[str, Any]] | None = None,
    ) -> RankingResult:
        """Stage 5 → 5b → 6 순차 실행 (Phase 2 범위).

        Stage 1-4, 7 은 별도 메서드로 처리 (Agent A/B/D 담당).

        Args:
            job: ScreeningJobContext (af3_folder, ligand_ff, platform 등 포함)
            af3_parsed: Stage 4 결과 (AF3ParseResult).
            openmm_results_for_smina: Stage 5b 에 전달할 dict 목록.
                None이면 Stage 5b 스킵.

        Returns:
            RankingResult from Stage 6.
        """
        log.info("screening.pipeline_5_6.start", job_id=job.job_id)

        # Stage 5: OpenMM Rescoring
        await self._stage_5_openmm(job, af3_parsed)

        # Stage 5b: Smina (선택적)
        if openmm_results_for_smina is not None:
            await self._stage_5b_smina_rescore(
                job_id=job.job_id,
                openmm_results=openmm_results_for_smina,
                ws_broadcast=job.ws_broadcast,
            )
        else:
            log.info("screening.pipeline_5_6.stage_5b.skipped", reason="openmm_results_for_smina is None")

        # Stage 6: Composite Ranking
        ranking_result = await self._stage_6_rank(job)

        log.info(
            "screening.pipeline_5_6.done",
            job_id=job.job_id,
            n_ranked=ranking_result.n_ranked,
        )
        return ranking_result


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

def _fmt(val: float | None, decimals: int = 3) -> str:
    """숫자를 소수점 decimals 자리 문자열로 포맷. None 이면 '-'."""
    if val is None:
        return "-"
    return f"{val:.{decimals}f}"


def _quartile_stats(vals: list[float]) -> dict[str, float | None]:
    """간단한 분위 통계 반환 (min, q1, median, q3, max)."""
    if not vals:
        return {"min": None, "q1": None, "median": None, "q3": None, "max": None}
    s = sorted(vals)
    n = len(s)
    return {
        "min": round(s[0], 4),
        "q1": round(s[n // 4], 4),
        "median": round(s[n // 2], 4),
        "q3": round(s[min(3 * n // 4, n - 1)], 4),
        "max": round(s[-1], 4),
    }


def _fallback_summary(target_name: str, n_ranked: int) -> ScreeningSummary:
    """LLM 실패 시 결정론적 fallback ScreeningSummary 반환."""
    return ScreeningSummary(
        headline=f"[자동 요약 불가] '{target_name}' 스크리닝 완료 — 상위 {n_ranked}개 리간드 랭킹 완료.",
        highlights=[
            "LLM 해석 서비스를 사용할 수 없어 자동 요약이 생성되지 않았습니다.",
            f"총 {n_ranked}개 리간드가 랭킹되었습니다.",
            "상세 결과는 /api/screening/{job_id}/results 엔드포인트에서 확인하세요.",
        ],
        table_md="| 참고 |\n|---|\n| LLM 요약 없음 — DB 결과 직접 조회 필요 |",
        caveats=[
            "OpenMM e_interaction 은 vacuum NoCutoff 기반이라 동일 target 내 *상대 랭킹* 만 의미가 있습니다. 타겟 간 절대 비교 부적합.",
            "Smina docking score (Vinardo) 는 score-only 모드입니다 — pose 재탐색 없이 현재 좌표를 그대로 평가하므로 도킹 친화도와 다를 수 있습니다.",
            "ipTM ≥ 0.55 통과 ligand 만 composite score 에 포함됩니다. 실패 리간드는 별도 검토 필요.",
            "[fallback] LLM API 오류 또는 미설정으로 인해 자동 해석이 생략되었습니다.",
        ],
        next_actions=[
            "OPENROUTER_API_KEY 설정 후 서비스 재시작 → LLM 요약 활성화.",
            "랭킹 결과를 /api/screening/{job_id}/results 에서 직접 확인하세요.",
        ],
    )


def _f(val: object) -> float | None:
    """안전한 float 변환."""
    if val is None:
        return None
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _broadcast(ws_broadcast: Any | None, payload: dict) -> None:
    """WS 브로드캐스트 callable 이 있으면 호출. 없으면 로그만.

    NOTE: structlog 의 첫 positional 인자가 'event' 필드이므로, payload['event'] 는
    별도 kwarg 이름 'ws_event' 으로 전달해 충돌 방지.
    """
    if ws_broadcast is None:
        log.debug("screening.ws_broadcast.skipped", ws_event=payload.get("event"))
        return
    try:
        if asyncio.iscoroutinefunction(ws_broadcast):
            await ws_broadcast(payload)
        else:
            ws_broadcast(payload)
    except Exception as exc:
        log.warning("screening.ws_broadcast.error", error=str(exc))
