#!/usr/bin/env bash
# Build one linux/arm64 image and push it to ECR.
#
#   build-and-push.sh <region> <repo-url> <tag> <dockerfile>
#
# Called by terraform/images.tf for both the agent and the MCP images. Run from
# the repository root — the build context is the whole repo so the agent image
# can bake in config/.
set -euo pipefail

REGION="$1"
REPO_URL="$2"
TAG="$3"
DOCKERFILE="$4"
REGISTRY="${REPO_URL%%/*}"

ecr_login() {
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "$REGISTRY"
}

# Docker Desktop on macOS stores registry credentials in the login keychain, and
# its helper fails with "The specified item already exists in the keychain
# (-25299)" when a stale entry for this registry is already there. Clearing it
# first is the reliable fix; the isolated-config fallback below covers the case
# where the keychain item cannot be removed at all (locked keychain, corrupt
# entry, restricted managed Mac).
docker logout "$REGISTRY" >/dev/null 2>&1 || true

if ! ecr_login; then
  echo "docker login failed — retrying with an isolated credential store." >&2

  REAL_CONFIG="${DOCKER_CONFIG:-$HOME/.docker}"
  CURRENT_CONTEXT="$(docker context show 2>/dev/null || echo default)"

  ISOLATED_CONFIG="$(mktemp -d)"
  trap 'rm -rf "$ISOLATED_CONFIG"' EXIT

  # Symlink EVERYTHING except config.json. `cli-plugins` is the critical one —
  # buildx is a CLI plugin resolved out of $DOCKER_CONFIG, so a bare config makes
  # `docker buildx build` degrade to `docker build`, which has no --platform.
  # `buildx` (builder state) and `contexts` matter too.
  if [ -d "$REAL_CONFIG" ]; then
    for entry in "$REAL_CONFIG"/* "$REAL_CONFIG"/.[!.]*; do
      [ -e "$entry" ] || continue
      base="$(basename "$entry")"
      [ "$base" = "config.json" ] && continue
      ln -s "$entry" "$ISOLATED_CONFIG/$base" 2>/dev/null || true
    done
  fi

  # The one file we do not inherit: a config.json without credsStore/credHelpers
  # means credentials are written here in plaintext instead of the keychain.
  # Carry the active context over so a non-default builder still resolves.
  printf '{"currentContext":"%s"}\n' "$CURRENT_CONTEXT" > "$ISOLATED_CONFIG/config.json"

  export DOCKER_CONFIG="$ISOLATED_CONFIG"
  ecr_login
fi

# Fail loudly rather than silently falling back to `docker build`, which ignores
# --platform and would push an x86 image that AgentCore refuses to run.
if ! docker buildx version >/dev/null 2>&1; then
  echo "ERROR: 'docker buildx' is unavailable (DOCKER_CONFIG=${DOCKER_CONFIG:-$HOME/.docker})." >&2
  echo "       AgentCore Runtime requires linux/arm64 images, which needs buildx." >&2
  echo "       Install/enable Docker buildx, then re-run ./deploy.sh" >&2
  exit 1
fi

echo "Building $(basename "$(dirname "$DOCKERFILE")") for linux/arm64 — this is the slow step on an x86 host."

docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  -f "$DOCKERFILE" \
  -t "${REPO_URL}:${TAG}" \
  -t "${REPO_URL}:latest" \
  --push \
  .
