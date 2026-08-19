#!/usr/bin/env bash
# Single-command deployment for the Multi-Agent RIV Workshop.
#
#   ./deploy.sh            deploy everything
#   ./deploy.sh destroy    tear it all down
#
# Configuration comes from terraform/terraform.tfvars (copy the .example).
set -euo pipefail

cd "$(dirname "$0")"
TF_DIR="terraform"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

# ── Preflight ─────────────────────────────────────────────────────────────────

missing=0
for cmd in terraform docker aws; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    red "missing: $cmd"
    missing=1
  fi
done

if ! docker buildx version >/dev/null 2>&1; then
  red "missing: docker buildx (needed to build the linux/arm64 images AgentCore requires)"
  missing=1
fi

if ! docker info >/dev/null 2>&1; then
  red "docker daemon is not running"
  missing=1
fi

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  red "AWS credentials are not configured or have expired"
  missing=1
fi

[ "$missing" -eq 0 ] || { red "Fix the above, then re-run. See PRE_REQUISITES.md."; exit 1; }

if [ ! -f "$TF_DIR/terraform.tfvars" ]; then
  red "$TF_DIR/terraform.tfvars not found."
  echo "  cp $TF_DIR/terraform.tfvars.example $TF_DIR/terraform.tfvars"
  echo "  then either fill in your Atlas org ID and API keys, or set mongodb_uri"
  echo "  to a cluster you already have."
  exit 1
fi

tfvar() {
  sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" \
    "$TF_DIR/terraform.tfvars" | head -1
}

REGION="$(tfvar aws_region)"
REGION="${REGION:-us-east-1}"

VOYAGE_KEY="$(tfvar voyage_api_key)"
CLUSTER_TIER="$(tfvar atlas_cluster_tier)"
EMBED_MODEL="$(tfvar voyage_embed_model)"
MONGODB_DB="$(tfvar mongodb_db)"
MONGODB_DB="${MONGODB_DB:-riv_workshop}"

# Bring-your-own cluster: mongodb_uri set means Terraform creates no Atlas
# resources and destroys none either. Never echoed — it carries a password.
if [ -n "$(tfvar mongodb_uri)" ]; then BYO_ATLAS=1; else BYO_ATLAS=0; fi

# ── Destroy ───────────────────────────────────────────────────────────────────

if [ "${1:-}" = "destroy" ]; then
  bold "Destroying the workshop stack…"
  if [ "$BYO_ATLAS" -eq 1 ]; then
    echo "Your own cluster stays — including the '$MONGODB_DB' database the seed wrote."
    echo "Drop that database by hand if you want the collections and indexes gone."
    echo
  fi
  terraform -chdir="$TF_DIR" destroy
  green "Done. Check the AWS and Atlas consoles to confirm nothing is left billing."
  exit 0
fi

# ── Atlas ─────────────────────────────────────────────────────────────────────

if [ "$BYO_ATLAS" -eq 1 ]; then
  bold "Atlas: using the cluster in mongodb_uri — nothing will be created or destroyed."
  echo "  It needs to already have:"
  echo "    · network access allowing the AgentCore runtimes and this stack's EC2 box"
  echo "    · a user with readWrite on '$MONGODB_DB' and atlasAdmin on admin"
  echo "      (the seed creates Atlas Search indexes, which readWrite cannot do)"
  echo "    · room for 7 vector indexes — M0 allows 3"
  echo "  The seed upserts into '$MONGODB_DB'; it does not touch other databases."
else
  for v in atlas_org_id atlas_public_key atlas_private_key; do
    if [ -z "$(tfvar $v)" ]; then
      red "$v is empty in $TF_DIR/terraform.tfvars."
      echo "  Terraform needs it to create the Atlas project and cluster."
      echo "  Already have a cluster? Set mongodb_uri instead and skip all three."
      exit 1
    fi
  done
  green "Atlas: creating a dedicated project and ${CLUSTER_TIER:-M10} cluster — destroy removes it."
fi
echo

# ── Embedding mode ────────────────────────────────────────────────────────────
#
# This is the one choice that cannot be changed after the fact: it decides the
# shape of the Atlas vector indexes. Terraform validates it too, but ask here so
# an attendee makes the decision knowingly instead of inheriting a default, and
# so a missing key fails in two seconds rather than twelve minutes in.

EMBEDDING_MODE="$(tfvar embedding_mode)"

if [ -z "$EMBEDDING_MODE" ]; then
  if [ -t 0 ]; then
    bold "How should text be turned into vectors?"
    echo "  1) atlas-voyage  Voyage via the Atlas Embedding API. Needs an API key.   [default]"
    echo "  2) auto          Atlas Automated Embedding. No key — Atlas embeds in-cluster."
    echo "  3) titan         Bedrock Titan v2. No key beyond AWS."
    printf 'Choice [1]: '
    read -r choice
    case "${choice:-1}" in
      1|"") EMBEDDING_MODE="atlas-voyage" ;;
      2)    EMBEDDING_MODE="auto" ;;
      3)    EMBEDDING_MODE="titan" ;;
      *)    red "Not one of 1, 2, 3."; exit 1 ;;
    esac
  else
    EMBEDDING_MODE="atlas-voyage"
  fi
  printf '\nembedding_mode = "%s"\n' "$EMBEDDING_MODE" >> "$TF_DIR/terraform.tfvars"
  green "  recorded embedding_mode = \"$EMBEDDING_MODE\" in $TF_DIR/terraform.tfvars"
  echo
fi

case "$EMBEDDING_MODE" in
  atlas-voyage)
    if [ -z "$VOYAGE_KEY" ]; then
      red "embedding_mode = \"atlas-voyage\" but voyage_api_key is empty."
      echo "  Set it in $TF_DIR/terraform.tfvars — an \`al-…\` key from Atlas"
      echo "  (Project Settings → model API keys) or a \`pa-…\` key from Voyage AI."
      echo "  Or switch to embedding_mode = \"auto\" (no key) or \"titan\"."
      exit 1
    fi
    case "$VOYAGE_KEY" in
      al-*|pa-*) ;;
      *) echo "  note: voyage_api_key starts with neither \`al-\` nor \`pa-\`; if it 403s,"
         echo "        set VOYAGE_API_BASE to the endpoint that issued it." ;;
    esac
    ;;
  auto)
    if [ "$BYO_ATLAS" -eq 1 ]; then
      echo "  check your cluster before continuing: auto-embedding needs M10+ with"
      echo "  storage auto-scaling on. Nothing here can verify that for you."
    else
      case "${CLUSTER_TIER:-M10}" in
        M0|M2|M5|FLEX)
          red "embedding_mode = \"auto\" needs a dedicated cluster; atlas_cluster_tier is ${CLUSTER_TIER}."
          exit 1 ;;
      esac
    fi
    case "${EMBED_MODEL:-voyage-4}" in
      voyage-4|voyage-4-large|voyage-4-lite|voyage-code-3) ;;
      *) red "embedding_mode = \"auto\" does not support voyage_embed_model = \"$EMBED_MODEL\"."
         echo "  Supported: voyage-4, voyage-4-large, voyage-4-lite, voyage-code-3."
         exit 1 ;;
    esac
    if [ -n "$VOYAGE_KEY" ]; then
      echo "  note: voyage_api_key is set but unused in auto mode — Atlas holds the key."
    fi
    ;;
  titan)
    echo "  Titan mode: confirm \"Amazon Titan Text Embeddings V2\" is enabled in"
    echo "  Bedrock → Model access for $REGION, or seeding fails."
    ;;
  *)
    red "embedding_mode = \"$EMBEDDING_MODE\" is not one of: atlas-voyage, auto, titan."
    exit 1 ;;
esac

green "Embedding mode: $EMBEDDING_MODE"
echo

# ── Deploy ────────────────────────────────────────────────────────────────────

bold "Enabling CloudWatch Transaction Search (AgentCore end-to-end traces)…"
# Best-effort: an account-level setting, already on in many accounts, and the
# stack works without it — you just lose the linked trace view.
aws xray update-trace-segment-destination \
  --destination CloudWatchLogs --region "$REGION" >/dev/null 2>&1 \
  && green "  enabled" || echo "  skipped (already set, or no permission — not fatal)"

bold "Deploying…"
if [ "$BYO_ATLAS" -eq 1 ]; then
  echo "Expect 8-12 minutes: no cluster to provision, so the ARM64 image builds are"
  echo "the long pole, then Atlas Vector Search indexes build during EC2 boot."
else
  echo "Expect 15-20 minutes: the Atlas cluster and the ARM64 image builds run"
  echo "in parallel, then Atlas Vector Search indexes build during EC2 boot."
fi
if [ "$EMBEDDING_MODE" = "auto" ]; then
  echo "Auto-embedding adds a few minutes: Atlas embeds the corpus after the index builds."
fi
echo

terraform -chdir="$TF_DIR" init -input=false
terraform -chdir="$TF_DIR" apply -auto-approve

echo
green "━━ Deployed ━━"
terraform -chdir="$TF_DIR" output
echo
bold "The UI answers once seeding finishes (~5 min after the instance boots)."
echo "Watch it:  $(terraform -chdir="$TF_DIR" output -raw ssh_session_command)"
echo "           sudo tail -f /var/log/riv-seed.log"
