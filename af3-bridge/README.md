**English** | [한국어](README.ko.md)

# af3-bridge/ — AF3↔MCP Bridge (reference only, cannot run standalone)

This directory contains a **reference implementation** of the **external AlphaFold3 (AF3)
MCP server** that kgaf3_chatbot's **screening mode connects to**. The kgaf3_chatbot core
(the `kgaf3-chat` service) does **not run AF3 directly**; it merely sends HTTP requests to
this AF3 MCP server pointed to by `AF3_MCP_URL` in `configure.md`.

```
kgaf3-chat container ──HTTP──► AF3 MCP server(:8002/mcp) ──docker run──► alphafold3 (GPU, weights·DBs)
   (AF3_MCP_URL)              (the files here are that reference impl)   (user-owned·licensed)
```

> **Key point: these files do not run on their own.** The code below is a copy carved out
> of a separate **`af3_chatbot` backend repository**, and it is tightly coupled to that
> repository's `app.services.*` modules. The actual structure prediction is performed by
> the **`alphafold3` Docker image + the licensed weights·DBs the user obtains themselves**.
> Therefore this directory cannot be run in isolation. The kgaf3_chatbot distribution
> includes this code only as a **reference showing how it is deployed and run**.

> **AF3 weights·MSA DBs are not distributed (licensing).** The AF3 model weights and
> sequence DBs licensed by Google DeepMind are **not included** in this repository. The
> user must obtain them themselves under the AF3 license terms and operate them on their own
> GPU machine. For the acquisition·installation procedure, see
> [`../docs/AF3_SETUP.md`](../docs/AF3_SETUP.md).

---

## 1. Included files

| File | Role |
|------|------|
| `af3_mcp_http.py` | **HTTP MCP transport server.** The actual endpoint (`:8002/mcp`) that kgaf3-chat connects to. Exposes MCP tools over Streamable HTTP transport. |
| `af3_mcp_server.py` | **stdio MCP transport variant.** A separate entry point exposing a subset of the same tools over standard input/output (e.g. for direct local MCP client connections). Completely independent of the HTTP server. |
| `af3-mcp.service` | A (sample) systemd service unit file that keeps the HTTP server above running continuously. |

These actually correspond to `app/mcp/af3_mcp_http.py` and `app/mcp/af3_mcp_server.py` in
the `af3_chatbot` repository. That is, they must be run **inside the `af3_chatbot` checkout
on the machine that operates AF3**, and the copies here are for content review·comparison.

---

## 2. Coupling to the af3_chatbot backend (why it can't run standalone)

`af3_mcp_http.py` and `af3_mcp_server.py` lazy-import `af3_chatbot`'s service objects on the
first tool call:

- `app.services.af3_service.AF3Service` — handles AF3 job submission·status·result lookup,
  and running the `alphafold3` Docker container itself.
- `app.services.json_builder.JsonBuilder` — generates·validates the AF3 input JSON
  (`version: 2`) from protein sequence + ligand.
- `app.services.batch_dock_service.BatchDockService` — orchestrates batch docking of a
  single protein + N ligands (reusing the MSA for the same sequence).
- `app.services.result_service.ResultService` — collects the confidence summary of completed
  jobs (ipTM, ranking_score, PAE, pLDDT) and the result CIF paths.

If these modules are not on `PYTHONPATH`, the server fails at the tool-execution stage. In
addition, `AF3Service` requires the `alphafold3` Docker image and mounted weights·DB paths at
run time. So these files only work in an environment where all three are in place: **①
af3_chatbot app code + ② alphafold3 image + ③ user weights·DBs**.

---

## 3. HTTP MCP endpoint and tools

`af3_mcp_http.py` binds to `AF3_MCP_HOST`/`AF3_MCP_PORT` (default `0.0.0.0:8002`) and mounts
the MCP transport at the `/mcp` path. So the endpoint is:

```
http://<AF3 host>:8002/mcp
```

kgaf3_chatbot's `AF3_MCP_URL` points here (container default
`http://host.docker.internal:8002/mcp/`).

**6 core tools:**

| Tool | Description |
|------|------|
| `af3_create_job` | Generate AF3 input JSON from protein sequence + ligand (SMILES/CCD). |
| `af3_run_job` | Submit an AF3 job from input JSON (`full`/`data_pipeline_only`/`inference_only`). |
| `af3_get_status` | Query job status. |
| `af3_get_results` | Query the confidence summary of a completed job (ipTM, ranking_score, mean_pae, mean_plddt). |
| `af3_get_input_json` | Return the stored input JSON spec (for validating the `version: 2` contract). |
| `af3_list_jobs` | List jobs. |

**4 batch docking tools** (for the multi-ligand pipeline of screening mode, 1 MSA + N
inferences):

| Tool | Description |
|------|------|
| `af3_create_batch_job` | Create a batch of a single protein + N ligands (automatically reusing the MSA for the same sequence). |
| `af3_get_batch_status` | Query batch progress (`msa_reused`, per-ligand counts). |
| `af3_get_batch_results` | Return per-ligand results for a completed batch in bulk. |
| `af3_get_batch_ligand_result` | Details of a single ligand within a batch (cif_path, summary_confidences). |

> `af3_mcp_server.py` (the stdio variant) exposes only the 5 core tools among these
> (`af3_create_job`, `af3_run_job`, `af3_get_status`, `af3_get_results`, `af3_list_jobs`).
> What kgaf3-chat actually uses is the HTTP server (`af3_mcp_http.py`).

---

## 4. Running via systemd (`af3-mcp.service`)

This unit runs `python -m app.mcp.af3_mcp_http` in a uv virtual environment inside the
`af3_chatbot` checkout on the AF3 host. `WorkingDirectory`, `User`/`Group`, the `AF3_*`
paths, the GPU device, and the Docker image tag are all **sample values**, so you must edit
them to match your environment.

```bash
# Install the unit (after editing paths·env vars)
sudo cp af3-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now af3-mcp

# Management
sudo systemctl status af3-mcp     # status
sudo systemctl restart af3-mcp    # restart
journalctl -u af3-mcp -f          # live logs
```

To run the `alphafold3` container, the unit obtains Docker socket access via
`SupplementaryGroups=docker`. Verify that `AF3_MODELS_DIR`/`AF3_DB_DIR` etc. point to the
user's actual weights·DB locations.

---

## 5. Making it reachable from a container·remote (host patch)

The original `af3_chatbot`'s `main()` hardcodes the binding to `127.0.0.1:8002` (loopback
only). In that case it cannot be reached from the kgaf3-chat **container** (which sees the
host as `host.docker.internal`/docker0) or from a **remote machine** (the reachability
problem "B3").

To solve this, apply [`../patches/af3_mcp_host_env.patch`](../patches/af3_mcp_host_env.patch)
to the **external `af3_chatbot` repository** to switch the binding to the `AF3_MCP_HOST`/
`AF3_MCP_PORT` environment variables. (The `af3_mcp_http.py` copy included here already
reflects this patch.)

```bash
# From the af3_chatbot repository root
git apply /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
# If not a git checkout:
patch -p1 < /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
```

After applying the patch, specify the binding in `af3-mcp.service`:

```ini
[Service]
Environment=AF3_MCP_HOST=0.0.0.0      # or the docker0 bridge IP (e.g. 172.17.0.1)
Environment=AF3_MCP_PORT=8002
```

`0.0.0.0` opens on all interfaces, so use a firewall to allow only trusted targets (docker0/
specific remote IPs). Ways to solve it without the patch (host network, socat forwarder, SSH
tunnel), along with firewall configuration and the verification procedure, are documented in
[`../docs/BRIDGE.md`](../docs/BRIDGE.md).

---

## 6. Summary of user responsibilities

- **Acquiring·installing·operating AF3 weights·sequence DBs**: the user's responsibility.
  This repository does not redistribute them.
- **Building the `alphafold3` Docker image·preparing the GPU**: the user's responsibility.
- **The `af3_chatbot` backend runtime environment** (the `app.services.*` modules above):
  the user's responsibility.
- kgaf3_chatbot only handles "connecting to an already-running AF3 MCP via `AF3_MCP_URL`".

Full AF3 preparation·operation·verification flow: [`../docs/AF3_SETUP.md`](../docs/AF3_SETUP.md) ·
bridge reachability problems and remedies: [`../docs/BRIDGE.md`](../docs/BRIDGE.md).
