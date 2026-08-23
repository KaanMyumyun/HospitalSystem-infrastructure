resource "terraform_data" "ansible_bootstrap" {
  count = var.run_ansible_bootstrap ? 1 : 0

  input = {
    always_run   = timestamp()
    cluster_name = aws_eks_cluster.main.name
    region       = var.aws_region
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

      ansible-playbook ansible/playbooks/bootstrap.yml
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    working_dir = "${path.module}/.."
    interpreter = ["/bin/bash", "-c"]
    command     = "ansible-playbook ansible/playbooks/cleanup-kubernetes.yml || true"
  }

  depends_on = [
    aws_eks_cluster.main,
    aws_eks_node_group.hospitalsystempr1,
    aws_iam_role.eks_deploy_hospitalsystem
  ]
}
