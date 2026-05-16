#!/usr/bin/env bash
# Build, package, and deploy the Elden Ring agent CloudFormation stack.
#
# Usage:
#   scripts/deploy.sh [KNOWLEDGE_BASE_ID]
#
# If KNOWLEDGE_BASE_ID is omitted, the existing stack parameter is reused
# (or PLACEHOLDER on first deploy).
set -euo pipefail

# Load local config (gitignored). See .env.example for required keys.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${ARTIFACTS_BUCKET:?ARTIFACTS_BUCKET must be set (in .env or environment)}"
STACK_NAME="${STACK_NAME:-elden-ring-agent}"
KB_ID="${1:-${KB_ID:-}}"

# sam ships as sam.cmd on Windows; prefer it if present.
if command -v sam.cmd >/dev/null 2>&1; then
  SAM=sam.cmd
else
  SAM=sam
fi

echo "==> sam build"
"$SAM" build --template-file infra/template.yaml

echo "==> aws cloudformation package"
aws cloudformation package \
  --template-file .aws-sam/build/template.yaml \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --output-template-file infra/packaged.yaml

echo "==> aws cloudformation deploy"
DEPLOY_ARGS=(
  --template-file infra/packaged.yaml
  --stack-name "$STACK_NAME"
  --capabilities CAPABILITY_NAMED_IAM
)
if [[ -n "$KB_ID" ]]; then
  DEPLOY_ARGS+=(--parameter-overrides "KnowledgeBaseId=$KB_ID")
fi
aws cloudformation deploy "${DEPLOY_ARGS[@]}"

echo "==> done"
