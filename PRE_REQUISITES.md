# Prerequisites

Everything here must be done **before** the session starts. Working through it
live costs 30+ minutes and blocks the hands-on portion. Budget 20 minutes the day
before, and run the verification block at the end to confirm.

---

## 1. Accounts

| | What you need | Why |
|---|---|---|
| **AWS** | An account where you can create IAM roles | Every agent runs as an AgentCore Runtime with its own role |
| **MongoDB Atlas** | An organisation where you can create a project | Terraform creates a dedicated project and an M10 cluster |

Neither can be a locked-down corporate account with a restrictive SCP. If you are
not sure, run the verification block below — it will tell you.

---

## 2. AWS setup

### 2.1 Region

Use a region with **Bedrock AgentCore** available. `us-east-1` is the default and
the safest choice. If you pick another, confirm AgentCore and both models below
are available there first.

### 2.2 Enable Bedrock model access

Bedrock console → **Model access** → enable:

- **Anthropic Claude Haiku 4.5** — every agent's model. Always required.
- **Amazon Titan Text Embeddings V2** — only if you pick `embedding_mode = "titan"`
  in [2.3](#23-choose-an-embedding-mode). Enable it anyway; it costs nothing and
  leaves you a fallback.

Approval is usually instant but is **not** guaranteed to be. Do this first.

### 2.3 Choose an embedding mode

`./deploy.sh` asks this on the first run and records the answer as
`embedding_mode` in `terraform.tfvars`. Deciding now saves a restart, because the
required secret is checked before anything is created.

| Mode | You need | Good when |
|---|---|---|
| `atlas-voyage` *(default)* | a Voyage or Atlas model API key | you want the Voyage quality and already have a key |
| `auto` | nothing | you want the least moving parts, and Preview features are fine |
| `titan` | Bedrock model access (2.2) | you have no Atlas or Voyage key at all |

#### If you pick `atlas-voyage`

Set `voyage_api_key` in `terraform.tfvars`. Two things to get right:

**1. There are two kinds of Voyage key, and they are not interchangeable.**
The stack detects which you have from its prefix and calls the matching
endpoint, so you do not need to configure this — but you do need to know that
mixing them up produces a `403`, not a helpful message:

| Key prefix | Issued by | Endpoint used |
|---|---|---|
| `al-…` | MongoDB Atlas → model API keys | `https://ai.mongodb.com/v1/embeddings` |
| `pa-…` | Voyage AI directly | `https://api.voyageai.com/v1/embeddings` |

**2. Check the rate limit.** Each agent turn makes 4–6 embedding calls, so a
low-tier key (a few requests per minute) returns `429` part-way through a
conversation and the answer stalls. A key rated in the hundreds of requests per
minute is comfortable. Titan has no such limit at workshop volume, and `auto`
makes no API call from your side at all.

#### If you pick `auto`

Nothing to obtain — MongoDB Atlas generates the embeddings inside the cluster and
bills them to your Atlas account. Four things to know before you choose it:

- **It is a Preview feature.** Fine for a workshop, not for production.
- **Dedicated cluster only.** `atlas_cluster_tier` must be `M10` or larger (it is
  by default) with storage auto-scaling on — `atlas.tf` enables it, because a full
  disk pauses embedding generation and marks the index `Stale`.
- **Model choice is narrower**: `voyage-4` (default), `voyage-4-large`,
  `voyage-4-lite`, or `voyage-code-3`. Terraform rejects anything else up front.
- **Embedding runs on MongoDB's inference infrastructure in a US region**,
  wherever your cluster lives, and your text is sent there. Data transfer costs
  apply.

Seeding takes a few minutes longer in this mode: Atlas embeds the corpus
asynchronously after the index is built, and `seed.py` waits for a real query to
come back before it lets the UI start.

### 2.4 Credentials and permissions

Configure a profile that works from your terminal:

```bash
aws configure           # or aws sso login --profile <yours>
aws sts get-caller-identity
```

The identity needs the permissions in [`iam-permissions.json`](iam-permissions.json).
Attach it as an inline or managed policy. `AdministratorAccess` also works if
that is what your account gives you.

### 2.5 If your account is in a governed AWS Organization

Some organizations enforce a Bedrock **guardrail policy** across member accounts. When they
do, AWS applies a guardrail owned by another account to every `Converse` call, and the
caller fails with:

```
is not authorized to perform: bedrock:ApplyGuardrail on resource:
arn:aws:bedrock:<region>:<OTHER-ACCOUNT>:guardrail/<id>
```

The Terraform grants `bedrock:ApplyGuardrail` for this reason, so it should just work.
Accounts with no org guardrail are unaffected by the extra permission.

What it does **not** work around is a guardrail that *blocks* your content — that surfaces as
a refusal in the agent's answer, not an IAM error. If that happens, the guardrail belongs to
your org's security team, not to this stack.

### 2.6 Service quotas

Defaults are fine for a fresh account. Check only if yours is heavily used:

- 1 × `t4g.small` EC2 instance
- 5 × AgentCore runtimes (orchestrator + 3 specialists + MCP server)
- 2 × ECR repositories

---

## 3. MongoDB Atlas setup

You have two options. Read 3.0 first — it decides whether you need 3.1–3.3 at all.

### 3.0 New cluster, or one you already have?

| | Terraform creates it *(default)* | You bring one (`mongodb_uri`) |
|---|---|---|
| Setup | API key + org ID (3.1, 3.2) | a connection string |
| Deploy time | 15–20 min | 8–12 min |
| Cost | ~USD 0.11/hour while it runs | whatever your cluster already costs |
| `destroy` | removes the project and all data | leaves your cluster untouched |
| Good for | first run, clean slate per attendee | re-running the workshop, or an existing dev cluster |

**Bringing your own** — set one line in `terraform.tfvars` and skip 3.1–3.3:

```hcl
mongodb_uri = "mongodb+srv://user:password@cluster.abcde.mongodb.net/"
```

Terraform then creates no Atlas resources at all, so four things become yours to
get right — `./deploy.sh` prints this list back at you before deploying:

- **Network access.** The Atlas project must allow the AgentCore runtimes and the
  EC2 instance in. Those have no fixed egress IPs, so `0.0.0.0/0` is what the
  managed path uses. Atlas → Network Access → Add IP Address.
- **A database user** with `readWrite` on your `mongodb_db` **and** `atlasAdmin`
  on `admin`. The seed creates Atlas Search indexes, which `readWrite` alone
  cannot do. Put its username and password in the URI.
- **Tier.** The stack builds 7 vector indexes, so M0 (limit 3) will not work.
  `embedding_mode = "auto"` additionally needs M10+ with storage auto-scaling on.
- **The data.** The seed upserts into the `mongodb_db` database (`riv_workshop`
  by default) and touches nothing else. `destroy` leaves all of it in place —
  drop that database by hand if you want it gone.

> Switching `mongodb_uri` on/off between runs of an already-deployed stack means
> Terraform destroys the managed cluster, or builds a new one. Decide before the
> first `apply`, not after.

### 3.1 API key

*Skip 3.1–3.3 if you set `mongodb_uri`.*

Atlas → **Organization** → **Access Manager** → **API Keys** → **Create API Key**

- Role: **Organization Project Creator**
- Save the **public** and **private** key — the private key is shown once

### 3.2 Organisation ID

Atlas → **Organization Settings** — copy the ID (24 hex characters).

### 3.3 Billing

The stack creates an **M10** cluster, roughly **USD 0.11/hour**. Your org needs a
payment method on file. The free M0 tier will not work: it allows three Atlas
Search indexes and this stack needs seven. (With `mongodb_uri` set, none of this
applies — you are already paying for whatever you brought.)

**Run `./deploy.sh destroy` when you are finished.**

---

## 4. Local tools

| Tool | Minimum | Check |
|---|---|---|
| Terraform | 1.9 | `terraform version` |
| Docker | with `buildx` | `docker buildx version` |
| AWS CLI | v2 | `aws --version` |
| git | any | `git --version` |

### Docker must be running and able to build ARM64

AgentCore Runtime requires `linux/arm64` images. On Apple Silicon this is native.
On an x86 machine it runs under emulation — enable it once:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
```

Confirm:

```bash
docker buildx build --platform linux/arm64 -o type=cacheonly - <<'EOF'
FROM alpine
RUN uname -m
EOF
```

It must print `aarch64`. On x86 the two image builds take 5–10 minutes; on Apple
Silicon, about 2.

### macOS: the Docker keychain error

If a build fails with:

```
error storing credentials ... The specified item already exists in the keychain. (-25299)
```

`scripts/build-and-push.sh` already retries with an isolated credential store, so
this should self-heal. If it still fails — a locked keychain, or a managed Mac
that blocks writes — clear the stale entry by hand and re-run `./deploy.sh`:

```bash
docker logout <account>.dkr.ecr.<region>.amazonaws.com
security delete-generic-password -l 'Docker Credentials' 2>/dev/null || true
```

Or open **Keychain Access**, search `docker`, and delete the ECR entries.

### Optional: SSM Session Manager plugin

Lets you reach the EC2 box to read seed and UI logs without a key pair.
[Install instructions](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)

---

## 5. Verify

Run this. Every line must pass.

```bash
set -e
terraform version | head -1
docker buildx version
docker info > /dev/null && echo "docker: running"
aws --version
aws sts get-caller-identity --query Arn --output text

REGION=us-east-1
aws bedrock list-foundation-models --region $REGION \
  --query "modelSummaries[?contains(modelId,'claude-haiku-4-5')].modelId" --output text
aws bedrock list-foundation-models --region $REGION \
  --query "modelSummaries[?contains(modelId,'titan-embed-text-v2')].modelId" --output text
aws bedrock-agentcore-control list-agent-runtimes --region $REGION \
  --max-results 1 > /dev/null && echo "agentcore: reachable"
```

The two `list-foundation-models` calls must each print a model id. Empty output
means model access is not enabled — go back to step 2.2.

---

## 6. What deployment will cost

Assume a 4-hour workshop, then destroyed:

| | Approx. |
|---|---|
| Atlas M10 | USD 0.44 |
| EC2 `t4g.small` | USD 0.07 |
| AgentCore runtimes | consumption-based, cents at workshop volume |
| Bedrock (Haiku + Titan) | under USD 1 for a few dozen conversations |
| ECR, S3, Secrets Manager, CloudWatch | pennies |
| **Total** | **well under USD 5** |

Leaving it running costs roughly **USD 3/day**, almost all of it the Atlas
cluster. `./deploy.sh destroy` removes everything, including the Atlas project —
unless you set `mongodb_uri`, in which case your cluster and its data survive and
the AWS side of the bill is pennies.
