"""Which image a Deployment runs and which commit it was built from. Used by
image-check.py.
"""

from datetime import timezone
import re

from lib.checks import parse_time
from lib.commands import DROP, fields, has_command, last_line, run

# Tags naming a commit: <date>-<sha> from the bootstrap push (-dirty when its
# checkout had uncommitted changes), <date>-<sha>-<run>-<attempt> from Docker
# Image CI (<date>-<sha>-<run> before it added the attempt).
IMAGE_REVISION_TAG = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-([0-9a-f]{7})(-dirty)?(-[0-9]+){0,2}")

# For <sha>...main, "ahead" means main has commits the image doesn't, and
# files are what those commits change.
COMPARE_JQ = ('[.status, .ahead_by, .behind_by, (.files | length), ([.files[:5][].filename] | join(", ")), '
              '((.commits[-1].sha // "")[0:7])] | @tsv')


def check_image(checks, deployment, namespace, github_repository):
    """Report the image deployment runs, whether its pods run it, and how the
    commit it was built from compares with main."""
    image = checks.kube("get", "deployment", deployment, "-n", namespace,
                        "-o", "jsonpath={.spec.template.spec.containers[0].image}")
    if not image.ok:
        checks.fail(f"Could not read the image {deployment} runs: {last_line(image.out)}")
        return
    if not image.out:
        checks.fail(f"Deployment {deployment} has no container image")
        return
    image = image.out
    repo, _, tag = image.rpartition(":")
    # No tag, or the only colon is a registry port.
    if ":" not in image.rsplit("/", 1)[-1]:
        repo, tag = image, "latest"
    name = repo.rsplit("/", 1)[-1]

    row = checks.aws("ecr", "describe-images", "--repository-name", repo.split("/", 1)[-1],
                     "--image-ids", f"imageTag={tag}",
                     "--query", "imageDetails[0].[imageDigest, imagePushedAt, join(`,`, imageTags)]",
                     "--output", "text")
    if not row.ok:
        checks.fail(f"Could not find {deployment}'s image {name}:{tag} in ECR: {last_line(row.out)}")
        return
    digest, pushed, tags = fields(row.out, 3)
    moment = parse_time(pushed)
    if moment:
        pushed = moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    checks.note(f"{deployment}: {name}:{tag}, pushed {pushed}, tags {tags.replace(',', ', ')}")

    # The tag can move (latest does on every push), so compare what the pods run.
    pods = checks.kube("get", "pods", "-n", namespace, "-l", f"app.kubernetes.io/name={deployment}", "-o",
                       r'jsonpath={range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[0].imageID}{"\n"}{end}')
    if not pods.ok:
        checks.fail(f"Could not list the {deployment} pods: {last_line(pods.out)}")
    elif not pods.out:
        checks.warn(f"{deployment} has no pods, so no image is running")
    else:
        lines = pods.out.split("\n")
        matching = 0
        for line in lines:
            pod, image_id = fields(line, 2)
            running = image_id.rsplit("@", 1)[-1]
            if not image_id:
                checks.warn(f"Pod {pod} hasn't started its container yet")
            elif running != digest:
                checks.fail(f"Pod {pod} runs {running}, but {tag} now points to {digest} "
                            "(a rollout in progress, or the tag moved)")
            else:
                matching += 1
        if matching == len(lines):
            checks.ok(f"All {len(lines)} {deployment} pod(s) run {tag} ({digest[:19]})")

    sha = dirty = None
    for candidate in [tag, *(tags.split(",") if tags else [])]:
        match = IMAGE_REVISION_TAG.fullmatch(candidate)
        if match:
            sha, dirty = match.group(1, 2)
            break
    if not sha:
        checks.warn(f"No tag of {deployment}'s image names the commit it was built from")
        return
    if dirty:
        checks.warn(f"{deployment} was built from {sha} plus uncommitted changes, so its exact code isn't in Git")

    if not github_repository:
        checks.skip(f"No GitHub repository configured to compare {sha} with main")
        return
    if not has_command("gh"):
        checks.skip(f"gh is not installed, so {sha} isn't compared with main")
        return
    if not run(["gh", "auth", "status"], stderr=DROP).ok:
        checks.skip(f"gh is not logged in (gh auth login), so {sha} isn't compared with main")
        return
    compare = run(["gh", "api", f"repos/{github_repository}/compare/{sha}...main", "--jq", COMPARE_JQ])
    if not compare.ok:
        checks.warn(f"Could not compare {deployment}'s commit {sha} with main: {last_line(compare.out)}")
        return
    status, ahead, behind, files, changed, main = fields(compare.out, 6)
    if status == "identical":
        checks.ok(f"{deployment} was built from {sha}, the tip of main")
    elif status == "ahead" and files == "0":
        checks.ok(f"{deployment} was built from {sha}; main ({main}) is {ahead} commit(s) newer, "
                  "but none change files")
    elif status == "ahead":
        checks.warn(f"{deployment} was built from {sha}; main ({main}) has {ahead} newer commit(s) "
                    f"changing {files} file(s): {changed}")
    elif status == "behind":
        checks.warn(f"{deployment} was built from {sha}, {behind} commit(s) ahead of main (not merged yet)")
    else:
        checks.warn(f"{deployment} was built from {sha}, which isn't on main "
                    f"({status}: {ahead} new on main, {behind} only in the image)")
