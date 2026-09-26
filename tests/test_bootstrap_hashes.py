"""Check bootstrap trigger behavior with Terraform, without providers or AWS."""

import json
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase, main, skipUnless


ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = shutil.which("terraform")


@skipUnless(TERRAFORM, "Terraform is required to evaluate bootstrap triggers")
class BootstrapHashesTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for directory in ("ansible", "kubernetes", "scripts", "config"):
            shutil.copytree(ROOT / directory, self.root / directory)
        shutil.copy2(ROOT / "ansible.cfg", self.root / "ansible.cfg")
        self.module = self.root / "terraform"
        self.module.mkdir()
        # Evaluate the real trigger expressions in a providerless fixture.
        source = (ROOT / "terraform/locals.tf").read_text()
        expressions = source[source.index("  ansible_bootstrap_files ="):]
        (self.module / "main.tf").write_text("locals {\n" + expressions)

    def evaluate(self, expression):
        result = subprocess.run(
            [TERRAFORM, f"-chdir={self.module}", "console", "-no-color"],
            input=expression + "\n", text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)

    def fingerprint(self):
        return self.evaluate("local.ansible_bootstrap_files_hash")

    def assert_changes(self, paths, expected):
        baseline = self.fingerprint()
        for relative_path in paths:
            with self.subTest(path=relative_path):
                path = self.root / relative_path
                original = path.read_text()
                try:
                    path.write_text(original + "\n# trigger regression check\n")
                    self.assertEqual(self.fingerprint() != baseline, expected)
                finally:
                    path.write_text(original)

    def test_operational_playbooks_do_not_retrigger_bootstrap(self):
        self.assert_changes(
            [f"ansible/playbooks/{name}.yml" for name in (
                "deploy-image", "pause", "resume", "cleanup-kubernetes", "status",
            )],
            expected=False,
        )

    def test_generated_variables_are_not_file_triggers(self):
        baseline = self.fingerprint()
        generated = self.root / "ansible/group_vars/all/terraform.yml"
        generated.write_text("# These variables have their own Terraform trigger.\n")
        self.assertEqual(self.fingerprint(), baseline)

    def test_bootstrap_dependencies_retrigger_bootstrap(self):
        self.assert_changes(
            [
                "ansible.cfg",
                "ansible/inventory.ini",
                "ansible/group_vars/all/main.yml",
                "ansible/playbooks/apply-kubernetes.yml",
                "ansible/tasks/kubeconfig.yml",
                "kubernetes/backend/deployment.yaml.j2",
                "scripts/apply-workload.py",
                "scripts/backend-secret.py",
                "config/alb-alarms.json",
            ],
            expected=True,
        )

    def test_imported_playbooks_and_tasks_are_tracked(self):
        tracked = set(json.loads(self.evaluate("jsonencode(local.ansible_bootstrap_files)")))
        pending = ["ansible/playbooks/bootstrap.yml"]
        seen = set()
        while pending:
            relative_path = pending.pop()
            if relative_path in seen:
                continue
            seen.add(relative_path)
            self.assertIn(relative_path, tracked)
            path = self.root / relative_path
            imports = re.findall(
                r"ansible\.builtin\.(?:import_playbook|import_tasks|include_tasks):\s+([^\s]+)",
                path.read_text(),
            )
            for imported in imports:
                child = (path.parent / imported.strip("\"'")).resolve()
                pending.append(str(child.relative_to(self.root)))


if __name__ == "__main__":
    main()
