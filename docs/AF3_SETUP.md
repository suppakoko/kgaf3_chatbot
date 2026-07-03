**English** | [한국어](AF3_SETUP.ko.md)

# AF3 Setup — Connecting AlphaFold3 to the AF3 MCP Bridge

The 🧪 **Virtual Screening** mode of kgaf3_chatbot delegates protein–ligand
cofolding to an **external AlphaFold3 (AF3)**. This repository does **not run
AF3 directly**, and it does **not bundle** AF3 weights or sequence databases.
kgaf3_chatbot merely sends HTTP requests via `AF3_MCP_URL` to an **AF3 MCP
server** that you launch yourself.

```
kgaf3-chat container ──HTTP──► AF3 MCP bridge (:8002/mcp/) ──docker run──► alphafold3 (GPU · weights · sequence DBs)
   (AF3_MCP_URL)               (operated by the external af3_chatbot backend)
```

This document describes the four stages of standing up the pipeline above from
scratch: ① prepare/run AlphaFold3 → ② run the AF3 MCP bridge server → ③ apply
the host-binding patch → ④ set `AF3_MCP_URL` → verify. The structural points
where reachability breaks down and their workarounds are collected separately in
**[`docs/BRIDGE.md`](BRIDGE.md)**, so read it alongside this one.

> [!IMPORTANT]
> **AF3 weights and sequence databases are assets licensed by Google DeepMind
> and are not redistributed in this repository.** Users must obtain the weights
> and DBs themselves **under AF3's own license terms** and run them **on their
> own GPU machines**. kgaf3_chatbot is only responsible for "connecting to an
> AF3 that already has the weights/DBs." The code in this repository is MIT
> (`LICENSE`), but compliance with the licenses of AF3 and its data is entirely
> the user's responsibility.

---

## Prerequisites

- **NVIDIA GPU** + drivers + `nvidia-container-toolkit` (for AF3 inference).
- **AlphaFold3 Docker image** (`alphafold3:latest`, etc.) — built by the user.
- **AF3 model weights** — obtained after license issuance from DeepMind.
- **AF3 sequence databases** — for MSA/template search (hundreds of GB in size).
- Host paths where the above assets live (e.g., weights directory, DB directory,
  input/output working directories).

All of this must reside on the **machine that runs AF3**, which may or may not be
the same machine that runs kgaf3_chatbot (see `docs/BRIDGE.md` for remote
configurations).

---

## Stage 1 — Prepare and run AlphaFold3

The procedures for obtaining weights, DBs, and license issuance all follow the
**official AlphaFold3 repository**. This document does not replace those
procedures.

- Official repository and license: <https://github.com/google-deepmind/alphafold3>
- Model parameters (weights) are received from DeepMind through a separate
  application and approval process. **They are not included in the kgaf3_chatbot
  repository.**
- Following the official guidance, build the `alphafold3` Docker image and
  download and place the weights and sequence DBs.

Once this stage is complete, the user should be in a state where they can run the
`alphafold3` Docker image on a GPU to generate predicted CIFs from input JSON.
The MCP bridge in the next stage drives exactly this Docker image.

---

## Stage 2 — Run the AF3 MCP bridge server

For Virtual Screening to talk to AF3, an **MCP server** that wraps AF3 is
required. This repository ships a **reference implementation** of that bridge in
`af3-bridge/`.

- **`af3-bridge/af3_mcp_http.py`** — HTTP MCP transport. This bundled copy binds
  by reading the `AF3_MCP_HOST` (default `0.0.0.0`) / `AF3_MCP_PORT` (default
  `8002`) environment variables, so the default endpoint is
  `http://<AF3 host>:8002/mcp` (i.e., the host patch is already applied — see
  [`af3-bridge/README.md`](../af3-bridge/README.md) and
  [`docs/BRIDGE.md`](BRIDGE.md)). This is what kgaf3-chat actually connects to.
- **`af3-bridge/af3_mcp_server.py`** — stdio MCP variant (for reference).
- **`af3-bridge/af3-mcp.service`** — example systemd unit that keeps the bridge
  running continuously.
- For detailed explanation, tool list, and how to run it, see
  **[`af3-bridge/README.md`](../af3-bridge/README.md)**.

> [!WARNING]
> **This bridge code cannot run standalone.** It is tightly coupled to the
> external **`af3_chatbot` backend**, importing
> `app.services.af3_service.AF3Service`, `JsonBuilder`, and `BatchDockService`,
> and it performs the actual inference by running the `alphafold3` Docker image
> via `docker run`. The bridge must therefore be run inside (or together with) an
> environment that has the **af3_chatbot app + alphafold3 image + the user's
> weights/DBs**. The files in `af3-bridge/` are provided as the reference bridge
> server + systemd unit to use in that environment.

The bridge exposes the following **6 core tools** over HTTP MCP (`:8002`) (batch
tools are additional):

| Tool | What it does |
|------|--------------|
| `af3_create_job` | Generate AF3 input JSON from a protein sequence + ligand SMILES |
| `af3_run_job` | Run cofolding for the generated job on a GPU |
| `af3_get_status` | Query job status |
| `af3_get_results` | Retrieve results (CIF, etc.) of a completed job |
| `af3_get_input_json` | Retrieve the input JSON used for a job |
| `af3_list_jobs` | List jobs |

(In addition, library batch tools such as `af3_create_batch_job` /
`af3_get_batch_status` / `af3_get_batch_results` /
`af3_get_batch_ligand_result` are exposed alongside. The installer verifies the
batch tools too in the final verification.)

Example of running it continuously with systemd (adjust the paths and
environment variables in the unit to match your environment):

```bash
# Edit af3-bridge/af3-mcp.service to match your environment, then install it
sudo cp af3-bridge/af3-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now af3-mcp
sudo systemctl status af3-mcp        # confirm active (running)
journalctl -u af3-mcp -f             # watch logs in real time
```

---

## Stage 3 — Apply the host-binding patch (B3 reachability issue)

The original `af3_mcp_http.py` in the external **`af3_chatbot` repository**
**hardcodes** the binding to `127.0.0.1:8002` in `main()` (this is what
`patches/af3_mcp_host_env.patch` targets). For reference, the **bundled** copy
`af3-bridge/af3_mcp_http.py` in this repository already has this patch applied
and reads `AF3_MCP_HOST` (default `0.0.0.0`) / `AF3_MCP_PORT` (default `8002`)
(see [`af3-bridge/README.md`](../af3-bridge/README.md) ·
[`docs/BRIDGE.md`](BRIDGE.md)). The problem lies in the original on the
**external af3_chatbot** side that the user actually operates, and its
loopback-only binding can be reached **only by other processes on the same
host**, making it unreachable in the following two situations:

- From **inside the kgaf3-chat container**, the host is seen as
  `host.docker.internal` (or the docker0 bridge IP), so it cannot reach the
  host's loopback.
- From a **remote machine** (e.g., kgaf3_chatbot on one side and AF3 on another
  Linux box), it naturally cannot reach it either.

This is the **B3 reachability issue**. This repository bundles a 1-line patch
that changes the binding to be driven by environment variables.

- **`patches/af3_mcp_host_env.patch`** — makes `main()` read the `AF3_MCP_HOST`
  (default `0.0.0.0`) / `AF3_MCP_PORT` (default `8002`) environment variables.

> Apply this patch to the **external `af3_chatbot` repository** (not
> kgaf3_chatbot). The installer does **not apply this patch automatically.**

```bash
# From the af3_chatbot repository root (if it is a git checkout)
git apply /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
# If it is not a git checkout
patch -p1 < /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
```

After applying the patch, configure the binding (e.g., in the `[Service]` block
of `af3-mcp.service`):

```ini
[Service]
Environment=AF3_MCP_HOST=0.0.0.0      # or the docker0 bridge IP (e.g., 172.17.0.1)
Environment=AF3_MCP_PORT=8002
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart af3-mcp
```

Since `0.0.0.0` opens on all interfaces, you must **narrow the scope to trusted
targets with a firewall**. Alternatives that resolve this without the patch
(host network mode, socat/nginx forwarder, SSH tunnel) and example firewall
configurations are all collected in **[`docs/BRIDGE.md`](BRIDGE.md)**. This
Stage 3 and the B3-related content apply entirely to the **external AF3 MCP** and
are unrelated to the GraphRAG stack (internal compose).

---

## Stage 4 — Set `AF3_MCP_URL` (configure.md)

Once the bridge is reachable, set `AF3_MCP_URL` in the ```ini block of
[`configure.md`](../configure.md) so that kgaf3_chatbot knows its address.

```ini
AF3_MCP_URL     = http://host.docker.internal:8002/mcp/   # AF3 on the same host
AF3_OUTPUT_ROOT = /data/af3_output                        # absolute path to AF3 results (must already exist)
AF3_MCP_AUTH_TOKEN =                                      # only when the AF3 MCP requires authentication
```

The key is whether it points to the correct host from the container's
perspective:

- **AF3 on the same host** → `http://host.docker.internal:8002/mcp/`.
  If `host.docker.internal` does not resolve in Linux compose, map
  `host-gateway` via `extra_hosts` or write the docker0 IP (e.g., `172.17.0.1`)
  directly.
- **Remote AF3 host** → `http://<remote-host-IP>:8002/mcp/`.

`AF3_OUTPUT_ROOT` is the **host absolute path** where AF3 writes its results, and
kgaf3-chat mounts it **read-only** to read the predicted CIFs. The path must
already exist before installation. Only if the bridge is placed behind
authentication should you put a Bearer token in `AF3_MCP_AUTH_TOKEN`. Follow
[`docs/INSTALL.md`](INSTALL.md) for the rest of the installation flow.

---

## Stage 5 — Verification

In its final stage, the installer (`install.sh` / `install.bat`) calls MCP
`initialize` + `tools/list` on `AF3_MCP_URL` to confirm the **connection and the
tool list (including batch tools)**, and on failure it prints exactly what needs
to be fixed. On success, the report shows
`External AF3: <URL> [connected, batch tools verified]`.

To quickly check just whether the endpoint is alive by hand:

```bash
# A 200 response means the endpoint is alive (the MCP handshake is performed by the installer)
curl -i http://host.docker.internal:8002/mcp/
```

If the connection fails, narrow down the cause in the following order (one of
these is almost always the cause):

1. Is the bridge up — `systemctl status af3-mcp` / `journalctl -u af3-mcp -f`.
2. Is the binding loopback-only — **B3**, the Stage 3 patch is needed.
3. Does `AF3_MCP_URL` point to the correct host from the container's perspective
   — Stage 4.

Virtual Screening **absolutely requires** a reachable AF3 MCP. If AF3 is not
connected, the app will still start but will fail every time at the cofolding
step (Stage 3). See **[`docs/BRIDGE.md`](BRIDGE.md)** for detailed diagnostics
and workarounds, and [`docs/USAGE.md`](USAGE.md) for usage.

---

Related documents: [`docs/BRIDGE.md`](BRIDGE.md) (B3 reachability · workarounds),
[`af3-bridge/README.md`](../af3-bridge/README.md) (bridge file details),
[`configure.md`](../configure.md) (`AF3_MCP_URL` · `AF3_OUTPUT_ROOT` ·
`AF3_MCP_AUTH_TOKEN`), [`docs/INSTALL.md`](INSTALL.md) (full installation).
GraphRAG is a completely separate internal stack → [`docs/GRAPHRAG.md`](GRAPHRAG.md).
