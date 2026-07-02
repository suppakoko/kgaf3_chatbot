"""AF3 holo 복합체(mmCIF) → receptor.pdb + ligand.sdf 분리 유틸 (RDKit).

smina-minimize 재구성(docs/02-design/features/smina-minimize-refactor.md §3.4 B-1)에서
OpenMM `run_af3_full_pipeline` 의 복합체 분리 기능을 대체한다.

의존성: RDKit + 표준 라이브러리만 사용 (gemmi/biopython 미사용 — 환경 미설치).

처리:
  1. AF3 mmCIF 의 `_atom_site` loop 를 직접 파싱.
  2. 단백질 원자(group_PDB=ATOM) → PDB 로 기록 = receptor.pdb.
  3. 리간드 원자(group_PDB=HETATM) → PDB block → RDKit MolFromPDBBlock(removeHs)
     → SMILES template 로 `AssignBondOrdersFromTemplate` bond order 복원 → ligand.sdf.

체인 가정: 단일 단백질 + 단일 리간드 holo. group_PDB(ATOM/HETATM)로 1차 분류하므로
체인 문자(A/B)에 의존하지 않는다(설계 문서의 A/B 가정보다 견고).
"""

from __future__ import annotations

from pathlib import Path

import structlog
from rdkit import Chem
from rdkit.Chem import AllChem

log = structlog.get_logger("service.complex_splitter")


class ComplexSplitError(RuntimeError):
    """복합체 분리 실패 (per-ligand 격리에서 catch)."""


# ── mmCIF atom_site 파싱 ──────────────────────────────────────────────────────

def _parse_atom_site(cif_path: str) -> list[dict]:
    """mmCIF 의 _atom_site loop 를 파싱하여 atom dict 리스트 반환.

    각 dict 키: group_PDB, id, type_symbol, atom_id, comp_id, asym_id,
    seq_id, x, y, z. 누락 컬럼은 빈 문자열/0.0.
    """
    text = Path(cif_path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    # _atom_site loop 의 컬럼 헤더 수집
    headers: list[str] = []
    data_start = -1
    in_loop = False
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line == "loop_":
            # 다음 줄들이 _atom_site. 로 시작하는지 확인하기 위해 헤더 임시 수집 시작
            headers = []
            in_loop = True
            continue
        if in_loop and line.startswith("_atom_site."):
            headers.append(line.split(".", 1)[1].split()[0])
            continue
        if in_loop and headers and not line.startswith("_atom_site."):
            # 헤더 종료 → 데이터 시작
            if any(h for h in headers):
                data_start = i
                break
            in_loop = False

    if data_start < 0 or not headers:
        raise ComplexSplitError(f"_atom_site loop not found in {cif_path}")

    idx = {name: pos for pos, name in enumerate(headers)}

    def col(tok: list[str], key: str, default: str = "") -> str:
        p = idx.get(key)
        if p is None or p >= len(tok):
            return default
        return tok[p]

    atoms: list[dict] = []
    for raw in lines[data_start:]:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("loop_") or line.startswith("_"):
            if line.startswith("_atom_site."):
                continue
            # 다른 카테고리/loop 시작 → atom_site 종료
            if line.startswith("_") or line == "loop_":
                break
            continue
        tok = line.split()
        if len(tok) < len(headers):
            continue
        try:
            atoms.append(
                {
                    "group_PDB": col(tok, "group_PDB", "ATOM"),
                    "id": col(tok, "id", "0"),
                    "type_symbol": col(tok, "type_symbol", ""),
                    "atom_id": col(tok, "label_atom_id") or col(tok, "auth_atom_id", "X"),
                    "comp_id": col(tok, "label_comp_id") or col(tok, "auth_comp_id", "UNL"),
                    "asym_id": col(tok, "auth_asym_id") or col(tok, "label_asym_id", "A"),
                    "seq_id": col(tok, "auth_seq_id") or col(tok, "label_seq_id", "1"),
                    "x": float(col(tok, "Cartn_x", "0") or 0.0),
                    "y": float(col(tok, "Cartn_y", "0") or 0.0),
                    "z": float(col(tok, "Cartn_z", "0") or 0.0),
                }
            )
        except (ValueError, IndexError):
            continue

    if not atoms:
        raise ComplexSplitError(f"no atoms parsed from {cif_path}")
    return atoms


# ── PDB 라인 포맷 ─────────────────────────────────────────────────────────────

def _pdb_atom_line(rec: str, serial: int, atom: dict) -> str:
    """단일 원자 → PDB ATOM/HETATM 라인(컬럼 규약 준수)."""
    name = atom["atom_id"]
    # atom name 정렬: 1글자 element 는 col 14 부터(앞 공백), 4글자는 그대로
    elem = (atom["type_symbol"] or name[:1]).strip()
    if len(name) >= 4:
        name_fmt = name[:4]
    elif len(elem) == 1:
        name_fmt = f" {name:<3}"
    else:
        name_fmt = f"{name:<4}"
    resname = (atom["comp_id"] or "UNL")[:3]
    chain = (atom["asym_id"] or "A")[:1]
    try:
        resseq = int(atom["seq_id"])
    except (ValueError, TypeError):
        resseq = 1
    return (
        f"{rec:<6}{serial:>5} {name_fmt}"
        f" {resname:>3} {chain:1}{resseq:>4}    "
        f"{atom['x']:8.3f}{atom['y']:8.3f}{atom['z']:8.3f}"
        f"{1.0:6.2f}{0.0:6.2f}          {elem:>2}"
    )


def _is_ligand(atom: dict, ligand_chain: str | None) -> bool:
    """원자가 리간드인지 판정 — group_PDB=HETATM 1차, 체인 보조."""
    if atom["group_PDB"].upper() == "HETATM":
        return True
    if ligand_chain and atom["asym_id"] == ligand_chain and atom["group_PDB"].upper() != "ATOM":
        return True
    return False


# ── 메인 진입점 ───────────────────────────────────────────────────────────────

def split_complex(
    cif_path: str,
    smiles: str,
    out_dir: str,
    ligand_id: str,
    ligand_chain: str | None = "B",
) -> tuple[str, str]:
    """AF3 holo CIF → (receptor_pdb_path, ligand_sdf_path).

    Args:
        cif_path: AF3 holo 복합체 mmCIF 경로.
        smiles: 해당 리간드 SMILES (bond order 복원 template). 필수.
        out_dir: 출력 디렉토리 (smina 가 읽을 수 있는 공유 경로여야 함).
        ligand_id: 출력 파일 prefix.
        ligand_chain: 리간드 체인 힌트(보조). 기본 "B".

    Returns:
        (receptor_pdb_path, ligand_sdf_path)

    Raises:
        ComplexSplitError: 파싱/분리/bond-order 복원 실패.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    atoms = _parse_atom_site(cif_path)
    protein_atoms = [a for a in atoms if not _is_ligand(a, ligand_chain)]
    ligand_atoms = [a for a in atoms if _is_ligand(a, ligand_chain)]

    if not protein_atoms:
        raise ComplexSplitError(f"no protein(ATOM) atoms in {cif_path}")
    if not ligand_atoms:
        raise ComplexSplitError(f"no ligand(HETATM) atoms in {cif_path}")

    # 1) receptor.pdb
    receptor_path = out / f"{ligand_id}_receptor.pdb"
    rec_lines = [_pdb_atom_line("ATOM", i + 1, a) for i, a in enumerate(protein_atoms)]
    rec_lines.append("TER")
    rec_lines.append("END")
    receptor_path.write_text("\n".join(rec_lines) + "\n", encoding="utf-8")

    # 2) ligand: HETATM → PDB block → RDKit → SMILES template bond order → SDF
    lig_lines = [_pdb_atom_line("HETATM", i + 1, a) for i, a in enumerate(ligand_atoms)]
    lig_lines.append("END")
    lig_block = "\n".join(lig_lines) + "\n"

    pose = Chem.MolFromPDBBlock(lig_block, sanitize=False, removeHs=True)
    if pose is None:
        raise ComplexSplitError(f"RDKit failed to read ligand PDB block (ligand_id={ligand_id})")

    template = Chem.MolFromSmiles(smiles)
    if template is None:
        raise ComplexSplitError(f"invalid SMILES template: {smiles!r}")

    try:
        mol = AllChem.AssignBondOrdersFromTemplate(template, pose)
    except Exception as exc:  # noqa: BLE001 — RDKit 다양한 예외
        raise ComplexSplitError(
            f"AssignBondOrdersFromTemplate failed (ligand_id={ligand_id}): {exc}"
        ) from exc

    mol.SetProp("_Name", ligand_id)
    ligand_path = out / f"{ligand_id}_ligand.sdf"
    writer = Chem.SDWriter(str(ligand_path))
    try:
        writer.write(mol)
    finally:
        writer.close()

    log.debug(
        "complex_splitter.done",
        ligand_id=ligand_id,
        n_protein=len(protein_atoms),
        n_ligand=len(ligand_atoms),
        receptor=str(receptor_path),
        ligand=str(ligand_path),
    )
    return str(receptor_path), str(ligand_path)
