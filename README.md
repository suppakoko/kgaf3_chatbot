# kgaf3_chatbot

**English** | [한국어](README.ko.md)

**kgaf3_chatbot** is a local-first web chatbot with two modes: 🧬 **GraphRAG** —
natural-language Q&A over a natural-product knowledge graph (NPASS 3.0 +
Open Targets 25.12), and 🧪 **Virtual screening** — protein–ligand cofolding via
an external AlphaFold3 MCP server plus a bundled `smina --minimize` rescoring
engine, with a results web UI (ranked candidate table + interactive Mol* 3D view
+ downloadable CIF). Everything runs on Docker Compose behind a single web UI at
`http://localhost:5013`.

## Features

- 🧬 **GraphRAG Q&A (ON by default)** — ask questions in natural language over a
  baked-in knowledge graph of **273,519 nodes / 1,493,463 relationships** (NPASS
  3.0 + Open Targets 25.12). One-shot NL → Cypher → answer.
- 🧪 **Virtual screening** — paste a protein FASTA + one or more ligand SMILES
  (or upload an SDF/SMI library); get AF3 cofolding → `smina --minimize`
  rescoring → a ranked candidate table.
- 📊 **Results web UI** — interactive Mol* 3D view, downloadable CIF, sortable
  columns (smina / composite / iptm / ranking_score / e_inter).
- ⚗️ **Bundled rescoring** — ships its own `smina` docking MCP; no setup needed.
- 🔌 **OpenRouter LLM** — pick the chat model (default
  `anthropic/claude-sonnet-4-6`) from the UI.
- 🕸️ **MCP-based** — three GraphRAG MCP tools (`graphrag_query`, `get_kg_stats`,
  `run_cypher`) and an external AF3 MCP bridge.
- 🐳 **One-command install** — edit `configure.md`, run `./install.sh`, open the
  browser. All four services start with a plain `docker compose up -d`.

## Architecture

```
[Browser]  Vanilla JS + Mol* + Plotly
   │ REST / WebSocket
   ▼
┌────────────────────────────────────────────────────┐
│ kgaf3-chat   (FastAPI :5013, host loopback)         │
│   ├── OpenRouter   (LLM, chat)         ──────────────┼──► api.openrouter.ai      (external)
│   ├── HTTP MCP ─► smina-mcp  (:8001 internal)        │    bundled — smina --minimize rescoring
│   ├── SSE  MCP ─► graphrag-mcp (:8893) ─► neo4j      │    bundled — KG Q&A (bolt://neo4j:7687)
│   └── HTTP MCP ─► AF3 MCP    (:8002)   ──────────────┼──► EXTERNAL AF3 server    (you run it, on a GPU)
└────────────────────────────────────────────────────┘
        Docker Compose network
```

kgaf3-chat is an **orchestration layer**. `smina-mcp`, `graphrag-mcp`, and
`neo4j` all run inside the Compose stack; the knowledge graph is baked into the
`neo4j` image and auto-loads on first boot (~1 min). **AlphaFold3 runs outside**
this package — its weights and databases are licensed and stay in your own
environment. kgaf3-chat has no hard dependency on the GraphRAG containers, so the
app still starts and degrades gracefully (logging `service.graphrag.mcp_unreachable`)
if the KG is down.

Host ports: `5013` (kgaf3-chat web UI), `8893` (graphrag-mcp SSE, also usable by
other local MCP clients), `8002` (external AF3 MCP, user-run). `smina-mcp` (8001)
and `neo4j` (7474/7687) are internal only.

## Requirements

- **Docker** + Docker Compose (Linux, macOS, or Windows/WSL2).
- An **OpenRouter API key** — <https://openrouter.ai/keys> (needed for chat and
  for the GraphRAG `graphrag_query` tool).
- For **screening mode**: an **external, reachable AlphaFold3 MCP server** on a
  **GPU** machine that you run yourself — see [docs/AF3_SETUP.md](docs/AF3_SETUP.md).
  kgaf3-chat itself needs no GPU.
- For **GraphRAG mode**: the two GraphRAG images are **pulled** automatically from
  Docker Hub (`yoonjuho94/graphrag-neo4j:1.0`, `yoonjuho94/graphrag-mcp-server:1.1`);
  no build required.

## Quick start

```bash
# 1. Get the code
git clone https://github.com/suppakoko/kgaf3_chatbot.git
cd kgaf3_chatbot

# 2. Configure: edit the single config file
#    set OPENROUTER_API_KEY, AF3_MCP_URL, AF3_OUTPUT_ROOT
$EDITOR configure.md          # see configure.md for every option

# 3. Install (builds & starts the Docker Compose stack)
./install.sh                  # Linux / macOS
# install.bat                 # Windows / WSL2

# 4. Open the web UI
#    http://localhost:5013
```

**GraphRAG is ON by default.** A plain install starts all four services
(kgaf3-chat, smina-mcp, neo4j, graphrag-mcp) and loads the knowledge graph.
To run screening-only, set `GRAPHRAG_ENABLED = false` in `configure.md` — the
installer then starts only `kgaf3-chat` + `smina-mcp` and skips the KG images.

The installer verifies the external AF3 connection at the end and prints exactly
what to fix if it fails. See [docs/INSTALL.md](docs/INSTALL.md) for prerequisites,
step-by-step details, health checks, nginx, and troubleshooting.

> **AlphaFold3 is external and NOT bundled.** AF3 model weights and sequence
> databases are licensed by Google DeepMind and are **not** distributed with this
> project. You must obtain AF3 and its weights/databases yourself, under AF3's own
> license, and run its MCP server on your own GPU machine. kgaf3_chatbot only
> connects to it via `AF3_MCP_URL`. See [docs/AF3_SETUP.md](docs/AF3_SETUP.md).

## Using pre-built images (skip the build)

The two application images are published to GitHub Container Registry on release,
and the GraphRAG images live on Docker Hub, so you can pull instead of building:

```bash
docker pull ghcr.io/suppakoko/kgaf3-chat:latest       # web UI / API
docker pull ghcr.io/suppakoko/afmm-smina-mcp:latest   # bundled smina docking MCP
docker pull yoonjuho94/graphrag-neo4j:1.0             # Neo4j with the KG baked in
docker pull yoonjuho94/graphrag-mcp-server:1.1        # GraphRAG MCP SSE server
```

To make the Compose stack use the app images instead of building them, add a
small override that pins them:

```yaml
# docker-compose.override.yml
services:
  kgaf3-chat:
    image: ghcr.io/suppakoko/kgaf3-chat:latest
    build: null        # disable local build
  smina-mcp:
    image: ghcr.io/suppakoko/afmm-smina-mcp:latest
    build: null
```

The GraphRAG images (`yoonjuho94/graphrag-neo4j:1.0`,
`yoonjuho94/graphrag-mcp-server:1.1`) are already pulled, not built, so they need
no override.

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — detailed local install: prerequisites
  (including the GPU host for AF3), step-by-step flow, health checks, nginx,
  troubleshooting.
- [docs/USAGE.md](docs/USAGE.md) — how to use both chat modes and the results web UI.
- [docs/AF3_SETUP.md](docs/AF3_SETUP.md) — acquiring AF3 (license, weights,
  databases), running the AF3 MCP bridge, setting `AF3_MCP_URL`, and verifying.
- [docs/GRAPHRAG.md](docs/GRAPHRAG.md) — the GraphRAG knowledge-graph stack
  (default-on): KG facts, verification, and troubleshooting.
- [docs/BRIDGE.md](docs/BRIDGE.md) — making the external AF3 MCP reachable
  (host-binding patch / SSH tunnel; the "B3" reachability problem).
- [af3-bridge/README.md](af3-bridge/README.md) — the vendored AF3 MCP bridge files
  (coupled to the external af3_chatbot backend; not standalone).
- [configure.md](configure.md) — the single install config file (every key).
- [sharing_chtbot.md](sharing_chtbot.md) — maintainer guide for publishing this
  repo (GitHub, ghcr.io, Docker Hub, Zenodo DOI).

## License

[MIT](LICENSE) — © 2026 kgaf3_chatbot contributors.
See [CITATION.cff](CITATION.cff) for how to cite this software.

> **Third-party licenses.** Nothing licensed is redistributed here. AF3 model
> weights and sequence databases (Google DeepMind), and the knowledge-graph source
> datasets (**NPASS 3.0**, **Open Targets 25.12**), are governed by their **own
> licenses** and are the user's responsibility to obtain and use accordingly.
