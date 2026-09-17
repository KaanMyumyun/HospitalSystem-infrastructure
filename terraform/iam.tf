data "aws_iam_policy_document" "github_actions_assume_role" {
  # The OIDC subject each role trusts. Docker Image CI is triggered by
  # workflow_run, which runs on the default branch. The deploy job uses a
  # GitHub environment, and a job with an environment gets the environment as
  # its subject instead of the branch.
  for_each = {
    ecr_push   = "repo:${var.github_repository}:ref:refs/heads/${var.github_deploy_branch}"
    eks_deploy = "repo:${var.github_repository}:environment:${var.github_deploy_environment}"
  }

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
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

data "aws_iam_policy_document" "eks_cluster_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "eks_node_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "load_balancer_controller_assume_role" {
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
      values   = ["system:serviceaccount:kube-system:aws-load-balancer-controller"]
    }
  }
}

data "aws_iam_policy_document" "vpc_flow_logs_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["06b25927c42a721631c1efd9431e648fa62e1e39"]
}

resource "aws_iam_openid_connect_provider" "github_actions" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = ["sts.amazonaws.com"]

  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd"
  ]
}

# Based on AWS's published controller policy, with every resource that supports
# it limited to this account, region and VPC. Only actions that have no
# resource-level permissions (Describe*, List*, Get*) keep "*".
#
# Inline, because the scoped ARNs push it past the 6,144-character limit for
# managed policies; inline role policies allow 10,240.
resource "aws_iam_role_policy" "load_balancer_controller" {
  name = "AWSLoadBalancerControllerIAMPolicy"
  role = aws_iam_role.load_balancer_controller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iam:CreateServiceLinkedRole"]
        Resource = "arn:aws:iam::${local.account_id}:role/aws-service-role/elasticloadbalancing.amazonaws.com/AWSServiceRoleForElasticLoadBalancing"
        Condition = {
          StringEquals = {
            "iam:AWSServiceName" = "elasticloadbalancing.amazonaws.com"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeAccountAttributes",
          "ec2:DescribeAddresses",
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeCoipPools",
          "ec2:DescribeInstances",
          "ec2:DescribeInternetGateways",
          "ec2:DescribeIpamPools",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeRouteTables",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSubnets",
          "ec2:DescribeTags",
          "ec2:DescribeVpcPeeringConnections",
          "ec2:DescribeVpcs",
          "ec2:GetCoipPoolUsage",
          "ec2:GetSecurityGroupsForVpc",
          "elasticloadbalancing:DescribeCapacityReservation",
          "elasticloadbalancing:DescribeListenerAttributes",
          "elasticloadbalancing:DescribeListenerCertificates",
          "elasticloadbalancing:DescribeListeners",
          "elasticloadbalancing:DescribeLoadBalancerAttributes",
          "elasticloadbalancing:DescribeLoadBalancers",
          "elasticloadbalancing:DescribeRules",
          "elasticloadbalancing:DescribeSSLPolicies",
          "elasticloadbalancing:DescribeTags",
          "elasticloadbalancing:DescribeTargetGroupAttributes",
          "elasticloadbalancing:DescribeTargetGroups",
          "elasticloadbalancing:DescribeTargetHealth",
          "elasticloadbalancing:DescribeTrustStores",
          "acm:ListCertificates",
          "iam:ListServerCertificates",
          "shield:GetSubscriptionState"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["acm:DescribeCertificate"]
        Resource = aws_acm_certificate.app.arn
      },
      {
        Effect   = "Allow"
        Action   = ["iam:GetServerCertificate"]
        Resource = "arn:aws:iam::${local.account_id}:server-certificate/*"
      },
      {
        Effect   = "Allow"
        Action   = ["cognito-idp:DescribeUserPoolClient"]
        Resource = "arn:aws:cognito-idp:${var.aws_region}:${local.account_id}:userpool/*"
      },
      {
        Effect   = "Allow"
        Action   = ["shield:CreateProtection", "shield:DeleteProtection", "shield:DescribeProtection"]
        Resource = concat(["arn:aws:shield::${local.account_id}:protection/*"], local.elb_arns)
      },
      {
        Effect = "Allow"
        Action = [
          "waf-regional:AssociateWebACL",
          "waf-regional:DisassociateWebACL",
          "waf-regional:GetWebACL",
          "waf-regional:GetWebACLForResource"
        ]
        Resource = concat(["arn:aws:waf-regional:${var.aws_region}:${local.account_id}:webacl/*"], local.elb_arns)
      },
      {
        Effect = "Allow"
        Action = [
          "wafv2:AssociateWebACL",
          "wafv2:DisassociateWebACL",
          "wafv2:GetWebACL",
          "wafv2:GetWebACLForResource"
        ]
        Resource = concat(["arn:aws:wafv2:${var.aws_region}:${local.account_id}:regional/webacl/*/*"], local.elb_arns)
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"]
        Resource = "${local.ec2_arn_prefix}:security-group/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateSecurityGroup"]
        Resource = ["${local.ec2_arn_prefix}:security-group/*", aws_vpc.kubes.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateTags"]
        Resource = "${local.ec2_arn_prefix}:security-group/*"
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = "CreateSecurityGroup"
          }
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateTags", "ec2:DeleteTags"]
        Resource = "${local.ec2_arn_prefix}:security-group/*"
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster"  = "true"
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:DeleteSecurityGroup"]
        Resource = "${local.ec2_arn_prefix}:security-group/*"
        Condition = {
          Null = {
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["elasticloadbalancing:CreateLoadBalancer", "elasticloadbalancing:CreateTargetGroup"]
        Resource = concat(local.elb_arns, local.target_grp_arns)
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:CreateListener",
          "elasticloadbalancing:CreateRule",
          "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:DeleteRule"
        ]
        Resource = concat(local.elb_arns, local.listener_arns, local.rule_arns)
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:RemoveTags"
        ]
        Resource = concat(local.elb_arns, local.target_grp_arns)
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster"  = "true"
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:AddListenerCertificates",
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:ModifyListener",
          "elasticloadbalancing:ModifyRule",
          "elasticloadbalancing:RemoveListenerCertificates",
          "elasticloadbalancing:RemoveTags",
          "elasticloadbalancing:SetRulePriorities",
          "elasticloadbalancing:SetWebAcl"
        ]
        Resource = concat(local.elb_arns, local.listener_arns, local.rule_arns, local.target_grp_arns)
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:DeleteLoadBalancer",
          "elasticloadbalancing:DeleteTargetGroup",
          "elasticloadbalancing:ModifyCapacityReservation",
          "elasticloadbalancing:ModifyIpPools",
          "elasticloadbalancing:ModifyListenerAttributes",
          "elasticloadbalancing:ModifyLoadBalancerAttributes",
          "elasticloadbalancing:ModifyTargetGroup",
          "elasticloadbalancing:ModifyTargetGroupAttributes",
          "elasticloadbalancing:SetIpAddressType",
          "elasticloadbalancing:SetSecurityGroups",
          "elasticloadbalancing:SetSubnets"
        ]
        Resource = concat(local.elb_arns, local.listener_arns, local.target_grp_arns)
        Condition = {
          Null = {
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["elasticloadbalancing:DeregisterTargets", "elasticloadbalancing:RegisterTargets"]
        Resource = local.target_grp_arns
      }
    ]
  })
}

resource "aws_iam_role" "load_balancer_controller" {
  name               = "AmazonEKSLoadBalancerControllerRole"
  path               = "/"
  assume_role_policy = data.aws_iam_policy_document.load_balancer_controller_assume_role.json
}

resource "aws_iam_role" "vpc_flow_logs" {
  name                 = "${local.vpc_name}-vpc-flow-logs"
  path                 = "/"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.vpc_flow_logs_assume_role.json
}

resource "aws_iam_role_policy" "vpc_flow_logs" {
  name = "${local.vpc_name}-vpc-flow-logs-write"
  role = aws_iam_role.vpc_flow_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteFlowLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.vpc_flow_logs.arn}:*"
      }
    ]
  })
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

# The EKS endpoint is private, so the deploy job doesn't call the Kubernetes
# API. It may only send the deploy document to the ops instance and read the
# result.
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

resource "aws_iam_instance_profile" "ops" {
  name = local.ops_name
  role = aws_iam_role.ops.name
}

resource "aws_iam_role" "eks_cluster" {
  name                 = "${local.cluster_name}-cluster-role"
  path                 = "/"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.eks_cluster_assume_role.json
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role" "eks_node" {
  name                 = "hospital-pr1-node-role"
  path                 = "/"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.eks_node_assume_role.json
}

resource "aws_iam_role_policy_attachment" "eks_node_worker" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "eks_node_cni" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "eks_node_ecr_readonly" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "eks_node_ecr_public_readonly" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonElasticContainerRegistryPublicReadOnly"
}
