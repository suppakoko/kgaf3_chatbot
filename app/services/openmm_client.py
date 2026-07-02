"""OpenMM MCP HTTP 클라이언트 어댑터.

mcp_integration.md §4 권위.
Phase 1: connect / ping / list_tools 구현.
Phase 2: extract_af3_cifs / run_af3_full_pipeline / compute_interaction_energy 구현.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.mcp_http import MCPHttpClient, MCPError  # noqa: F401 (re-export)

log = structlog.get_logger("service.openmm")


class OpenMMMCPClient(MCPHttpClient):
    """openMM_bot MCP HTTP 어댑터.

    base_url: OPENMM_MCP_URL (예: http://127.0.0.1:8001/mcp)
    """

    def __init__(self, base_url: str, auth_token: str = "", timeout: float = 600.0):
        super().__init__(base_url=base_url, auth_token=auth_token, timeout=timeout, name="openmm")

    # ── Phase 1: 연결 / 상태 확인 ────────────────────────────────────────────

    async def connect(self) -> None:
        """MCP initialize + tools/list 캐시."""
        await super().connect()
        log.info("openmm.connected", tool_count=len(self._tool_index))

    async def ping(self) -> bool:
        """5초 타임아웃 헬스체크."""
        ok = await super().ping()
        log.debug("openmm.ping", ok=ok)
        return ok

    async def list_tools(self) -> list[dict]:
        """캐시된 openmm 도구 목록 반환."""
        return await super().list_tools()

    # ── Phase 2: 핵심 도구 호출 ─────────────────────────────────────────────

    async def extract_af3_cifs(self, af3_folder: str) -> dict:
        """AF3 폴더에서 단백질·리간드 CIF 분리.

        openMM_bot MCP `extract_af3_cifs` 도구 호출.

        Args:
            af3_folder: AF3 batch output 루트 폴더 경로.

        Returns:
            dict: { project_id, protein_pdb, ligand_sdf, ligand_chain_id }
        """
        log.info("openmm.extract_af3_cifs", af3_folder=af3_folder)
        result = await self.call("extract_af3_cifs", {"af3_folder": af3_folder})
        log.debug("openmm.extract_af3_cifs.done", result_keys=list(result.keys()) if isinstance(result, dict) else None)
        return result

    async def run_af3_full_pipeline(
        self,
        af3_folder: str,
        output_dir: str,
        ligand_ff: str = "gaff2",
        staged: bool = True,
        platform: str = "CUDA",
    ) -> dict:
        """AF3 결과 폴더 → OpenMM 전체 파이프라인 실행 (minimization 포함).

        openMM_bot MCP `run_af3_full_pipeline` 도구 호출.
        Stage 4+5 결합 실행 — 리간드별 minimized_pdb_path 생성.

        Args:
            af3_folder: AF3 batch output 루트 폴더 경로.
            output_dir: OpenMM 결과 저장 경로 (예: {af3_folder}/pipeline_output).
            ligand_ff: 리간드 forcefield. "gaff2" (default) | "openff".
            staged: staged minimization (k=50→5→0). True 권장.
            platform: OpenMM 플랫폼. "CUDA" | "CPU".

        Returns:
            dict: {
                "pipeline": {
                    "results": [
                        {
                            "job_name": str,
                            "energy_before": float,
                            "energy_after": float,
                            "rmsd_ligand": float,
                            "minimized_pdb_path": str,
                            "status": str,
                            "error": str | None,
                        },
                        ...
                    ]
                }
            }
        """
        args = {
            "af3_folder": af3_folder,
            "output_dir": output_dir,
            "ligand_ff": ligand_ff,
            "staged": staged,
            "platform": platform,
        }
        log.info("openmm.run_af3_full_pipeline", af3_folder=af3_folder, output_dir=output_dir)
        result = await self.call("run_af3_full_pipeline", args, timeout=self.timeout)
        n_results = len(result.get("pipeline", {}).get("results", [])) if isinstance(result, dict) else 0
        log.info("openmm.run_af3_full_pipeline.done", n_ligands=n_results)
        return result

    @staticmethod
    def _detect_ligand_resname(pdb_file: str, hint: str = "LIG") -> str:
        """PDB HETATM 레코드에서 비-HOH 리간드 잔기 이름 자동 감지.

        openMM_bot 은 minimized PDB 의 실제 리간드 resname (예: "UNL") 을
        그대로 사용하므로 힌트("LIG")가 틀릴 수 있다.
        HETATM 레코드 중 HOH/WAT 을 제외한 첫 번째 resname 을 반환.
        파일 읽기 실패 또는 HETATM 없으면 hint 반환.
        """
        try:
            seen: list[str] = []
            with open(pdb_file) as f:
                for line in f:
                    if line.startswith("HETATM"):
                        resname = line[17:20].strip()
                        if resname and resname not in ("HOH", "WAT") and resname not in seen:
                            seen.append(resname)
            if seen:
                # hint 가 실제로 존재하면 그대로, 아니면 첫 번째 발견 resname
                return hint if hint in seen else seen[0]
        except Exception:
            pass
        return hint

    async def compute_interaction_energy(
        self,
        pdb_file: str,
        ligand_resname: str,
        ligand_ff: str = "openff",
        decompose: bool = True,
        ligand_sdf: str | None = None,
        ligand_smiles: str | None = None,
    ) -> dict:
        """minimized 복합체에서 단백질-리간드 interaction energy 계산.

        openMM_bot MCP `compute_protein_ligand_interaction_energy` 도구 호출.
        vacuum NoCutoff, nonbonded force group 분리 방식.

        Args:
            pdb_file: minimized 복합체 PDB 파일 경로 (절대경로).
            ligand_resname: PDB 내 리간드 residue name 힌트 (예: "LIG", "UNL").
                           실제 HETATM resname 이 다르면 자동 보정 (_detect_ligand_resname).
            ligand_ff: 리간드 forcefield. "openff" (default — minimization 단계와 일관).
            decompose: LJ / Coulomb 개별 decomposition 여부.
            ligand_sdf: 분리된 ligand SDF 파일 경로. 비표준 잔기의 force field
                template 등록을 위해 *권장*. run_af3_full_pipeline 의 03_separated 출력.
                미제공 시 도구가 MCPError("TOOL_ERROR") 반환.
            ligand_smiles: ligand SMILES (ligand_sdf fallback). stereochem 일치 보장 X.

        Returns:
            dict: {
                "e_interaction": float,     # kJ/mol (정규 키)
                "e_full": float,            # kJ/mol
                "e_protein_only": float,    # kJ/mol
                "e_ligand_only": float,     # kJ/mol
                "e_inter_lj": float,        # kJ/mol (decompose=True 시)
                "e_inter_coul": float,      # kJ/mol (decompose=True 시)
                "nonbonded_method": str,    # "NoCutoff"
                "ligand_source": str,       # "sdf" | "smiles"
                "charge_method": str,       # "nagl_am1bcc" | "ambertools_am1bcc" | ...
            }

        Note:
            반환 키는 `e_interaction` (smina_ prefix 없음).
            DB 컬럼 매핑: om_e_interaction ← result["e_interaction"]
        Raises:
            MCPError("TOOL_ERROR"): 도구가 응답 JSON 내 "error" 키 반환 시.
        """
        # PDB 실제 HETATM resname 자동 감지 (힌트 "LIG" 가 틀릴 수 있음)
        actual_resname = self._detect_ligand_resname(pdb_file, hint=ligand_resname)
        if actual_resname != ligand_resname:
            log.info(
                "openmm.compute_interaction_energy.resname_corrected",
                hint=ligand_resname,
                actual=actual_resname,
                pdb_file=pdb_file,
            )

        args: dict[str, Any] = {
            "pdb_file": pdb_file,
            "ligand_resname": actual_resname,
            "ligand_ff": ligand_ff,
            "decompose": decompose,
        }
        if ligand_sdf is not None:
            args["ligand_sdf"] = ligand_sdf
        if ligand_smiles is not None:
            args["ligand_smiles"] = ligand_smiles
        log.info(
            "openmm.compute_interaction_energy",
            pdb_file=pdb_file,
            ligand_resname=actual_resname,
        )
        result = await self.call("compute_protein_ligand_interaction_energy", args)

        # 도구가 isError=false 로 응답하더라도 content JSON 에 "error" 키가 있으면
        # 실패로 처리 (openMM_bot 의 application-level error 패턴)
        if isinstance(result, dict) and "error" in result and "e_interaction" not in result:
            err_msg = result["error"]
            log.warning(
                "openmm.compute_interaction_energy.tool_error",
                pdb_file=pdb_file,
                ligand_resname=actual_resname,
                error=err_msg,
            )
            raise MCPError("TOOL_ERROR", f"compute_interaction_energy: {err_msg}")

        e_inter = result.get("e_interaction") if isinstance(result, dict) else None
        log.debug("openmm.compute_interaction_energy.done", e_interaction_kJ=e_inter)
        return result
