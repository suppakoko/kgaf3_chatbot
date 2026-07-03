**English** | [한국어](sharing_chtbot.ko.md)

# kgaf3_chatbot Publishing Guide (sharing_chtbot.md)

An administrator's step-by-step guide to releasing kgaf3_chatbot as a
**public GitHub repository + prebuilt ghcr.io images + a Zenodo DOI**.
Follow it top to bottom exactly as written. Every command is copy-paste ready.

- Repository: <https://github.com/suppakoko/kgaf3_chatbot> (Public, owner `suppakoko`)
- Main compose service/image: **`kgaf3-chat`** (FastAPI web UI/API, `127.0.0.1:5013`)

> Intended audience: the person **publishing this repository to the world**. Ordinary
> users (who only install it) just need
> [README.md](README.md), [configure.md](configure.md), and [docs/INSTALL.md](docs/INSTALL.md).

---

## 0. Overview — What is published, and what is not

kgaf3_chatbot is a local-first web chatbot with **two modes**.

1. 🧬 **GraphRAG** — natural-language Q&A over a natural-product knowledge graph
   (NPASS 3.0 + Open Targets 25.12). **ON by default.**
2. 🧪 **Virtual screening** — protein–ligand cofolding via an external AlphaFold3 MCP +
   bundled `smina --minimize` rescoring, with results in the web UI.

**What is published**

- The kgaf3_chatbot application source (`app/`, `static/`, `templates/`, `run.py`,
  `pyproject.toml`, `uv.lock`) — internal module names remain `app`/`afmm_*` (they are
  internal identifiers, so they are left as-is).
- Container definitions: root `Dockerfile`, `smina-mcp/Dockerfile.smina`, `docker-compose*.yml`
- Bundled docking engine: `smina-mcp/` (server + Dockerfile)
- One-click installer: `install.sh` / `install.bat`, declarative config `configure.md`
- Example env file: `.env.docker.example` (all values are placeholders)
- **GraphRAG stack definitions (ON by default)**: the `neo4j` and `graphrag-mcp`
  services in `docker-compose.yml`. These two images are not built by us — they are
  **pulled from Docker Hub**: `yoonjuho94/graphrag-neo4j:1.0`,
  `yoonjuho94/graphrag-mcp-server:1.1` (third-party images). The knowledge graph
  (273,519 nodes / 1,493,463 relationships) is baked into the image, so the KG source
  is not included in this repository.
- Documentation: `README.md` / `README.ko.md`, `docs/INSTALL.md`, `docs/USAGE.md`,
  `docs/AF3_SETUP.md`, `docs/BRIDGE.md`, `docs/GRAPHRAG.md`, `af3-bridge/README.md`, this file
- License/citation/CI: `LICENSE`, `CITATION.cff`, `.github/workflows/docker-publish.yml`
- AF3 bridge reference assets: `af3-bridge/` (bridge server + systemd unit),
  and the external AF3 patch proposal `patches/af3_mcp_host_env.patch`

**What is never published**

- **AF3 model weights / MSA and sequence DBs** — not redistributable under the Google
  DeepMind license. Users obtain AF3 and the weights/DBs **themselves** under the AF3
  license and run them on their own GPU machine. This repository only connects to the
  AF3 MCP the user has launched, via `AF3_MCP_URL`.
- **KG source datasets (NPASS, Open Targets)** — each follows its own license, and no
  source is included in this repository (they are distributed only baked into the image).
- **Secrets** — a real `OPENROUTER_API_KEY` (`sk-or-...`), AF3 tokens, BasicAuth
  passwords, the Neo4j password.
- **Real env files** — `.env`, `.env.docker` (only the `*.example` samples are committed).
- **Runtime data** — `data/`, `*.db` (SQLite job history), prediction/docking outputs
  (`*.cif`, `*.pdb`).
- **Personally identifiable information (PII)** — internal paths, usernames, real names,
  internal IPs. (Swept in Section 1.)

`.gitignore` enforces this boundary as a first line of defense, but **you must run the
sanitization checks in Section 1 manually before publishing.**

---

## 1. Pre-release sanitization checklist (publishing gate)

Right before publishing, pass **all** of the following from the repository root. If any
of them returns a match, stop and fix it before publishing.

```bash
# Run from the repo root. (No output = pass.)

# 1) Combined scan for PII / internal hosts / real names / key patterns
grep -rnE "yjko|Dr\.?\s*Yoon|Yoon|161\.122|sk-or-[A-Za-z0-9]{20}|/home/[a-z]+" . \
  --exclude-dir=.git

# 2) Private/internal IP ranges
grep -rnE "\b(10|192\.168)\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+" . \
  --exclude-dir=.git

# 3) Verify no real env file is tracked
git ls-files | grep -E "(^|/)\.env($|\.docker$|\.[^e].*)" | grep -v "\.example$"

# 4) Verify no DB / data / weights / structure outputs are tracked
git ls-files | grep -nE "\.(db|sqlite3?|cif|pdb|pdbqt|bin|npz)$|(^|/)(data|weights|models|af3_output)/"
```

**Expected responses**

- (1)/(2) match → replace the offending line with a placeholder. Use `/data/...` for
  paths, `host.docker.internal` or `<af3-host>` for hosts, `sk-or-REPLACE_ME` for keys.
  Design documents with internal IPs baked in (`*_plan.md`, `research.md`, etc.) **must
  not be included in the public repository.**
- (3) match → `git rm --cached <file>`, then check `.gitignore`. If it has already
  landed in commit history, remove it from history with `git filter-repo`, or start over
  with a clean new repository.
- (4) match → likewise untrack it. Never commit weights/DBs/structure outputs.

Final check that `.env` is not committed:

```bash
git ls-files --error-unmatch .env 2>/dev/null && echo "DANGER: .env is tracked!" || echo "OK: .env untracked"
```

> If a secret ever entered a **past commit**, deleting the current file still leaves it
> in history. The safest route is to **start with fresh git history**:
> `rm -rf .git && git init && git add -A && git commit -m "Initial public release"`.

---

## 2. GitHub repository

- **Name:** `kgaf3_chatbot` (final)
- **URL:** <https://github.com/suppakoko/kgaf3_chatbot>
- **Visibility:** Public (start public for DOI issuance and public ghcr packages)
- **Description:**
  > Local-first chatbot for natural-product knowledge-graph Q&A (GraphRAG) + AF3 (external) protein–ligand cofolding with bundled smina rescoring. One-click Docker install.
- **Topics:** `graphrag`, `knowledge-graph`, `natural-products`, `neo4j`,
  `alphafold3`, `virtual-screening`, `protein-ligand`, `cofolding`, `smina`,
  `docking`, `mcp`, `chatbot`, `drug-discovery`, `docker`, `fastapi`
- **Do not include:** skip the default `LICENSE`/`.gitignore` that GitHub creates
  (this repository already has them).

```bash
# Example with the gh CLI (after passing the Section 1 sweep). If a remote already exists, just push.
gh repo create suppakoko/kgaf3_chatbot --public \
  --description "Local-first chatbot: natural-product GraphRAG Q&A + AF3 (external) cofolding with bundled smina rescoring." \
  --source . --remote origin
git push -u origin main
```

Set topics via the web UI or:

```bash
gh repo edit --add-topic graphrag,knowledge-graph,natural-products,neo4j,alphafold3,virtual-screening,protein-ligand,cofolding,smina,docking,mcp,chatbot,drug-discovery,docker,fastapi
```

---

## 3. What to commit and what to ignore

Rely on `.gitignore`, but eyeball what is tracked before the first commit:

```bash
git add -A
git status            # review the list of files to be tracked
git ls-files | sort   # see the full set of files to be committed
```

**Commit (O):** `app/` source, `static/`, `templates/`, `run.py`, `pyproject.toml`,
`uv.lock`, `Dockerfile`, `docker-compose*.yml`, `install.sh`/`install.bat`,
`configure.md`, all of `smina-mcp/`, all of `af3-bridge/`, `patches/`,
`.env.docker.example`, `README*.md`, `docs/`, `LICENSE`, `CITATION.cff`,
`.github/`, `sharing_chtbot.md`.

`docker-compose.yml` also contains the GraphRAG service definitions (`neo4j`,
`graphrag-mcp`), but the images themselves (`yoonjuho94/graphrag-neo4j:1.0`,
`yoonjuho94/graphrag-mcp-server:1.1`) are **third-party images** pulled from Docker Hub,
so they are not committed to this repository. `af3-bridge/` is only **reference source**
for the external AF3 MCP bridge and contains no AF3 weights/DBs.

**Ignore (X):** `.env`, `.env.docker`, `data/`, `*.db`, `weights/`, `models/`,
`*.cif`/`*.pdb` outputs, `.venv/`, `__pycache__/`, `.pytest_cache/`, OS leftover files.
This list is enforced by `.gitignore`.

---

## 4. License choice

**Recommended: MIT** (already included as `LICENSE`).

- **Rationale:** the lowest friction anywhere in academia or industry, and it encourages
  reuse, forking, and citation.
- **Caution:** MIT applies only to **the kgaf3_chatbot code itself**. The following
  follow their own licenses and are not included in this repository:
  - **AF3 weights/DBs** — Google DeepMind license. Users obtain and run them themselves.
  - **KG source datasets (NPASS, Open Targets)** — each dataset's own license.
  - **Bundled tools such as smina** — each tool's own license (see `smina-mcp/README.md`).
- **Setting the copyright holder:** the current `LICENSE` uses the neutral placeholder
  `Copyright (c) 2026 kgaf3_chatbot contributors`. To publish under a real name/institution,
  change that one line (e.g., `Copyright (c) 2026 <Your Name or Lab>`). For anonymous
  release, leave it as-is.

---

## 5. Recommended README structure

Keep `README.md` (English) and `README.ko.md` (Korean) on the same skeleton:

1. One-line identity + language toggle + (once issued) DOI badge
2. **Features** (emoji bullets) — GraphRAG Q&A + AF3 cofolding + smina rescoring
3. **Architecture** ASCII diagram — `kgaf3-chat` + bundled `smina-mcp` +
   GraphRAG stack (`neo4j` + `graphrag-mcp`) + external AF3 MCP + OpenRouter
4. **Requirements** (Docker; a GPU machine for the external AF3 MCP + AF3; an OpenRouter
   key; `kgaf3-chat` itself needs no GPU)
5. **Quick start** (clone → configure.md → `./install.sh`)
6. **Using prebuilt images** (ghcr pull + compose override)
7. **Documentation links** (INSTALL.md / USAGE.md / AF3_SETUP.md / BRIDGE.md /
   GRAPHRAG.md / af3-bridge/README.md / configure.md / sharing_chtbot.md)
8. **License** + citation + notice that AF3 weights and KG datasets are not included

> **GraphRAG is ON by default.** `docker compose up -d` (or `./install.sh`) starts all
> four services (`kgaf3-chat`, `smina-mcp`, `neo4j`, `graphrag-mcp`). There is **no
> longer** a separate `graphrag` compose profile. To use screening only, set
> `GRAPHRAG_ENABLED=false` in `configure.md`, or run
> `docker compose up -d kgaf3-chat smina-mcp` to skip the KG images. See
> [docs/GRAPHRAG.md](docs/GRAPHRAG.md) for details.

> Make image provenance explicit in the README:
> - **Built and pushed by us (ghcr.io):** `ghcr.io/suppakoko/kgaf3-chat`,
>   `ghcr.io/suppakoko/afmm-smina-mcp` (Section 6 CI).
> - **Pulled from Docker Hub (third-party, not built by us):** `yoonjuho94/graphrag-neo4j:1.0`,
>   `yoonjuho94/graphrag-mcp-server:1.1`.
> - **Not distributed:** AlphaFold3 weights/DBs (the user's own assets).

---

## 6. Prebuilt ghcr.io images (GitHub Actions)

Goal: on every release, automatically build and push **the two images we produce** →
`ghcr.io/suppakoko/kgaf3-chat`, `ghcr.io/suppakoko/afmm-smina-mcp`.
(The GraphRAG images are not built here — they are pulled from Docker Hub `yoonjuho94/*`.)

### 6-1. Workflow (already included)

`.github/workflows/docker-publish.yml` does the following:

- Triggers: `v*` tag push, a published GitHub Release, manual runs (`workflow_dispatch`).
- Logs in to ghcr via `docker/login-action` — **no extra secret needed**, uses the
  built-in `GITHUB_TOKEN` (`permissions: packages: write`).
- Builds two images as a matrix:
  - `kgaf3-chat` → context `.`, `./Dockerfile`
  - `afmm-smina-mcp` → context `./smina-mcp`, `./smina-mcp/Dockerfile.smina`
- `docker/metadata-action` derives tags automatically: **semver** (`1.2.3`, `1.2`) + **`latest`**.
- Attaches build provenance/SBOM (attestation).
- The owner is **resolved automatically** as `${{ github.repository_owner }}`
  (= `suppakoko`, lowercase-normalized) — no file edits needed.

### 6-2. Enable Actions

- Public repositories have Actions enabled by default. If disabled:
  **Settings → Actions → General → Allow all actions and reusable workflows**.
- Turn on **Read and write permissions** under
  **Settings → Actions → General → Workflow permissions** (needed to push packages).

### 6-3. Make packages Public

On first push, packages may be **private**. They must be public for users to pull:

- GitHub profile/org → **Packages** → select `kgaf3-chat` →
  **Package settings → Danger Zone → Change visibility → Public**.
- Do the same for `afmm-smina-mcp`.
- (Optional) On the same screen, use **Connect repository** to link the package to the
  repository so it appears alongside the README/releases.

### 6-4. How end users consume the prebuilt images

As instructed in the README, pull without building:

```bash
docker pull ghcr.io/suppakoko/kgaf3-chat:latest
docker pull ghcr.io/suppakoko/afmm-smina-mcp:latest
```

To make Compose use these images instead of building, a single override:

```yaml
# docker-compose.override.yml
services:
  kgaf3-chat:
    image: ghcr.io/suppakoko/kgaf3-chat:latest
    build: null
  smina-mcp:
    image: ghcr.io/suppakoko/afmm-smina-mcp:latest
    build: null
```

> The GraphRAG services (`neo4j`, `graphrag-mcp`) need no override — compose already
> pulls the Docker Hub images directly.
>
> For reproducibility, we recommend **pinning a version tag** such as `:v0.1.0` instead
> of `latest`.

---

## 7. Versioning and release tagging

Follow [SemVer](https://semver.org): `vMAJOR.MINOR.PATCH`.

```bash
# First public release
git tag -a v0.1.0 -m "kgaf3_chatbot — first public release"
git push origin v0.1.0
```

Pushing the tag triggers the Section 6 workflow, producing
`ghcr.io/suppakoko/kgaf3-chat:0.1.0` (+`latest`) and
`ghcr.io/suppakoko/afmm-smina-mcp:0.1.0` (+`latest`).

Create a GitHub Release (release notes + DOI issuance trigger):

```bash
gh release create v0.1.0 \
  --title "kgaf3_chatbot v0.1.0" \
  --notes "First public release. Two modes: GraphRAG Q&A over a natural-product KG (NPASS 3.0 + Open Targets 25.12, default ON) and AF3 (external) protein–ligand cofolding with bundled smina rescoring. See README, docs/INSTALL.md, docs/AF3_SETUP.md, configure.md."
```

**Recommended release-note items:** a one-line summary, an intro to the two modes,
requirements (Docker / external AF3 MCP + GPU / OpenRouter key), known constraints (AF3
reachability → links to [docs/BRIDGE.md](docs/BRIDGE.md) and
[docs/AF3_SETUP.md](docs/AF3_SETUP.md)), and the notice that AF3 weights and KG datasets
are not included.

---

## 8. Issuing a Zenodo DOI

Immortalize the GitHub Release as an academically citable DOI.

1. Log in to <https://zenodo.org> with your GitHub account (authorize).
2. Go to **Zenodo → top-right menu → GitHub**. In the repository list, flip the
   **switch ON** next to `kgaf3_chatbot`.
   (DOIs are issued for releases created *after* you turn this switch on.)
3. **Create a new Release** on GitHub (Section 7). If you already cut v0.1.0 before
   enabling the switch, cut one more new release such as v0.1.1.
4. Zenodo automatically fetches the release archive and **issues a DOI**:
   - **Concept DOI** (represents all versions, always points to the latest)
   - per-version DOIs
   Academic citations usually use the **Concept DOI**.
5. **Add badge/citation:**
   - Fill the DOI into `identifiers` in `CITATION.cff` (uncomment the currently
     commented block):
     ```yaml
     identifiers:
       - type: doi
         value: "10.5281/zenodo.XXXXXXX"
         description: "Concept DOI (all versions)"
     ```
   - Add a DOI badge at the top of the README:
     ```markdown
     [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.XXXXXXX.svg)](https://doi.org/10.5281/zenodo.XXXXXXX)
     ```
6. **ORCID:** putting the author's ORCID into the Zenodo upload metadata (or the author
   field of `CITATION.cff`) automatically links it to the ORCID profile. It is marked as
   a TODO in `CITATION.cff`.

> Don't forget to change `repository-code`/`url` (in CITATION.cff) to the real GitHub URL
> `https://github.com/suppakoko/kgaf3_chatbot` as well.

---

## 9. Final release checklist

```text
[ ] All 4 Section-1 sanitization greps pass (no output)
[ ] git ls-files has no .env / .env.docker / *.db / weights / *.cif
[ ] LICENSE copyright line finalized (neutral placeholder or real name)
[ ] configure.md example keys are all placeholders (sk-or-REPLACE_ME, etc.)
[ ] configure.md GRAPHRAG_ENABLED default is true (ON by default) confirmed
[ ] install.sh / install.bat generates configure.md → .env.docker, and
    the real .env.docker is not committed
[ ] GitHub repo (suppakoko/kgaf3_chatbot) Public + description/topics set
[ ] Actions: Read and write permissions enabled
[ ] v0.1.0 tag push → docker-publish workflow green
[ ] Both ghcr packages (kgaf3-chat, afmm-smina-mcp) switched to Public
[ ] ghcr.io/suppakoko/kgaf3-chat:latest verified with a real pull
[ ] (GraphRAG ON by default) verify docker compose up -d starts the four services
    (kgaf3-chat, smina-mcp, neo4j, graphrag-mcp)
[ ] (GraphRAG) both Docker Hub images
    (yoonjuho94/graphrag-neo4j:1.0, yoonjuho94/graphrag-mcp-server:1.1) verified with a real pull,
    KG 273,519 nodes / 1,493,463 relationships autoloaded (~1 min on first boot) confirmed
[ ] (screening only) verify opt-out via GRAPHRAG_ENABLED=false or
    docker compose up -d kgaf3-chat smina-mcp
[ ] Re-confirm AF3 weights/DBs and KG source datasets are redistributed nowhere in the repo or images
[ ] Zenodo switch ON → Release created → DOI issuance confirmed
[ ] CITATION.cff + README DOI badge updated
[ ] (final) reproduce the README Quick start verbatim in a clean environment
```

---

## 10. AF3 bridge guidance (external dependency)

kgaf3_chatbot's screening mode requires a reachable **external AF3 MCP server**.
`kgaf3-chat` does not run AF3 itself — it only calls out via `AF3_MCP_URL` (default
`http://host.docker.internal:8002/mcp/`). AF3 itself (including weights/DBs) is **not
distributed by this repository**; users run it on their own GPU machine under the AF3
license.

- Obtaining AF3, weights/DBs, launching the AF3 MCP bridge, setting `AF3_MCP_URL`, and
  verification steps: **[docs/AF3_SETUP.md](docs/AF3_SETUP.md)**
- What the bundled bridge reference assets (`af3-bridge/`) are and how to run them
  (coupled to the external `af3_chatbot` backend + `alphafold3` image, and **not
  standalone**): **[af3-bridge/README.md](af3-bridge/README.md)**
- The reachability problem where the AF3 MCP is hardcoded to `127.0.0.1` and gets blocked
  from containers/remote hosts, and how to resolve it (the one-line patch
  `patches/af3_mcp_host_env.patch`, host network mode, a socat/nginx forwarder, an SSH
  tunnel): **[docs/BRIDGE.md](docs/BRIDGE.md)**

When publishing, make it clear in the README and release notes that **"screening mode
requires a reachable external AF3 MCP."** The installer actively verifies this connection
at the end of installation and, on failure, points to BRIDGE.md/AF3_SETUP.md. (GraphRAG
mode works without AF3.)
