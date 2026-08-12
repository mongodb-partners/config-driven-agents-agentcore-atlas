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
  echo "  then fill in your Atlas org ID and API keys."
  exit 1
fi

# ── Destroy ───────────────────────────────────────────────────────────────────

if [ "${1:-}" = "destroy" ]; then
  bold "Destroying the workshop stack…"
  terraform -chdir="$TF_DIR" destroy
  green "Done. Check the AWS and Atlas consoles to confirm nothing is left billing."
  exit 0
fi

# ── Deploy ────────────────────────────────────────────────────────────────────

REGION="$(grep -E '^\s*aws_region' "$TF_DIR/terraform.tfvars" 2>/dev/null \
          | sed -E 's/.*"(.*)".*/\1/' || true)"
REGION="${REGION:-us-east-1}"

bold "Enabling CloudWatch Transaction Search (AgentCore end-to-end traces)…"
# Best-effort: an account-level setting, already on in many accounts, and the
# stack works without it — you just lose the linked trace view.
aws xray update-trace-segment-destination \
  --destination CloudWatchLogs --region "$REGION" >/dev/null 2>&1 \
  && green "  enabled" || echo "  skipped (already set, or no permission — not fatal)"

bold "Deploying…"
echo "Expect 15-20 minutes: the Atlas M10 cluster and the ARM64 image builds run"
echo "in parallel, then Atlas Vector Search indexes build during EC2 boot."
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
