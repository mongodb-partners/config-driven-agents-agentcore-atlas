#!/usr/bin/env bash
# Run the UI on your laptop against the deployed runtimes.
#
# Nothing runs locally except Streamlit: the agents stay in AgentCore and the
# data stays in Atlas. So this is the loop for UI work — edit app.py, Streamlit
# reloads, no deploy. Agent changes still need ./deploy.sh.
#
# Needs: terraform state in terraform/, AWS credentials, and an Atlas network
# access rule that lets your laptop's IP in (the deployed stack allows the
# runtimes, not you).
set -euo pipefail
cd "$(dirname "$0")"

command -v terraform >/dev/null || { echo "terraform not on PATH"; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || {
  echo "AWS credentials are missing or expired — the UI signs its calls to the"
  echo "orchestrator runtime with them. Refresh, then re-run."; exit 1; }

TF=$(terraform -chdir=terraform output -json)
py() { python3 -c "import json,sys;print(json.load(sys.stdin)$1)" <<<"$TF"; }

export AWS_REGION AWS_DEFAULT_REGION ORCHESTRATOR_RUNTIME_ARN AGENT_LOG_GROUP \
       MONGODB_DB MONGODB_URI_SECRET_ARN WORKSHOP_USER_ID
ORCHESTRATOR_RUNTIME_ARN=$(py "['agent_runtimes']['value']['orchestrator']")
AGENT_LOG_GROUP=$(py "['cloudwatch_log_groups']['value']['orchestrator']")
MONGODB_URI_SECRET_ARN=$(py "['mongodb_uri_secret_arn']['value']")
MONGODB_DB=$(py "['atlas_cluster']['value']['database']")
AWS_REGION=${AWS_REGION:-$(awk -F'"' '/^ *aws_region/{print $2}' terraform/terraform.tfvars)}
AWS_DEFAULT_REGION=$AWS_REGION
WORKSHOP_USER_ID=${WORKSHOP_USER_ID:-workshop-user}
# app.py falls back to Secrets Manager; skipping it saves a call and works when
# the local role cannot read the secret.
MONGODB_URI=$(awk -F'"' '/^ *mongodb_uri *=/{print $2}' terraform/terraform.tfvars)
[ -n "$MONGODB_URI" ] && export MONGODB_URI

# .venv is already gitignored; reuse it rather than growing a second one.
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q -r ui/requirements.txt

echo "orchestrator: ${ORCHESTRATOR_RUNTIME_ARN##*/}   db: $MONGODB_DB   region: $AWS_REGION"
exec ./.venv/bin/streamlit run ui/app.py --server.port "${PORT:-8501}"
