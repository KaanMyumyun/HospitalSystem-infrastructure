resource "aws_iam_service_linked_role" "eks" {
  aws_service_name = "eks.amazonaws.com"
  description      = "Allows Amazon EKS to call AWS services on your behalf."
}

resource "aws_iam_service_linked_role" "eks_nodegroup" {
  aws_service_name = "eks-nodegroup.amazonaws.com"
  description      = "This policy allows Amazon EKS to create and manage Nodegroups"
}

resource "aws_iam_service_linked_role" "autoscaling" {
  aws_service_name = "autoscaling.amazonaws.com"
  description      = "Default Service-Linked Role enables access to AWS Services and Resources used or managed by Auto Scaling"
}

resource "aws_iam_service_linked_role" "cost_optimization_hub" {
  aws_service_name = "cost-optimization-hub.bcm.amazonaws.com"
  description      = "Allows Cost Optimization Hub to retrieve organization information and collect optimization-related data and metadata."
}

resource "aws_iam_service_linked_role" "elasticloadbalancing" {
  aws_service_name = "elasticloadbalancing.amazonaws.com"
  description      = "Allows ELB to call AWS services on your behalf."
}

resource "aws_iam_service_linked_role" "resource_explorer" {
  aws_service_name = "resource-explorer-2.amazonaws.com"
}

resource "aws_iam_service_linked_role" "support" {
  aws_service_name = "support.amazonaws.com"
  description      = "Enables resource access for AWS to provide billing, administrative and support services"
}

resource "aws_iam_service_linked_role" "trusted_advisor" {
  aws_service_name = "trustedadvisor.amazonaws.com"
  description      = "Access for the AWS Trusted Advisor Service to help reduce cost, increase performance, and improve security of your AWS environment."
}
