# ProdAI Atlas

> The Map Behind Production AI Systems — a self-hosted production AI platform that splits 3 layers · 9 concepts (+A2A) into 47 independently deployable micro-agents.
> Requirements: [.docs/ProdAI-Atlas_PRD.pdf](.docs/ProdAI-Atlas_PRD.pdf) · Compliance: [COMPLIANCE.md](COMPLIANCE.md) · Plan: [docs/implementation-plan.md](docs/implementation-plan.md) · Korean: [README.md](README.md)

**Status: M0 Foundation + all of M1 (Epics 6–12) + all of M2 (Epics 13–18) + M3 first pass (Epics 19–21) complete** (2026-10-02, v0.2.0) — Epics 19–21 add LLM-as-Judge and scores (versioned rubric judges, binary/pairwise with position swap/jury/RAG faithfulness, human-agreement calibration with Cohen's kappa gating, shadow re-scoring, online sampling that turns low-score traces into golden-set candidates, run diffs with McNemar and bootstrap CIs, trends, drift, feedback, scorecards), Bitter Lesson automation (model catalog, 0% candidates, baseline-vs-candidate regression with pass^k, adoption rules, sticky canary with SLO auto-rollback, alias switch with the old model as fallback, end-of-life scans, model cards; prompt registry, harness manifests, externalised rules, flagd-backed compensation flags, ablations, publish gates; compute policies with difficulty-based effort, reasoning-token budgets, best-of-n with verifiers, quality-compute curves) and the maturity assessor (four attribute scores, L0–L4 gates, 9-concept coverage, compound-loop metrics, 90-day roadmap, shared metric definitions, monthly snapshots and reports, the home evolution band, outcome metrics). Epics 16–18 add A2A (Manager-signed Agent Cards with JCS + JWS ES256, skill registry with BM25 and visibility, remote delegation with streaming, async tracking, INPUT_REQUIRED relay, cancel and timeout, signed push notifications), multi-agent units (orchestrator, merger, research over a built-in corpus, code review on local diffs with SARIF, analysis in an isolated sandbox service) and the approval inbox plus notifications (loop HITL requests land in one inbox sorted by risk and expiry; approve, reject or edit-then-approve resumes the same A2A task; event rules deliver HMAC-signed webhooks and email via Mailpit) with console screens for the inbox, org admin and the A2A registry. Epics 13–15 add inference economics (provider prompt-cache hints and KPIs, exact response cache, Batch API, rule routing, per-task model policy, reasoning-effort normalization, hierarchical budgets, per-task token budgets, cost simulator), enterprise IAM and isolation (Keycloak SSO for web/CLI/service accounts, OpenFGA role templates, API keys, orgs/projects/envs/quotas, PostgreSQL RLS, tenant-signed A2A JWTs, hash-chained audit log) and secrets (file/OpenBao SecretStore, least-privilege agent secret broker with audit, zero-downtime provider key rotation). Epics 11–12 add console v1 (served by the Manager binary via embed.FS, `/svc/*` platform proxy, org/project/env switcher, loop/MCP/guard/trace/evals screens) and packaging & release (multi-arch non-root images, single binaries for 6 targets, Python wheel, compose bundle, `atlas` installer CLI, SemVer/compat, release-please, GoReleaser, cosign, SBOM, license policy, channels, Tauri desktop for macOS). Earlier epics added observability (ClickHouse trace store, search DSL, span tree, replay, dashboards, SLOs, alerts) and evals (golden datasets with review and immutable versions, trace promotion, regression suites with pass^k and a CI gate) on top of: agentic loop runtime (AL-*), MCP core (registry with pre-install scan, tool pinning and allowlists; host; BM25 selector; OpenAPI→MCP), guardrails (pipeline, Korean PII recognizers, rule-based jailbreak detection, policy decision point, gateway hooks).

## Layout (polyglot monorepo)

| Path | Role | Stack |
|------|------|-------|
| `apps/manager/` | Control plane — agent catalog (register · revisions · delete), deployment health collection, agent-proxy | Go 1.26 · Gin · GORM · goose · NATS |
| `apps/gateway/` | AI Gateway — OpenAI-compatible `/v1`: routing · virtual-key auth · RPM/TPM limits · upstream key pool · cost tracking · policy & hash-chained audit | Go 1.26 · Redis · NATS · OTel |
| `agents/runtime/` | Data plane — `atlas_agent_core` (A2A v1.0 server SDK) + unit agents `units/*` (AL-PLAN/ACT/OBS/REF/EXIT/RUN, MCP-REG/HOST/SEL, GR-IO/PII/JB/POL) + `mcp_servers/` | Python 3.12 · FastAPI · uv · SQLAlchemy 2 + Alembic · fastmcp 4 · OTel |
| `apps/console/` | Console SPA v1 — `/o/{org}/p/{proj}/e/{env}/{section}` router, overview, agents, loop runs (HITL), MCP, guardrails, trace waterfall, eval review queue; embedded in the Manager in prod | React 19 · TypeScript · Vite 7 · pnpm |
| `apps/desktop/` | Desktop — Tauri 2 shell with `atlas-manager` as a sidecar (loopback, random port, SQLite in app data) | Rust · Tauri 2 |
| `deploy/bundle/` | Install bundle — image-only `compose.yaml`, `install.sh`, `.env.example`, `compat.json` (also embedded in the `atlas` CLI) | Docker Compose |
| `scripts/` | Release tooling — `release-snapshot.sh`, `build-multiarch.sh`, `sbom.sh`, `license-check.py`, `channels.py`, `make-bundle.py` | bash · Python |
| `compose/` | Shared infra | Redis 7.4 · NATS JetStream 2.11 · OTel Collector 0.136 |

Request path (loop): caller → Manager agent-proxy → AL-RUN — plans via AL-PLAN (A2A), model calls via the AI Gateway (guardrail pre/post hooks → GR-IO), tools = built-ins + MCP (registry-approved and pinned, BM25-selected), tool authorization and result sanitization via GR-IO. One W3C trace end to end; events go to NATS as CloudEvents 1.0.

## Run (docker compose only)

```bash
make preflight && make local-all   # local: per-service SQLite, hot reload
open http://127.0.0.1:5173         # console
make mcp-seed                      # register + approve the MCP servers, default allowlist
make test-all                      # go test · pytest · vitest
make guard-redteam                 # guardrail red-team regression
make eval-seed && make eval        # seed the example golden set, run the regression gate (exit 0/100/2)
make prod-all                      # prod: shared PostgreSQL 17 (replace CHANGE_ME in .env.prod first)
```

| Service | Port | Notes |
|---------|------|-------|
| manager | 8080 | `/api/v1` (catalog, agents, deployments, agent-proxy, OpenAPI), `/health` (version), embedded console `/` (prod), platform proxy `/svc/{gw,ob,ev,gr,mcpreg,alrun}/*` |
| gateway | 8090 | `/v1/chat/completions` (stream), `/v1/models`, `/admin/*` |
| agent-al-plan | 8000 | Agent Card, `/a2a`, `/healthz`, `/readyz` |
| agent-al-run | 8001 | loop runtime: `loop.run`, `loop.resume`, `/runs/{id}/events` (SSE) |
| agent-mcp-reg | 8002 | MCP registry (Registry API v0.1 compatible) |
| mcp-demo / mcp-tool | 8003 / 8004 | demo MCP server / Manager OpenAPI as MCP tools |
| agent-gr | 8005 | guardrails `/v1/guard/*` |
| agent-ob | 8006 | observability `/v1/traces`, `/v1/replay`, `/v1/dashboards/llm`, `/v1/slos`, `/v1/alerts/*` |
| agent-ev | 8007 | evals `/v1/datasets…`, `/v1/runs` |
| clickhouse | 8123 | trace store (30-day TTL) |
| console | 5173 | dev server / nginx, proxies `/api/*` and `/svc/*` to the manager (in prod use the manager on 8080) |
| nats / redis / otel-collector | 4222·8222 / 6379 / 4317·4318 | shared infra |
| agent-ma-orch / merge / res / cr / anl | 8010 / 8011 / 8012 / 8013 / 8014 | multi-agent units: `ma.orchestrate` (skill security required), `ma.merge`, `ma.research`, `ma.code_review`, `ma.analysis` |
| sandbox | internal only (`sandbox-net`) | isolated code execution `POST /run`: no external network, read-only, cap_drop ALL, UID 61000, per-run rlimits |
| agent-bl | 8015 | Bitter Lesson: model candidates, canary, aliases, EOL, model cards, prompt/harness/rule registry with publish gates, compensation flags, ablations, best-of-n, quality-compute curves |
| flagd | internal only (OFREP 8016) | OpenFeature flag evaluation; the flag file is written by agent-bl |
| agent-mat | 8017 | maturity assessor: attribute scores, L0–L4, coverage, roadmap, metrics, snapshots, report, home band |
| agent-kb | 8018 | knowledge & memory: KBs, corpus/URL sources, chunking, hybrid search (pgvector + FTS, RRF, ACL), quarantined memory writes, consolidation, erasure propagation, RAG eval |
| agent-sbx | 8019 | code sandbox `sbx.run_code` (Python/JS/shell), resource and egress limits, isolation level, secret/PII output scan, run audit |
| agent-mcp-sys | 8020 | standard connector pack + resources at `/connectors/mcp` (GitHub, Slack, Jira, ERP DB read-only, web), `GET /resources/subscribe` (SSE) |
| agent-rpt | 8022 | usage & audit: metering, cost grouping, chargeback, unified audit (hash chain, append-only, WORM), SIEM (OCSF/Splunk HEC), monthly report, anomaly detection |
| agent-mkt | 8023 | templates & marketplace: catalog, one-click install from a params form, signature/SLSA/SBOM verification, private publishing via the approval inbox, updates, MCP registry sync |
| agent-mcp-gw | 8024 | MCP gateway: one endpoint `/t/{tenant}/mcp/{vs}` aggregating allowed servers (`{alias}__{tool}`, virtual servers / toolset profiles), MRTR input-request and progress relay, guard hooks (tainted run → external writes blocked), Origin check, inbound token audience `atlas-mcp-gateway` |
| mcp-protected | internal | test OAuth-protected MCP server (Keycloak token validation, RFC 9728 metadata) used to verify service-account OAuth |
| egress-proxy | internal 3128 / admin 8080 | egress allowlist proxy per run/server, blocks private and metadata addresses, decision log |
| mcp-sbx | internal (`mcp-sbx-net`) | isolation bridge for low-trust stdio MCP servers (`/servers/{name}/mcp`), read-only, cap_drop ALL, UID 61001 |
| fake-upstream | internal | test-only fake external systems (docs, GitHub, Slack, Jira, public MCP Registry, Splunk HEC) |
| mailpit | 8025 / 1025 | captures notification email (web UI and API / SMTP); dev and rehearsal only |

Local env values are development-only for the mock provider (not real credentials). New in Epics 25–28: `ATLAS_MCP_GW_AUTH` (`optional` locally, `required` in prod). New in Epics 22–24: `ATLAS_KB_URL_ALLOW_HOSTS`, `ATLAS_EGRESS_ADMIN_TOKEN`, `ATLAS_MKT_ADMIN_TOKEN`, `ATLAS_RPT_ADMIN_TOKEN`, `ATLAS_RPT_INGEST_SECRET` (each imported once into a Manager secret); the Manager forwards every PLT/GW event to X-RPT through `ATLAS_RPT_INGEST_URL` (HMAC-signed). New in Epics 16–18: `ATLAS_PUSH_ALLOW_HOSTS`, `ATLAS_SANDBOX_TOKEN` (imported once into the `sandbox-token` secret), `ATLAS_SMTP_ADDR`, `ATLAS_SMTP_FROM`, `ATLAS_NOTIFY_ALLOW_HOSTS`, `MAILPIT_UI_PORT`, `MAILPIT_SMTP_PORT`. New in Epics 13–15: `ATLAS_AUTH_MODE` (`off` locally, `oidc` in prod), `ATLAS_KEYCLOAK_URL`, `KEYCLOAK_ADMIN_PASSWORD`, `ATLAS_KC_CI_SECRET`, `OPENFGA_PRESHARED_KEY`, `ATLAS_A2A_SIGNING_KEY`, `ATLAS_MANAGER_BOOTSTRAP_KEY`, `ATLAS_A2A_AUTH`, `ATLAS_SECRET_STORE`, `ATLAS_GATEWAY_SECRET_STORE`, `ATLAS_ALLOW_ENV_SECRETS`, `ATLAS_BATCH_POLL_BASE_SEC`. New in Epics 11–12: `ATLAS_VERSION` (image tag, matches `VERSION`), `ATLAS_LISTEN` (Manager listen address, default `:8080`), `ATLAS_SVC_URLS` (`/svc/*` proxy allowlist).

## Identity, access and secrets (M2)

- **Sign-in**: the console uses Keycloak Authorization Code + PKCE; the CLI uses `atlas login` (device code, credentials stored with mode 0600); CI uses the `atlas-ci` service account; API keys look like `atl_{env}_{ulid}_{secret}` and require scopes and an expiry of at most 365 days.
- **Roles (OpenFGA)**: org-owner, org-admin, billing, auditor, viewer, project-admin, agent-developer, deployer, operator, env-deployer. Platform admins hold the Keycloak realm role `platform-admin`.
- **Isolation**: the org context comes from `?org=`, `X-Atlas-Org` or the token's `atlas_org` claim; app-level filters plus PostgreSQL RLS; the agent proxy enforces `params.tenant` and attaches an ES256 tenant JWT that agents verify.
- **Approvals**: `POST /api/v1/approvals` (agents file their own; AL-RUN does it automatically on HITL), `GET /api/v1/approvals` sorted by risk then expiry, `POST /api/v1/approvals/{id}/decision` with `approve | reject | edit` (operator or higher, never the requester). The decision is audited with the payload hash and sent to the same A2A task; pending requests expire to `deny`; a task resumed directly withdraws its request.
- **Notifications**: `/api/v1/orgs/{o}/notifications/rules` (NATS topic pattern, declarative conditions, webhook or email channels, throttle), `…/deliveries`, `…/rules/{id}/test`, and `/api/v1/notifications/templates` (budget 80/100%, quota exceeded, approval pending/expired). Webhooks carry `X-Atlas-Signature: t=<unix>,v1=<hex HMAC-SHA256>`; private addresses are rejected unless listed in `ATLAS_NOTIFY_ALLOW_HOSTS`.
- **Enterprise IAM (Epic 28)**: SCIM 2.0 at `/scim/v2/{Users,Groups}` with an org key of the dedicated `scim` scope; directory groups map to roles via `PUT /api/v1/orgs/{o}/scim/group-roles`; deactivating or deleting a user removes group tuples, invalidates earlier tokens and revokes the user's API keys. ABAC conditions on role grants (OpenFGA Conditions: `business_hours`, `ip_allowlist`, `mfa_verified` for env-deployer; `amount_limit` for billing), preview with `GET /api/v1/auth/check`. Org security policy `PUT /api/v1/orgs/{o}/security-policy` (admin actions need `acr ≥ 2` → 401 `mfa_required` with an RFC 9470 step-up challenge; idle, max-lifetime and concurrent-session limits per token `sid`). Agent tokens carry `spiffe_id = spiffe://atlas/{org}/{proj}/{env}/{agent}` and a per-peer audience (`agent:{id}`, `mcp:{name}`); agents can down-scope a user's credentials with RFC 8693 `POST /api/v1/token/exchange` (`act.sub` = agent). Budgets inherit org → project → env (`PUT /api/v1/orgs/{o}/budgets/{id}`, children ≤ parent, enforced by Gateway hierarchical budgets); plans (`PUT /api/v1/plans/{id}`) cap agents, concurrent runs and monthly tokens, with `plt.quota.threshold` at 80 % and 100 %.
- **Secrets**: admins can write, rotate and delete but never read values; only agents that declare a secret in `spec.secrets[]` receive it (file 0400 + memory, refreshed every 5 minutes); env-var secrets are blocked by default. Gateway provider keys rotate without downtime via `POST /admin/upstream-keys/{provider}/rotate`.

## Install & release

`VERSION` (SemVer, `0.2.0`) is the single source of truth; compatibility ranges live in `deploy/bundle/compat.json`.

| Artifact | How | Notes |
|----------|-----|-------|
| Images ×4 | `ATLAS_REGISTRY=<reg> scripts/build-multiarch.sh` | linux/amd64 + arm64, non-root, OCI labels, read-only rootfs in prod compose |
| Single binaries | `scripts/release-snapshot.sh` (GoReleaser snapshot, no publish) | `atlas` (installer) and `atlas-manager` (console embedded) for linux/darwin/windows × amd64/arm64, deb/rpm, checksums |
| Python wheel | `cd agents/runtime && uv build --wheel` | `prodai-agents`: `atlas-agent serve --unit units.al_plan` runs standalone |
| Compose bundle | `cd agents/runtime && uv run --frozen python ../../scripts/make-bundle.py` | `deploy/bundle/` |
| SBOM + licenses | `scripts/sbom.sh` | SPDX + CycloneDX per image and binary; `license-check.py` rejects AGPL/SSPL/BUSL and similar |
| Desktop (macOS) | `cd apps/desktop && pnpm install && bash scripts/prepare.sh && pnpm exec tauri build --bundles app` | `ProdAI Atlas.app` (ad-hoc signed); requires Rust |

```bash
atlas install --values atlas-values.yaml   # writes the bundle + .env with random secrets (mode 600); --apply starts it
./install.sh --project atlas               # from the bundle directory
atlas backup --dir <bundle>                # pg_dump into backups/
atlas upgrade --dir <bundle> --to 0.2.1 --dry-run   # compat check (refuses downgrades) + plan, no changes
```

CI ([release.yml](.github/workflows/release.yml)): release-please → multi-arch push, cosign keyless signing, SBOM attestation, grype, license check, GoReleaser, wheel and bundle upload, tauri-action → `beta` channel; nightly builds daily. Promotion nightly → beta → stable is a manual dispatch that verifies signatures and re-tags the same digest (`scripts/channels.py`, `deploy/channels.json`).

## Tests

| Iteration | Scope | Verdict |
|-----------|-------|---------|
| dev-env | integrated stack, local + prod | **PASS** (round 2) |
| impl 1st | Epic 1 Manager catalog | **PASS** 13/13 |
| impl 2nd | Epic 2 Agent SDK + AL-PLAN | **PASS** 12/12 |
| impl 3rd | Epic 3 AI Gateway | **PASS** 16/16 |
| impl 4th | Epic 4 OB-TRACE | **PASS** 8/8 (round 2) |
| impl 5th | Epic 5 deployment E2E + console v0, M0 gate | **PASS** 11/11 (round 2) · gate 36 PASS |
| impl 6th | Epic 6 agentic loop runtime | **PASS** 11/11 |
| impl 7th | Epic 7 MCP core | **PASS** 12/12 (round 2) |
| impl 8th | Epic 8 guardrails, M1 first-pass gate | **PASS** 13/13 · gate 62 PASS |
| impl 9th | Epic 9 observability | **PASS** 12/12 (round 2) |
| impl 10th | Epic 10 evals, M1 second-pass gate | **PASS** 11/11 · gate 79 PASS (round 3) |
| impl 11th | Epic 11 console v1 | **PASS** 14/14 |
| impl 13th | Epic 13 inference economics | **PASS** 13/13 (round 2) · measured lever savings 50–98% |
| impl 14th | Epic 14 enterprise IAM and isolation | **PASS** 17/17 · prod 85 PASS (round 2) |
| impl 15th | Epic 15 secrets; M2 first-pass gate | **PASS** 8/8 · gate 160 PASS (round 3) |
| impl 12th | Epic 12 packaging, release, desktop; M1 final gate | **PASS** 13/13 · gate 108 PASS |
| impl 16th | Epic 16 A2A (signed cards, registry, delegation, push) | **PASS** 12/12 |
| impl 17th | Epic 17 multi-agent + sandbox | **PASS** 12/12 (local round 4 · prod round 3) |
| impl 18th | Epic 18 approval inbox, notifications, console M2; **M2 completion gate** | **PASS** 12/12 (local round 5 · prod round 2) · gate 205 PASS (round 2) |
| impl 19th | Epic 19 judges and scores | **PASS** 12/12 (local round 3 · prod round 1) |
| impl 20th | Epic 20 Bitter Lesson | **PASS** 12/12 (local round 3 · prod round 1) |
| impl 21st | Epic 21 maturity assessor, console M3; **M3 first-pass gate** | **PASS** 12/12 (local round 3 · prod round 2) · gate 248 PASS (round 2) |
| impl 22nd | Epic 22 knowledge & memory (X-KB) | **PASS** 12/12 (local round 1 · prod round 1) |
| impl 23rd | Epic 23 sandbox, MCP system connectors, resources (X-SBX, MCP-SYS, MCP-RES) | **PASS** 15/15 (local round 4 · prod round 1) |
| impl 24th | Epic 24 marketplace & reports (X-MKT, X-RPT), console S101/S102; **M3 completion gate** | **PASS** 16/16 (local round 5 · prod round 2) · gate 301 PASS (round 3) |
| impl 25th | Epic 25 deployment hardening (canary, blue-green, rollback, env promotion, drift, signed manifests, k8s render) | **PASS** 11/11 (local round 2 · prod round 2) |
| impl 26th | Epic 26 advanced MCP (OAuth service account, gateway, MRTR relay, guard hooks, signatures, channels) | **PASS** 12/12 (local round 3 · prod round 1) |
| impl 27th | Epic 27 guardrails & approval safety (indirect injection, secrets, PII vault, shadow, tuning, loop budget, approval policy, notification dedup/digest/escalation) | **PASS** 10/10 (local round 4 · prod round 2) |
| impl 28th | Epic 28 enterprise IAM (SCIM, ABAC, workload identity, token exchange, MFA/session, budget inheritance, plan quotas); **remaining P0/P1 gate** | **PASS** 11/11 (local round 1 · prod round 3) · gate PASS — round 8 full run 345 + split re-run 21 (16 GiB Docker, fresh rehearsal volumes) |

Re-run the remaining-work gate with `test/impl/28th/gate.sh` (Epics 1–28). Previous: the M3 completion gate `test/impl/24th/m3-gate.sh` (before that: `test/impl/21st/m3a-gate.sh`, `test/impl/18th/m2-gate.sh`, `test/impl/15th/m2a-gate.sh`, `test/impl/12th/m1-final-gate.sh`). If host port 8080 is taken by another project, start with `MANAGER_PORT=18080` and set `ATLAS_MANAGER_HOST_URL=http://127.0.0.1:18080` for the test scripts. Release rehearsals (need a local `registry:2` on port 5555): `test/impl/12th/e12_{cosign,release}.sh`, `e12_wheel.sh`, `e12_desktop.sh`.

**Known limitation**: the rule-based jailbreak classifier scores ASR 0 / FPR 0 on its regression set, but **ASR 0.5 / FPR 0.33** on a holdout set written after tuning. A model-based classifier (M2) is needed before production. Details, troubleshooting and remaining M1+ scope: see [README.md](README.md).
