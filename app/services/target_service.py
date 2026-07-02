"""타겟 단백질 준비 서비스 — Stage 2.

pipeline_design.md §2 Stage 2 권위.

우선순위: sequence > uniprot_id > pdb_id > protein_name
UniProt / PDB FASTA 조회, 서열 검증, AF3 input JSON template 생성.
"""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache
from typing import Literal, Any

import httpx
import structlog

from app.utils.validators import validate_sequence, ValidationError

log = structlog.get_logger("service.target")

# FASTA 헤더 제거용
_FASTA_HEADER = re.compile(r"^>.*$", re.MULTILINE)

# UniProt 단백질 이름 검색 URL
_UNIPROT_FASTA_URL = "https://rest.uniprot.org/uniprotkb/{uid}.fasta"
_UNIPROT_SEARCH_URL = (
    "https://rest.uniprot.org/uniprotkb/search"
    "?query=protein_name:{name}+AND+organism_id:9606&format=fasta&size=1"
)
_PDB_FASTA_URL = "https://www.rcsb.org/fasta/entry/{pdb_id}"


# ── Pydantic 모델 ─────────────────────────────────────────────────────────────

from pydantic import BaseModel, model_validator


class TargetInput(BaseModel):
    """Stage 2 입력 — 택일 (우선순위: sequence > uniprot_id > pdb_id > protein_name)."""

    protein_name: str | None = None
    uniprot_id: str | None = None
    pdb_id: str | None = None
    sequence: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "TargetInput":
        if not any([self.protein_name, self.uniprot_id, self.pdb_id, self.sequence]):
            raise ValueError("TargetInput: 최소 하나의 필드가 필요합니다.")
        return self


class TargetPrepResult(BaseModel):
    """Stage 2 출력 — 타겟 준비 결과."""

    target_name: str
    uniprot_id: str | None
    sequence: str
    sequence_length: int
    source: Literal["user_input", "uniprot", "pdb", "uniprot_search"]
    json_template: dict  # AF3 input JSON template (ligand 플레이스홀더 포함)
    warnings: list[str] = []


# ── 캐시 헬퍼 (in-memory, per-process) ───────────────────────────────────────

@lru_cache(maxsize=128)
def _cached_uniprot_fasta(uid: str) -> str:
    """UniProt FASTA 동기 캐시 항목 (bytes → str).

    실제 네트워크 호출은 async 에서 수행 후 이 캐시에 저장하는 방식 아님.
    lru_cache 는 동기 함수에만 적용 가능하므로, async 래퍼가 캐시 키를 직접 관리.
    사용처: _async_uniprot_fasta 의 in-process 결과 저장.
    """
    raise RuntimeError("직접 호출 금지 — async 래퍼 경유")


@lru_cache(maxsize=128)
def _cached_pdb_fasta(pdb_id: str) -> str:
    raise RuntimeError("직접 호출 금지 — async 래퍼 경유")


# process-local async 캐시 (캐시 TTL: 프로세스 수명 동안 유지, 실질 ~1h 내 재시작)
_uniprot_cache: dict[str, str] = {}
_pdb_cache: dict[str, str] = {}


# ── FASTA 파서 ────────────────────────────────────────────────────────────────

def _extract_sequence_from_fasta(fasta: str, first_chain_only: bool = True) -> str:
    """FASTA 텍스트 → 아미노산 서열 (헤더 제거, 공백 제거).

    first_chain_only=True 이면 첫 번째 엔트리만 사용.
    """
    entries: list[str] = []
    current_seq_lines: list[str] = []
    in_first = False

    for line in fasta.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            if first_chain_only and in_first and current_seq_lines:
                break
            in_first = True
            current_seq_lines = []
        elif in_first and stripped:
            current_seq_lines.append(stripped)

    return "".join(current_seq_lines).upper()


# ── 서비스 ────────────────────────────────────────────────────────────────────

class TargetService:
    """타겟 단백질 준비 서비스.

    httpx.AsyncClient 를 소유한다 (lifespan 에서 aclose 필요).
    """

    def __init__(self, http_timeout: float = 10.0) -> None:
        self._timeout = http_timeout
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=http_timeout,
            follow_redirects=True,
            headers={"User-Agent": "afmm_chat/0.2 (contact: suppakoko@gmail.com)"},
        )

    async def aclose(self) -> None:
        """httpx 클라이언트 종료."""
        await self._client.aclose()

    # ── 공개 API ─────────────────────────────────────────────────────────────

    async def resolve(self, input: TargetInput) -> TargetPrepResult:
        """TargetInput → TargetPrepResult.

        우선순위: sequence > uniprot_id > pdb_id > protein_name
        """
        log.info(
            "target.resolve.start",
            has_sequence=bool(input.sequence),
            has_uniprot=bool(input.uniprot_id),
            has_pdb=bool(input.pdb_id),
            has_name=bool(input.protein_name),
        )

        if input.sequence:
            result = await self.from_sequence(input.sequence, name="user_target")
        elif input.uniprot_id:
            result = await self.from_uniprot(input.uniprot_id)
        elif input.pdb_id:
            result = await self.from_pdb(input.pdb_id)
        elif input.protein_name:
            result = await self._from_name(input.protein_name)
        else:
            raise ValueError("TargetInput 필드가 모두 None")

        log.info(
            "target.resolve.done",
            target_name=result.target_name,
            source=result.source,
            sequence_length=result.sequence_length,
            n_warnings=len(result.warnings),
        )
        return result

    async def from_uniprot(self, uniprot_id: str) -> TargetPrepResult:
        """UniProt ID → TargetPrepResult."""
        uid = uniprot_id.strip().upper()
        fasta = await self._fetch_uniprot_fasta(uid)
        seq = _extract_sequence_from_fasta(fasta)
        seq, warnings = self._validate_and_warn(seq)

        return TargetPrepResult(
            target_name=uid,
            uniprot_id=uid,
            sequence=seq,
            sequence_length=len(seq),
            source="uniprot",
            json_template=self.build_af3_template(seq, uid),
            warnings=warnings,
        )

    async def from_pdb(self, pdb_id: str) -> TargetPrepResult:
        """PDB ID → TargetPrepResult (첫 chain 만 사용)."""
        pid = pdb_id.strip().upper()
        fasta = await self._fetch_pdb_fasta(pid)
        seq = _extract_sequence_from_fasta(fasta, first_chain_only=True)
        seq, warnings = self._validate_and_warn(seq)

        return TargetPrepResult(
            target_name=pid,
            uniprot_id=None,
            sequence=seq,
            sequence_length=len(seq),
            source="pdb",
            json_template=self.build_af3_template(seq, pid),
            warnings=warnings,
        )

    async def from_sequence(
        self, seq: str, name: str = "user_target"
    ) -> TargetPrepResult:
        """직접 입력 서열 → TargetPrepResult."""
        validated, warnings = self._validate_and_warn(seq)
        return TargetPrepResult(
            target_name=name,
            uniprot_id=None,
            sequence=validated,
            sequence_length=len(validated),
            source="user_input",
            json_template=self.build_af3_template(validated, name),
            warnings=warnings,
        )

    async def _from_name(self, protein_name: str) -> TargetPrepResult:
        """단백질 이름 → UniProt 검색 → TargetPrepResult."""
        name_clean = protein_name.strip()
        fasta = await self._search_uniprot_by_name(name_clean)
        seq = _extract_sequence_from_fasta(fasta)
        seq, warnings = self._validate_and_warn(seq)

        warnings.insert(0, f"protein_name '{name_clean}' → UniProt 검색 결과 (첫 번째 hit 사용)")

        return TargetPrepResult(
            target_name=name_clean,
            uniprot_id=None,
            sequence=seq,
            sequence_length=len(seq),
            source="uniprot_search",
            json_template=self.build_af3_template(seq, name_clean),
            warnings=warnings,
        )

    def build_af3_template(self, sequence: str, name: str) -> dict:
        """AF3 input JSON v2 template 생성.

        sequences: [protein, ligand placeholder]
        dialect: 'alphafold3', version: 2
        """
        return {
            "name": name,
            "modelSeeds": [1],
            "sequences": [
                {
                    "protein": {
                        "id": "A",
                        "sequence": sequence,
                    }
                },
                {
                    "ligand": {
                        "id": "B",
                        "smiles": "PLACEHOLDER",
                    }
                },
            ],
            "dialect": "alphafold3",
            "version": 2,
        }

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _validate_and_warn(self, seq: str) -> tuple[str, list[str]]:
        """서열 검증 + 경고 생성. ValidationError 는 상위로 전파."""
        warnings: list[str] = []
        validated = validate_sequence(seq)
        n = len(validated)
        if n > 1500:
            warnings.append(
                f"서열 길이 {n}aa > 1500 — AF3 inference 시간 증가 예상"
            )
        if n > 800:
            warnings.append(
                f"서열 길이 {n}aa — AF3 token budget 초과 가능성 확인 필요"
            )
        return validated, warnings

    async def _fetch_with_retry(self, url: str) -> str:
        """GET 요청, 실패 시 1회 재시도."""
        for attempt in range(2):
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                return resp.text
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                if attempt == 0:
                    log.warning(
                        "target.http_retry",
                        url=url,
                        error=str(exc),
                    )
                    await asyncio.sleep(1.0)
                    continue
                log.error("target.http_failed", url=url, error=str(exc))
                raise RuntimeError(f"HTTP 요청 실패: {url} — {exc}") from exc
        raise RuntimeError("unreachable")

    async def _fetch_uniprot_fasta(self, uid: str) -> str:
        """UniProt FASTA 조회 (in-process 캐시)."""
        if uid in _uniprot_cache:
            log.debug("target.uniprot_cache.hit", uid=uid)
            return _uniprot_cache[uid]
        url = _UNIPROT_FASTA_URL.format(uid=uid)
        fasta = await self._fetch_with_retry(url)
        _uniprot_cache[uid] = fasta
        return fasta

    async def _fetch_pdb_fasta(self, pdb_id: str) -> str:
        """PDB FASTA 조회 (in-process 캐시)."""
        if pdb_id in _pdb_cache:
            log.debug("target.pdb_cache.hit", pdb_id=pdb_id)
            return _pdb_cache[pdb_id]
        url = _PDB_FASTA_URL.format(pdb_id=pdb_id)
        fasta = await self._fetch_with_retry(url)
        _pdb_cache[pdb_id] = fasta
        return fasta

    async def _search_uniprot_by_name(self, protein_name: str) -> str:
        """UniProt 이름 검색 (human, size=1)."""
        url = _UNIPROT_SEARCH_URL.format(name=protein_name)
        fasta = await self._fetch_with_retry(url)
        if not fasta.strip():
            raise ValueError(
                f"UniProt 검색 결과 없음: protein_name='{protein_name}'"
            )
        return fasta
