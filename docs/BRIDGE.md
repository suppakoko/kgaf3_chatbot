**English** | [한국어](BRIDGE.ko.md)

# AF3 Bridge — Reaching an External AF3 MCP (B3)

> **Scope of this document — connecting the bridge to an external AF3 MCP.**
> This document covers only the network problem (B3) and its remedies for how the
> kgaf3_chatbot stack reaches an **AF3 MCP server that the user operates externally
> on their own**. For obtaining the AF3 license, weights, and databases and for the
> procedure of standing up an AF3 MCP server for the first time, see
> `docs/AF3_SETUP.md`; for the bridge server code bundled in this repository (for
> reference), see `af3-bridge/` (described in `af3-bridge/README.md`).

kgaf3_chatbot does **not run AlphaFold3 directly.** It merely sends HTTP requests to
an **AF3 MCP server** that runs separately, externally. This document lays out the
single structural point (B3) where that connection can be blocked, and how to unblock
it when it is.

```
kgaf3-chat container ──HTTP──► AF3 MCP server (:8002/mcp/) ──docker run──► alphafold3 (GPU, weights·DBs)
   (AF3_MCP_URL)               (operated by the external af3_chatbot repo)
```

The `AF3_MCP_URL` in `configure.md` points at the middle box (the AF3 MCP server).
If only the rightmost AF3 docker is installed and the middle MCP server is
unreachable, kgaf3_chatbot will still start up but will fail every time at the
cofolding stage.

> **Note — GraphRAG does not apply here.** GraphRAG is a stack that runs by default
> with `docker compose up -d` and runs **entirely inside compose**
> (the neo4j + graphrag-mcp containers). The host loopback binding problem (B3) and
> the AF3 bridge remedies in this document do not apply to GraphRAG. For deployment
> and usage, see `docs/GRAPHRAG.md`.

> **AF3 weights and MSA DBs are never a distribution target (license).** They remain
> entirely in the user's environment. kgaf3_chatbot is responsible only for
> "connecting to an AF3 that already has the weights/DBs."

---

## 1. B3 — What Gets Blocked

The `main()` in the external `af3_chatbot/app/mcp/af3_mcp_http.py` **hardcodes** the
binding as follows:

```python
uvicorn.run(asgi_app, host="127.0.0.1", port=8002, log_level="info")
```

Because it binds to `127.0.0.1` (loopback), **only other processes on the same host**
can reach it:

- From **inside the kgaf3-chat container**, the host is seen not via the host
  loopback but via `host.docker.internal` (or the docker0 bridge IP) → unreachable.
- On a **remote machine** (e.g., kgaf3_chatbot on Windows, AF3 on a separate Linux
  box), it is naturally unreachable as well.

At the end of installation the installer performs an active connection check against
`AF3_MCP_URL`, explicitly diagnosing this reachability failure and guiding you to the
remedies below.

---

## 2. Recommended Remedy — a One-Line Patch (When You Manage the External AF3 Yourself)

If you operate the AF3 MCP server yourself, this is the cleanest option. It changes
the binding to an environment variable.

### 2-1. Apply the Patch

Apply this repository's `patches/af3_mcp_host_env.patch` to the **external
af3_chatbot repository**.

```bash
# From the root of the af3_chatbot repository
git apply /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
# If it is not a git checkout:
patch -p1 < /path/to/kgaf3_chatbot/patches/af3_mcp_host_env.patch
```

The patch changes `main()` to the following (and adds `import os`):

```python
host = os.getenv("AF3_MCP_HOST", "0.0.0.0")
port = int(os.getenv("AF3_MCP_PORT", "8002"))
uvicorn.run(asgi_app, host=host, port=port, log_level="info")
```

### 2-2. Configure the Binding (af3-mcp.service)

Add the environment variables to the `af3-mcp` systemd unit.

```ini
[Service]
Environment=AF3_MCP_HOST=0.0.0.0      # or the docker0 bridge IP (e.g., 172.17.0.1)
Environment=AF3_MCP_PORT=8002
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart af3-mcp
```

### 2-3. Open the Port Only to Trusted Targets

`0.0.0.0` opens on all interfaces. **Narrow the scope with a firewall.** An example
that allows only the docker0 bridge (for containers):

```bash
# Put the docker0 interface into the trusted zone (allow container → host reach)
sudo firewall-cmd --permanent --zone=trusted --add-interface=docker0
sudo firewall-cmd --reload
```

To open only for a specific remote IP, use `--zone=trusted --add-source=<remoteIP>/32`.
If exposing `0.0.0.0` feels risky, pinning `AF3_MCP_HOST` to the docker0 IP is safer.

### 2-4. Match `AF3_MCP_URL` (configure.md)

- kgaf3-chat container → AF3 on the same host: `AF3_MCP_URL = http://host.docker.internal:8002/mcp/`
  (If `host.docker.internal` does not resolve under Linux compose, map
  `host-gateway` via `extra_hosts` or write the docker0 IP directly.)
- Remote AF3 host: `AF3_MCP_URL = http://<remote-host-IP>:8002/mcp/`

---

## 3. Solving It Without the Patch (When You Cannot Touch the External Code)

### 3-A. Host Network Mode (Single Linux User, Last Resort)

Attaching the kgaf3-chat container to the host network reaches `127.0.0.1:8002`
directly. A compose override example:

```yaml
# docker-compose.override.yml
services:
  kgaf3-chat:
    network_mode: host
```

In this case, `AF3_MCP_URL = http://127.0.0.1:8002/mcp/`. Downsides: port isolation
is lost, and you lose the internal-network isolation benefit of the bundled
smina-mcp. Use it only when nothing else works.

### 3-B. Port Forwarder (socat / nginx)

Relay on the host from docker0 (or an external interface) → loopback.

```bash
# Forward traffic arriving at docker0(172.17.0.1):8002 to 127.0.0.1:8002
socat TCP-LISTEN:8002,bind=172.17.0.1,fork,reuseaddr TCP:127.0.0.1:8002
```

Then `AF3_MCP_URL = http://172.17.0.1:8002/mcp/`. The same relay is possible with an
nginx `stream{}` block. For production use, wrap socat in a systemd unit.

### 3-C. SSH Tunnel (Remote AF3)

When AF3 is up only on a remote Linux host at `127.0.0.1:8002`, open a tunnel from the
machine where kgaf3_chatbot runs:

```bash
ssh -N -L 8002:127.0.0.1:8002 user@af3host
```

This connects the local `127.0.0.1:8002` to the remote AF3 MCP. To reach this local
port from the kgaf3-chat container, either combine it with host network mode (3-A), or
open the tunnel binding as `-L 0.0.0.0:8002:127.0.0.1:8002` and set `AF3_MCP_URL` to
the docker0 IP. (Recommended for a setup with kgaf3_chatbot on Windows and AF3 on a
remote Linux.)

---

## 4. Verification

At the end, the installer calls MCP `initialize` + `tools/list` against `AF3_MCP_URL`
to confirm the connection and the tool list. For a quick manual check:

```bash
# A 200 response means the endpoint is alive (the MCP handshake is done by the installer)
curl -i http://host.docker.internal:8002/mcp/
```

If the connection fails, check in the order of sections 1–3 above: ① is af3-mcp up
(`systemctl status af3-mcp`) → ② is the binding loopback-only (B3) → ③ does
`AF3_MCP_URL` point to the correct host from the container's perspective. One of these
is always the cause.

Related settings are `AF3_MCP_URL`, `AF3_MCP_AUTH_TOKEN`, and `AF3_OUTPUT_ROOT` in
`configure.md`.
