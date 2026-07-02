"""Smina docking score-only / minimize tools (subprocess, CPU).

These functions are pure async logic — they are registered as MCP tools by
``server.py``. They invoke the ``smina`` binary via subprocess and parse its
stdout. No OpenMM / RDKit / conda dependency.

smina runs ``--score_only`` (no docking, evaluate at given coordinates) or
``--minimize`` (local steepest-descent of the ligand in a rigid receptor).

Output: binding affinity (kcal/mol). More negative = stronger binding.

Tool names and return keys are kept IDENTICAL to the originals so the
afmm-chat client works with zero code change:
  - smina_score_only -> smina_affinity_kcal_mol, intramolecular_energy_kcal_mol, ...
  - smina_minimize   -> minimized_affinity_kcal_mol, minimized_pose_path, ...
  - smina_score_batch (batch convenience)
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import structlog

log = structlog.get_logger("smina.tools")

# smina binary path is configurable via env (Docker installs to /usr/local/bin/smina).
SMINA_BIN = os.environ.get("SMINA_BIN", "/usr/local/bin/smina")
DEFAULT_TIMEOUT_SEC = int(os.environ.get("SMINA_DEFAULT_TIMEOUT_SEC", "120"))


def _parse_version(out: str) -> str:
    """smina 버전 문자열 추출 (실패 시 'unknown')."""
    ver_match = re.search(r"smina is based on AutoDock Vina[^\n]*\n[^\n]*", out)
    return ver_match.group(0) if ver_match else "unknown"


async def smina_score_only(
    receptor_pdb: str,
    ligand_pdb_or_sdf: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> dict:
    """Smina --score_only로 binding affinity 평가 (도킹 X, 좌표 고정).

    minimized 좌표를 그대로 입력하여 smina scoring function (Vinardo) 으로
    binding affinity를 산출한다.

    Args:
        receptor_pdb: 단백질 PDB 경로 (water/ion 제거 권장)
        ligand_pdb_or_sdf: 리간드 PDB or SDF 경로
        timeout_sec: smina subprocess timeout (default 120s)

    Returns:
        {
            "smina_affinity_kcal_mol": float,    # 음수일수록 강한 결합
            "intramolecular_energy_kcal_mol": float | None,
            "scoring_function": "vinardo",
            "smina_version": str,
            "stdout_excerpt": str,               # 첫 1000자
        }
    """
    if not Path(SMINA_BIN).exists():
        raise FileNotFoundError(f"smina not found at {SMINA_BIN}")
    if not Path(receptor_pdb).is_file():
        raise FileNotFoundError(f"receptor not found: {receptor_pdb}")
    if not Path(ligand_pdb_or_sdf).is_file():
        raise FileNotFoundError(f"ligand not found: {ligand_pdb_or_sdf}")

    cmd = [SMINA_BIN, "--score_only",
           "--receptor", receptor_pdb,
           "--ligand", ligand_pdb_or_sdf]
    log.debug("smina.score_only.exec", receptor=receptor_pdb, ligand=ligand_pdb_or_sdf)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"smina timed out after {timeout_sec}s")

    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise RuntimeError(f"smina failed (rc={proc.returncode}): {err[:500]}")

    aff_match = re.search(r"Affinity:\s*(-?\d+\.\d+)", out)
    if not aff_match:
        raise RuntimeError(f"failed to parse smina affinity from: {out[:500]}")
    affinity = float(aff_match.group(1))

    intra_match = re.search(r"Intramolecular energy:\s*(-?\d+\.\d+)", out)
    intra = float(intra_match.group(1)) if intra_match else None

    return {
        "smina_affinity_kcal_mol": affinity,
        "intramolecular_energy_kcal_mol": intra,
        "scoring_function": "vinardo",
        "smina_version": _parse_version(out),
        "stdout_excerpt": out[:1000],
    }


async def smina_minimize(
    receptor_pdb: str,
    ligand_pdb_or_sdf: str,
    out_path: str | None = None,
    scoring: str = "vinardo",
    minimize_iters: int = 0,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> dict:
    """Smina --minimize로 ligand 로컬 에너지 최소화 후 binding affinity 산출.

    rigid receptor 필드 안에서 ligand 를 국소 최소화(steepest descent)하여
    minimized pose 와 minimized affinity 를 얻는다.

    Args:
        receptor_pdb: 단백질 PDB 경로 (water/ion 제거 권장)
        ligand_pdb_or_sdf: 리간드 PDB or SDF 경로 (시작 포즈)
        out_path: minimized pose 출력 경로(SDF). None 이면 ligand 옆에 `_minimized.sdf`.
        scoring: smina scoring function (기본 "vinardo"). "vina" 도 가능.
        minimize_iters: steepest descent 반복 수. 0=수렴까지(smina 기본).
        timeout_sec: smina subprocess timeout (default 120s)

    Returns:
        {
            "minimized_affinity_kcal_mol": float,   # 음수일수록 강한 결합
            "minimized_pose_path": str | None,
            "intramolecular_energy_kcal_mol": float | None,
            "scoring_function": str,                 # 실제 전달된 함수
            "smina_version": str,
            "stdout_excerpt": str,
        }
    """
    if not Path(SMINA_BIN).exists():
        raise FileNotFoundError(f"smina not found at {SMINA_BIN}")
    if not Path(receptor_pdb).is_file():
        raise FileNotFoundError(f"receptor not found: {receptor_pdb}")
    if not Path(ligand_pdb_or_sdf).is_file():
        raise FileNotFoundError(f"ligand not found: {ligand_pdb_or_sdf}")

    if out_path is None:
        lig_p = Path(ligand_pdb_or_sdf)
        out_path = str(lig_p.with_name(f"{lig_p.stem}_minimized.sdf"))
    out_dir = Path(out_path).parent
    if not out_dir.is_dir():
        raise FileNotFoundError(f"output dir does not exist: {out_dir}")

    cmd = [SMINA_BIN, "--minimize",
           "--scoring", scoring,
           "--receptor", receptor_pdb,
           "--ligand", ligand_pdb_or_sdf,
           "--out", out_path]
    if minimize_iters and minimize_iters > 0:
        cmd += ["--minimize_iters", str(minimize_iters)]

    log.debug("smina.minimize.exec", receptor=receptor_pdb, ligand=ligand_pdb_or_sdf, scoring=scoring)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"smina --minimize timed out after {timeout_sec}s")

    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise RuntimeError(f"smina --minimize failed (rc={proc.returncode}): {err[:500]}")

    aff_match = re.search(r"Affinity:\s*(-?\d+\.\d+)", out)
    if not aff_match:
        aff_match = re.search(r"minimizedAffinity[\s:]*(-?\d+\.\d+)", out)
    if not aff_match:
        raise RuntimeError(f"failed to parse minimized affinity from: {out[:500]}")
    affinity = float(aff_match.group(1))

    intra_match = re.search(r"Intramolecular energy:\s*(-?\d+\.\d+)", out)
    intra = float(intra_match.group(1)) if intra_match else None

    return {
        "minimized_affinity_kcal_mol": affinity,
        "minimized_pose_path": out_path if Path(out_path).is_file() else None,
        "intramolecular_energy_kcal_mol": intra,
        "scoring_function": scoring,
        "smina_version": _parse_version(out),
        "stdout_excerpt": out[:1000],
    }


async def smina_score_batch(
    receptor_pdb: str,
    ligand_files: list[str],
    timeout_sec_per_ligand: int = 60,
) -> dict:
    """여러 ligand에 대해 smina --score_only 일괄 실행.

    Args:
        receptor_pdb: 공통 단백질 PDB
        ligand_files: ligand 파일 경로 리스트
        timeout_sec_per_ligand: 각 호출 timeout

    Returns:
        {
            "n_total": int,
            "n_success": int,
            "n_failed": int,
            "results": [{"ligand": path, "affinity_kcal_mol": float|None,
                         "intramolecular": float|None, "error": str|None}, ...]
        }
    """
    results = []
    for lig in ligand_files:
        try:
            res = await smina_score_only(receptor_pdb, lig, timeout_sec=timeout_sec_per_ligand)
            results.append({
                "ligand": lig,
                "affinity_kcal_mol": res["smina_affinity_kcal_mol"],
                "intramolecular": res.get("intramolecular_energy_kcal_mol"),
                "error": None,
            })
        except Exception as e:
            results.append({
                "ligand": lig,
                "affinity_kcal_mol": None,
                "intramolecular": None,
                "error": str(e)[:300],
            })
    n_success = sum(1 for r in results if r["affinity_kcal_mol"] is not None)
    return {
        "n_total": len(results),
        "n_success": n_success,
        "n_failed": len(results) - n_success,
        "results": results,
    }
