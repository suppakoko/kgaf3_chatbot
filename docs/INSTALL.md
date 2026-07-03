**English** | [한국어](INSTALL.ko.md)

# Installation Guide — Getting kgaf3_chatbot Running on Your Machine

This document lays out the detailed procedure for installing **kgaf3_chatbot** on a
local machine from scratch. The design is such that you fill in the single
`configure.md` file and run `./install.sh`, and the entire Docker Compose stack comes
up. Here we cover everything around that — the prerequisites, verification, and
troubleshooting before and after.

kgaf3_chatbot is a local-first web chatbot with two modes.

- 🧬 **GraphRAG** — natural-language question answering over a natural-product
  knowledge graph (NPASS 3.0 + Open Targets 25.12). **ON by default.**
- 🧪 **Virtual screening** — performs protein–ligand co-folding via an external
  AlphaFold3 MCP plus built-in `smina --minimize` rescoring, and shows the results in
  the web UI.

The main service is a FastAPI container called `kgaf3-chat`, which comes up on the host
loopback `http://localhost:5013`. The smina docking engine (`smina-mcp`) and the
GraphRAG stack (`neo4j` + `graphrag-mcp`) are managed together within the same Compose
stack.

```
[browser] ──HTTP──► kgaf3-chat (FastAPI :5013)
                        ├── OpenRouter (chat LLM)              → external internet (OPENROUTER_API_KEY)
                        ├── smina-mcp (:8001, internal only)   → built-in docking/rescore
                        ├── graphrag-mcp (:8893/sse) ─► neo4j  → knowledge graph (ON by default)
                        └── AF3 MCP (:8002)                    → external GPU machine (AF3_MCP_URL, run separately)
```

> **AF3 is not included in this package (licensing).** The AlphaFold3 **weights** and
> **sequence databases** are assets licensed by Google DeepMind and are **not
> redistributed** in this repository. To use screening, you must obtain AF3 and its
> weights/DBs yourself, run them on your own GPU machine, and put the address of the AF3
> MCP server in front of it into `AF3_MCP_URL`. For how to obtain and configure it, see
> **[docs/AF3_SETUP.md](AF3_SETUP.md)**.

---

## 1. Prerequisites

Before installing, the following three items must be ready.

### 1-1. Docker + Docker Compose

- A Linux, macOS, or Windows/WSL2 machine with **Docker Engine** and **Docker Compose
  v2** installed. (The compose file uses v2 syntax, so it has no top-level `version:`
  key.)
- Verify the installation:

  ```bash
  docker --version
  docker compose version
  ```

- You need **free disk space** to hold the GraphRAG images (several GB) and the app
  image. kgaf3-chat itself uses only the CPU and **does not require a GPU** (AF3's GPU is
  on an external host).

### 1-2. A GPU machine running AF3 (only if you use screening)

🧪 Screening mode only works if there is a **reachable external AF3 MCP server**.

- Run the **AlphaFold3 + licensed weights/DBs** that you obtained yourself on your own
  **GPU machine**, and bring up an AF3 MCP server (port `8002`) in front of it.
- It may be on the **same machine** as kgaf3-chat or on a **remote machine**. Either
  way, from the container's point of view, `AF3_MCP_URL` must point to that MCP server.
- The full process — obtaining AF3, running the MCP bridge, patching the host binding,
  configuring `AF3_MCP_URL`, and verification — is in
  **[docs/AF3_SETUP.md](AF3_SETUP.md)** and **[docs/BRIDGE.md](BRIDGE.md)**.
- If you will only use GraphRAG, installation completes without AF3 (regardless of the
  opt-out in section 5 below, an AF3 verification failure is treated only as a warning
  and the app still comes up).

### 1-3. OpenRouter API key

- The chat LLM and GraphRAG's `graphrag_query` step use **OpenRouter**.
- Get a key: <https://openrouter.ai/keys> — put a key of the form `sk-or-...` into
  `OPENROUTER_API_KEY`.
- This key is **shared** by chat and GraphRAG (no separate key needed). If the key is
  empty, chat and `graphrag_query` fail. On the other hand, GraphRAG's
  `get_kg_stats`/`run_cypher` do not use the LLM, so they work without a key.

---

## 2. Get the code

```bash
git clone https://github.com/suppakoko/kgaf3_chatbot.git
cd kgaf3_chatbot
```

All subsequent commands are run from this repository root.

---

## 3. Configuration — filling in `configure.md`

The installer reads **only the first ```ini code block in `configure.md`** (all other
prose is just documentation and is ignored). Inside the block, one `KEY = value` per
line, whitespace around `=` is trimmed, text after `#` is a comment, and blank lines are
skipped. **Do not change the key names.**

### 3-1. Keys you must fill in (required)

| Key | Meaning |
|-----|---------|
| `OPENROUTER_API_KEY` | OpenRouter key for the chat/GraphRAG LLM (`sk-or-...`). |
| `AF3_MCP_URL` | External AF3 MCP server URL. The default is `http://host.docker.internal:8002/mcp/`, which assumes AF3 on the same host. |
| `AF3_OUTPUT_ROOT` | The **absolute host path** of the AF3 output directory. The app mounts this path **read-only** to read prediction results. It **must already exist**. |

### 3-2. Optional keys

| Key | Default | Meaning |
|-----|---------|---------|
| `PROFILE` | `lite` | The only currently working profile. `full` (OpenMM-based) is **reserved** and not yet functional, so leave it at `lite`. |
| `APP_PORT` | `5013` | Web UI host port (`http://localhost:<APP_PORT>`). |
| `LLM_DEFAULT_MODEL` | `anthropic/claude-sonnet-4-6` | The chat model preselected in the UI (changeable in the app). |
| `AF3_MCP_AUTH_TOKEN` | _(empty)_ | Bearer token used only when the AF3 MCP requires authentication. |
| `ENABLE_NGINX` | `false` | If `true`, places the app behind an nginx reverse proxy with BasicAuth. |
| `BASIC_AUTH_USER` | _(empty)_ | BasicAuth username (only when `ENABLE_NGINX=true`). |
| `BASIC_AUTH_PASS` | _(empty)_ | BasicAuth password (only when `ENABLE_NGINX=true`). |
| `GRAPHRAG_ENABLED` | `true` | The GraphRAG stack (Neo4j KG + `:8893` MCP) plus the 🧬 GraphRAG chat mode. **ON by default.** If `false`, opts out to screening-only (skips pulling the heavy KG images). |
| `GRAPHRAG_OPENROUTER_MODEL` | `anthropic/claude-opus-4-7` | **Only a label displayed** in the UI/history for GraphRAG answers. The model that actually runs the query is authoritatively the MCP server's own `OPENROUTER_MODEL`. |

> Fixed values the installer injects automatically (you do not touch these):
> `GRAPHRAG_MCP_URL = http://graphrag-mcp:8893/sse`, plus the smina/OpenMM fixed
> variables. The Neo4j password is also auto-wired on the internal network to
> `kist2026npi` to match the KG image.
> **Do not put `NEO4J_*` directly into configure.md** — the Neo4j driver lives inside
> the graphrag-mcp container (for details, see [docs/GRAPHRAG.md](GRAPHRAG.md)).

### 3-3. Example ini block

Fill in the block inside `configure.md` as below (change only the values, leave the keys
as-is).

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

The full description of every key is in **[configure.md](../configure.md)**.

---

## 4. Installation — `./install.sh` (recommended)

Once configuration is done, run the installer.

```bash
./install.sh            # Linux / macOS
# install.bat           # Windows / WSL2 (double-click or run in a terminal)
```

The installer is idempotent. It is safe to run it again after fixing the configuration.

### 4-1. The 8 stages of install.sh

| Stage | What it does |
|-------|--------------|
| **1/8 Preflight** | Pre-checks such as Docker/Compose presence and whether `APP_PORT` is occupied. |
| **2/8 Parse** | Parses the ini block in `configure.md` to generate `.env.docker`. Aborts with a clear message if a required key is missing. |
| **3/8 Bootstrap dirs** | Creates host directories and verifies that `AF3_OUTPUT_ROOT` actually exists. |
| **4/8 SELinux/firewalld** | (best-effort) If SELinux is Enforcing, labels `AF3_OUTPUT_ROOT` with `container_file_t`; if firewalld is on, puts `docker0` into the trusted zone. Proceeds even if this fails. |
| **5/8 Build** | Builds the kgaf3-chat and smina-mcp images via `docker compose build`. |
| **6/8 Start** | Starts the stack. **By default, all four services** (`docker compose up -d`) — kgaf3-chat, smina-mcp, neo4j, graphrag-mcp. If `GRAPHRAG_ENABLED=false`, only brings up `docker compose up -d kgaf3-chat smina-mcp` and skips pulling the KG images. |
| **7/8 Health poll** | Polls until `http://localhost:5013/health/ready` is ready (up to 180 seconds). |
| **8/8 Verify AF3** | Calls MCP `initialize` + `tools/list` at `AF3_MCP_URL` to actively verify the external AF3 connection and the tool list. |

At the end it prints a success report containing the URL, profile, LLM, smina status,
GraphRAG status, and the external AF3 connection result. Even if the AF3 connection
fails, the stack is up, so the installer exits normally and states what you need to fix
before running screening (→ [docs/BRIDGE.md](BRIDGE.md)).

### 4-2. GraphRAG is on by default

If you leave `GRAPHRAG_ENABLED` alone (default `true`), stage 6 brings up all four
services and **pulls the two KG images from Docker Hub once, the first time**.

```
yoonjuho94/graphrag-neo4j:1.0        # Neo4j with the entire KG baked in
yoonjuho94/graphrag-mcp-server:1.1   # the :8893 MCP SSE server
```

- These images are large, so **the pull on the first `up` takes time.**
- On its first boot, Neo4j **auto-loads** the KG, which takes **about 1 minute** to load
  **273,519 nodes / 1,493,463 relationships**. There is no separate import or external
  DB procedure.
- To confirm the load completion via logs:

  ```bash
  docker logs -f graphrag-neo4j       # wait until KG auto-load completes
  ```

- Verify node/relationship counts (works without an LLM key):

  ```bash
  docker exec graphrag-neo4j cypher-shell -u neo4j -p kist2026npi \
    "MATCH (n) RETURN count(n);"        # expect 273519
  docker exec graphrag-neo4j cypher-shell -u neo4j -p kist2026npi \
    "MATCH ()-[r]->() RETURN count(r);" # expect 1493463
  ```

For everything about GraphRAG deployment, usage, and troubleshooting, see
**[docs/GRAPHRAG.md](GRAPHRAG.md)**.

---

## 5. Manual installation — running Docker Compose directly

If you want to bring things up by hand without using the installer, follow the flow
below. In this case you do the `configure.md → .env.docker` conversion yourself or copy
the example file and fill it in.

```bash
# 1. Prepare the environment file (fill in OPENROUTER_API_KEY, AF3_MCP_URL, AF3_OUTPUT_ROOT, etc.)
cp .env.docker.example .env.docker
$EDITOR .env.docker

# 2. Build the images (matching the host uid/gid keeps AF3 output mount permissions clean)
docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)

# 3. Start the stack — by default all four services (including GraphRAG)
docker compose up -d

# 4. Health check
curl -fsS http://127.0.0.1:5013/health/ready
```

- The default `docker compose up -d` brings up all four of **kgaf3-chat, smina-mcp,
  neo4j, graphrag-mcp**. The `graphrag` compose profile that existed in earlier releases
  is **gone.**
- kgaf3-chat has **no** `depends_on` for GraphRAG. So even if the KG is not up, the app
  starts normally, and if it cannot reach the MCP it only logs
  `service.graphrag.mcp_unreachable` and degrades gracefully.

---

## 6. GraphRAG opt-out (using screening only)

If you do not need the knowledge graph and will only use screening, drop GraphRAG in one
of two ways. Then the heavy KG images are not pulled.

- **With the installer:** set `GRAPHRAG_ENABLED = false` in `configure.md` and run
  `./install.sh`. The installer runs only `docker compose up -d kgaf3-chat smina-mcp` in
  stage 6.
- **Manually:** bring up only the screening services.

  ```bash
  docker compose up -d kgaf3-chat smina-mcp
  ```

To turn it back on later, revert to `GRAPHRAG_ENABLED=true` and reinstall, or bring up
all four services with `docker compose up -d`.

---

## 7. Using prebuilt images (skipping the build)

Instead of building locally, you can pull and use the distribution images. The
kgaf3-chat and smina-mcp images are published to the GitHub Container Registry (ghcr.io)
on release (`.github/workflows/docker-publish.yml`). The GraphRAG images come from
`yoonjuho94/*` on Docker Hub (always pulled, never built).

```bash
docker pull ghcr.io/suppakoko/kgaf3-chat
docker pull ghcr.io/suppakoko/afmm-smina-mcp
```

To make Compose use these images instead of a local build, add an override file. It is
good practice to pin version tags for reproducibility.

```yaml
# docker-compose.override.yml
services:
  kgaf3-chat:
    image: ghcr.io/suppakoko/kgaf3-chat
    build: null        # disable local build
  smina-mcp:
    image: ghcr.io/suppakoko/afmm-smina-mcp
    build: null
```

The GraphRAG images (`yoonjuho94/graphrag-neo4j:1.0`, `yoonjuho94/graphrag-mcp-server:1.1`)
are already specified as pull targets in compose, so no override is needed.

---

## 8. After installation — health check and access

- **Health endpoint:**

  ```bash
  curl -fsS http://localhost:5013/health/ready
  ```

  Both installer stage 7 and the compose health check look at this endpoint.

- **Web UI:** in a browser, `http://localhost:5013` (or the port you changed via
  `APP_PORT`). Use the mode toggle to switch between 🧪 screening (default) / 🧬
  GraphRAG. For actual usage and examples, see **[docs/USAGE.md](USAGE.md)**.

- **Logs:**

  ```bash
  docker compose logs -f kgaf3-chat        # app logs
  docker compose ps                        # service status
  ```

- Setting `ENABLE_NGINX=true` places the app behind an nginx reverse proxy with
  BasicAuth. Expose it this way to allow remote access (kgaf3-chat itself binds only to
  the host loopback `127.0.0.1:5013`).

---

## 9. Troubleshooting

### 9-A. Port conflicts (5013 / 8893)

- **5013** — the kgaf3-chat web UI. If another process is already using it, Preflight
  catches it. Change `APP_PORT` in `configure.md` to a different value and reinstall.
- **8893** — the graphrag-mcp SSE is also exposed on the host loopback `127.0.0.1:8893`
  (so the server can be shared with other local MCP clients, e.g. Claude Desktop). If a
  process already uses 8893 locally, the container cannot come up. Clean up the occupying
  process or adjust the host publish (inter-container communication happens over the
  internal network `graphrag-mcp:8893`, so it is separate from host port conflicts).
- **7474 / 7687** — the neo4j host ports are **commented out** by default in compose, so
  they usually do not conflict. If you opened them to use Neo4j Browser and an existing
  Neo4j occupies them, map them to different host ports, or don't expose them at all and
  access via `docker exec ... cypher-shell`.

### 9-B. SELinux (RHEL / Rocky / Fedora, Enforcing)

On SELinux Enforcing hosts, plain bind mounts are **denied**. Installer stage 4 labels
`AF3_OUTPUT_ROOT` on a best-effort basis, but if that fails, add a relabel suffix to the
mount in `docker-compose.yml`.

- Private RW bind: `:Z` (e.g. when changing `/data` from a named volume to a bind)
- Shared RO bind: `:z,ro` — use on the `AF3_OUTPUT_ROOT` line. Because the AF3 service
  **writes** to that path, adding `:Z` (a private label) would break that writer. Always
  use `:z,ro`.

  ```yaml
  # docker-compose.yml (only on SELinux hosts)
  - "${AF3_OUTPUT_ROOT}:${AF3_OUTPUT_ROOT}:z,ro"
  ```

The default is a plain mount — because `:Z`/`:z` would instead break on non-SELinux
hosts (Docker Desktop/Ubuntu). Edit it as above only when deploying to a SELinux host.

### 9-C. `host.docker.internal` does not resolve (Linux)

The kgaf3-chat container maps `host.docker.internal:host-gateway` via `extra_hosts` to
reach the AF3 MCP on the same host. If it still does not resolve, write the docker0
bridge IP (e.g. `172.17.0.1`) directly into `AF3_MCP_URL`. For detailed handling, see
**[docs/BRIDGE.md](BRIDGE.md)**.

### 9-D. Cannot reach external AF3 (stage 8 failure)

If the AF3 connection fails at installer stage 8, the stack is up but screening fails
every time at the co-folding step. The cause is almost always one of three.

1. The AF3 MCP is not up — check with `systemctl status af3-mcp`.
2. The AF3 MCP is bound only to `127.0.0.1` and is unreachable from the container/remote
   (the B3 problem).
3. `AF3_MCP_URL` points to the wrong host from the container's point of view.

The fixes (host-binding patch `patches/af3_mcp_host_env.patch`, an SSH tunnel, a port
forwarder, etc.) are in **[docs/BRIDGE.md](BRIDGE.md)**, and the whole of obtaining AF3,
running the MCP, and configuring `AF3_MCP_URL` is in **[docs/AF3_SETUP.md](AF3_SETUP.md)**.

### 9-E. `service.graphrag.mcp_unreachable`

If you see this line in the kgaf3-chat logs, the app could not reach the GraphRAG MCP.
Check in order.

- Is graphrag-mcp actually up — `docker compose ps`.
- Is `GRAPHRAG_ENABLED=true` — if false, it does not even try to connect.
- Is `GRAPHRAG_MCP_URL` set to `http://graphrag-mcp:8893/sse` — the installer injects
  this as a fixed value, so it is correct not to touch it.

Even when this log appears, kgaf3-chat itself (screening/chat) keeps working normally.
For detailed diagnosis, see section 6 of **[docs/GRAPHRAG.md](GRAPHRAG.md)**.

---

## Related documents

- **[configure.md](../configure.md)** — the single installation configuration file (all keys explained).
- **[docs/AF3_SETUP.md](AF3_SETUP.md)** — obtaining the AF3 license/weights/DBs + running the AF3 MCP bridge + configuring/verifying `AF3_MCP_URL`.
- **[docs/BRIDGE.md](BRIDGE.md)** — the external AF3 MCP reachability problem (B3) and its solutions.
- **[docs/GRAPHRAG.md](GRAPHRAG.md)** — the GraphRAG knowledge-graph stack (deployment, usage, troubleshooting).
- **[docs/USAGE.md](USAGE.md)** — the two chat modes and how to use the result web UI, with examples.
- **[README.md](../README.md)** — project overview.
