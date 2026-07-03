**English** | [한국어](GRAPHRAG.ko.md)

# GraphRAG — Self-Contained Knowledge-Graph Stack (ON by default)

kgaf3_chatbot does **not carry the knowledge graph (KG) itself.** GraphRAG runs
separately as a **self-contained Docker stack** with the entire KG baked in, and
`kgaf3-chat` only talks to the **MCP SSE** endpoint in front of it. `kgaf3-chat`
is a pure orchestration layer and never touches Neo4j directly. This document
covers how to turn that stack on, how to use it, and how to unblock it when it
gets stuck.

```
kgaf3-chat container ──MCP SSE──► graphrag-mcp(:8893/sse) ──bolt──► neo4j(:7687, KG embedded)
   (GRAPHRAG_MCP_URL)            (holds the Neo4j driver + LLM stage)
```

The KG combines **NPASS 3.0 + Open Targets release 25.12**, totaling
**273,519 nodes / 1,493,463 relationships**. This data is **baked in** to the
`yoonjuho94/graphrag-neo4j:1.0` image in its entirety, so it autoloads on the
container's first boot (about 1 minute). There is no separate import procedure.

> **GraphRAG is ON by default in kgaf3_chatbot.** With no separate profile,
> simply running `docker compose up -d` brings up `neo4j` and `graphrag-mcp`
> automatically alongside the other services (`kgaf3-chat`, `smina-mcp`). The
> `graphrag` compose profile from earlier releases **no longer exists.** Even
> so, `kgaf3-chat` has no `depends_on` for GraphRAG, so the app boots normally
> even when the KG stack is not up. If the stack is not up, the app simply logs
> `service.graphrag.mcp_unreachable` and moves on quietly.

For installation and configuration in general, see [`INSTALL.md`](INSTALL.md);
for usage, see [`USAGE.md`](USAGE.md).

---

## 1. What's Inside — Two Self-Contained Images

Both images are obtained from Docker Hub by **pull, not build**.

- `yoonjuho94/graphrag-neo4j:1.0` — Neo4j with the KG baked in.
  Container name `graphrag-neo4j` (compose service name `neo4j`).
  Internal bolt is `bolt://neo4j:7687`. Default password `kist2026npi` (matched
  to the image). The host ports `7474`/`7687` are commented out by default in
  compose. Data persists in the named volume `neo4j-data`.
- `yoonjuho94/graphrag-mcp-server:1.1` — An MCP **SSE** server on port **8893**.
  Container name `graphrag-mcp` (compose service name `graphrag-mcp`).
  Internal endpoint `http://graphrag-mcp:8893/sse`. It is also exposed on the
  host loopback `127.0.0.1:8893`, so other local MCP clients (e.g., Claude
  Desktop) can share the same server. This container holds the Neo4j driver and
  runs the LLM stage of `graphrag_query` itself (using `OPENROUTER_API_KEY`
  from the shared `.env.docker`).

---

## 2. How to Turn It On (ON by default) / How to Turn It Off (opt-out)

### 2-1. Just Turn It On — the Default

```bash
docker compose up -d
```

- This single line brings up **all four services**: `kgaf3-chat`, `smina-mcp`,
  `neo4j`, `graphrag-mcp`. GraphRAG is included by default, with no separate
  flag or profile.
- Installing via the installer (`install.sh`) brings up the full stack exactly
  as above and auto-injects the fixed value
  `GRAPHRAG_MCP_URL = http://graphrag-mcp:8893/sse`.
- The GraphRAG-related keys in `configure.md` (with defaults):

  ```ini
  GRAPHRAG_ENABLED = true                                # default true
  GRAPHRAG_OPENROUTER_MODEL = anthropic/claude-opus-4-7  # display/logging label
  ```

### 2-2. How to Turn It Off — Screening-Only (opt-out)

To use 🧪 screening only, without the KG, choose one of the two.

- **Installer path:** Set `GRAPHRAG_ENABLED = false` in `configure.md`, and the
  installer skips pulling the GraphRAG images and runs only
  `docker compose up -d kgaf3-chat smina-mcp`.
- **Manual path:** Bring up only the services you need, by name.

  ```bash
  docker compose up -d kgaf3-chat smina-mcp
  ```

  This way `neo4j`/`graphrag-mcp` never come up at all. If the full stack is
  already up and you want to take just the KG down, scale the two services to
  zero.

  ```bash
  docker compose up -d --scale neo4j=0 --scale graphrag-mcp=0
  ```

`kgaf3-chat` has **no** `depends_on` for GraphRAG. So even if you turn GraphRAG
off (or while the KG is still loading), the app boots normally, and other
features such as screening and chat are unaffected.

---

## 3. App Environment Variables (.env.docker / config.py)

| Key | Default | Meaning |
|----|--------|----|
| `GRAPHRAG_ENABLED` | `true` | User toggle. When true (default), the app connects to the MCP server and the full stack comes up. When false, the installer skips the KG images and brings things up screening-only. |
| `GRAPHRAG_MCP_URL` | `http://graphrag-mcp:8893/sse` | Fixed to the internal compose service. The installer auto-injects it. |
| `GRAPHRAG_MCP_AUTH_TOKEN` | (empty) | A Bearer token, used only when you place the MCP server behind authentication. |
| `GRAPHRAG_OPENROUTER_MODEL` | `anthropic/claude-opus-4-7` | **Just a display/logging label.** The model actually used is authoritatively the MCP server's own `OPENROUTER_MODEL`. |

> `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` **do not exist as app
> settings.** This is because the Neo4j driver was moved into the graphrag-mcp
> container. The compose file still has a `NEO4J_PASSWORD` interpolation
> variable, but that is only a **compose-level knob** that sets the neo4j
> container's `NEO4J_AUTH` (default `kist2026npi`), not a kgaf3_chatbot app
> setting. Do not instruct users in configure.md to configure `NEO4J_*`.

---

## 4. MCP Tools — Exactly Three

The graphrag-mcp exposes only the three tools below.

- **`graphrag_query(question, provider="openrouter")`** — Handles natural
  language → Cypher → execution → Markdown answer in **one shot**. Returned JSON
  keys: `answer`, `cypher`, `row_count`, `rows_preview`,
  `token_usage {input_tokens, output_tokens}`, `model_id`, `timestamp`,
  `provider`. An LLM key **is required**.
- **`get_kg_stats()`** — Node/relationship counts. LLM key **not required**.
- **`run_cypher(query, params)`** — Read-only direct Cypher. LLM key **not
  required**.

### 4-1. One-Shot Query and How the Progress Card Gets Filled

The 🧬 GraphRAG chat mode shows four stages on the progress card:
`cypher_gen` / `neo4j_exec` / `answer_synth` / `complete`. But because the MCP
call is **one shot**, these are not filled by three separate live LLM calls.
Instead, each stage is filled **retrospectively** from the metadata returned by
`graphrag_query` (`cypher`, `row_count`, `token_usage`). The frontend contract
(what you see) stays the same, but internally the card is completed by tracing
back through the result of a single call.

For actual usage examples in chat mode, see [`USAGE.md`](USAGE.md).

---

## 5. Verification

### 5-1. Confirm KG Autoload

After bringing up the stack, check the logs to see whether Neo4j has finished
loading the KG (about 1 minute).

```bash
docker compose up -d
docker logs -f graphrag-neo4j   # wait until KG autoload completes
```

### 5-2. Node/Relationship Counts (expect 273519 / 1493463)

Once loading is done, count them directly to confirm the KG is intact. This
works without an LLM key.

```bash
docker exec graphrag-neo4j cypher-shell -u neo4j -p kist2026npi \
  "MATCH (n) RETURN count(n);"        # expect 273519
docker exec graphrag-neo4j cypher-shell -u neo4j -p kist2026npi \
  "MATCH ()-[r]->() RETURN count(r);" # expect 1493463
```

### 5-3. Confirm the MCP Server

```bash
docker logs graphrag-mcp
```

You can also confirm it is alive via the host-exposed SSE endpoint
(`127.0.0.1:8893`).

---

## 6. Troubleshooting

### 6-A. `service.graphrag.mcp_unreachable`

If you see this line in the `kgaf3-chat` logs, the app failed to reach the MCP
server. Check in order:

- ① Is graphrag-mcp actually up — check for `graphrag-mcp` in `docker ps`.
  If it's not there, you brought things up screening-only (2-2) or scaled the KG
  services to zero. Restart the full stack with `docker compose up -d`.
- ② Is `GRAPHRAG_ENABLED=true` — if false, the app doesn't even try to connect.
- ③ Is `GRAPHRAG_MCP_URL` set to `http://graphrag-mcp:8893/sse` — this value is
  fixed and auto-injected by the installer to the internal service name, so
  leaving it untouched is correct.

Remember that since `kgaf3-chat` has no GraphRAG `depends_on`, the app itself
keeps working normally even when this log appears (other features like
cofolding and chat are unrelated).

### 6-B. Port 8893 / 7474 Conflicts

- `graphrag-mcp` is also exposed on the host loopback `127.0.0.1:8893`. If a
  process already using 8893 locally (another MCP server, etc.) exists, the
  container cannot come up. Clean up the occupying process, or adjust the host
  publish. (Container-to-container communication happens over
  `graphrag-mcp:8893`, so it is independent of the host port conflict.)
- The host ports `7474` (HTTP browser) / `7687` (bolt) of `graphrag-neo4j` are
  **commented out** by default in compose. If you opened these ports to use the
  Neo4j Browser but an existing local Neo4j already occupies them, there is a
  conflict. In that case, map to different host ports, or don't expose to the
  host at all and access via `docker exec ... cypher-shell` (5-2).

### 6-C. OPENROUTER_API_KEY

- `graphrag_query` (natural-language queries) runs an LLM stage, so it
  **requires `OPENROUTER_API_KEY`.** This is the **same key** kgaf3_chatbot chat
  already requires, and it is read from the shared `.env.docker`. If the key is
  empty, GraphRAG queries fail in chat.
- By contrast, `get_kg_stats` / `run_cypher` do not use an LLM, so they work
  **even without a key**. So even if you don't have a key yet, you can perform
  the count verification in 5-2 and direct Cypher queries without any problem —
  confirm the KG itself is alive first, and verify natural-language queries
  after adding the key.

---

Related settings are `GRAPHRAG_ENABLED` and `GRAPHRAG_OPENROUTER_MODEL` in
`configure.md` (plus `GRAPHRAG_MCP_URL`, which the installer fixes).
For installation in general, see [`INSTALL.md`](INSTALL.md); for usage, see
[`USAGE.md`](USAGE.md). The AF3 bridge is an entirely separate optional stack,
so see [`BRIDGE.md`](BRIDGE.md) / [`AF3_SETUP.md`](AF3_SETUP.md). The KG source
data (NPASS, Open Targets) follows its own licenses, and this repository does
not redistribute any licensed assets.
