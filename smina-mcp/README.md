# smina-mcp

A small, self-contained **smina-only MCP server** used by **afmm-chat** for the
docking-rescoring / minimization step of its virtual-screening pipeline.

It speaks the **Model Context Protocol** over **streamable-HTTP** and runs the
[`smina`](https://sourceforge.net/projects/smina/) docking engine as a CPU
subprocess. **No OpenMM, no conda, no RDKit, no GPU.**

## Why smina-only (no OpenMM)

The full OpenMM rescoring engine pulls in a large compchem stack (OpenMM,
openff, pdbfixer, conda). For a portable public distribution that only needs
binding-affinity rescoring, `smina --score_only` / `smina --minimize` on the
AF3 holo pose is sufficient and dramatically lighter:

- single small statically-linked binary (~10 MB), no native runtime deps;
- pure-CPU, no CUDA;
- the Python image is plain `python:3.12-slim` + `uv` (no conda).

RDKit is intentionally **not** bundled here: afmm-chat splits the AF3 complex
into `receptor.pdb` / `ligand.sdf` itself and passes **file paths** to this
server.

## Tools exposed

All tool names, argument keys, and return keys are **identical** to the
original `openMM_bot` smina tools, so afmm-chat calls this server with zero
code change.

| Tool | Arguments | Returns (key fields) |
|------|-----------|----------------------|
| `smina_score_only` | `receptor_pdb: str`, `ligand_pdb_or_sdf: str`, `timeout_sec: int = 120` | `smina_affinity_kcal_mol`, `intramolecular_energy_kcal_mol`, `scoring_function` (`"vinardo"`), `smina_version`, `stdout_excerpt` |
| `smina_minimize` | `receptor_pdb: str`, `ligand_pdb_or_sdf: str`, `out_path: str \| None = None`, `scoring: str = "vinardo"`, `minimize_iters: int = 0`, `timeout_sec: int = 120` | `minimized_affinity_kcal_mol`, `minimized_pose_path`, `intramolecular_energy_kcal_mol`, `scoring_function`, `smina_version`, `stdout_excerpt` |
| `smina_score_batch` | `receptor_pdb: str`, `ligand_files: list[str]`, `timeout_sec_per_ligand: int = 60` | `n_total`, `n_success`, `n_failed`, `results[]` |

`receptor_pdb` and `ligand_pdb_or_sdf` are **file paths** on the shared work
volume (see below). `smina_minimize` writes the minimized pose to
`out_path` (default: `<ligand>_minimized.sdf` next to the input ligand).

More negative affinity = stronger binding.

## Network / runtime contract

| Property | Value |
|----------|-------|
| Transport | streamable-HTTP (FastMCP `http`) |
| Bind | `0.0.0.0:8001` |
| MCP endpoint | `/mcp/` |
| Health endpoint | `GET /health` → `OK` |
| Compose service name | `smina-mcp` (reach it at `http://smina-mcp:8001/mcp/`) |
| Host port | **none** — internal compose network only |
| Shared volume | `afmm_work` mounted at `/data/work` (receptor/ligand in, minimized pose out) |
| smina binary | `$SMINA_BIN`, default `/usr/local/bin/smina` |

### Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `SMINA_BIN` | `/usr/local/bin/smina` | smina binary path |
| `SMINA_MCP_HOST` | `0.0.0.0` | bind host |
| `SMINA_MCP_PORT` | `8001` | bind port |
| `SMINA_MCP_PATH` | `/mcp/` | MCP HTTP path |
| `SMINA_DEFAULT_TIMEOUT_SEC` | `120` | default per-call smina timeout |

## smina binary provenance

| | |
|---|---|
| Source | SourceForge project **smina**, file `smina.static` |
| Version | `Smina Oct 15 2019. Based on AutoDock Vina 1.1.2.` |
| Released | 2019-10-15 |
| Size | 9,853,920 bytes |
| Format | x86-64, statically-linked ELF (no dynamic runtime libs) |
| URL | `https://downloads.sourceforge.net/project/smina/smina.static` |
| SHA256 | `ffe5e1e78c947f76b0df8805e2c54383d0bbaf2e827a633b643a708cf682a958` |

The Dockerfile verifies the SHA256 at build time. If SourceForge is
unreachable, build smina from source
(<https://github.com/mwojcikowski/smina>) and install it to
`/usr/local/bin/smina`.

## Build & run

```bash
# build
docker build -f Dockerfile.smina -t smina-mcp:latest .

# run standalone (the shared volume is normally provided by docker-compose)
docker run --rm \
  -v afmm_work:/data/work \
  --name smina-mcp \
  smina-mcp:latest
```

The server listens on `8001` inside the container only; afmm-chat reaches it
over the internal compose network at `http://smina-mcp:8001/mcp/`.

## Local development

```bash
uv sync
uv run python server.py   # serves on http://0.0.0.0:8001/mcp/
```

You need a local `smina` on `$PATH` (or set `SMINA_BIN`).
