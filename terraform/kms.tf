# Customer-managed key for ECR images, Kubernetes Secrets and CloudWatch log
# groups. Every decrypt is recorded in CloudTrail, and access can be revoked by
# editing this policy.
resource "aws_kms_key" "main" {
  description             = "HospitalSystem encryption key for ECR, EKS Secrets and CloudWatch Logs"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action = [
          "kms:Decrypt*",
          "kms:Describe*",
          "kms:Encrypt*",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:*"
          }
        }
      },
      {
        Sid       = "EksSecretsEncryption"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.eks_cluster.arn }
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:Encrypt"
        ]
        Resource = "*"
      },
      {
        Sid       = "EksSecretsGrants"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.eks_cluster.arn }
        Action    = "kms:CreateGrant"
        Resource  = "*"
        Condition = {
          Bool = {
            "kms:GrantIsForAWSResource" = "true"
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-Terraform"
  }
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.project_name}"
  target_key_id = aws_kms_key.main.key_id
}
