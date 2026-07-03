# USAGE — kgaf3_chatbot User Guide

**English** | [한국어](USAGE.ko.md)

kgaf3_chatbot packs **two modes into a single web UI**:

- 🧬 **GraphRAG** — a mode for **natural-language queries** against a natural-product
  knowledge graph (NPASS 3.0 + Open Targets 25.12,
  **273,519 nodes / 1,493,463 relationships**).
- 🧪 **Virtual Screening (AlphaFold)** — a mode that **ranks candidates** via
  protein–ligand **cofolding** (external AF3) plus bundled `smina --minimize`
  rescoring.

For installation see `docs/INSTALL.md`, for deploying and validating the GraphRAG
stack itself see `docs/GRAPHRAG.md`, and for preparing external AF3 see
`docs/AF3_SETUP.md` (and `docs/BRIDGE.md`). This document covers **how to use it in
the browser** once installation is complete.

---

## 0. Access and Screen Layout

Once installation succeeds, open the web UI in your browser (the port is `APP_PORT`
in `configure.md`, default `5013`).

```text
http://localhost:5013
```

The header at the top of the screen has a **mode toggle** consisting of two tab
buttons:

- **🧪 AlphaFold** — virtual screening mode. **Selected by default** (active on first
  app entry).
- **🧬 GraphRAG** — knowledge-graph query mode.

The mode selection is saved in the browser's `localStorage`, so on your next visit
the mode you last used opens directly. On the right side of the header there is an
**LLM model selection dropdown** (default `anthropic/claude-sonnet-4-6`,
`LLM_DEFAULT_MODEL` in `configure.md`) and a connection status indicator.

At the very bottom of the screen the **system bar** has status chips for each
backend: `AF3` (external AF3 MCP), `Smina` (bundled docking MCP), `GraphRAG`
(Neo4j/MCP), `LLM`, `jobs` (number of in-progress jobs), and GPU utilization.
Before using a mode, it's a good idea to first check these chips to confirm the
required backend is connected.

> Depending on the mode, the input-box placeholder and the welcome panel's examples
> change. Use **+ New Conversation** in the left sidebar to start a new
> conversation, and reopen a previous session from the recent-conversation list.

---

## 1. 🧬 GraphRAG Mode — Natural-Language Knowledge-Graph Queries

### 1-1. What Can You Ask

GraphRAG answers against a natural-product–target–disease knowledge graph via
**Text2Cypher**. When you type a question in natural language, the backend
**converts that question into a Cypher query → runs it on Neo4j → synthesizes the
results into a Markdown answer**.

> **English is recommended for disease and protein names.** Because the knowledge
> graph's node labels and identifiers are in English (disease names, UniProt/Ensembl
> IDs, gene symbols, etc.), English queries have higher matching accuracy. Korean
> queries also work.

You can start with the example questions right from the welcome panel:

```text
• Tell me the targets for glaucoma treatment
• Return the top 20 natural products active against ROCK1 with IC50 < 100 nM
• Compare ROCK1 and ROCK2 head-to-head: list UniProt/Ensembl IDs,
  associated glaucoma subtypes, NP counts, top compounds, and approved drugs.
• 알츠하이머 관련 단백질 타겟에 활성이 있는 천연물 상위 10개를 보여줘
```

A short example also appears in the input-box placeholder (e.g.,
`ROCK1 에 활성 있는 천연물 상위 20개`). Enter sends, Shift+Enter inserts a line break.

### 1-2. Progress Card — Four Steps

When you send a question, a **"GraphRAG Progress"** card appears in the answer slot,
and the four steps below fill in one after another:

| Step key | Card label | Meaning |
|---|---|---|
| `cypher_gen` | 🧠 Cypher generation | Convert the natural-language question into a Cypher query |
| `neo4j_exec` | 🔍 Neo4j execution | Run the generated Cypher on the knowledge graph |
| `answer_synth` | 📝 Answer synthesis | Organize the result rows into a Markdown answer |
| `complete` | (summary in card header) | Finalize the total elapsed time and status summary |

The card also shows the **actual Cypher query that was executed**, the **number of
rows returned (row count)**, and token usage. The final **answer is rendered as
Markdown** (tables, lists, bold text, etc.).

> Internally, these four steps are filled in after the fact from the metadata
> returned by a **single call** to the MCP `graphrag_query` (the shape the frontend
> shows stays the same). For details on the internal behavior, see the "One-shot
> query and progress card" section of `docs/GRAPHRAG.md`.

### 1-3. The Three Available MCP Tools

The GraphRAG MCP server (`http://graphrag-mcp:8893/sse`, also exposed on the host
loopback `127.0.0.1:8893`) exposes exactly three tools. The 🧬 GraphRAG queries in
the chat UI use the first of these.

- **`graphrag_query`** — handles natural language → Cypher → execution → Markdown
  answer **in one shot**. Returns `answer`, `cypher`, `row_count`, `token_usage`,
  etc. Since it runs an LLM step, it **requires `OPENROUTER_API_KEY`** (the same key
  as chat).
- **`get_kg_stats`** — returns node/relationship counts. **No LLM key required.**
- **`run_cypher`** — read-only direct Cypher execution. **No LLM key required.**

Other local MCP clients (e.g., Claude Desktop) can also connect to the same server
at `127.0.0.1:8893` and use these tools.

### 1-4. GraphRAG Worked Example

1. Click the **🧬 GraphRAG** tab in the header.
2. Paste the following into the input box and press Enter:

   ```text
   Return the top 20 natural products active against ROCK1 with IC50 < 100 nM
   ```

3. The progress card fills in in order: `🧠 Cypher generation` → `🔍 Neo4j
   execution` → `📝 Answer synthesis` → complete. The card shows the generated
   Cypher (e.g., `MATCH (np:NaturalProduct)
   -[a:HAS_ACTIVITY]->(t:Target {symbol:'ROCK1'}) WHERE a.ic50 < 100 ...`) and the
   number of rows returned.
4. In the answer slot, the natural-product list (name, IC50, etc.) is presented as a
   **Markdown table**.

> **When there is no key:** if `OPENROUTER_API_KEY` is empty, `graphrag_query`
> (natural-language queries) fails. However, you can still check whether the KG is
> alive without a key using `get_kg_stats` / `run_cypher` (see `docs/GRAPHRAG.md`
> for how). If the card shows an error related to
> `service.graphrag.mcp_unreachable`, first check whether the GraphRAG stack is up.

---

## 2. 🧪 Virtual Screening Mode — Cofolding + Rescoring

### 2-1. Pipeline Overview

The screening mode **predicts and ranks protein–ligand complexes**:

```text
Input (protein FASTA + ligand SMILES) or a library file
      │
      ▼  AF3 cofolding  (external AlphaFold3 MCP — you operate it yourself)
      ▼  smina --minimize  rescoring  (bundled smina-mcp, no configuration)
      ▼
Ranked candidate table  +  Mol* interactive 3D view  +  complex CIF (served via endpoint)
```

> **Screening mode absolutely requires a reachable external AF3 MCP.** If AF3 is not
> connected, the pipeline **fails at the cofolding step**. The AF3 model weights and
> sequence DBs are **not redistributed** in this repository (Google DeepMind
> license); you must obtain AF3 and the weights yourself and run them on your own GPU
> machine. For preparation and connection instructions, see `docs/AF3_SETUP.md` and
> `docs/BRIDGE.md`. Check the connection status first using the `AF3` chip in the
> system bar at the bottom of the screen. The bundled `smina` needs no separate
> configuration (auto-connected on the internal Docker network).

### 2-2. Input Method 1 — Paste Directly into Chat

Paste a protein **FASTA sequence** together with **one or more ligand SMILES** (one
per line, optionally followed by a name), and add a **docking request phrase**
(e.g., "dock and rank them") before sending. The example format from the welcome
panel remains valid as-is:

```text
>ROCK1_HUMAN_1-415
MSTGDSFETRFEKMDNLLRDPKSEVNSDCLLDGLDALVYDLDFPALRKNKNIDNFLSRYK
... (full 415aa sequence) ...

CN(C)CCOC1=C(C=CC(=C1)C2=CNN=C2)NC(=O)[C@H]3CC4=C(C=CC(=C4)OC)OC3 Chroman_1
C[C@H]1CNCCCN1S(=O)(=O)C2=CC=CC3=C2C(=CN=C3)C H1152
C[C@H](C1CCC(CC1)C(=O)NC2=CC=NC=C2)N Y-27632

위 단백질에 화합물 3개를 docking해서 순위를 매겨주세요
```

- Each SMILES line has the form `SMILES [name]`. The name is optional; if included,
  it is used as-is in the `Ligand` column of the results table.
- To start docking, you need a request phrase containing a **docking keyword** (e.g.,
  "docking", "도킹", "순위"). If you paste only the protein/ligands, the chatbot
  treats it as ordinary conversation.

### 2-3. Input Method 2 — Upload a Library File

To screen many compounds at once, upload a file with the **library upload** button
in the input box. Allowed extensions are **`.sdf`, `.smi`, `.csv`, `.txt`**. After
uploading, the filename and parsed compound information appear in the preview area.
Then send the protein FASTA together with a docking request phrase to make the entire
library the screening target.

### 2-4. Progress and Results

After sending, a **Job Progress** panel appears, and once cofolding→rescoring
finishes, the results card fills in. The results card includes the following.

**① Ranked candidate table.** The columns are as follows (units and direction noted
in the headers):

| Column | Unit/Direction | Meaning |
|---|---|---|
| `#` | — | Rank under the current sort criterion |
| `Ligand` | — | Ligand name/ID |
| `ipTM` | 0–1 ↑ | AF3 interface confidence (higher is better) |
| `PAE` | Å ↓ | Predicted aligned error (lower is better) |
| `Smina` | kcal/mol ↓ | `smina --minimize` rescore (lower means stronger binding) |
| `Score` | composite ↑ | Composite score (higher is better) |

The table is **sortable**. From the results card's **sort dropdown**, choose `Smina`
/ `Composite` / `AF3 Score` (ranking_score) / `ipTM`, or click a sortable numeric
column header (`ipTM` / `Smina` / `Score`) to sort (the `PAE` and `#` headers are not
sort buttons). The backend results API accepts these sort criteria:
**`smina` / `composite` / `iptm` / `ranking_score` / `e_inter`** (interaction
energy). Limit the number displayed with the `Top N` input (max 100), and use the
**Refresh**, **CSV**, and **TSV** buttons to download the table under the current
sort and top-N criteria.

**② Interactive Mol* 3D view.** When you open a candidate, a **Mol***-based 3D
complex viewer appears as an overlay. Rotate and zoom the protein–ligand complex,
and save the current screen as an image with the **Screenshot** button in the
toolbar.

**③ Complex CIF.** Each candidate's predicted complex structure is served as
**CIF**. There is no dedicated download button, but you can fetch the same structure
that the Mol* viewer loads directly from the `/api/results/{job}/{ligand}/cif`
endpoint and open it in an external tool (PyMOL, etc.).

**④ Distribution charts.** Open the distribution panel with the **Chart** button in
the results card to see the **Smina distribution** and an **ipTM vs Smina** scatter
plot.

### 2-5. Screening Worked Example

1. Confirm that the **🧪 AlphaFold** tab is selected in the header (the default).
2. Confirm that the `AF3` chip in the system bar is connected. If it shows `-` or is
   disconnected, first connect the AF3 MCP via `docs/AF3_SETUP.md`.
3. Paste the ROCK1 FASTA + 3 SMILES (Chroman_1, H1152, Y-27632) from 2-2 above +
   "docking해서 순위를 매겨주세요" and press Enter.
4. In the progress panel, AF3 cofolding → smina rescoring proceeds (large jobs take
   time).
5. In the results card, check the top candidates by sorting on **Smina**, then open a
   candidate of interest to examine the binding pose in the **Mol* 3D view**. If
   needed, fetch the structure via the CIF endpoint above. Save the full table as
   **CSV/TSV**.

---

## 3. Common Sticking Points

- **Screening fails at cofolding** — this happens when the external AF3 MCP is
  unreachable. It's common for the host to be remote or for the AF3 MCP to be bound
  only to `127.0.0.1`. For fixes such as host-binding patches and SSH tunnels, see
  `docs/AF3_SETUP.md` / `docs/BRIDGE.md`; for the connection value `AF3_MCP_URL`,
  see `configure.md`.
- **GraphRAG query fails** — a missing `OPENROUTER_API_KEY` (natural-language queries
  need an LLM), or the GraphRAG stack not being up (`service.graphrag.mcp_unreachable`).
  For starting and validating the stack, see `docs/GRAPHRAG.md`. Even if GraphRAG is
  off, the app and the screening mode work normally.
- **The mode toggle on screen stays put with no response** — refresh the browser,
  then press the header tab (🧪/🧬) again. The selection is saved in `localStorage`.

---

## Related Documents

- [docs/INSTALL.md](INSTALL.md) — installation, health checks, nginx, troubleshooting.
- [docs/GRAPHRAG.md](GRAPHRAG.md) — GraphRAG stack deployment, validation, troubleshooting (ON by default).
- [docs/AF3_SETUP.md](AF3_SETUP.md) — external AF3 license, weights, MCP bridge preparation.
- [docs/BRIDGE.md](BRIDGE.md) — external AF3 MCP reachability (host binding/tunnel).
- [configure.md](../configure.md) — all configuration keys and default values.
