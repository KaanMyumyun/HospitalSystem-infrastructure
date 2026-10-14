# Checks and Tests

`.github/workflows/ci.yml` runs on every pull request and push to `main`. None
of it needs AWS credentials:

| Job | What it checks |
| --- | -------------- |
| No state or env files | No Terraform state or tfvars, `.env*` file other than `.env.example`, kubeconfig or generated file is tracked, or was added in any commit of the push or pull request. |
| Terraform | `fmt`, `validate` and TFLint, for `terraform/` and `terraform/bootstrap/`. |
| Ansible | A syntax check of every playbook, and `ansible-lint`. |
| Kubernetes manifests | Every template renders with stand-in values and passes kubeconform. |
| Shell scripts | ShellCheck on every tracked `*.sh`; warnings and errors fail. |
| Regression tests | Every `tests/test_*.py`. A skipped test fails the job. |
| Security scans | Checkov and Trivy on `terraform/`. Report only: findings are listed in the job summary and never fail it, and none are suppressed. |

Run the regression tests from the repository root:

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

A full run takes about two minutes. Some suites need `terraform`, `kubectl`,
and the Python packages `jmespath` and `PyYAML`. Without them that suite skips
itself; CI treats a skip as a failure.

The other checks locally (the workflow pins each tool's version):

```bash
./scripts/tf.sh fmt -check -recursive
./scripts/tf.sh validate
(cd terraform && tflint)
ansible-playbook --syntax-check ansible/playbooks/*.yml
ansible-lint ansible/
shellcheck --severity=warning $(git ls-files '*.sh')
checkov -d terraform --framework terraform --quiet --compact
trivy config terraform
```
