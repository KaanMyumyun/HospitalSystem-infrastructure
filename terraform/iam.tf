data "aws_iam_policy_document" "github_actions_assume_role" {
  for_each = {
    ecr_push   = "repo:${var.github_repository}:ref:refs/heads/${var.github_deploy_branch}"
    eks_deploy = "repo:${var.github_repository}:environment:${var.github_deploy_environment}"
  }

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [each.value]
    }
  }
}

resource "aws_iam_openid_connect_provider" "github_actions" {
  count = var.github_oidc_provider_arn == "" ? 1 : 0

  url = "https://token.actions.githubusercontent.com"

  client_id_list = ["sts.amazonaws.com"]

  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd"
  ]
}

resource "aws_iam_role" "ecr_push_hospitalsystem" {
  name                 = "ecr-push-hospitalsystem"
  path                 = "/"
  description          = "GitHub Actions OIDC role for pushing HospitalSystem images to ECR"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.github_actions_assume_role["ecr_push"].json
}

resource "aws_iam_role_policy" "ecr_push_hospitalsystem" {
  name = "ecr-push-hospitalsystem-repos"
  role = aws_iam_role.ecr_push_hospitalsystem.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowEcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "AllowPushToHospitalSystemRepos"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeRepositories",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart"
        ]
        Resource = [
          aws_ecr_repository.backend.arn,
          aws_ecr_repository.frontend.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role" "eks_deploy_hospitalsystem" {
  name                 = "eks-deploy-hospitalsystem"
  path                 = "/"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.github_actions_assume_role["eks_deploy"].json
}

resource "aws_iam_role_policy" "eks_deploy_hospitalsystem" {
  name = "eks-deploy-hospitalsystem-ssm-deploy"
  role = aws_iam_role.eks_deploy_hospitalsystem.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SendDeployDocumentToOpsInstance"
        Effect   = "Allow"
        Action   = "ssm:SendCommand"
        Resource = [aws_ssm_document.deploy.arn, aws_instance.ops.arn]
      },
      {
        Sid    = "FindOpsInstanceAndReadResults"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ssm:ListCommandInvocations",
          "ssm:ListCommands"
        ]
        Resource = "*"
      }
    ]
  })
}

data "aws_iam_policy_document" "ops_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ops" {
  name                 = local.ops_name
  path                 = "/"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.ops_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ops_ssm" {
  role       = aws_iam_role.ops.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "ops_ecr_describe" {
  name = "${local.ops_name}-ecr-describe"
  role = aws_iam_role.ops.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CheckReleaseImagesExist"
        Effect   = "Allow"
        Action   = "ecr:DescribeImages"
        Resource = [aws_ecr_repository.backend.arn, aws_ecr_repository.frontend.arn]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "ops" {
  name = local.ops_name
  role = aws_iam_role.ops.name
}
