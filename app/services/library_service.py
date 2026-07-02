"""라이브러리 서비스 — SDF/SMI 파싱, RDKit 정규화, dedup, DB 저장.

backend_plan.md §4 권위.
InChIKey: rdkit.Chem.inchi 모듈 경유 (Chem.MolToInchiKey 사용 금지).
최대 fragment: SaltRemover + GetMolFrags(asMols=True) 헤비 원자 최대값.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from app.models.library import Library, LigandEntry
from app.utils.ids import ulid_str, short_id
from app.utils.validators import validate_smiles, ValidationError

log = structlog.get_logger("service.library")


# ── RDKit lazy imports ────────────────────────────────────────────────────────

def _rdkit_imports() -> tuple[Any, Any, Any, Any, Any]:
    """RDKit 관련 모듈 lazy import (import 시점 에러 방지)."""
    from rdkit import Chem
    from rdkit.Chem import Descriptors, AllChem
    from rdkit.Chem.MolStandardize import rdMolStandardize
    from rdkit.Chem.SaltRemover import SaltRemover
    from rdkit.Chem import rdmolops
    return Chem, Descriptors, AllChem, SaltRemover, rdmolops


def _get_inchi_key(mol: Any) -> str | None:
    """InChIKey 생성.

    rdkit.Chem.inchi.InchiToInchiKey(inchi.MolToInchi(mol)) 경로만 사용.
    Chem.MolToInchiKey 는 일부 버전에서 누락되므로 금지.
    """
    try:
        from rdkit.Chem import inchi
        inchi_str = inchi.MolToInchi(mol)
        if inchi_str is None:
            return None
        return inchi.InchiToInchiKey(inchi_str)
    except Exception:
        return None


def _largest_fragment(mol: Any) -> Any:
    """최대 헤비원자 fragment 반환 (SaltRemover → GetMolFrags)."""
    from rdkit.Chem.SaltRemover import SaltRemover
    from rdkit.Chem import rdmolops

    remover = SaltRemover()
    stripped = remover.StripMol(mol)
    if stripped is None:
        stripped = mol

    frags = rdmolops.GetMolFrags(stripped, asMols=True)
    if not frags:
        return stripped

    return max(frags, key=lambda m: m.GetNumHeavyAtoms())


def _mol_to_entry(
    mol: Any,
    source_index: int,
    Chem: Any,
    Descriptors: Any,
) -> LigandEntry | None:
    """RDKit Mol 객체 → LigandEntry. 실패 시 None."""
    try:
        frag = _largest_fragment(mol)
        canonical_smi = Chem.MolToSmiles(frag, canonical=True)

        # validate_smiles 로 drug-like 검증 (heavy_atoms 5~100)
        try:
            canonical_smi = validate_smiles(canonical_smi)
        except ValidationError:
            return None

        inchi_key = _get_inchi_key(frag)
        if inchi_key is None:
            return None

        mw = Descriptors.MolWt(frag)
        heavy_atoms = frag.GetNumHeavyAtoms()

        # 분자 이름 (SDF 기준 _Name, SMI 기준 두 번째 컬럼)
        name: str | None = mol.GetProp("_Name") if mol.HasProp("_Name") else None
        if name and not name.strip():
            name = None

        # 추가 메타데이터 수집
        metadata: dict = {}
        for prop in mol.GetPropNames():
            if prop.startswith("_"):
                continue
            try:
                metadata[prop] = mol.GetProp(prop)
            except Exception:
                pass

        return LigandEntry(
            ligand_id=short_id(),
            source_index=source_index,
            name=name,
            smiles=canonical_smi,
            inchi_key=inchi_key,
            mw=round(mw, 4),
            heavy_atoms=heavy_atoms,
            metadata=metadata,
        )
    except Exception as e:
        log.warning("library.mol_parse_error", source_index=source_index, error=str(e))
        return None


# ── 파서 ─────────────────────────────────────────────────────────────────────

def _parse_sdf(content: bytes, max_entries: int) -> list[Any]:
    """SDF 파일 바이트 → Mol 리스트 (max_entries 제한)."""
    from rdkit.Chem import SDMolSupplier
    import io

    supplier = SDMolSupplier()
    supplier.SetData(content.decode("utf-8", errors="replace"))
    mols = []
    for mol in supplier:
        if mol is None:
            continue
        mols.append(mol)
        if len(mols) >= max_entries:
            break
    return mols


def _parse_smi(content: bytes, max_entries: int) -> list[Any]:
    """SMI/SMILES 파일 바이트 → Mol 리스트 (탭·공백 구분, 2열 = 이름)."""
    from rdkit import Chem

    mols = []
    for line in content.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        smi = parts[0]
        name = parts[1] if len(parts) > 1 else None

        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        if name:
            mol.SetProp("_Name", name)
        mols.append(mol)
        if len(mols) >= max_entries:
            break
    return mols


# ── 서비스 ────────────────────────────────────────────────────────────────────

class LibraryService:
    """SDF/SMI 라이브러리 파싱·저장 서비스.

    Methods (Phase 1):
        parse(file_bytes, filename, max_entries) -> Library
        save_to_db(lib) -> None
        get(library_id) -> Library | None
    """

    def __init__(self, db_path: Path, library_dir: Path | None = None):
        self._db_path = db_path
        self._library_dir = library_dir

    # ── 파싱 ─────────────────────────────────────────────────────────────────

    def parse(
        self,
        file_bytes: bytes,
        filename: str,
        max_entries: int = 1000,
    ) -> Library:
        """파일 바이트 → Library 모델.

        - SDF 또는 SMI/SMILES 파일 자동 감지 (확장자 기준)
        - RDKit canonical SMILES 정규화
        - InChIKey 두 단계 생성 (rdkit.Chem.inchi 경유)
        - largest fragment (SaltRemover + GetMolFrags)
        - InChIKey 기준 중복 제거
        """
        Chem, Descriptors, _, _sr, _rdmolops = _rdkit_imports()

        ext = Path(filename).suffix.lower()
        if ext in (".sdf", ".mol", ".mol2"):
            raw_mols = _parse_sdf(file_bytes, max_entries)
        else:
            # .smi / .smiles / .csv / 기타 → SMILES 형식으로 시도
            raw_mols = _parse_smi(file_bytes, max_entries)

        log.info("library.parse.start", filename=filename, raw_count=len(raw_mols))

        entries: list[LigandEntry] = []
        seen_inchi: dict[str, int] = {}  # inchi_key → first source_index
        n_duplicates = 0

        for idx, mol in enumerate(raw_mols):
            entry = _mol_to_entry(mol, idx, Chem, Descriptors)
            if entry is None:
                continue
            if entry.inchi_key in seen_inchi:
                n_duplicates += 1
                log.debug(
                    "library.duplicate",
                    inchi_key=entry.inchi_key,
                    source_index=idx,
                    first_seen=seen_inchi[entry.inchi_key],
                )
                continue
            seen_inchi[entry.inchi_key] = idx
            entries.append(entry)

        library_id = ulid_str()
        lib = Library(
            library_id=library_id,
            source_filename=filename,
            n_entries=len(raw_mols),
            n_unique=len(entries),
            n_duplicates=n_duplicates,
            entries=entries,
        )
        log.info(
            "library.parse.done",
            library_id=library_id,
            n_entries=lib.n_entries,
            n_unique=lib.n_unique,
            n_duplicates=lib.n_duplicates,
        )
        return lib

    # ── DB 저장 ───────────────────────────────────────────────────────────────

    async def save_to_db(self, lib: Library) -> None:
        """Library 를 SQLite libraries 테이블에 저장."""
        import aiosqlite
        from datetime import datetime, timezone

        payload = lib.model_dump()
        payload_json = json.dumps(payload)
        now = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO libraries
                    (library_id, filename, n_entries, n_unique, n_duplicates, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(library_id) DO UPDATE SET
                    n_entries=excluded.n_entries,
                    n_unique=excluded.n_unique,
                    n_duplicates=excluded.n_duplicates,
                    payload_json=excluded.payload_json
                """,
                (
                    lib.library_id,
                    lib.source_filename,
                    lib.n_entries,
                    lib.n_unique,
                    lib.n_duplicates,
                    now,
                    payload_json,
                ),
            )
            await db.commit()
        log.info("library.saved", library_id=lib.library_id)

    # ── 조회 ─────────────────────────────────────────────────────────────────

    async def get(self, library_id: str) -> Library | None:
        """library_id 로 Library 조회. 없으면 None."""
        import aiosqlite

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT payload_json FROM libraries WHERE library_id = ?",
                (library_id,),
            ) as cur:
                row = await cur.fetchone()

        if row is None:
            return None

        payload = json.loads(row["payload_json"])
        return Library.model_validate(payload)
