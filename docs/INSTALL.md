# 설치 가이드 — 내 컴퓨터에 kgaf3_chatbot 올리기

이 문서는 로컬 머신에 **kgaf3_chatbot**을 처음부터 설치하는 상세 절차를 정리한다.
`configure.md` 한 파일만 채우고 `./install.sh`를 돌리면 Docker Compose 스택이
통째로 뜨는 구조이며, 여기서는 그 앞뒤로 필요한 사전 준비·검증·문제 해결까지 다룬다.

kgaf3_chatbot은 두 가지 모드를 가진 로컬 우선(local-first) 웹 챗봇이다.

- 🧬 **GraphRAG** — 천연물 지식그래프(NPASS 3.0 + Open Targets 25.12)에 대한
  자연어 질의응답. **기본 켜짐(ON by default).**
- 🧪 **가상 스크리닝(virtual screening)** — 외부 AlphaFold3 MCP + 내장
  `smina --minimize` 리스코어링으로 단백질–리간드 코폴딩을 수행하고 결과를
  웹 UI로 보여준다.

메인 서비스는 `kgaf3-chat`이라는 FastAPI 컨테이너로, 호스트 루프백
`http://localhost:5013`에 뜬다. smina 도킹 엔진(`smina-mcp`)과 GraphRAG 스택
(`neo4j` + `graphrag-mcp`)은 같은 Compose 스택 안에서 함께 관리된다.

```
[브라우저] ──HTTP──► kgaf3-chat (FastAPI :5013)
                        ├── OpenRouter (채팅 LLM)              → 외부 인터넷 (OPENROUTER_API_KEY)
                        ├── smina-mcp (:8001, 내부 전용)        → 내장 도킹/리스코어
                        ├── graphrag-mcp (:8893/sse) ─► neo4j  → 지식그래프 (기본 켜짐)
                        └── AF3 MCP (:8002)                    → 외부 GPU 머신 (AF3_MCP_URL, 별도 운영)
```

> **AF3는 이 패키지에 들어 있지 않다(라이선스).** AlphaFold3 **가중치**와 **서열
> 데이터베이스**는 Google DeepMind가 라이선스하는 자산으로 이 저장소에 **재배포되지
> 않는다.** 스크리닝을 쓰려면 사용자가 직접 AF3와 그 가중치/DB를 확보해 자기 GPU
> 머신에서 돌리고, 그 앞의 AF3 MCP 서버 주소를 `AF3_MCP_URL`에 넣어야 한다. 획득·구성
> 방법은 **[docs/AF3_SETUP.md](AF3_SETUP.md)** 참조.

---

## 1. 사전 준비물 (prerequisites)

설치 전에 아래 세 가지가 준비돼야 한다.

### 1-1. Docker + Docker Compose

- **Docker Engine**과 **Docker Compose v2**가 깔린 Linux, macOS, 또는
  Windows/WSL2 머신. (compose 파일은 v2 문법이라 최상위 `version:` 키가 없다.)
- 설치 확인:

  ```bash
  docker --version
  docker compose version
  ```

- GraphRAG 이미지(수 GB)와 앱 이미지를 담을 **디스크 여유**가 필요하다.
  kgaf3-chat 자체는 CPU만 쓰며 **GPU가 필요 없다**(AF3의 GPU는 외부 호스트에 있다).

### 1-2. AF3를 돌리는 GPU 머신 (스크리닝을 쓸 때만)

🧪 스크리닝 모드는 **도달 가능한 외부 AF3 MCP 서버**가 있어야 동작한다.

- 사용자가 직접 확보한 **AlphaFold3 + 라이선스 가중치/DB**를 자기 **GPU 머신**에서
  돌리고, 그 앞에 AF3 MCP 서버(포트 `8002`)를 띄운다.
- kgaf3-chat과 **같은 머신**일 수도, **원격 머신**일 수도 있다. 어느 쪽이든
  컨테이너 관점에서 `AF3_MCP_URL`이 그 MCP 서버를 가리켜야 한다.
- AF3 획득·MCP 브리지 구동·호스트 바인딩 패치·`AF3_MCP_URL` 설정·검증까지의 전
  과정은 **[docs/AF3_SETUP.md](AF3_SETUP.md)** 와 **[docs/BRIDGE.md](BRIDGE.md)** 에 있다.
- GraphRAG만 쓸 거라면 AF3 없이도 설치가 완료된다(아래 5절 opt-out과 무관하게,
  AF3 검증 실패는 경고로만 처리되고 앱은 뜬다).

### 1-3. OpenRouter API 키

- 채팅 LLM과 GraphRAG의 `graphrag_query` 단계가 **OpenRouter**를 쓴다.
- 키 발급: <https://openrouter.ai/keys> — `sk-or-...` 형태의 키를
  `OPENROUTER_API_KEY`에 넣는다.
- 이 키는 채팅과 GraphRAG가 **공유**한다(별도 키 불필요). 키가 비어 있으면 채팅과
  `graphrag_query`가 실패한다. 반면 GraphRAG의 `get_kg_stats`/`run_cypher`는
  LLM을 쓰지 않으므로 키 없이도 동작한다.

---

## 2. 코드 받기

```bash
git clone https://github.com/suppakoko/kgaf3_chatbot.git
cd kgaf3_chatbot
```

이후 모든 명령은 이 저장소 루트에서 실행한다.

---

## 3. 설정 — `configure.md` 채우기

설치기는 **`configure.md`의 첫 번째 ```ini 코드 블록만** 읽는다(나머지 서술은 전부
문서일 뿐 무시된다). 블록 안은 한 줄에 `KEY = value` 하나, `=` 주변 공백은 잘리고,
`#` 뒤는 주석, 빈 줄은 건너뛴다. **키 이름은 바꾸지 말 것.**

### 3-1. 반드시 채워야 하는 키 (required)

| 키 | 뜻 |
|----|----|
| `OPENROUTER_API_KEY` | 채팅·GraphRAG LLM용 OpenRouter 키(`sk-or-...`). |
| `AF3_MCP_URL` | 외부 AF3 MCP 서버 URL. 기본값은 같은 호스트의 AF3를 가정한 `http://host.docker.internal:8002/mcp/`. |
| `AF3_OUTPUT_ROOT` | AF3 출력 디렉터리의 **호스트 절대경로**. 앱이 이 경로를 **읽기 전용(read-only)** 으로 마운트해 예측 결과를 읽는다. **이미 존재하는 경로**여야 한다. |

### 3-2. 선택 키 (optional)

| 키 | 기본값 | 뜻 |
|----|--------|----|
| `PROFILE` | `lite` | 현재 동작하는 유일한 프로파일. `full`(OpenMM 기반)은 **예약(reserved)** 이며 아직 기능이 없으니 `lite`로 둔다. |
| `APP_PORT` | `5013` | 웹 UI 호스트 포트(`http://localhost:<APP_PORT>`). |
| `LLM_DEFAULT_MODEL` | `anthropic/claude-sonnet-4-6` | UI에 미리 선택되는 채팅 모델(앱에서 변경 가능). |
| `AF3_MCP_AUTH_TOKEN` | _(빈 값)_ | AF3 MCP가 인증을 요구할 때만 쓰는 Bearer 토큰. |
| `ENABLE_NGINX` | `false` | `true`면 앱을 BasicAuth를 건 nginx 리버스 프록시 뒤에 둔다. |
| `BASIC_AUTH_USER` | _(빈 값)_ | BasicAuth 사용자명(`ENABLE_NGINX=true`일 때만). |
| `BASIC_AUTH_PASS` | _(빈 값)_ | BasicAuth 비밀번호(`ENABLE_NGINX=true`일 때만). |
| `GRAPHRAG_ENABLED` | `true` | GraphRAG 스택(Neo4j KG + `:8893` MCP) + 🧬 GraphRAG 채팅 모드. **기본 켜짐.** `false`면 스크리닝 전용으로 opt-out(무거운 KG 이미지 pull을 건너뜀). |
| `GRAPHRAG_OPENROUTER_MODEL` | `anthropic/claude-opus-4-7` | GraphRAG 답변에 UI/기록으로 **표시되는 라벨일 뿐**. 실제로 질의를 돌리는 모델은 MCP 서버 자신의 `OPENROUTER_MODEL`이 권위를 갖는다. |

> 설치기가 자동으로 주입하는 고정값(사용자가 손대지 않는다):
> `GRAPHRAG_MCP_URL = http://graphrag-mcp:8893/sse`, 그리고 smina/OpenMM 고정 변수.
> Neo4j 비밀번호도 KG 이미지에 맞춰 `kist2026npi`로 내부망에서 자동 배선된다.
> **`NEO4J_*`를 configure.md에 직접 넣지 말 것** — Neo4j 드라이버는 graphrag-mcp
> 컨테이너 안에 있다(자세한 내용은 [docs/GRAPHRAG.md](GRAPHRAG.md)).

### 3-3. 예시 ini 블록

`configure.md` 안의 블록을 아래처럼 채운다(값만 바꾸고 키는 그대로 둔다).

```ini
# ── Required ─────────────────────────────────────────────
OPENROUTER_API_KEY      = sk-or-REPLACE_ME                # LLM key (required)
AF3_MCP_URL             = http://host.docker.internal:8002/mcp/
AF3_OUTPUT_ROOT         = /data/af3_output               # absolute host path, must exist

# ── Profile ──────────────────────────────────────────────
PROFILE                 = lite                            # lite | full (full is reserved/not functional yet)

# ── Optional ─────────────────────────────────────────────
APP_PORT                = 5013
LLM_DEFAULT_MODEL       = anthropic/claude-sonnet-4-6
AF3_MCP_AUTH_TOKEN      =                                 # set only if your AF3 MCP requires auth
ENABLE_NGINX            = false                           # true = BasicAuth reverse proxy
BASIC_AUTH_USER         =
BASIC_AUTH_PASS         =

# ── GraphRAG (knowledge-graph chat mode — ON by default) ─
GRAPHRAG_ENABLED          = true                          # false = opt out (screening-only, skips Neo4j/MCP image pull)
GRAPHRAG_OPENROUTER_MODEL = anthropic/claude-opus-4-7     # display label only
```

모든 키의 전체 설명은 **[configure.md](../configure.md)** 에 있다.

---

## 4. 설치 — `./install.sh` (권장)

설정을 마쳤으면 설치기를 실행한다.

```bash
./install.sh            # Linux / macOS
# install.bat           # Windows / WSL2 (더블클릭 또는 터미널에서 실행)
```

설치기는 재실행 가능(idempotent)하다. 설정을 고친 뒤 다시 돌려도 안전하다.

### 4-1. install.sh 8단계

| 단계 | 하는 일 |
|------|---------|
| **1/8 Preflight** | Docker/Compose 존재, `APP_PORT` 점유 여부 등 사전 점검. |
| **2/8 Parse** | `configure.md`의 ini 블록을 파싱해 `.env.docker`를 생성. 필수 키 누락 시 명확한 메시지와 함께 중단. |
| **3/8 Bootstrap dirs** | 호스트 디렉터리를 만들고 `AF3_OUTPUT_ROOT`가 실제로 존재하는지 검증. |
| **4/8 SELinux/firewalld** | (best-effort) SELinux Enforcing이면 `AF3_OUTPUT_ROOT`에 `container_file_t` 라벨링, firewalld가 켜져 있으면 `docker0`를 trusted 존으로. 실패해도 진행. |
| **5/8 Build** | `docker compose build`로 kgaf3-chat·smina-mcp 이미지 빌드. |
| **6/8 Start** | 스택 기동. **기본은 네 서비스 전체**(`docker compose up -d`) — kgaf3-chat, smina-mcp, neo4j, graphrag-mcp. `GRAPHRAG_ENABLED=false`면 `docker compose up -d kgaf3-chat smina-mcp`만 띄우고 KG 이미지 pull을 건너뜀. |
| **7/8 Health poll** | `http://localhost:5013/health/ready`가 준비될 때까지 폴링(최대 180초). |
| **8/8 Verify AF3** | `AF3_MCP_URL`로 MCP `initialize` + `tools/list`를 호출해 외부 AF3 연결과 도구 목록을 능동 검증. |

마지막에 URL·프로파일·LLM·smina 상태·GraphRAG 상태·외부 AF3 연결 결과를 담은 성공
리포트를 출력한다. AF3 연결이 실패해도 스택은 떠 있으므로 설치기는 정상 종료하며,
스크리닝을 돌리기 전에 고쳐야 할 사항을 명시한다(→ [docs/BRIDGE.md](BRIDGE.md)).

### 4-2. GraphRAG는 기본으로 켜진다

`GRAPHRAG_ENABLED`를 건드리지 않으면(기본 `true`) 6단계에서 네 서비스가 모두 뜨고,
**Docker Hub에서 두 KG 이미지를 처음 한 번 pull** 한다.

```
yoonjuho94/graphrag-neo4j:1.0        # KG가 통째로 구워진 Neo4j
yoonjuho94/graphrag-mcp-server:1.0   # :8893 MCP SSE 서버
```

- 이 이미지들은 크기가 커서 **첫 `up`의 pull에 시간이 걸린다.**
- Neo4j는 첫 부팅 때 KG를 **자동 로드**하는데, **273,519 노드 / 1,493,463 관계**를
  올리는 데 **약 1분**이 걸린다. 별도 임포트나 외부 DB 절차가 없다.
- 로드 완료를 로그로 확인하려면:

  ```bash
  docker logs -f graphrag-neo4j       # KG 자동 로드 완료까지 대기
  ```

- 노드/관계 개수 검증(LLM 키 없이 동작):

  ```bash
  docker exec graphrag-neo4j cypher-shell -u neo4j -p kist2026npi \
    "MATCH (n) RETURN count(n);"        # 273519 기대
  docker exec graphrag-neo4j cypher-shell -u neo4j -p kist2026npi \
    "MATCH ()-[r]->() RETURN count(r);" # 1493463 기대
  ```

GraphRAG의 배포·사용·문제 해결 전반은 **[docs/GRAPHRAG.md](GRAPHRAG.md)** 참조.

---

## 5. 수동 설치 — Docker Compose 직접 실행

설치기를 쓰지 않고 손으로 올리고 싶으면 아래 흐름을 따른다. 이 경우
`configure.md → .env.docker` 변환을 직접 하거나 예시 파일을 복사해 채운다.

```bash
# 1. 환경 파일 준비 (OPENROUTER_API_KEY, AF3_MCP_URL, AF3_OUTPUT_ROOT 등 채우기)
cp .env.docker.example .env.docker
$EDITOR .env.docker

# 2. 이미지 빌드 (호스트 uid/gid를 맞추면 AF3 출력 마운트 권한이 깔끔)
docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)

# 3. 스택 기동 — 기본은 네 서비스 전체(GraphRAG 포함)
docker compose up -d

# 4. 헬스 확인
curl -fsS http://127.0.0.1:5013/health/ready
```

- 기본 `docker compose up -d`는 **kgaf3-chat, smina-mcp, neo4j, graphrag-mcp** 넷을
  모두 띄운다. 예전 배포판에 있던 `graphrag` compose 프로파일은 **더 이상 없다.**
- kgaf3-chat에는 GraphRAG에 대한 `depends_on`이 **없다.** 그래서 KG가 안 떠 있어도
  앱은 정상 기동하고, MCP에 못 닿으면 `service.graphrag.mcp_unreachable`만 로그로
  남기고 우아하게 축소 동작한다.

---

## 6. GraphRAG opt-out (스크리닝 전용으로 쓰기)

지식그래프가 필요 없고 스크리닝만 쓸 거라면 두 방법 중 하나로 GraphRAG를 뺀다.
그러면 무거운 KG 이미지를 pull하지 않는다.

- **설치기로:** `configure.md`에서 `GRAPHRAG_ENABLED = false`로 두고 `./install.sh`.
  설치기가 6단계에서 `docker compose up -d kgaf3-chat smina-mcp`만 실행한다.
- **수동으로:** 스크리닝 서비스만 지정해 올린다.

  ```bash
  docker compose up -d kgaf3-chat smina-mcp
  ```

나중에 다시 켜려면 `GRAPHRAG_ENABLED=true`로 되돌리고 재설치하거나,
`docker compose up -d`로 네 서비스를 전부 올리면 된다.

---

## 7. 미리 빌드된 이미지 쓰기 (빌드 건너뛰기)

로컬 빌드 대신 배포용 이미지를 pull해서 쓸 수 있다. kgaf3-chat과 smina-mcp 이미지는
릴리스 때 GitHub Container Registry(ghcr.io)에 게시된다
(`.github/workflows/docker-publish.yml`). GraphRAG 이미지는 Docker Hub의
`yoonjuho94/*`에서 받는다(항상 pull, 빌드 아님).

```bash
docker pull ghcr.io/suppakoko/kgaf3-chat
docker pull ghcr.io/suppakoko/afmm-smina-mcp
```

Compose가 로컬 빌드 대신 이 이미지를 쓰게 하려면 override 파일을 둔다.
재현성을 위해 버전 태그를 고정하는 것이 좋다.

```yaml
# docker-compose.override.yml
services:
  kgaf3-chat:
    image: ghcr.io/suppakoko/kgaf3-chat
    build: null        # 로컬 빌드 비활성화
  smina-mcp:
    image: ghcr.io/suppakoko/afmm-smina-mcp
    build: null
```

GraphRAG 이미지(`yoonjuho94/graphrag-neo4j:1.0`, `yoonjuho94/graphrag-mcp-server:1.0`)는
compose에 이미 pull 대상으로 지정돼 있으므로 override가 필요 없다.

---

## 8. 설치 후 — 헬스 체크와 접속

- **헬스 엔드포인트:**

  ```bash
  curl -fsS http://localhost:5013/health/ready
  ```

  설치기 7단계와 compose 헬스체크가 모두 이 엔드포인트를 본다.

- **웹 UI:** 브라우저에서 `http://localhost:5013` (또는 `APP_PORT`로 바꾼 포트).
  모드 토글로 🧪 스크리닝(기본) / 🧬 GraphRAG를 전환한다. 실제 사용법과 예시는
  **[docs/USAGE.md](USAGE.md)** 참조.

- **로그:**

  ```bash
  docker compose logs -f kgaf3-chat        # 앱 로그
  docker compose ps                        # 서비스 상태
  ```

- `ENABLE_NGINX=true`로 두면 앱이 BasicAuth를 건 nginx 리버스 프록시 뒤에 놓인다.
  원격에서 접근하려면 이 방식으로 노출한다(kgaf3-chat 자체는 호스트 루프백
  `127.0.0.1:5013`에만 바인딩된다).

---

## 9. 트러블슈팅

### 9-A. 포트 충돌 (5013 / 8893)

- **5013** — kgaf3-chat 웹 UI. 이미 다른 프로세스가 쓰고 있으면 Preflight가
  잡아낸다. `configure.md`의 `APP_PORT`를 다른 값으로 바꾸고 재설치한다.
- **8893** — graphrag-mcp SSE가 호스트 루프백 `127.0.0.1:8893`에도 공개된다(로컬의
  다른 MCP 클라이언트, 예컨대 Claude Desktop과 서버를 공유하기 위함). 로컬에서
  이미 8893을 쓰는 프로세스가 있으면 컨테이너가 뜨지 못한다. 점유 프로세스를
  정리하거나 호스트 publish를 조정한다(컨테이너 간 통신은 내부망
  `graphrag-mcp:8893`으로 이뤄지므로 호스트 포트 충돌과는 별개다).
- **7474 / 7687** — neo4j 호스트 포트는 compose에서 기본 **주석 처리**돼 있어 보통
  충돌하지 않는다. Neo4j Browser를 쓰려고 열었는데 기존 Neo4j가 점유 중이면
  다른 호스트 포트로 매핑하거나, 굳이 노출하지 말고 `docker exec ... cypher-shell`로
  접근한다.

### 9-B. SELinux (RHEL / Rocky / Fedora, Enforcing)

SELinux Enforcing 호스트에서는 평범한 bind 마운트가 **거부**된다. 설치기 4단계가
`AF3_OUTPUT_ROOT`를 best-effort로 라벨링하지만, 안 되면 `docker-compose.yml`의
마운트에 relabel 접미사를 붙인다.

- 사설 RW 바인드: `:Z` (예: `/data`를 named volume 대신 bind로 바꿀 때)
- 공유 RO 바인드: `:z,ro` — `AF3_OUTPUT_ROOT` 줄에 사용. AF3 서비스가 그 경로에
  **쓰기**를 하므로 `:Z`(사설 라벨)를 붙이면 그 writer가 깨진다. 반드시 `:z,ro`.

  ```yaml
  # docker-compose.yml (SELinux 호스트에서만)
  - "${AF3_OUTPUT_ROOT}:${AF3_OUTPUT_ROOT}:z,ro"
  ```

기본값은 평범한 마운트다 — `:Z`/`:z`는 비-SELinux 호스트(Docker Desktop/Ubuntu)에서
오히려 깨지기 때문이다. SELinux 호스트에 배포할 때만 위처럼 편집한다.

### 9-C. `host.docker.internal`이 안 풀림 (Linux)

kgaf3-chat 컨테이너는 `extra_hosts`로 `host.docker.internal:host-gateway`를
매핑해 같은 호스트의 AF3 MCP에 닿는다. 그래도 안 풀리면 `AF3_MCP_URL`에
docker0 브리지 IP(예: `172.17.0.1`)를 직접 적는다. 자세한 대처는
**[docs/BRIDGE.md](BRIDGE.md)** 참조.

### 9-D. 외부 AF3에 닿지 않음 (8단계 실패)

설치기 8단계에서 AF3 연결이 실패하면 스택은 떠 있어도 스크리닝은 코폴딩 단계에서
매번 실패한다. 원인은 거의 항상 셋 중 하나다.

1. AF3 MCP가 안 떠 있음 — `systemctl status af3-mcp`로 확인.
2. AF3 MCP가 `127.0.0.1`에만 바인딩돼 컨테이너/원격에서 도달 불가(B3 문제).
3. `AF3_MCP_URL`이 컨테이너 관점에서 잘못된 호스트를 가리킴.

해결법(호스트 바인딩 패치 `patches/af3_mcp_host_env.patch`, SSH 터널, 포트 포워더
등)은 **[docs/BRIDGE.md](BRIDGE.md)** 에, AF3 획득·MCP 구동·`AF3_MCP_URL` 설정
전반은 **[docs/AF3_SETUP.md](AF3_SETUP.md)** 에 있다.

### 9-E. `service.graphrag.mcp_unreachable`

kgaf3-chat 로그에 이 줄이 보이면 앱이 GraphRAG MCP에 못 닿은 것이다. 순서대로 확인한다.

- graphrag-mcp가 실제로 떠 있나 — `docker compose ps`.
- `GRAPHRAG_ENABLED=true`인가 — false면 애초에 붙으려 하지 않는다.
- `GRAPHRAG_MCP_URL`이 `http://graphrag-mcp:8893/sse`인가 — 설치기가 고정 주입하므로
  손대지 않는 게 맞다.

이 로그가 떠도 kgaf3-chat 자체(스크리닝·채팅)는 계속 정상 동작한다. 세부 진단은
**[docs/GRAPHRAG.md](GRAPHRAG.md)** 6절 참조.

---

## 관련 문서

- **[configure.md](../configure.md)** — 단일 설치 설정 파일(모든 키 설명).
- **[docs/AF3_SETUP.md](AF3_SETUP.md)** — AF3 라이선스/가중치/DB 획득 + AF3 MCP 브리지 구동 + `AF3_MCP_URL` 설정·검증.
- **[docs/BRIDGE.md](BRIDGE.md)** — 외부 AF3 MCP 도달 문제(B3)와 해결책.
- **[docs/GRAPHRAG.md](GRAPHRAG.md)** — GraphRAG 지식그래프 스택(배포·사용·문제 해결).
- **[docs/USAGE.md](USAGE.md)** — 두 채팅 모드와 결과 웹 UI 사용법·예시.
- **[README.md](../README.md)** — 프로젝트 개요.
