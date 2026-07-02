# kgaf3_chatbot — Install Configuration (configure.md)

This is the **single configuration file** for installing kgaf3_chatbot (Lite).
Fill in the values, then run the installer:

- **Linux / macOS:** `./install.sh`
- **Windows:** `install.bat` (double-click, or run from a terminal)

---

## How this file works

- The installer reads **only the first ```ini fenced code block** below — everything
  else on this page is documentation and is ignored.
- Inside that block: one `KEY = value` per line. Whitespace around `=` is trimmed,
  text after `#` is treated as a comment, blank lines are skipped.
- Unknown keys produce a warning but do not stop the install.
- Missing **required** keys stop the install with a clear message telling you what
  to add.

> Edit the values **inside the ```ini block only**. Do not rename the keys.

---

## What you must provide

| Key | Required | What it is |
|---|---|---|
| `OPENROUTER_API_KEY` | ✅ | Your OpenRouter API key for the chat LLM. Get one at <https://openrouter.ai/keys>. |
| `AF3_MCP_URL` | ✅ | The URL of your **external** AlphaFold3 MCP server. Default assumes AF3 runs on the same machine reachable at `host.docker.internal:8002`. |
| `AF3_OUTPUT_ROOT` | ✅ | An **absolute path on this host** to the AF3 output directory. kgaf3_chatbot mounts it read-only to read prediction results. The path must already exist. |

### The bundled docking engine needs **no configuration**

kgaf3_chatbot ships its own **smina** docking MCP as part of the Docker stack. You do
**not** need to install, configure, or point at it — the installer wires it up
automatically on the internal Docker network. There is no GPU requirement for the
Lite profile.

### AlphaFold3 must be reachable (the one external dependency)

kgaf3_chatbot does **not** bundle AlphaFold3 (weights and databases are licensed and
must stay in your own environment). You must already run an AF3 MCP server and put
its reachable address in `AF3_MCP_URL`. The installer actively verifies this
connection at the end of the install and prints exactly what to fix if it fails.

> If AF3 runs on a **remote** Linux host, or the AF3 MCP binds to `127.0.0.1`
> only, the connection check may fail even though AF3 is running. See
> **`docs/BRIDGE.md`** for how to make AF3 reachable (host binding patch or an
> SSH tunnel such as `ssh -L 8002:127.0.0.1:8002 user@host`).

### GraphRAG knowledge graph (ON by default)

kgaf3_chatbot answers **natural-language questions about a natural-product
knowledge graph** (NPASS 3.0 + Open Targets 25.12, 273,519 nodes /
1,493,463 relationships) in a 🧬 **GraphRAG** chat mode. This is
**enabled by default** — the KG is a first-class part of this package.

By default the installer starts two extra containers from pre-built images
(no build, pulled automatically). Set `GRAPHRAG_ENABLED = false` to opt out
(screening-only) and skip pulling these images:

- `graphrag-neo4j` — Neo4j with the knowledge graph **baked in** (auto-loads on
  first boot; no external database or import step).
- `graphrag-mcp` — an MCP **SSE** server on `:8893` that kgaf3_chatbot connects to at
  `http://graphrag-mcp:8893/sse`. It holds the Neo4j driver and runs the
  `graphrag_query` LLM step, so kgaf3_chatbot itself never touches Neo4j directly.

Requirements & notes:

- Uses your `OPENROUTER_API_KEY` (already required) for the GraphRAG LLM step.
- These images are large; the first `up` will take a while to pull, and Neo4j
  needs ~1 minute to auto-load the graph on first boot.
- Nothing else to configure — the MCP URL and Neo4j password are wired
  automatically on the internal Docker network (default password `kist2026npi`,
  matching the shipped KG image).
- To run it by hand instead of via the installer:
  `docker compose up -d` (GraphRAG services are part of the default stack).

---

## Optional settings

| Key | Default | What it is |
|---|---|---|
| `PROFILE` | `lite` | `lite` is the only functional profile today. `full` is **reserved** (OpenMM-based pipeline, not yet available) — leave it as `lite`. |
| `APP_PORT` | `5013` | Host port for the web UI (`http://localhost:<APP_PORT>`). |
| `LLM_DEFAULT_MODEL` | `anthropic/claude-sonnet-4-6` | Model pre-selected in the UI (changeable in the app). |
| `AF3_MCP_AUTH_TOKEN` | _(empty)_ | Bearer token, only if your AF3 MCP requires auth. |
| `ENABLE_NGINX` | `false` | `true` puts the app behind an nginx reverse proxy with BasicAuth. |
| `BASIC_AUTH_USER` | _(empty)_ | BasicAuth username (only when `ENABLE_NGINX=true`). |
| `BASIC_AUTH_PASS` | _(empty)_ | BasicAuth password (only when `ENABLE_NGINX=true`). |
| `GRAPHRAG_ENABLED` | `true` | GraphRAG stack (Neo4j KG + MCP server on `:8893`) + the 🧬 **GraphRAG** chat mode. **On by default**; set `false` to opt out (screening-only, skips the KG image pull). See the GraphRAG section above. |
| `GRAPHRAG_OPENROUTER_MODEL` | `anthropic/claude-opus-4-7` | Model **label** shown in the UI/history for GraphRAG answers. The MCP server's own `OPENROUTER_MODEL` is what actually runs the query. |

---

## Configuration

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
GRAPHRAG_OPENROUTER_MODEL = anthropic/claude-opus-4-7     # display label only (MCP server's OPENROUTER_MODEL is authoritative)
```

---

## After install

The installer prints a success report with your URL (default
`http://localhost:5013`), the active profile, the LLM, the bundled smina status,
and the external AF3 connection result. Open the URL in a browser and paste a
protein sequence + a SMILES string into the chat to start.
