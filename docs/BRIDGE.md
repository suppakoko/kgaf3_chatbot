# AF3 브리지 — 외부 AF3 MCP에 도달하기 (B3)

> **이 문서의 범위 — 외부 AF3 MCP 브리지 연결.** 여기서는 kgaf3_chatbot 스택이
> **외부에서 사용자가 직접 운영하는 AF3 MCP 서버**에 도달하는 네트워크 문제(B3)와
> 해결책만 다룬다. AF3 라이선스·가중치·DB 확보와 AF3 MCP 서버를 처음 세우는 절차는
> `docs/AF3_SETUP.md`를, 이 저장소에 동봉된 브리지 서버 코드(참조용)는 `af3-bridge/`
> (설명은 `af3-bridge/README.md`)를 참조한다.

kgaf3_chatbot은 AlphaFold3를 **직접 실행하지 않는다.** 외부에서 따로 돌아가는
**AF3 MCP 서버**에 HTTP로 요청을 보낼 뿐이다. 이 문서는 그 연결이 막히는
유일한 구조적 지점(B3)과, 막혔을 때 푸는 방법을 정리한다.

```
kgaf3-chat 컨테이너 ──HTTP──► AF3 MCP 서버(:8002/mcp/) ──docker run──► alphafold3 (GPU, 가중치·DB)
   (AF3_MCP_URL)               (외부 af3_chatbot 저장소가 운영)
```

`configure.md`의 `AF3_MCP_URL`이 가운데 칸(AF3 MCP 서버)을 가리킨다.
맨 오른쪽 AF3 docker만 깔려 있고 가운데 MCP 서버가 도달 불가하면,
kgaf3_chatbot은 뜨긴 하지만 cofolding 단계에서 매번 실패한다.

> **참고 — GraphRAG는 여기 해당 없음.** GraphRAG는 `docker compose up -d`로
> 기본 실행되는 스택으로, **compose 내부에서 완전히** 돌아간다
> (neo4j + graphrag-mcp 컨테이너). 외부 호스트 루프백 바인딩 문제(B3)나 이 문서의
> AF3 브리지 대책은 GraphRAG에 적용되지 않는다. 배포·사용법은 `docs/GRAPHRAG.md` 참조.

> **AF3 가중치·MSA DB는 절대 배포 대상이 아니다(라이선스).** 사용자 환경에
> 그대로 남는다. kgaf3_chatbot은 "이미 가중치/DB를 갖춘 AF3에 연결"만 책임진다.

---

## 1. B3 — 무엇이 막히나

외부 `af3_chatbot/app/mcp/af3_mcp_http.py`의 `main()`은 바인딩을 다음처럼
**하드코딩**한다:

```python
uvicorn.run(asgi_app, host="127.0.0.1", port=8002, log_level="info")
```

`127.0.0.1`(루프백) 바인딩이라, **같은 호스트의 다른 프로세스만** 도달한다:

- kgaf3-chat **컨테이너 안**에서는 호스트 루프백이 아니라
  `host.docker.internal`(또는 docker0 브리지 IP)로 호스트를 본다 → 도달 불가.
- **원격 머신**(예: Windows에서 kgaf3_chatbot, 별도 Linux 박스에서 AF3)에서도
  당연히 도달 불가.

설치기는 설치 마지막에 `AF3_MCP_URL`로 능동 연결 검증을 수행하며, 이 도달 실패를
명시적으로 진단하고 아래 해결책을 안내한다.

---

## 2. 권장 해결책 — 1줄 패치 (외부 AF3를 직접 관리할 때)

AF3 MCP 서버를 직접 운영한다면 이게 가장 깔끔하다. 바인딩을 환경변수로 바꾼다.

### 2-1. 패치 적용

이 저장소의 `patches/af3_mcp_host_env.patch`를 **외부 af3_chatbot 저장소**에 적용한다.

```bash
# af3_chatbot 저장소 루트에서
git apply /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
# git 체크아웃이 아니면:
patch -p1 < /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
```

패치는 `main()`을 다음과 같이 바꾼다(그리고 `import os`를 추가):

```python
host = os.getenv("AF3_MCP_HOST", "0.0.0.0")
port = int(os.getenv("AF3_MCP_PORT", "8002"))
uvicorn.run(asgi_app, host=host, port=port, log_level="info")
```

### 2-2. 바인딩 설정 (af3-mcp.service)

`af3-mcp` systemd 유닛에 환경변수를 넣는다.

```ini
[Service]
Environment=AF3_MCP_HOST=0.0.0.0      # 또는 docker0 브리지 IP (예: 172.17.0.1)
Environment=AF3_MCP_PORT=8002
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart af3-mcp
```

### 2-3. 포트를 신뢰 대상에만 개방

`0.0.0.0`은 모든 인터페이스에 연다. **방화벽으로 범위를 좁혀라.**
docker0 브리지(컨테이너용)만 허용하는 예:

```bash
# docker0 인터페이스를 신뢰 영역으로 (컨테이너 → 호스트 도달 허용)
sudo firewall-cmd --permanent --zone=trusted --add-interface=docker0
sudo firewall-cmd --reload
```

특정 원격 IP만 열려면 `--zone=trusted --add-source=<원격IP>/32`를 쓴다.
`0.0.0.0` 노출이 부담스러우면 `AF3_MCP_HOST`를 docker0 IP로 못 박는 편이 더 안전하다.

### 2-4. `AF3_MCP_URL` 맞추기 (configure.md)

- kgaf3-chat 컨테이너 → 같은 호스트의 AF3: `AF3_MCP_URL = http://host.docker.internal:8002/mcp/`
  (Linux compose에서 `host.docker.internal`이 안 풀리면 `extra_hosts`로
  `host-gateway`를 매핑하거나 docker0 IP를 직접 적는다.)
- 원격 AF3 호스트: `AF3_MCP_URL = http://<원격호스트IP>:8002/mcp/`

---

## 3. 패치 없이 푸는 방법 (외부 코드 못 건드릴 때)

### 3-A. host network 모드 (Linux 단일 사용자, 최후 수단)

kgaf3-chat 컨테이너를 호스트 네트워크에 붙이면 `127.0.0.1:8002`에 직접 닿는다.
compose override 예:

```yaml
# docker-compose.override.yml
services:
  kgaf3-chat:
    network_mode: host
```

이때 `AF3_MCP_URL = http://127.0.0.1:8002/mcp/`. 단점: 포트 격리가 사라지고,
동봉 smina-mcp 내부망 격리 이점을 잃는다. 정말 안 될 때만 쓴다.

### 3-B. 포트 포워더 (socat / nginx)

호스트에서 docker0(또는 외부 인터페이스) → 루프백으로 중계한다.

```bash
# docker0(172.17.0.1):8002 로 들어온 트래픽을 127.0.0.1:8002 로 포워딩
socat TCP-LISTEN:8002,bind=172.17.0.1,fork,reuseaddr TCP:127.0.0.1:8002
```

그리고 `AF3_MCP_URL = http://172.17.0.1:8002/mcp/`. nginx `stream{}` 블록으로도
같은 중계가 가능하다. 운영을 원하면 socat을 systemd 유닛으로 감싼다.

### 3-C. SSH 터널 (원격 AF3)

AF3가 원격 Linux 호스트에서만 `127.0.0.1:8002`로 떠 있을 때, kgaf3_chatbot이
도는 머신에서 터널을 연다:

```bash
ssh -N -L 8002:127.0.0.1:8002 user@af3host
```

그러면 로컬 `127.0.0.1:8002`가 원격 AF3 MCP로 연결된다. kgaf3-chat 컨테이너에서
이 로컬 포트에 닿으려면 host network 모드(3-A)와 조합하거나, 터널 바인딩을
`-L 0.0.0.0:8002:127.0.0.1:8002`로 열고 `AF3_MCP_URL`을 docker0 IP로 둔다.
(Windows에서 kgaf3_chatbot, 원격 Linux에서 AF3 구성에 권장.)

---

## 4. 검증

설치기는 끝에서 `AF3_MCP_URL`로 MCP `initialize` + `tools/list`를 호출해
연결과 도구 목록을 확인한다. 수동으로 빠르게 점검하려면:

```bash
# 200 응답이면 엔드포인트는 살아있음 (MCP 핸드셰이크는 설치기가 수행)
curl -i http://host.docker.internal:8002/mcp/
```

연결이 실패하면 위 1~3절 순서로 확인한다: ① af3-mcp가 떠 있나
(`systemctl status af3-mcp`) → ② 바인딩이 루프백 전용인가(B3) → ③ `AF3_MCP_URL`이
컨테이너 관점에서 올바른 호스트를 가리키나. 이 중 하나가 항상 원인이다.

관련 설정은 `configure.md`의 `AF3_MCP_URL`, `AF3_MCP_AUTH_TOKEN`, `AF3_OUTPUT_ROOT`.
