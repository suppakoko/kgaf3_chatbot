# kgaf3_chatbot 공개 가이드 (sharing_chtbot.md)

kgaf3_chatbot을 **GitHub 공개 저장소 + ghcr.io 사전 빌드 이미지 + Zenodo DOI**로
배포하는 관리자용 단계별 가이드다. 위에서 아래로 그대로 따라 하면 된다.
명령은 전부 복사·붙여넣기 가능하다.

- 저장소: <https://github.com/suppakoko/kgaf3_chatbot> (Public, 소유자 `suppakoko`)
- 메인 compose 서비스·이미지: **`kgaf3-chat`** (FastAPI 웹 UI/API, `127.0.0.1:5013`)

> 대상 독자: 이 저장소를 **세상에 공개하는 사람**. 일반 사용자(설치만 하는 사람)는
> [README.ko.md](README.ko.md)와 [configure.md](configure.md), [docs/INSTALL.md](docs/INSTALL.md)만
>보면 된다.

---

## 0. 개요 — 무엇을 공개하고, 무엇을 공개하지 않는가

kgaf3_chatbot은 **두 가지 모드**를 가진 로컬 우선 웹 챗봇이다.

1. 🧬 **GraphRAG** — 천연물 지식 그래프(NPASS 3.0 + Open Targets 25.12)에 대한
   자연어 Q&A. **기본 활성(ON by default)**.
2. 🧪 **Virtual screening** — 외부 AlphaFold3 MCP 기반 단백질–리간드 cofolding +
   동봉 `smina --minimize` 리스코어링, 결과 웹 UI.

**공개하는 것**

- kgaf3_chatbot 애플리케이션 소스(`app/`, `static/`, `templates/`, `run.py`,
  `pyproject.toml`, `uv.lock`) — 내부 모듈명은 `app`/`afmm_*`로 유지된다(내부
  식별자이므로 그대로 둔다).
- 컨테이너 정의: 루트 `Dockerfile`, `smina-mcp/Dockerfile.smina`, `docker-compose*.yml`
- 동봉 도킹 엔진: `smina-mcp/` (서버 + Dockerfile)
- 원클릭 설치기: `install.sh` / `install.bat`, 선언적 설정 `configure.md`
- 예시 환경파일: `.env.docker.example` (값은 전부 플레이스홀더)
- **GraphRAG 스택 정의(기본 활성)**: `docker-compose.yml`의 `neo4j`,
  `graphrag-mcp` 서비스. 이 두 이미지는 우리가 빌드하지 않고 **Docker Hub에서
  pull**한다 — `yoonjuho94/graphrag-neo4j:1.0`,
  `yoonjuho94/graphrag-mcp-server:1.0` (제3자 이미지). 지식 그래프(273,519 노드 /
  1,493,463 관계)가 이미지에 구워져 있어 이 저장소에는 KG 소스가 포함되지 않는다.
- 문서: `README.md` / `README.ko.md`, `docs/INSTALL.md`, `docs/USAGE.md`,
  `docs/AF3_SETUP.md`, `docs/BRIDGE.md`, `docs/GRAPHRAG.md`, `af3-bridge/README.md`, 이 파일
- 라이선스·인용·CI: `LICENSE`, `CITATION.cff`, `.github/workflows/docker-publish.yml`
- AF3 브리지 참고 자산: `af3-bridge/`(브리지 서버 + systemd 유닛),
  외부 AF3 패치 제안 `patches/af3_mcp_host_env.patch`

**절대 공개하지 않는 것**

- **AF3 모델 가중치 / MSA·서열 DB** — Google DeepMind 라이선스상 재배포 불가.
  사용자는 AF3와 가중치/DB를 **직접** AF3 라이선스에 따라 구해 자신의 GPU
  머신에서 실행한다. 이 저장소는 사용자가 띄운 AF3 MCP에 `AF3_MCP_URL`로 연결만 한다.
- **KG 원천 데이터셋(NPASS, Open Targets)** — 각자의 라이선스를 따르며 이 저장소에
  소스가 포함되지 않는다(이미지에만 구워져 배포됨).
- **시크릿** — 실제 `OPENROUTER_API_KEY`(`sk-or-...`), AF3 토큰, BasicAuth 비밀번호,
  Neo4j 비밀번호.
- **실 환경파일** — `.env`, `.env.docker` (예시 `*.example`만 커밋).
- **런타임 데이터** — `data/`, `*.db`(SQLite 작업 이력), 예측/도킹 출력(`*.cif`, `*.pdb`).
- **개인정보(PII)** — 내부 경로, 사용자명, 실명, 내부 IP. (1절에서 일괄 점검)

이 경계는 `.gitignore`가 1차로 강제하지만, **공개 전 1절의 정화 점검을 반드시 수동으로** 돌린다.

---

## 1. 사전 정화 체크리스트 (공개 게이트)

공개 직전, 저장소 루트에서 아래를 **전부** 통과시킨다. 하나라도 매치가 나오면
공개를 멈추고 고친다.

```bash
# 저장소 루트에서 실행. (출력이 없어야 통과)

# 1) 개인정보 / 내부 호스트 / 실명 / 키 패턴 통합 스캔
grep -rnE "yjko|Dr\.?\s*Yoon|Yoon|161\.122|sk-or-[A-Za-z0-9]{20}|/home/[a-z]+" . \
  --exclude-dir=.git

# 2) 사설/내부 IP 대역
grep -rnE "\b(10|192\.168)\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+" . \
  --exclude-dir=.git

# 3) 실 환경파일이 추적되고 있지 않은지
git ls-files | grep -E "(^|/)\.env($|\.docker$|\.[^e].*)" | grep -v "\.example$"

# 4) DB / 데이터 / 가중치 / 구조 출력이 추적되고 있지 않은지
git ls-files | grep -nE "\.(db|sqlite3?|cif|pdb|pdbqt|bin|npz)$|(^|/)(data|weights|models|af3_output)/"
```

**예상 대응**

- (1)·(2) 매치 → 해당 라인을 플레이스홀더로 치환. 경로는 `/data/...`,
  호스트는 `host.docker.internal` 또는 `<af3-host>`, 키는 `sk-or-REPLACE_ME`.
  내부 IP가 박힌 설계 문서(`*_plan.md`, `research.md` 등)는 **공개 저장소에
  넣지 않는다**.
- (3) 매치 → `git rm --cached <파일>` 후 `.gitignore` 확인. 이미 커밋 이력에
  들어갔다면 `git filter-repo`로 이력에서 제거하거나, 깨끗한 새 저장소로 다시 시작.
- (4) 매치 → 마찬가지로 추적 해제. 가중치/DB/구조 출력은 절대 커밋 금지.

`.env`가 커밋되지 않았는지 최종 확인:

```bash
git ls-files --error-unmatch .env 2>/dev/null && echo "위험: .env 추적됨!" || echo "OK: .env 미추적"
```

> 시크릿이 **과거 커밋**에 한 번이라도 들어갔다면, 현재 파일만 지워도 이력에 남는다.
> 가장 안전한 길은 **새 git 이력으로 시작**하는 것이다:
> `rm -rf .git && git init && git add -A && git commit -m "Initial public release"`.

---

## 2. GitHub 저장소

- **이름:** `kgaf3_chatbot` (확정)
- **URL:** <https://github.com/suppakoko/kgaf3_chatbot>
- **공개 범위:** Public (DOI 발급·ghcr 공개 패키지를 위해 공개로 시작)
- **설명(Description):**
  > Local-first chatbot for natural-product knowledge-graph Q&A (GraphRAG) + AF3 (external) protein–ligand cofolding with bundled smina rescoring. One-click Docker install.
- **Topics:** `graphrag`, `knowledge-graph`, `natural-products`, `neo4j`,
  `alphafold3`, `virtual-screening`, `protein-ligand`, `cofolding`, `smina`,
  `docking`, `mcp`, `chatbot`, `drug-discovery`, `docker`, `fastapi`
- **포함 안 함:** GitHub이 만드는 기본 `LICENSE`/`.gitignore`는 건너뛴다
  (이 저장소가 이미 가지고 있다).

```bash
# gh CLI 사용 예 (1절 정화 통과 후). 이미 원격이 있으면 push만 하면 된다.
gh repo create suppakoko/kgaf3_chatbot --public \
  --description "Local-first chatbot: natural-product GraphRAG Q&A + AF3 (external) cofolding with bundled smina rescoring." \
  --source . --remote origin
git push -u origin main
```

토픽은 웹 UI 또는:

```bash
gh repo edit --add-topic graphrag,knowledge-graph,natural-products,neo4j,alphafold3,virtual-screening,protein-ligand,cofolding,smina,docking,mcp,chatbot,drug-discovery,docker,fastapi
```

---

## 3. 무엇을 커밋하고 무엇을 무시하나

`.gitignore`에 의존하되, 첫 커밋 전에 추적 대상을 눈으로 확인한다:

```bash
git add -A
git status            # 추적될 파일 목록 확인
git ls-files | sort   # 최종 커밋 대상 전체 보기
```

**커밋 대상(O):** `app/` 소스, `static/`, `templates/`, `run.py`, `pyproject.toml`,
`uv.lock`, `Dockerfile`, `docker-compose*.yml`, `install.sh`/`install.bat`,
`configure.md`, `smina-mcp/` 전체, `af3-bridge/` 전체, `patches/`,
`.env.docker.example`, `README*.md`, `docs/`, `LICENSE`, `CITATION.cff`,
`.github/`, `sharing_chtbot.md`.

`docker-compose.yml`은 GraphRAG 서비스(`neo4j`, `graphrag-mcp`) 정의도 포함하지만,
그 이미지 자체(`yoonjuho94/graphrag-neo4j:1.0`, `yoonjuho94/graphrag-mcp-server:1.0`)는
Docker Hub에서 pull하는 **제3자 이미지**이므로 이 저장소에 커밋되지 않는다.
`af3-bridge/`는 외부 AF3 MCP 브리지의 **참고 소스**일 뿐 AF3 가중치/DB는 포함하지 않는다.

**무시 대상(X):** `.env`, `.env.docker`, `data/`, `*.db`, `weights/`, `models/`,
`*.cif`/`*.pdb` 출력, `.venv/`, `__pycache__/`, `.pytest_cache/`, OS 잔여 파일.
이 목록은 `.gitignore`가 강제한다.

---

## 4. 라이선스 선택

**권장: MIT** (이미 `LICENSE`로 포함).

- **근거:** 학술·산업 어디서나 가장 마찰이 적고, 재사용·포크·인용을 장려한다.
- **주의:** MIT는 **kgaf3_chatbot 코드 자체**에만 적용된다. 다음은 각자의 라이선스를
  따르며 이 저장소에 포함되지 않는다:
  - **AF3 가중치/DB** — Google DeepMind 라이선스. 사용자가 직접 확보·실행.
  - **KG 원천 데이터셋(NPASS, Open Targets)** — 각 데이터셋 라이선스.
  - **smina 등 동봉 도구** — 각 도구의 라이선스(`smina-mcp/README.md` 참조).
- **저작권자 설정:** 현재 `LICENSE`는 중립 플레이스홀더
  `Copyright (c) 2026 kgaf3_chatbot contributors`. 실명/기관명으로 공개하려면 이 한 줄을
  바꾼다(예: `Copyright (c) 2026 <Your Name or Lab>`). 익명 공개라면 그대로 둔다.

---

## 5. README 구조 권고

`README.md`(영문)·`README.ko.md`(국문)는 동일 골격을 유지하라:

1. 한 줄 정체성 + 언어 토글 + (발급 후) DOI 배지
2. **Features** (이모지 불릿) — GraphRAG Q&A + AF3 cofolding + smina 리스코어링
3. **Architecture** ASCII 다이어그램 — `kgaf3-chat` + 동봉 `smina-mcp` +
   GraphRAG 스택(`neo4j` + `graphrag-mcp`) + 외부 AF3 MCP + OpenRouter
4. **Requirements** (Docker; 외부 AF3 MCP + AF3용 GPU 머신; OpenRouter 키;
   `kgaf3-chat` 자체는 GPU 불필요)
5. **Quick start** (clone → configure.md → `./install.sh`)
6. **사전 빌드 이미지 사용** (ghcr pull + compose override)
7. **문서 링크** (INSTALL.md / USAGE.md / AF3_SETUP.md / BRIDGE.md /
   GRAPHRAG.md / af3-bridge/README.md / configure.md / sharing_chtbot.md)
8. **License** + 인용 + AF3 가중치·KG 데이터셋 비포함 고지

> **GraphRAG는 기본 활성**이다. `docker compose up -d`(또는 `./install.sh`)는
> 네 개 서비스(`kgaf3-chat`, `smina-mcp`, `neo4j`, `graphrag-mcp`)를 모두 띄운다.
> 별도 `graphrag` compose 프로파일은 **더 이상 없다**. 스크리닝만 쓰려면
> `configure.md`에서 `GRAPHRAG_ENABLED=false`로 두거나
> `docker compose up -d kgaf3-chat smina-mcp`로 KG 이미지를 건너뛴다. 상세는
> [docs/GRAPHRAG.md](docs/GRAPHRAG.md).

> 이미지 출처를 README에서 분명히 구분하라:
> - **우리가 빌드·푸시(ghcr.io):** `ghcr.io/suppakoko/kgaf3-chat`,
>   `ghcr.io/suppakoko/afmm-smina-mcp` (6절 CI).
> - **Docker Hub에서 pull(제3자, 빌드 안 함):** `yoonjuho94/graphrag-neo4j:1.0`,
>   `yoonjuho94/graphrag-mcp-server:1.0`.
> - **배포 안 함:** AlphaFold3 가중치/DB(사용자 자산).

---

## 6. ghcr.io 사전 빌드 이미지 (GitHub Actions)

목표: 릴리스마다 **우리가 만드는 두 이미지**를 자동 빌드·푸시 →
`ghcr.io/suppakoko/kgaf3-chat`, `ghcr.io/suppakoko/afmm-smina-mcp`.
(GraphRAG 이미지는 여기서 빌드하지 않는다 — Docker Hub `yoonjuho94/*`에서 pull.)

### 6-1. 워크플로 (이미 포함됨)

`.github/workflows/docker-publish.yml`이 다음을 수행한다:

- 트리거: `v*` 태그 push, GitHub Release 발행, 수동 실행(`workflow_dispatch`).
- `docker/login-action`으로 ghcr 로그인 — **별도 시크릿 불필요**, 빌트인
  `GITHUB_TOKEN` 사용(`permissions: packages: write`).
- 두 이미지를 매트릭스로 빌드:
  - `kgaf3-chat` → context `.`, `./Dockerfile`
  - `afmm-smina-mcp` → context `./smina-mcp`, `./smina-mcp/Dockerfile.smina`
- `docker/metadata-action`이 태그를 자동 산출: **semver**(`1.2.3`, `1.2`) + **`latest`**.
- 빌드 프로비넌스/SBOM(attestation) 첨부.
- 소유자는 `${{ github.repository_owner }}`(=`suppakoko`, 소문자 정규화)로
  **자동 해석** — 파일 수정 불필요.

### 6-2. Actions 활성화

- 공개 저장소는 Actions가 기본 활성. 비활성 상태면:
  **Settings → Actions → General → Allow all actions and reusable workflows**.
- **Settings → Actions → General → Workflow permissions**에서
  **Read and write permissions**를 켠다(패키지 푸시에 필요).

### 6-3. 패키지를 Public으로 전환

처음 푸시되면 패키지는 **private**일 수 있다. 사용자가 pull하려면 public이어야 한다:

- GitHub 프로필/조직 → **Packages** → `kgaf3-chat` 선택 →
  **Package settings → Danger Zone → Change visibility → Public**.
- `afmm-smina-mcp`도 동일하게 처리.
- (선택) 같은 화면에서 **Connect repository**로 패키지를 저장소에 연결하면
  README/릴리스에 함께 노출된다.

### 6-4. 최종 사용자가 사전 빌드 이미지를 쓰는 법

README에 안내된 대로, 빌드 없이 pull:

```bash
docker pull ghcr.io/suppakoko/kgaf3-chat:latest
docker pull ghcr.io/suppakoko/afmm-smina-mcp:latest
```

Compose가 빌드 대신 이 이미지를 쓰게 하려면 override 한 장:

```yaml
# docker-compose.override.yml
services:
  kgaf3-chat:
    image: ghcr.io/suppakoko/kgaf3-chat:latest
    build: null
  smina-mcp:
    image: ghcr.io/suppakoko/afmm-smina-mcp:latest
    build: null
```

> GraphRAG 서비스(`neo4j`, `graphrag-mcp`)는 override가 필요 없다 — compose가
> 이미 Docker Hub 이미지를 직접 pull한다.
>
> 재현성을 위해 `latest` 대신 `:v0.1.0`처럼 **버전 태그를 고정**하라고 권한다.

---

## 7. 버전·릴리스 태깅

[SemVer](https://semver.org)를 따른다: `vMAJOR.MINOR.PATCH`.

```bash
# 첫 공개 릴리스
git tag -a v0.1.0 -m "kgaf3_chatbot — first public release"
git push origin v0.1.0
```

태그 push가 6절 워크플로를 트리거해 `ghcr.io/suppakoko/kgaf3-chat:0.1.0`(+`latest`)와
`ghcr.io/suppakoko/afmm-smina-mcp:0.1.0`(+`latest`)를 만든다.

GitHub Release 생성(릴리스 노트 + DOI 발급 트리거):

```bash
gh release create v0.1.0 \
  --title "kgaf3_chatbot v0.1.0" \
  --notes "First public release. Two modes: GraphRAG Q&A over a natural-product KG (NPASS 3.0 + Open Targets 25.12, default ON) and AF3 (external) protein–ligand cofolding with bundled smina rescoring. See README, docs/INSTALL.md, docs/AF3_SETUP.md, configure.md."
```

**릴리스 노트 권장 항목:** 한 줄 요약, 두 모드 소개, 요구 사항(Docker / 외부 AF3 MCP +
GPU / OpenRouter 키), 알려진 제약(AF3 도달성 → [docs/BRIDGE.md](docs/BRIDGE.md),
[docs/AF3_SETUP.md](docs/AF3_SETUP.md) 링크), AF3 가중치·KG 데이터셋 비포함 고지.

---

## 8. Zenodo DOI 발급

GitHub Release를 학술 인용 가능한 DOI로 박제한다.

1. <https://zenodo.org>에 GitHub 계정으로 로그인(승인).
2. **Zenodo → 우상단 메뉴 → GitHub**로 이동. 저장소 목록에서
   `kgaf3_chatbot` 옆 **스위치를 ON**.
   (이 스위치를 켠 *이후*에 만든 릴리스부터 DOI가 발급된다.)
3. GitHub에서 **새 Release를 생성**(7절). 이미 v0.1.0을 켜기 전에 냈다면,
   v0.1.1 같은 새 릴리스를 한 번 더 낸다.
4. Zenodo가 자동으로 릴리스 아카이브를 받아 **DOI를 발급**한다:
   - **Concept DOI**(모든 버전 대표, 항상 최신을 가리킴)
   - 버전별 DOI
   학술 인용에는 보통 **Concept DOI**를 쓴다.
5. **배지·인용 추가:**
   - `CITATION.cff`의 `identifiers`에 DOI를 채운다(현재 주석 처리된 블록 해제):
     ```yaml
     identifiers:
       - type: doi
         value: "10.5281/zenodo.XXXXXXX"
         description: "Concept DOI (all versions)"
     ```
   - README 상단에 DOI 배지 추가:
     ```markdown
     [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.XXXXXXX.svg)](https://doi.org/10.5281/zenodo.XXXXXXX)
     ```
6. **ORCID:** Zenodo 업로드 메타데이터(또는 `CITATION.cff`의 author)에 저자 ORCID를
   넣으면 ORCID 프로필에 자동 연결된다. `CITATION.cff`에 TODO로 표시되어 있다.

> `repository-code`/`url`(CITATION.cff)도 실제 GitHub URL
> `https://github.com/suppakoko/kgaf3_chatbot`로 바꾸는 것을 잊지 말 것.

---

## 9. 최종 릴리스 체크리스트

```text
[ ] 1절 정화 grep 4종 전부 통과 (출력 없음)
[ ] git ls-files 에 .env / .env.docker / *.db / weights / *.cif 없음
[ ] LICENSE 저작권자 줄 확정 (중립 placeholder 또는 실명)
[ ] configure.md 의 예시 키가 전부 플레이스홀더 (sk-or-REPLACE_ME 등)
[ ] configure.md 의 GRAPHRAG_ENABLED 기본값이 true (기본 활성) 인지 확인
[ ] install.sh / install.bat 가 configure.md → .env.docker 를 생성하고
    실 .env.docker 는 커밋되지 않음
[ ] GitHub repo(suppakoko/kgaf3_chatbot) Public + 설명/토픽 설정
[ ] Actions: Read and write permissions 활성
[ ] v0.1.0 태그 push → docker-publish 워크플로 그린
[ ] ghcr 패키지 2종(kgaf3-chat, afmm-smina-mcp) Public 전환
[ ] ghcr.io/suppakoko/kgaf3-chat:latest 실제 pull 검증
[ ] (GraphRAG 기본 활성) docker compose up -d 가 네 서비스
    (kgaf3-chat, smina-mcp, neo4j, graphrag-mcp) 를 띄우는지 검증
[ ] (GraphRAG) Docker Hub 이미지 2종
    (yoonjuho94/graphrag-neo4j:1.0, yoonjuho94/graphrag-mcp-server:1.0) 실제 pull 검증,
    KG 273,519 노드 / 1,493,463 관계 자동 로드 (첫 boot ~1분) 확인
[ ] (스크리닝 전용) GRAPHRAG_ENABLED=false 또는
    docker compose up -d kgaf3-chat smina-mcp 로 opt-out 되는지 확인
[ ] AF3 가중치/DB · KG 원천 데이터셋이 저장소·이미지 어디에도 재배포되지 않음 재확인
[ ] Zenodo 스위치 ON → Release 생성 → DOI 발급 확인
[ ] CITATION.cff + README DOI 배지 갱신
[ ] (최종) 깨끗한 환경에서 README Quick start 그대로 재현 테스트
```

---

## 10. AF3 브리지 안내 (외부 의존성)

kgaf3_chatbot의 스크리닝 모드는 도달 가능한 **외부 AF3 MCP 서버**를 요구한다.
`kgaf3-chat`은 AF3를 직접 실행하지 않고 `AF3_MCP_URL`(기본
`http://host.docker.internal:8002/mcp/`)로 호출만 한다. AF3 자체(가중치/DB 포함)는
**이 저장소에서 배포하지 않으며**, 사용자가 AF3 라이선스에 따라 자신의 GPU 머신에서
직접 구동한다.

- AF3 확보·가중치/DB·AF3 MCP 브리지 구동·`AF3_MCP_URL` 설정·검증 절차:
  **[docs/AF3_SETUP.md](docs/AF3_SETUP.md)**
- 저장소에 동봉된 브리지 참고 자산(`af3-bridge/`)의 정체와 실행 방법(외부
  `af3_chatbot` 백엔드 + `alphafold3` 이미지에 결합되어 있으며 **standalone 아님**):
  **[af3-bridge/README.md](af3-bridge/README.md)**
- AF3 MCP가 `127.0.0.1`에 하드코딩되어 컨테이너/원격에서 막히는 도달성 문제와
  그 해소법(1줄 패치 `patches/af3_mcp_host_env.patch`, host network 모드,
  socat/nginx 포워더, SSH 터널): **[docs/BRIDGE.md](docs/BRIDGE.md)**

공개 시 README와 릴리스 노트에서 **"스크리닝 모드는 도달 가능한 외부 AF3 MCP가
반드시 있어야 한다"**는 점을 분명히 한다. 설치기는 설치 끝에서 이 연결을 능동
검증하고, 실패 시 BRIDGE.md/AF3_SETUP.md로 안내한다. (GraphRAG 모드는 AF3 없이도
동작한다.)
