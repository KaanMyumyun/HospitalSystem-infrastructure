data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  deploy_script = <<-EOT
    set -euo pipefail
    export IMAGE_TAG='{{ ImageTag }}'
    EKS_TOKEN="$(aws eks get-token --region '${var.aws_region}' --cluster-name '${aws_eks_cluster.main.name}' --query status.token --output text)"
    export EKS_TOKEN
    python3 - <<'PY'
    import base64
    import json
    import os
    import ssl
    import sys
    import time
    import urllib.error
    import urllib.request

    SERVER = "${aws_eks_cluster.main.endpoint}"
    NAMESPACE = "${local.k8s_namespace}"
    CA = base64.b64decode("${aws_eks_cluster.main.certificate_authority[0].data}").decode()
    TAG = os.environ["IMAGE_TAG"]
    DEPLOYMENTS = {
        "hospital-backend": ("backend", "${aws_ecr_repository.backend.repository_url}"),
        "hospital-frontend": ("frontend", "${aws_ecr_repository.frontend.repository_url}"),
    }
    CONTEXT = ssl.create_default_context(cadata=CA)


    def call(method, name, body=None):
        url = f"{SERVER}/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}"
        request = urllib.request.Request(url, method=method)
        request.add_header("Authorization", "Bearer " + os.environ["EKS_TOKEN"])
        if body is not None:
            request.data = json.dumps(body).encode()
            request.add_header("Content-Type", "application/strategic-merge-patch+json")
        try:
            with urllib.request.urlopen(request, context=CONTEXT, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            sys.exit(f"{method} {name}: HTTP {error.code}: {error.read().decode()}")


    scaled_up = False
    for name, (container, repository) in DEPLOYMENTS.items():
        container_patch = {"name": container, "image": f"{repository}:{TAG}"}
        patch = {"spec": {"template": {"spec": {"containers": [container_patch]} } } }
        deployment = call("PATCH", name, patch)
        scaled_up = scaled_up or deployment["spec"].get("replicas", 0) > 0
        print(f"{name}: {container} image set to {repository}:{TAG}")

    if not scaled_up:
        print(f"Both Deployments are scaled to 0. The next scale-up runs {TAG}.")
        sys.exit(0)

    deadline = time.monotonic() + 360
    for name in DEPLOYMENTS:
        while True:
            deployment = call("GET", name)
            want = deployment["spec"].get("replicas", 0)
            status = deployment.get("status", {})
            if any(c.get("reason") == "ProgressDeadlineExceeded" for c in status.get("conditions", [])):
                sys.exit(f"{name}: rollout exceeded its progress deadline")
            if (
                status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
                and status.get("updatedReplicas", 0) == want
                and status.get("replicas", 0) == want
                and status.get("availableReplicas", 0) == want
            ):
                print(f"{name}: rollout complete")
                break
            if time.monotonic() > deadline:
                sys.exit(f"{name}: rollout did not finish within 6 minutes")
            time.sleep(5)
    PY
  EOT
}

resource "aws_instance" "ops" {
  ami                         = data.aws_ssm_parameter.al2023_ami.insecure_value
  instance_type               = "t3.micro"
  subnet_id                   = aws_subnet.private_a.id
  vpc_security_group_ids      = [aws_security_group.ops.id]
  iam_instance_profile        = aws_iam_instance_profile.ops.name
  associate_public_ip_address = false
  monitoring                  = true
  ebs_optimized               = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
    kms_key_id  = aws_kms_key.main.arn
  }

  tags = {
    Name = local.ops_name
  }

  lifecycle {
    ignore_changes = [ami]
  }

  depends_on = [aws_vpc_endpoint.ssm]
}

resource "aws_eks_access_entry" "ops" {
  cluster_name      = aws_eks_cluster.main.name
  principal_arn     = aws_iam_role.ops.arn
  kubernetes_groups = [local.k8s_deploy_group]
  type              = "STANDARD"
}

resource "aws_ssm_document" "deploy" {
  name            = "${var.project_name}-deploy"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Deploy one image tag to the HospitalSystem backend and frontend Deployments."
    parameters = {
      ImageTag = {
        type           = "String"
        description    = "Image tag present in both ECR repositories."
        allowedPattern = "^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$"
      }
    }
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "deploy"
        inputs = {
          timeoutSeconds = "600"
          runCommand     = split("\n", trimspace(local.deploy_script))
        }
      }
    ]
  })
}
