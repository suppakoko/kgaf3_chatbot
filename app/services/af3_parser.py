"""AF3 결과 폴더 파서 + interface PAE 계산.

pipeline_design.md §2 Stage 4 권위.

AF3 v2 파일 레이아웃:
  {job_name}_summary_confidences.json  — iptm, ranking_score, mean_pae, mean_plddt
  {job_name}_confidences.json          — pae (n_tokens×n_tokens), atom_plddts, token_chain_ids
  {job_name}_model.cif                 — 최종 구조 (structure_paths[0])
"""

from __future__ import annotations

import glob
import json
import pathlib
from typing import Any

import structlog

log = structlog.get_logger("service.af3_parser")


# ── interface PAE 계산 ────────────────────────────────────────────────────────

def compute_interface_pae(
    pae_matrix: list[list[float]] | Any | None,
    token_chain_ids: list[str] | None,
    protein_chain: str = "A",
    ligand_chain: str = "B",
) -> tuple[float | None, float | None]:
    """protein-ligand cross-block PAE 추출 → (min, mean).

    Args:
        pae_matrix: PAE 행렬 (n_tokens × n_tokens).  None 이거나 빈 경우 (None, None) 반환.
        token_chain_ids: 각 토큰의 체인 ID 리스트 (pae 행렬과 동일 길이).
        protein_chain: 단백질 체인 ID (기본 "A").
        ligand_chain: 리간드 체인 ID (기본 "B").

    Returns:
        (pae_min_interface, pae_mean_interface) — 계산 불가 시 (None, None).
    """
    if not pae_matrix or not token_chain_ids:
        return None, None

    try:
        # numpy 사용 (있으면)
        try:
            import numpy as np  # type: ignore[import]

            mat = np.array(pae_matrix, dtype=float)
            chains = token_chain_ids
            p_idx = [i for i, c in enumerate(chains) if c == protein_chain]
            l_idx = [i for i, c in enumerate(chains) if c == ligand_chain]

            if not p_idx or not l_idx:
                log.warning(
                    "af3_parser.interface_pae.empty_idx",
                    protein_chain=protein_chain,
                    ligand_chain=ligand_chain,
                    n_tokens=len(chains),
                )
                return None, None

            cross = mat[np.ix_(p_idx, l_idx)]
            return float(cross.min()), float(cross.mean())

        except ImportError:
            # pure Python fallback
            chains = token_chain_ids
            p_idx = [i for i, c in enumerate(chains) if c == protein_chain]
            l_idx = [i for i, c in enumerate(chains) if c == ligand_chain]

            if not p_idx or not l_idx:
                return None, None

            values: list[float] = []
            for i in p_idx:
                row = pae_matrix[i]
                for j in l_idx:
                    values.append(float(row[j]))

            if not values:
                return None, None

            return min(values), sum(values) / len(values)

    except Exception as exc:
        log.warning("af3_parser.interface_pae.error", error=str(exc))
        return None, None


# ── 폴더 파싱 헬퍼 ────────────────────────────────────────────────────────────

def _glob_first(folder: pathlib.Path, pattern: str) -> pathlib.Path | None:
    """폴더에서 패턴에 맞는 첫 번째 파일 반환 (없으면 None)."""
    matches = sorted(folder.glob(pattern))
    return matches[0] if matches else None


def parse_af3_folder(
    folder: str | pathlib.Path,
    ligand_id: str,
    af3_job_id: str,
    protein_chain: str = "A",
    ligand_chain: str = "B",
) -> dict[str, Any]:
    """AF3 출력 폴더에서 신뢰도 지표를 파싱한다.

    Returns:
        dict 형태의 파싱 결과:
        {
            "ligand_id": str,
            "af3_job_id": str,
            "cif_path": str | None,
            "iptm": float | None,
            "ptm": float | None,
            "ranking_score": float | None,
            "fraction_disordered": float | None,
            "has_clash": bool | None,
            "pae_min_interface": float | None,
            "pae_mean_interface": float | None,
            "mean_plddt": float | None,
            "raw_summary": dict,
            "error": str | None,
        }
    """
    folder = pathlib.Path(folder)
    result: dict[str, Any] = {
        "ligand_id": ligand_id,
        "af3_job_id": af3_job_id,
        "cif_path": None,
        "iptm": None,
        "ptm": None,
        "ranking_score": None,
        "fraction_disordered": None,
        "has_clash": None,
        "pae_min_interface": None,
        "pae_mean_interface": None,
        "mean_plddt": None,
        "raw_summary": {},
        "error": None,
    }

    if not folder.exists():
        result["error"] = f"폴더 없음: {folder}"
        log.warning("af3_parser.folder_missing", folder=str(folder))
        return result

    # ── summary_confidences.json ──────────────────────────────────────────
    summary_path = _glob_first(folder, "*summary_confidences.json")
    if summary_path:
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            result["raw_summary"] = summary
            result["iptm"] = summary.get("iptm")
            result["ptm"] = summary.get("ptm")
            result["ranking_score"] = summary.get("ranking_score")
            result["fraction_disordered"] = summary.get("fraction_disordered")
            result["has_clash"] = summary.get("has_clash")
            # AF3 v2: summary 에 mean_pae / mean_plddt 포함
            result["mean_plddt"] = summary.get("mean_plddt")
            # mean_pae from summary if available (cross-chain interface가 아닌 전체 평균)
        except Exception as exc:
            log.warning(
                "af3_parser.summary_parse_error",
                path=str(summary_path),
                error=str(exc),
            )
            result["error"] = f"summary_confidences 파싱 실패: {exc}"
    else:
        log.warning("af3_parser.no_summary_file", folder=str(folder))

    # ── confidences.json (PAE matrix + token_chain_ids) ───────────────────
    conf_path = _glob_first(folder, "*confidences.json")
    # summary_confidences.json 과 겹치지 않도록 필터
    if conf_path and "summary" in conf_path.name:
        # 더 구체적인 패턴으로 재시도 (summary 제외)
        candidates = [
            p for p in folder.glob("*confidences.json")
            if "summary" not in p.name
        ]
        conf_path = candidates[0] if candidates else None

    if conf_path:
        try:
            confidences = json.loads(conf_path.read_text(encoding="utf-8"))
            pae = confidences.get("pae")
            token_chain_ids = confidences.get("token_chain_ids")
            pae_min, pae_mean = compute_interface_pae(
                pae, token_chain_ids, protein_chain, ligand_chain
            )
            result["pae_min_interface"] = pae_min
            result["pae_mean_interface"] = pae_mean

            # mean_plddt fallback: atom_plddts 평균 (summary에 없을 경우)
            if result["mean_plddt"] is None:
                atom_plddts = confidences.get("atom_plddts")
                if atom_plddts:
                    try:
                        result["mean_plddt"] = sum(atom_plddts) / len(atom_plddts)
                    except Exception:
                        pass
        except Exception as exc:
            log.warning(
                "af3_parser.confidences_parse_error",
                path=str(conf_path),
                error=str(exc),
            )
            # confidences 파싱 실패는 치명적이지 않음 — PAE None 으로 계속
    else:
        log.debug("af3_parser.no_confidences_file", folder=str(folder))

    # ── CIF 경로 ────────────────────────────────────────────────────────────
    cif_path = _glob_first(folder, "*.cif")
    if cif_path:
        result["cif_path"] = str(cif_path)

    return result
