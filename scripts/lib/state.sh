#!/usr/bin/env bash
# The S3 bucket that holds Terraform's state lives only as long as the stack:
# ./scripts/tf.sh creates it (terraform/bootstrap) before the first apply, and
# scripts/teardown.sh deletes it once the state is empty. Source after setting
# REPO_ROOT.
BOOTSTRAP_DIR="$REPO_ROOT/terraform/bootstrap"

# state_bucket ACCOUNT_ID: bucket names are global, so the account ID keeps
# this one unique.
state_bucket() {
  printf 'hospitalsystem-tfstate-%s' "$1"
}

# state_bucket_exists BUCKET: 0 when it exists, 1 when it doesn't, 2 when the
# buckets can't be listed.
state_bucket_exists() {
  local found
  found="$(aws s3api list-buckets --query "Buckets[?Name=='$1'].Name" --output text)" || return 2
  [ "$found" = "$1" ]
}

create_state_bucket() {
  terraform -chdir="$BOOTSTRAP_DIR" init -input=false >/dev/null \
    && terraform -chdir="$BOOTSTRAP_DIR" apply -auto-approve -input=false -var "bucket_name=$1"
}

# Deletes every state version in the bucket too (force_destroy).
delete_state_bucket() {
  terraform -chdir="$BOOTSTRAP_DIR" init -input=false >/dev/null \
    && terraform -chdir="$BOOTSTRAP_DIR" destroy -auto-approve -input=false -var "bucket_name=$1"
}

# The bucket terraform init last set up in terraform/, from the backend
# settings it saved; nothing before the first init.
backend_bucket() {
  python3 - "$REPO_ROOT/terraform/.terraform/terraform.tfstate" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as saved:
        backend = json.load(saved).get("backend") or {}
except FileNotFoundError:
    sys.exit()
if backend.get("type") == "s3":
    print((backend.get("config") or {}).get("bucket") or "")
PY
}

# ensure_backend BUCKET: runs terraform init in terraform/ unless it already
# uses BUCKET. A different bucket is another account's, so its state is left
# where it is (-reconfigure) rather than copied.
ensure_backend() {
  local current
  current="$(backend_bucket)"
  [ "$current" != "$1" ] || return 0
  terraform -chdir="$REPO_ROOT/terraform" init -input=false -backend-config="bucket=$1" ${current:+-reconfigure}
}
