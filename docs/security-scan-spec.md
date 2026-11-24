# Spec: Vulnerability Scan & Remediation Plan

Status: scan run 2026-11-23 (bandit, pip-audit, checkov, npm audit, manual review, git-history secret grep). R1–R10 implemented 2026-11-23; offline checks pass (§6). Needs one live deploy → chat → destroy cycle.
Scope: this repo only — Python agents/UI/seed, the MCP container, Dockerfiles, Terraform, shell scripts, git history.

## 1. Goal

1. A repeatable scan anyone can run from the repo root before tagging a workshop build.
2. A triaged list of what the scan found, with a fix or an explicit accept for each item.

Constraint that shapes every fix: this is a two-hour workshop stack that is created and
destroyed per attendee. Fixes must not add deploy time, extra manual steps, or new
paid services. Anything that would is listed under §5 (accepted risk) with the reason.

## 2. Scan procedure

Run from the repo root. Every command is read-only.

| # | Area | Tool | Command | Installed? |
|---|---|---|---|---|
| S1 | Python SAST | bandit | `bandit -r agent ui seed scripts -q -x '*/test_*.py' -s B101` | yes |
| S2 | Python deps (CVE) | pip-audit | `for f in agent ui; do pip-audit -r $f/requirements.lock --no-deps --disable-pip; done` (the locks are what ships) | yes |
| S3 | Terraform / IaC | checkov | `checkov -d terraform --compact --quiet` | yes |
| S4 | Dockerfiles | checkov | `checkov -f agent/Dockerfile -f mcp/Dockerfile --compact --quiet` | yes |
| S5 | Node deps of MCP image | npm audit | `docker buildx build --platform linux/arm64 --load -t riv-mcp -f mcp/Dockerfile . && docker run --rm --entrypoint sh riv-mcp -c 'cd /app && npm audit --omit=dev'` | yes (Docker) |
| S6 | Built images (OS + libs) | trivy / ECR | ECR scan-on-push is on (R8): `aws ecr describe-image-scan-findings --repository-name <project>/agent --image-id imageTag=latest`. Or `trivy image <ecr-uri>:latest` | ECR yes; trivy no — `brew install trivy` |
| S7 | Secrets, incl. history | gitleaks | `gitleaks git . --redact` | no — `brew install gitleaks` |
| S8 | Manual review | — | Checklist in §2.1 | — |

Pass bar for a release: no unaccepted High/Critical from S2/S5/S6, no secret from S7,
no new checkov failure that isn't listed in §5.

### 2.1 Manual review checklist (S8)

Scanners miss trust-boundary bugs. Check each on every change touching these files:

- [ ] Who can reach the UI (`terraform/ec2.tf` SG, `ui_allowed_cidr`) and is there auth?
- [ ] Is any secret placed in runtime `environment_variables` (`agentcore.tf`) or EC2 `user_data`? Both are readable via the control-plane API.
- [ ] Is `userId` / `sessionId` from the invocation payload trusted for data scoping (`agent/main.py:440`)?
- [ ] What DB privileges does the MCP runtime's connection string carry (`atlas.tf`), and which MCP tools are enabled (`mcp/Dockerfile` `MDB_MCP_DISABLED_TOOLS`)?
- [ ] IAM `Resource: "*"` and `runtime/*` grants (`iam.tf`) — can a principal call something it shouldn't (e.g. the MCP runtime directly)?
- [ ] Private-registry installs: is the index order safe against dependency confusion (`agent/Dockerfile`)?

## 3. Findings (scan of 2026-11-23)

Severity is for this deployment (public UI, per-attendee AWS account), not the tool's default.

| ID | Sev | Source | Finding | Location |
|---|---|---|---|---|
| F1 | **High** | manual | UI is unauthenticated, plain HTTP, open to `0.0.0.0/0` by default. Anyone who finds the IP can drive the agents (Bedrock spend on the attendee's account) and read the shared `workshop-user` memory/history. | `terraform/variables.tf:283`, `terraform/ec2.tf:107-113`, `ui/app.py` |
| F2 | **High** | manual | Atlas DB user has `atlasAdmin` and the same URI is given to the MCP runtime. Any prompt-injected agent call through MCP (`find`, `aggregate`, `update-many`, `delete-one`, `create-index`, …) runs with cluster-admin rights on every database, not just `mongodb_db`. Combined with Atlas IP access list `0.0.0.0/0`, the password is the only control. | `terraform/atlas.tf:74-94` |
| F3 | **High** | manual | Secrets in plaintext runtime env vars: `VOYAGE_API_KEY`, `AGENT_ENGINE_CLIENT_ID`, `AGENT_ENGINE_CLIENT_SECRET` are visible to anyone with `bedrock-agentcore:GetAgentRuntime`. The repo already avoids this for the Mongo URI (`atlas.tf:106-108`) — these were missed. | `terraform/agentcore.tf:101,112-113` |
| F4 | **Med** | manual | `VOYAGE_API_KEY` rendered into EC2 `user_data` (readable via `ec2:DescribeInstanceAttribute` and IMDS) and written to world-readable `/etc/riv-workshop.env` (`chmod 0644`). | `terraform/ec2.tf:157`, `terraform/user_data.sh.tftpl:26-28` |
| F5 | **Med** | pip-audit | `bedrock-agentcore` resolves to 0.1.7 under the `<1` cap — CVE-2026-16796 / GHSA-j6g5-3hh3-pgw8 (command injection in Code Interpreter `install_packages()`), fixed in 1.18.1. The repo does not call `install_packages`/Code Interpreter, so not reachable today, but the cap blocks every future fix. | `agent/requirements.txt:4` |
| F6 | **Med** | manual | `userId` is taken verbatim from the invocation payload and used to scope memory recall/storage. Any principal allowed to `InvokeAgentRuntime` can read/write another user's memory by passing their id. Today there is one user, so impact is latent. | `agent/main.py:440` |
| F7 | **Med** | checkov CKV_AWS_356 + manual | EC2 role may invoke `runtime/*` — including the MCP runtime directly, bypassing the agents. Should be the orchestrator ARN only. | `terraform/iam.tf:209-213` |
| F8 | **Low** | checkov CKV_AWS_163/51 | ECR repos: no scan-on-push, mutable tags. | `terraform/images.tf:24-34` |
| F9 | **Low** | manual | No lockfiles; Python deps are ranges, `agent-engine-sdk-memory` and `uv` unpinned. Builds are not reproducible and a compromised release lands silently. | `agent/requirements.txt`, `ui/requirements.txt`, `seed/requirements.txt`, `agent/Dockerfile:65-66` |
| F10 | Info | manual | Dependency confusion on `agent-engine-sdk-memory` via `--extra-index-url`: **verified not exploitable** — `uv` defaults to `--index-strategy first-index` and checks the extra index first. Becomes exploitable if anyone swaps `uv pip` for `pip`. | `agent/Dockerfile:66-67` |
| F11 | Info | git grep | No credentials found in git history (Mongo URIs with creds, AWS keys, Voyage keys, PEM). `terraform.tfvars` and state are gitignored. Re-confirm with gitleaks (S7). | — |
| F12 | **Med** | npm audit (S5) | MCP image: 8 high advisories, all `basic-ftp` ≤6.2.3 (GHSA-c475-qrg2-pj4r, GHSA-5pm8-86m8-9m6r, GHSA-r4jh-29pc-gfqr) via `pac-proxy-agent` in `mongodb-mcp-server@2.1.0`. Not reachable (no FTP, no PAC proxy), but cheap to clear. Upstream fix is the 3.x major. | `mcp/Dockerfile` |

False positives / noise (no action):

- bandit B101 ×100+ — `assert` in test files. Excluded by `-s B101` in S1.
- bandit B105 `platform_memory.py:62`, `test_agent_engine_memory.py` — token URL and test fixtures, not passwords.
- bandit B311 `embeddings.py:161` — `random` used for retry jitter.
- bandit B404/B603/B607 `scripts/memtest/turn.py` — fixed-arg `terraform` call, dev script.
- checkov CKV_SECRET_4 `variables.tf:62` — the `user:pass@` example in a validation message.
- checkov CKV_DOCKER_2 (no HEALTHCHECK) — AgentCore health-checks via `/ping`.

## 4. Remediation plan

Ordered by risk reduced per minute of work. Each item lists its verification.

### Phase 1 — before the next workshop (High)

**R1 → F1. Put the UI behind something.** Smallest working fix: Streamlit has no auth,
so gate it at the SG and make the default safe.
- `ui_allowed_cidr`: drop the `0.0.0.0/0` default; `deploy.sh` fills it with the
  deployer's `$(curl -s https://checkip.amazonaws.com)/32`. Attendees on the same
  network still work; an override remains for venues with NAT pools.
- Alternative if per-IP breaks venue Wi-Fi: no ingress at all, reach the UI with
  `aws ssm start-session --document-name AWS-StartPortForwardingSession` (SSM is
  already attached, `iam.tf:264`). Zero extra infra, TLS via the SSM channel.
- Verify: `checkov` unchanged; `curl http://<ip>:8501` from an outside IP times out.

**R2 → F2. Split the DB user.**
- Keep an `atlasAdmin` user only for seeding (EC2, first boot), stored in its own secret.
- MCP runtime gets a second user with `readWrite` on `var.mongodb_db` only, in the
  existing `mongodb_uri` secret. Index creation stays with the seed user.
- Add `delete-one`, `update-many`, `create-index`, `create-collection` to
  `MDB_MCP_DISABLED_TOOLS` unless an agent spec actually needs them (check
  `config/agents/*.agent.md` / skills before removing).
- BYO-cluster path (`mongodb_uri` set): document in the variable description that the
  supplied user should be `readWrite` on one database.
- Verify: via MCP, `find` on `admin`/another DB is denied; seed still creates search indexes.

**R3 → F3. Move runtime secrets to Secrets Manager.** Reuse the existing pattern:
one secret (JSON: `voyage_api_key`, `agent_engine_client_id`, `agent_engine_client_secret`),
`secretsmanager:GetSecretValue` on that ARN added to the agent runtime role, read once
at startup in `agent_config.py` (or wherever `os.environ` reads them today). Env vars
carry only `APP_SECRET_ARN`.
- Verify: `aws bedrock-agentcore-control get-agent-runtime …` shows no secret values;
  `test_config.py` / `test_embedding_modes.py` still pass.

### Phase 2 — this sprint (Medium)

**R4 → F4.** Same secret as R3; EC2 role gets `GetSecretValue` on it (already has it on
the Mongo secret). Drop `voyage_api_key` from the `templatefile` call; `user_data`
fetches it like it fetches `MONGODB_URI`. `chmod 0600` and `chown root` on
`/etc/riv-workshop.env` (systemd reads `EnvironmentFile` as root before dropping to `User=`).

**R5 → F5.** Raise the cap to `bedrock-agentcore>=1.18.1,<2`. Check the APIs used
(`BedrockAgentCoreApp`, memory client in `agent/memory.py`) against the 1.x changelog,
run the agent tests, and do one end-to-end deploy. Keep the upper bound — the
requirements comment explains why.

**R6 → F7.** EC2 `InvokeOrchestrator` resource → the orchestrator runtime ARN
(`aws_bedrockagentcore_agent_runtime.agents[local.orchestrator_id].agent_runtime_arn`).
Check for a dependency cycle (EC2 role ← runtime is fine; runtime does not depend on EC2).

**R7 → F6.** No code change while there is one user. When real users arrive, derive
`userId` from the caller identity (AgentCore inbound JWT authorizer) instead of the
payload. Leave a `ponytail:` comment at `main.py:440` naming this ceiling.

### Phase 3 — hygiene (Low)

**R8 → F8.** `image_scanning_configuration { scan_on_push = true }` on both ECR repos.
Immutable tags only if `build-and-push.sh` stops pushing `:latest` — otherwise skip.

**R9 → F9.** `pip-compile` (or `uv pip compile`) each `requirements.txt` into a
`requirements.lock` and install from it in Dockerfile/user_data. Pin `uv` and
`agent-engine-sdk-memory==x.y.z` once the first authenticated build records a version
(already flagged in the Dockerfile's `ponytail:` comment).

**R10 → F10.** Add `--index-strategy first-index` explicitly to the `uv pip install`
line so the safety doesn't depend on a default.

### 4.1 Implementation record (2026-11-23)

| R | Done | Where |
|---|---|---|
| R1 | `ui_allowed_cidr` default `""` = no ingress (SSM port forward, new `ui_port_forward_command` output); `/0` rejected by validation; `deploy.sh` sets `TF_VAR_ui_allowed_cidr` to the deployer's IP/32 when tfvars doesn't pin it. | `variables.tf`, `ec2.tf`, `outputs.tf`, `deploy.sh` |
| R2 | Agent user is `readWrite` on `mongodb_db` only; new `-seed` user (`atlasAdmin`) in new `<project>/mongodb-seed-uri` secret, readable by EC2 only. BYO gets optional `mongodb_seed_uri`. MCP: `create-index`, `create-collection` disabled (`delete-one` from the plan does not exist in 2.1.0; `update-many` is used by `memory.touch_session`, so kept). | `atlas.tf`, `iam.tf`, `variables.tf`, `mcp/Dockerfile`, docs |
| R3 | `<project>/app-credentials` JSON secret; runtimes get only `APP_SECRET_ARN`; `main._load_app_secret()` loads it before other imports (explicit env wins, empties skipped). Check: `test_tool_trace.py::test_app_secret_fills_env_without_overriding_it`. | `agentcore.tf`, `iam.tf`, `agent/main.py` |
| R4 | `voyage_api_key` out of `user_data`; seed reads it (and the seed URI) from Secrets Manager into its own process env, then unsets. `/etc/riv-workshop.env` is `root:root 0600` and holds no credentials. | `user_data.sh.tftpl`, `ec2.tf` |
| R5 | `bedrock-agentcore>=1.18.1,<2` (locks to 1.27.0). Verified: `/ping`, streaming `/invocations` (SSE), `MemoryClient.create_event` / `get_last_k_turns` signatures; agent image builds and `import main` succeeds. | `agent/requirements.txt` |
| R6 | EC2 may invoke only the orchestrator ARN (+ `/*` for its endpoints). | `iam.tf` |
| R7 | `ponytail:` comment at the `userId` read. | `agent/main.py` |
| R8 | `scan_on_push = true` on both repos. Tags stay mutable: `build-and-push.sh` re-pushes `:latest`. | `images.tf` |
| R9 | Hashed locks: `agent/requirements.lock` (py3.12, linux/aarch64) used by the Dockerfile; `ui/requirements.lock` (seed + UI, py3.11, linux/aarch64) used by `user_data`. Regenerate with the command in each file's header after editing a `requirements.txt`. `uv==0.11.26` pinned. `agent-engine-sdk-memory` still unpinned (version unknown until an authenticated build). | `agent/`, `ui/`, `agent/Dockerfile`, `user_data.sh.tftpl` |
| R10 | `--index-strategy first-index` explicit. | `agent/Dockerfile` |
| R11 → F12 | `npm pkg set overrides.basic-ftp=^6.2.4` before install; `@aws-sdk/client-secrets-manager@3` pinned to major. Built image: `npm audit` → 0 vulnerabilities. | `mcp/Dockerfile` |

Migration notes for an existing stack:

- A local `terraform.tfvars` with `ui_allowed_cidr = "0.0.0.0/0"` now fails validation — delete the line (deploy.sh fills it) or set a real CIDR.
- Rotate the Agent Engine service-account secret and the Voyage key after the first apply: the old values sat in runtime env vars (F3) and in `user_data` (F4), and remain in `terraform.tfstate` regardless.
- The apply replaces the EC2 instance (`user_data` changed) and re-seeds; the agent DB user loses `atlasAdmin` in place.

## 5. Accepted risk (workshop scope)

| Item | Why accepted | Revisit when |
|---|---|---|
| Atlas IP access list `0.0.0.0/0` | AgentCore runtimes in `PUBLIC` network mode have no fixed egress IP; PrivateLink adds ~10 min and cost per attendee. Mitigated by R2 (least-privilege user) + strong random password. | Any non-workshop deployment |
| KMS CMKs for Secrets Manager, ECR, S3, SSM (CKV_AWS_149/136/145, CKV2_AWS_34) | AWS-managed keys already encrypt at rest; CMKs add cost and teardown friction for no threat-model gain here. SSM param holds runtime ARNs, not secrets. | Compliance requirement |
| Secret rotation (CKV2_AWS_57) | Stacks live hours. | Long-lived stack |
| S3 versioning, logging, lifecycle, replication, events (CKV_AWS_18/21/144, CKV2_AWS_61/62) | Bucket holds a rebuildable app bundle; `force_destroy` by design. | — |
| EC2 detailed monitoring / EBS-optimized (CKV_AWS_126/135) | Cost/perf, not security. | — |
| SG egress `0.0.0.0/0` (CKV_AWS_382) | Needs Atlas, Bedrock, S3, PyPI; no fixed IPs. | — |
| `Resource: "*"` on `bedrock:InvokeModel`, logs, xray, `ecr:GetAuthorizationToken` (CKV_AWS_111/356) | Cross-region inference profiles and these APIs don't support narrower resources (see `iam.tf` comments). | — |
| Secrets Manager `recovery_window_in_days = 0` | Needed for create/destroy cycles. | — |

## 6. Done when

- [x] R1–R11 implemented; `terraform validate` passes; all 5 `agent/test_*.py` pass; both images build for linux/arm64; UI lock installs on python:3.11 arm64.
- [ ] One full `deploy.sh` → chat → `terraform destroy` cycle passes (needs AWS + Atlas).
- [x] S1–S4 re-run: only §3 false positives and §5 accepted items remain (checkov 25 failures, all §5; CKV_AWS_163 cleared; the 3 secrets each add CKV_AWS_149 / CKV2_AWS_57, accepted).
- [x] S2 on the locks: 0 known vulnerabilities. S5: 0 after R11.
- [ ] S6 (trivy, or read ECR scan results after first push) and S7 (gitleaks) — tools not installed here.
- [ ] §2.1 checklist added to the PR template or README contributor section.
