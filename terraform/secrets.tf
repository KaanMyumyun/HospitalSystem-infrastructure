# The backend's connection string and JWT key. Terraform creates the secret
# but never sets its value, so the value stays out of Terraform state:
# ansible/playbooks/backend-secret.yml writes it from the local environment,
# and External Secrets copies it into the cluster.
resource "aws_secretsmanager_secret" "backend" {
  name        = "${var.project_name}/backend"
  description = "HospitalSystem backend connection string and JWT key, synced into EKS by External Secrets"
  kms_key_id  = aws_kms_key.main.arn

  # Delete at once on destroy, so a rebuild can create the same name again.
  recovery_window_in_days = 0
}

locals {
  backend_secrets_reader_service_account = "backend-secrets-reader"
}

# External Secrets requests a token for this namespace's service account and
# assumes the role with it. The controller itself has no AWS permissions.
data "aws_iam_policy_document" "backend_secrets_reader_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub"
      values   = ["system:serviceaccount:${local.k8s_namespace}:${local.backend_secrets_reader_service_account}"]
    }
  }
}

resource "aws_iam_role" "backend_secrets_reader" {
  name               = "${local.cluster_name}-backend-secrets-reader"
  path               = "/"
  assume_role_policy = data.aws_iam_policy_document.backend_secrets_reader_assume_role.json
}

resource "aws_iam_role_policy" "backend_secrets_reader" {
  name = "read-backend-secret"
  role = aws_iam_role.backend_secrets_reader.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadBackendSecret"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = aws_secretsmanager_secret.backend.arn
      },
      {
        Sid      = "DecryptThroughSecretsManager"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = aws_kms_key.main.arn
        Condition = {
          StringEquals = {
            "kms:ViaService" = "secretsmanager.${var.aws_region}.amazonaws.com"
          }
        }
      }
    ]
  })
}
