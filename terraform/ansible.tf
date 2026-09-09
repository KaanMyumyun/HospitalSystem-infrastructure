resource "terraform_data" "ansible_bootstrap" {
  count = var.run_ansible_bootstrap ? 1 : 0

  triggers_replace = {
    bootstrap_vars_hash = sha256(jsonencode(local.ansible_bootstrap_vars))
    files_hash          = local.ansible_bootstrap_files_hash
    cluster_name        = aws_eks_cluster.main.name
    region              = var.aws_region
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/.."
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      mkdir -p ansible
      cat > ansible/inventory.ini <<'INVENTORY'
      [local]
      localhost ansible_connection=local ansible_python_interpreter=/usr/bin/python3
      INVENTORY

      if [ -f .env.local ]; then
        set -a
        source .env.local
        set +a
      fi

      ansible-playbook ansible/playbooks/bootstrap.yml \
        -e '${jsonencode(local.ansible_bootstrap_vars)}'
    EOT
  }

  depends_on = [
    aws_eks_cluster.main,
    aws_eks_node_group.hospitalsystempr1,
    aws_iam_role.eks_deploy_hospitalsystem,
    aws_acm_certificate_validation.app,
    terraform_data.initial_ecr_image_push
  ]
}

resource "terraform_data" "kubernetes_cleanup" {
  count = var.run_ansible_bootstrap ? 1 : 0

  input = {
    cluster_name = aws_eks_cluster.main.name
    region       = var.aws_region
  }

  provisioner "local-exec" {
    when        = destroy
    working_dir = "${path.module}/.."
    interpreter = ["/bin/bash", "-c"]
    command     = "ansible-playbook ansible/playbooks/cleanup-kubernetes.yml || true"
  }

  depends_on = [
    aws_eks_cluster.main,
    aws_eks_node_group.hospitalsystempr1
  ]
}
