#!/usr/bin/env bash
# Which image a Deployment runs and which commit it was built from. Used by
# scripts/image-check.sh and the ecr section of scripts/monitoring.sh.
# Source after defining ok, warn, fail, skip, note, kube, awsr and last_line,
# and setting NAMESPACE and GITHUB_REPOSITORY.

# Tags naming a commit: <date>-<sha> from the bootstrap push (-dirty when its
# checkout had uncommitted changes), <date>-<sha>-<run> from Docker Image CI.
IMAGE_REVISION_TAG='^[0-9]{4}-[0-9]{2}-[0-9]{2}-([0-9a-f]{7})(-dirty)?(-[0-9]+)?$'

check_image() {
  local deployment="$1" image repo tag pods pod image_id count matching digest row pushed tags
  local candidate sha="" dirty="" compare status ahead behind files changed main
  local -a tag_list

  if ! image="$(kube get deployment "$deployment" -n "$NAMESPACE" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>&1)"; then
    fail "Could not read the image $deployment runs: $(last_line "$image")"
    return
  elif [ -z "$image" ]; then
    fail "Deployment $deployment has no container image"
    return
  fi
  repo="${image%:*}"
  tag="${image##*:}"
  if [[ "${image##*/}" != *:* ]]; then
    repo="$image"
    tag=latest
  fi

  if ! row="$(
    awsr ecr describe-images --repository-name "${repo#*/}" --image-ids "imageTag=$tag" \
      --query 'imageDetails[0].[imageDigest, imagePushedAt, join(`,`, imageTags)]' \
      --output text 2>&1
  )"; then
    fail "Could not find $deployment's image ${repo##*/}:$tag in ECR: $(last_line "$row")"
    return
  fi
  IFS=$'\t' read -r digest pushed tags <<<"$row"
  pushed="$(date -u -d "$pushed" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || printf '%s' "$pushed")"
  note "$deployment: ${repo##*/}:$tag, pushed $pushed, tags ${tags//,/, }"

  # The tag can move (latest does on every push), so compare what the pods run.
  if ! pods="$(
    kube get pods -n "$NAMESPACE" -l "app.kubernetes.io/name=$deployment" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[0].imageID}{"\n"}{end}' 2>&1
  )"; then
    fail "Could not list the $deployment pods: $(last_line "$pods")"
  elif [ -z "$pods" ]; then
    warn "$deployment has no pods, so no image is running"
  else
    count=0
    matching=0
    while IFS=$'\t' read -r pod image_id; do
      count=$((count + 1))
      if [ -z "$image_id" ]; then
        warn "Pod $pod hasn't started its container yet"
      elif [ "${image_id##*@}" != "$digest" ]; then
        fail "Pod $pod runs ${image_id##*@}, but $tag now points to $digest (a rollout in progress, or the tag moved)"
      else
        matching=$((matching + 1))
      fi
    done <<<"$pods"
    if [ "$matching" -eq "$count" ]; then
      ok "All $count $deployment pod(s) run $tag (${digest:0:19})"
    fi
  fi

  IFS=, read -ra tag_list <<<"$tags"
  for candidate in "$tag" "${tag_list[@]}"; do
    if [[ "$candidate" =~ $IMAGE_REVISION_TAG ]]; then
      sha="${BASH_REMATCH[1]}"
      dirty="${BASH_REMATCH[2]}"
      break
    fi
  done
  if [ -z "$sha" ]; then
    warn "No tag of $deployment's image names the commit it was built from"
    return
  fi
  if [ -n "$dirty" ]; then
    warn "$deployment was built from $sha plus uncommitted changes, so its exact code isn't in Git"
  fi

  if [ -z "${GITHUB_REPOSITORY:-}" ]; then
    skip "No GitHub repository configured to compare $sha with main"
    return
  elif ! command -v gh >/dev/null 2>&1; then
    skip "gh is not installed, so $sha isn't compared with main"
    return
  elif ! gh auth status >/dev/null 2>&1; then
    skip "gh is not logged in (gh auth login), so $sha isn't compared with main"
    return
  fi
  # For <sha>...main, "ahead" means main has commits the image doesn't, and
  # files are what those commits change.
  if ! compare="$(
    gh api "repos/$GITHUB_REPOSITORY/compare/$sha...main" \
      --jq '[.status, .ahead_by, .behind_by, (.files | length), ([.files[:5][].filename] | join(", ")), ((.commits[-1].sha // "")[0:7])] | @tsv' 2>&1
  )"; then
    warn "Could not compare $deployment's commit $sha with main: $(last_line "$compare")"
    return
  fi
  IFS=$'\t' read -r status ahead behind files changed main <<<"$compare"
  case "$status" in
    identical)
      ok "$deployment was built from $sha, the tip of main"
      ;;
    ahead)
      if [ "$files" = 0 ]; then
        ok "$deployment was built from $sha; main ($main) is $ahead commit(s) newer, but none change files"
      else
        warn "$deployment was built from $sha; main ($main) has $ahead newer commit(s) changing $files file(s): $changed"
      fi
      ;;
    behind)
      warn "$deployment was built from $sha, $behind commit(s) ahead of main (not merged yet)"
      ;;
    *)
      warn "$deployment was built from $sha, which isn't on main ($status: $ahead new on main, $behind only in the image)"
      ;;
  esac
}
