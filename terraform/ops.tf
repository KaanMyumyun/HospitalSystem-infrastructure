data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  # scripts/deploy-release.py reads these on the ops instance.
  deploy_settings = {
    AWS_REGION          = var.aws_region
    EKS_CLUSTER         = aws_eks_cluster.main.name
    EKS_SERVER          = aws_eks_cluster.main.endpoint
    EKS_CA              = aws_eks_cluster.main.certificate_authority[0].data
    K8S_NAMESPACE       = local.k8s_namespace
    ECR_ENDPOINT        = "https://${aws_vpc_endpoint.ecr_api.dns_entry[0].dns_name}"
    BACKEND_REPOSITORY  = aws_ecr_repository.backend.repository_url
    FRONTEND_REPOSITORY = aws_ecr_repository.frontend.repository_url
  }

  # One command per line. An indented heredoc would also strip the script's
  # own indentation, so the script's lines are appended as they are.
  deploy_commands = concat(
    ["set -euo pipefail", "export IMAGE_TAG='{{ ImageTag }}'"],
    [for name, value in local.deploy_settings : "export ${name}='${value}'"],
    ["python3 - <<'PY'"],
    split("\n", trimspace(file("${path.module}/../scripts/deploy-release.py"))),
    ["PY"],
  )
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
    description   = "Deploy one image tag to the HospitalSystem backend and frontend, smoke test it, and roll back on failure."
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
          runCommand     = local.deploy_commands
        }
      }
    ]
  })
}
