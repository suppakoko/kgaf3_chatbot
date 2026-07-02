# GraphRAG — 자급식 지식그래프 스택 (기본 ON)

kgaf3_chatbot은 지식그래프(KG)를 **직접 들고 있지 않는다.** GraphRAG는 KG가
통째로 구워진 **자급식 Docker 스택**으로 따로 떠 있고, `kgaf3-chat`은 그 앞의
**MCP SSE** 엔드포인트에만 말을 건다. `kgaf3-chat`은 순수 오케스트레이션
계층이라 Neo4j를 직접 만지지 않는다. 이 문서는 그 스택을 켜고, 쓰고,
막혔을 때 푸는 방법을 정리한다.

```
kgaf3-chat 컨테이너 ──MCP SSE──► graphrag-mcp(:8893/sse) ──bolt──► neo4j(:7687, KG 내장)
   (GRAPHRAG_MCP_URL)            (Neo4j 드라이버 + LLM 단계 보유)
```

KG는 **NPASS 3.0 + Open Targets release 25.12**을 합친 것으로,
**273,519 노드 / 1,493,463 관계**다. 이 데이터는
`yoonjuho94/graphrag-neo4j:1.0` 이미지에 통째로 **구워져(baked in)** 있어서
컨테이너 첫 부팅 때 자동 로드된다(약 1분). 별도 임포트 절차가 없다.

> **GraphRAG는 kgaf3_chatbot에서 기본 ON이다.** 별도 프로파일 없이
> `docker compose up -d`만 실행하면 `neo4j`와 `graphrag-mcp`가 나머지
> 서비스(`kgaf3-chat`, `smina-mcp`)와 함께 자동으로 뜬다. 예전 배포판의
> `graphrag` compose 프로파일은 **더 이상 존재하지 않는다.** 그래도
> `kgaf3-chat`에는 GraphRAG에 대한 `depends_on`이 없어서, KG 스택이 안
> 떠 있어도 앱은 정상 기동한다. 스택이 안 떠 있으면 앱은
> `service.graphrag.mcp_unreachable`만 로그로 남기고 조용히 넘어간다.

설치·설정 전반은 [`INSTALL.md`](INSTALL.md), 사용법은 [`USAGE.md`](USAGE.md)를
참고하라.

---

## 1. 무엇이 들어 있나 — 두 개의 자급식 이미지

두 이미지 모두 Docker Hub에서 **빌드가 아니라 pull**로 받는다.

- `yoonjuho94/graphrag-neo4j:1.0` — KG가 구워진 Neo4j.
  컨테이너 이름 `graphrag-neo4j`(compose 서비스명 `neo4j`).
  내부 bolt는 `bolt://neo4j:7687`. 기본 비밀번호 `kist2026npi`(이미지에
  맞춰져 있다). 호스트 포트 `7474`/`7687`은 compose에서 기본으로 주석 처리다.
  named volume `neo4j-data`에 데이터가 유지된다.
- `yoonjuho94/graphrag-mcp-server:1.0` — 포트 **8893**의 MCP **SSE** 서버.
  컨테이너 이름 `graphrag-mcp`(compose 서비스명 `graphrag-mcp`).
  내부 엔드포인트 `http://graphrag-mcp:8893/sse`. 호스트 루프백
  `127.0.0.1:8893`에도 함께 공개돼서, 로컬의 다른 MCP 클라이언트(예:
  Claude Desktop)도 같은 서버를 공유할 수 있다. 이 컨테이너가 Neo4j
  드라이버를 쥐고, `graphrag_query`의 LLM 단계까지 직접 돌린다(공유
  `.env.docker`의 `OPENROUTER_API_KEY` 사용).

---

## 2. 켜는 법 (기본 ON) / 끄는 법 (opt-out)

### 2-1. 그냥 켜기 — 기본값

```bash
docker compose up -d
```

- 이 한 줄이 **네 서비스 모두**를 띄운다: `kgaf3-chat`, `smina-mcp`,
  `neo4j`, `graphrag-mcp`. GraphRAG는 별도 플래그나 프로파일 없이 기본으로
  포함된다.
- 설치기(`install.sh`)로 설치하면 위와 동일하게 전체 스택을 띄우고, 고정값
  `GRAPHRAG_MCP_URL = http://graphrag-mcp:8893/sse`를 자동 주입한다.
- `configure.md`의 GraphRAG 관련 키(기본값):

  ```ini
  GRAPHRAG_ENABLED = true                                # 기본 true
  GRAPHRAG_OPENROUTER_MODEL = anthropic/claude-opus-4-7  # 표시/기록용 라벨
  ```

### 2-2. 끄는 법 — 스크리닝 전용 (opt-out)

KG 없이 🧪 스크리닝만 쓰려면 둘 중 하나를 택한다.

- **설치기 경로:** `configure.md`에서 `GRAPHRAG_ENABLED = false`로 두면,
  설치기가 GraphRAG 이미지 pull을 건너뛰고
  `docker compose up -d kgaf3-chat smina-mcp`만 실행한다.
- **수동 경로:** 필요한 서비스만 이름으로 지정해 띄운다.

  ```bash
  docker compose up -d kgaf3-chat smina-mcp
  ```

  이렇게 하면 `neo4j`/`graphrag-mcp`는 아예 뜨지 않는다. 이미 전체 스택이
  떠 있는 상태에서 KG만 내리려면 두 서비스를 0으로 스케일한다.

  ```bash
  docker compose up -d --scale neo4j=0 --scale graphrag-mcp=0
  ```

`kgaf3-chat`에는 GraphRAG에 대한 `depends_on`이 **없다.** 그래서 GraphRAG를
꺼도(또는 KG가 아직 로드 중이어도) 앱은 정상 기동하며, 스크리닝·채팅 등
다른 기능은 영향을 받지 않는다.

---

## 3. 앱 환경변수 (.env.docker / config.py)

| 키 | 기본값 | 뜻 |
|----|--------|----|
| `GRAPHRAG_ENABLED` | `true` | 사용자 토글. true(기본)면 앱이 MCP 서버에 붙고 전체 스택이 뜬다. false로 두면 설치기가 KG 이미지를 건너뛰고 스크리닝 전용으로 띄운다. |
| `GRAPHRAG_MCP_URL` | `http://graphrag-mcp:8893/sse` | 내부망 compose 서비스로 고정. 설치기가 자동 주입한다. |
| `GRAPHRAG_MCP_AUTH_TOKEN` | (빈 값) | MCP 서버를 인증 뒤에 둘 때만 쓰는 Bearer 토큰. |
| `GRAPHRAG_OPENROUTER_MODEL` | `anthropic/claude-opus-4-7` | **표시/기록용 라벨일 뿐**. 실제로 쓰이는 모델은 MCP 서버 자신의 `OPENROUTER_MODEL`이 권위를 갖는다. |

> `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD`는 **앱 설정으로는 존재하지
> 않는다.** Neo4j 드라이버가 graphrag-mcp 컨테이너 안으로 옮겨졌기
> 때문이다. compose 파일에는 `NEO4J_PASSWORD` 보간 변수가 남아 있지만,
> 이건 neo4j 컨테이너의 `NEO4J_AUTH`(기본 `kist2026npi`)를 세팅하는
> **compose 레벨 노브**일 뿐 kgaf3_chatbot 앱 설정이 아니다. configure.md에서
> 사용자에게 `NEO4J_*`를 설정하라고 안내하지 말 것.

---

## 4. MCP 도구 — 딱 세 개

graphrag-mcp가 노출하는 도구는 아래 셋뿐이다.

- **`graphrag_query(question, provider="openrouter")`** — 자연어 → Cypher →
  실행 → Markdown 답변을 **한 방(one shot)에** 처리한다. 반환 JSON 키:
  `answer`, `cypher`, `row_count`, `rows_preview`,
  `token_usage {input_tokens, output_tokens}`, `model_id`, `timestamp`,
  `provider`. LLM 키가 **필요하다**.
- **`get_kg_stats()`** — 노드/관계 개수. LLM 키 **불필요**.
- **`run_cypher(query, params)`** — 읽기 전용 직접 Cypher. LLM 키 **불필요**.

### 4-1. 한 방 질의와 진행 카드가 채워지는 방식

🧬 GraphRAG 채팅 모드는 진행 카드에 네 단계
`cypher_gen` / `neo4j_exec` / `answer_synth` / `complete`를 보여준다.
하지만 MCP 호출이 **한 방**이라, 세 번의 개별 라이브 LLM 호출로
채워지는 게 아니다. 대신 `graphrag_query`가 돌려준 메타데이터(`cypher`,
`row_count`, `token_usage`)로 각 단계를 **사후에(retrospectively)** 채워
넣는다. 프론트엔드 계약(보이는 모양)은 그대로지만, 내부적으로는 단일
호출 결과를 되짚어 카드를 완성하는 것이다.

채팅 모드에서의 실제 사용 예시는 [`USAGE.md`](USAGE.md)를 참고하라.

---

## 5. 검증

### 5-1. KG 자동 로드 확인

스택을 띄운 뒤 Neo4j가 KG를 다 올렸는지 로그로 본다(약 1분).

```bash
docker compose up -d
docker logs -f graphrag-neo4j   # KG 자동 로드 완료까지 대기
```

### 5-2. 노드/관계 개수 (273519 / 1493463 기대)

로드가 끝났으면 개수를 직접 세서 KG가 온전한지 확인한다. LLM 키 없이
동작한다.

```bash
docker exec graphrag-neo4j cypher-shell -u neo4j -p kist2026npi \
  "MATCH (n) RETURN count(n);"        # 273519 기대
docker exec graphrag-neo4j cypher-shell -u neo4j -p kist2026npi \
  "MATCH ()-[r]->() RETURN count(r);" # 1493463 기대
```

### 5-3. MCP 서버 확인

```bash
docker logs graphrag-mcp
```

호스트에 공개된 SSE 엔드포인트(`127.0.0.1:8893`)로도 살아있음을 확인할 수 있다.

---

## 6. 트러블슈팅

### 6-A. `service.graphrag.mcp_unreachable`

`kgaf3-chat` 로그에 이 줄이 보이면, 앱이 MCP 서버에 닿지 못한 것이다.
순서대로 확인한다:

- ① graphrag-mcp가 실제로 떠 있나 — `docker ps`에서 `graphrag-mcp` 확인.
  안 보이면 스크리닝 전용으로 띄웠거나(2-2) KG 서비스를 0으로 스케일한
  상태다. `docker compose up -d`로 전체 스택을 다시 기동한다.
- ② `GRAPHRAG_ENABLED=true`인가 — false면 앱이 애초에 붙으려 하지 않는다.
- ③ `GRAPHRAG_MCP_URL`이 `http://graphrag-mcp:8893/sse`인가 — 이 값은
  설치기가 내부망 서비스명으로 고정 주입하므로 손대지 않는 게 맞다.

`kgaf3-chat`에는 GraphRAG `depends_on`이 없으니, 이 로그가 떠도 앱 자체는
계속 정상 동작한다는 점을 기억하라(cofolding·채팅 등 다른 기능은 무관).

### 6-B. 포트 8893 / 7474 충돌

- `graphrag-mcp`는 호스트 루프백 `127.0.0.1:8893`에도 공개된다. 로컬에서
  이미 8893을 쓰는 프로세스(다른 MCP 서버 등)가 있으면 컨테이너가 뜨지
  못한다. 점유 프로세스를 정리하거나, 호스트 publish를 조정한다.
  (컨테이너 사이 내부 통신은 `graphrag-mcp:8893`으로 이뤄지므로 호스트
  포트 충돌과는 별개다.)
- `graphrag-neo4j`의 호스트 포트 `7474`(HTTP 브라우저) / `7687`(bolt)은
  compose에서 기본 **주석 처리**돼 있다. Neo4j Browser를 쓰려고 이 포트를
  열었는데 로컬에 기존 Neo4j가 이미 그 포트를 점유하고 있으면 충돌한다.
  그럴 땐 다른 호스트 포트로 매핑하거나, 굳이 호스트에 노출하지 말고
  `docker exec ... cypher-shell`(5-2)로 접근한다.

### 6-C. OPENROUTER_API_KEY

- `graphrag_query`(자연어 질의)는 LLM 단계를 돌리므로
  **`OPENROUTER_API_KEY`가 필요하다.** 이 키는 kgaf3_chatbot 채팅이 이미
  요구하는 것과 **동일한 키**이며, 공유 `.env.docker`에서 읽는다.
  키가 비어 있으면 채팅에서 GraphRAG 질의가 실패한다.
- 반면 `get_kg_stats` / `run_cypher`는 LLM을 쓰지 않으므로 **키가 없어도**
  동작한다. 그래서 키가 아직 없어도 5-2의 개수 검증과 직접 Cypher 조회는
  문제없이 할 수 있다 — KG 자체가 살아있는지부터 먼저 확인하고, 자연어
  질의는 키를 넣은 뒤에 검증하면 된다.

---

관련 설정은 `configure.md`의 `GRAPHRAG_ENABLED`,
`GRAPHRAG_OPENROUTER_MODEL`(그리고 설치기가 고정하는 `GRAPHRAG_MCP_URL`).
설치 전반은 [`INSTALL.md`](INSTALL.md), 사용법은 [`USAGE.md`](USAGE.md)를
참고하라. AF3 브리지는 이와 완전히 별개의 선택 스택이니
[`BRIDGE.md`](BRIDGE.md) / [`AF3_SETUP.md`](AF3_SETUP.md)를 참고하라.
KG 원천 데이터(NPASS, Open Targets)는 각자의 라이선스를 따르며, 이 저장소는
어떤 라이선스 자산도 재배포하지 않는다.
