"""입력 검증: SMILES, 단백질 서열, 파일 경로.

security_plan §4 + §5.2 권위. 알파벳 정책: 표준 20 + U/O/X/* (FASTA 호환).
"""

from __future__ import annotations

import re
from pathlib import Path

# 표준 20 + selenocysteine(U) + pyrrolysine(O) + unknown(X) + stop(*)
_SEQUENCE_PATTERN = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYUOX*]+$")
_JOB_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")

# OpenRouter model ID: vendor/model 형식, 점 허용 (gemini-pro-1.5)
_MODEL_ID_PATTERN = re.compile(r"^[a-z0-9_-]+/[a-z0-9._-]+$")


class ValidationError(ValueError):
    """입력 검증 실패."""


def validate_sequence(seq: str) -> str:
    """단백질 아미노산 서열 검증 + 정규화.

    - 길이 [10, 2000]
    - 알파벳 표준 20 + U/O/X/* (security_plan 부록 B 와 일관)
    - whitespace strip + 대문자화

    Returns: 정규화된 서열
    Raises: ValidationError
    """
    if not seq:
        raise ValidationError("empty sequence")
    cleaned = re.sub(r"\s+", "", seq).upper()
    if not _SEQUENCE_PATTERN.fullmatch(cleaned):
        raise ValidationError("non-canonical amino acid characters")
    n = len(cleaned)
    if n < 10:
        raise ValidationError(f"sequence too short: {n} (min=10)")
    if n > 2000:
        raise ValidationError(f"sequence too long: {n} (max=2000)")
    return cleaned


def validate_smiles(smi: str) -> str:
    """SMILES 검증 + canonical 형식 반환.

    RDKit 가용 시 MolFromSmiles + MolToSmiles 라운드트립.
    헤비 원자 [5, 100] 범위 강제 (drug-like).

    Returns: canonical SMILES (implicit H)
    Raises: ValidationError
    """
    if not smi or len(smi) > 500:
        raise ValidationError(f"invalid SMILES length: {len(smi) if smi else 0}")

    try:
        from rdkit import Chem  # lazy import (parsing 시점에만 RDKit 필요)
    except ImportError as e:
        raise ValidationError("RDKit not installed") from e

    mol = Chem.MolFromSmiles(smi)
    if mol is None:
        raise ValidationError("invalid SMILES")
    ha = mol.GetNumHeavyAtoms()
    if ha < 5:
        raise ValidationError(f"too few heavy atoms: {ha} (min=5)")
    if ha > 100:
        raise ValidationError(f"too many heavy atoms: {ha} (max=100)")
    return Chem.MolToSmiles(mol, canonical=True)


def validate_job_name(name: str) -> str:
    """잡 이름 (alphanumeric + _ -, 1-64 chars)."""
    if not _JOB_NAME_PATTERN.fullmatch(name or ""):
        raise ValidationError(f"invalid job_name: {name!r}")
    return name


def validate_model_id(model_id: str, allowed: list[str]) -> str:
    """OpenRouter 모델 ID 검증 (regex + allowlist 2-layer).

    Phase 4+ 에서 OpenRouter GET /models 카탈로그 동기화 (3rd layer) 추가.
    """
    if not _MODEL_ID_PATTERN.fullmatch(model_id or ""):
        raise ValidationError(f"invalid model id format: {model_id!r}")
    if model_id not in allowed:
        raise ValidationError(f"model not in allowlist: {model_id!r}")
    return model_id


def safe_resolve(base: Path, user_path: str) -> Path:
    """Path traversal 방어 — base.resolve() 하위에 있는지 검증.

    security_plan §4.4 패턴.
    """
    if not user_path or "\x00" in user_path:
        raise ValidationError("invalid path")
    try:
        target = (base / user_path).resolve()
    except (ValueError, OSError) as e:
        raise ValidationError(f"path resolve failed: {e}") from e
    base_resolved = base.resolve()
    if not target.is_relative_to(base_resolved):
        raise ValidationError("path traversal detected")
    return target
