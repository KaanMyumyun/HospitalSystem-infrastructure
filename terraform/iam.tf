data "aws_iam_policy_document" "github_actions_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:*"]
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
      identifiers = ["arn:aws:iam::${local.account_id}:oidc-provider/oidc.eks.${var.aws_region}.amazonaws.com/id/A6FD7C80562D56D9FB37DD3E1F8A8DF6"]
    }

    condition {
      test     = "StringEquals"
      variable = "oidc.eks.${var.aws_region}.amazonaws.com/id/A6FD7C80562D56D9FB37DD3E1F8A8DF6:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "oidc.eks.${var.aws_region}.amazonaws.com/id/A6FD7C80562D56D9FB37DD3E1F8A8DF6:sub"
      values   = ["system:serviceaccount:kube-system:aws-load-balancer-controller"]
    }
  }
}

resource "aws_iam_policy" "load_balancer_controller" {
  name = "AWSLoadBalancerControllerIAMPolicy"
  path = "/"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iam:CreateServiceLinkedRole"]
        Resource = "*"
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
          "elasticloadbalancing:DescribeTrustStores"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "acm:DescribeCertificate",
          "acm:ListCertificates",
          "cognito-idp:DescribeUserPoolClient",
          "iam:GetServerCertificate",
          "iam:ListServerCertificates",
          "shield:CreateProtection",
          "shield:DeleteProtection",
          "shield:DescribeProtection",
          "shield:GetSubscriptionState",
          "waf-regional:AssociateWebACL",
          "waf-regional:DisassociateWebACL",
          "waf-regional:GetWebACL",
          "waf-regional:GetWebACLForResource",
          "wafv2:AssociateWebACL",
          "wafv2:DisassociateWebACL",
          "wafv2:GetWebACL",
          "wafv2:GetWebACLForResource"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateSecurityGroup"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateTags"]
        Resource = "arn:aws:ec2:*:*:security-group/*"
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
        Resource = "arn:aws:ec2:*:*:security-group/*"
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster"  = "true"
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:AuthorizeSecurityGroupIngress", "ec2:DeleteSecurityGroup", "ec2:RevokeSecurityGroupIngress"]
        Resource = "*"
        Condition = {
          Null = {
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["elasticloadbalancing:CreateLoadBalancer", "elasticloadbalancing:CreateTargetGroup"]
        Resource = "*"
        Condition = {
          Null = {
            "aws:RequestTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["elasticloadbalancing:CreateListener", "elasticloadbalancing:CreateRule", "elasticloadbalancing:DeleteListener", "elasticloadbalancing:DeleteRule"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:RemoveTags"
        ]
        Resource = [
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
          "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*"
        ]
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
        Resource = "*"
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
        Resource = "*"
        Condition = {
          Null = {
            "aws:ResourceTag/elbv2.k8s.aws/cluster" = "false"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["elasticloadbalancing:DeregisterTargets", "elasticloadbalancing:RegisterTargets"]
        Resource = "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*"
      }
    ]
  })
}

resource "aws_iam_role" "load_balancer_controller" {
  name               = "AmazonEKSLoadBalancerControllerRole"
  path               = "/"
  assume_role_policy = data.aws_iam_policy_document.load_balancer_controller_assume_role.json
}

resource "aws_iam_role_policy_attachment" "load_balancer_controller" {
  role       = aws_iam_role.load_balancer_controller.name
  policy_arn = aws_iam_policy.load_balancer_controller.arn
}

resource "aws_iam_role" "ecr_push_hospitalsystem" {
  name                 = "ecr-push-hospitalsystem"
  path                 = "/"
  description          = "GitHub Actions OIDC role for pushing HospitalSystem images to ECR"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.github_actions_assume_role.json
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
          "arn:aws:ecr:${var.aws_region}:${local.account_id}:repository/hospital-backend",
          "arn:aws:ecr:${var.aws_region}:${local.account_id}:repository/hospital-frontend"
        ]
      }
    ]
  })
}

resource "aws_iam_role" "eks_deploy_hospitalsystem" {
  name                 = "eks-deploy-hospitalsystem"
  path                 = "/"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.github_actions_assume_role.json
}

resource "aws_iam_role_policy" "eks_deploy_hospitalsystem" {
  name = "eks-deploy-hospitalsystem-cluster-describe"
  role = aws_iam_role.eks_deploy_hospitalsystem.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowEksDescribeCluster"
        Effect   = "Allow"
        Action   = "eks:DescribeCluster"
        Resource = aws_eks_cluster.main.arn
      }
    ]
  })
}

resource "aws_iam_role" "eks_cluster" {
  name                 = "eks-pr1-cluster-role"
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
