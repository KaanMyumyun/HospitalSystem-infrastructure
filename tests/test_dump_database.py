"""Run ansible/playbooks/dump-database.yml in a copy of the repo with a fake kubectl.

The fake logs every call with its stdin and plays the pod, psql and pg_dump,
so the tests check when seed.sql is replaced and when it's left alone. The SSM
tunnel is skipped (kubeconfig_configured); nothing reaches AWS or a cluster.
"""

from pathlib import Path
from unittest import TestCase, main, skipUnless
import json
import os
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")
OLD_SEED = "-- the seed.sql from before\n"
TERRAFORM_VARS = """db_host: hospitalsystem-db.abc123.eu-north-1.rds.amazonaws.com
db_port: 5432
db_name: hospital
db_username: postgres
k8s_namespace: hospitalsystem
"""

# The shape of a pg_dump 18 plain dump.
DUMP = """--
-- PostgreSQL database dump
--

\\restrict 4fKq

-- Dumped from database version 18.1
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;

CREATE TABLE public.patients (
    id integer NOT NULL,
    name text NOT NULL
);

COPY public.patients (id, name) FROM stdin;
1\tAyse Demir
\\.

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT patients_pkey PRIMARY KEY (id);

--
-- PostgreSQL database dump complete
--

\\unrestrict 4fKq

"""

# The pod exists from kubectl run until kubectl delete.
# FAKE_NODES: what get nodes prints. FAKE_TABLES: the table count, or "fail"
# when psql can't connect. FAKE_DUMP: "complete", "cut" (stops before the end
# but exits 0) or "fail" (stops and exits 1).
FAKE_KUBECTL = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
stdin = sys.stdin.read() if "--stdin" in args else ""
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "stdin": stdin, "kubeconfig": os.environ.get("KUBECONFIG")}) + "\n")
pod = Path(os.environ["FAKE_LOG"] + ".pod")
if args[:2] == ["get", "nodes"]:
    print(os.environ.get("FAKE_NODES", "node/ip-10-0-1-10"))
elif args[:2] == ["delete", "pod"]:
    if pod.exists():
        pod.unlink()
        print(f'pod "{args[2]}" deleted')
elif args[0] == "run":
    if pod.exists():
        print(f'Error from server (AlreadyExists): pods "{args[1]}" already exists', file=sys.stderr)
        sys.exit(1)
    pod.touch()
    print(f"pod/{args[1]} created")
elif args[0] == "wait":
    sys.exit(0 if pod.exists() else 1)
elif args[0] == "exec" and "pg_dump" in args[-1]:
    mode = os.environ.get("FAKE_DUMP", "complete")
    sys.stdout.write(DUMP if mode == "complete" else DUMP.split("--\n-- PostgreSQL database dump complete")[0])
    if mode == "fail":
        print("pg_dump: error: SSL connection has been closed unexpectedly", file=sys.stderr)
        sys.exit(1)
elif args[0] == "exec":
    tables = os.environ.get("FAKE_TABLES", "4")
    if tables == "fail":
        print('psql: error: password authentication failed for user "postgres"', file=sys.stderr)
        sys.exit(2)
    print(tables)
else:
    sys.exit(2)
'''


@skipUnless(ANSIBLE_PLAYBOOK, "ansible-playbook is not installed")
class DumpDatabaseTests(TestCase):
    def run_playbook(self, *, seed=OLD_SEED, password="fake-password", leftover_pod=False, env=None):
        """Runs the playbook in a fake repo. seed: seed.sql before the run; None for none."""
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = tmp / "repo"
        shutil.copytree(ROOT / "ansible", repo / "ansible",
                        ignore=shutil.ignore_patterns("terraform.yml", "__pycache__"))
        shutil.copy(ROOT / "ansible.cfg", repo / "ansible.cfg")
        (repo / "ansible/group_vars/all/terraform.yml").write_text(TERRAFORM_VARS)
        if seed is not None:
            (repo / "seed.sql").write_text(seed)

        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        kubectl.write_text(f"#!{sys.executable}\nDUMP = {json.dumps(DUMP)}\n{FAKE_KUBECTL}")
        kubectl.chmod(0o755)
        self.log = tmp / "kubectl.log"
        self.log.touch()
        self.pod = Path(f"{self.log}.pod")
        if leftover_pod:
            self.pod.touch()
        self.repo = repo

        environment = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp), "LC_ALL": "C.UTF-8",
                       "FAKE_LOG": str(self.log), **(env or {})}
        if password:
            environment["HOSPITALSYSTEM_DB_PASSWORD"] = password
        # kubeconfig_configured skips tasks/kubeconfig.yml and its SSM tunnel.
        return subprocess.run(
            [ANSIBLE_PLAYBOOK, "ansible/playbooks/dump-database.yml", "-e", '{"kubeconfig_configured": true}'],
            cwd=repo, env=environment, capture_output=True, text=True, timeout=120,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def steps(self):
        """Each kubectl call in a word or two; exec as what it ran."""
        steps = []
        for args in (call["args"] for call in self.calls()):
            if args[0] == "exec":
                steps.append("exec pg_dump" if "pg_dump" in args[-1] else "exec psql")
            else:
                steps.append(" ".join(args[:2]) if args[0] in ("get", "delete") else args[0])
        return steps

    def seed(self):
        return (self.repo / "seed.sql").read_text()

    def backups(self):
        return sorted((self.repo / ".generated/seed-backups").glob("*"))

    def assertOk(self, result, ok=True):
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)

    def assertCleanedUp(self):
        self.assertEqual(self.steps()[-1], "delete pod")
        self.assertFalse(self.pod.exists())
        self.assertFalse((self.repo / ".generated/seed.sql.partial").exists())

    def test_complete_dump_replaces_seed_sql_and_keeps_the_old_one(self):
        result = self.run_playbook()
        self.assertOk(result)
        self.assertEqual(self.steps(),
                         ["get nodes", "delete pod", "run", "wait", "exec psql", "exec pg_dump", "delete pod"])
        self.assertEqual(self.seed(), DUMP)
        self.assertEqual((self.repo / "seed.sql").stat().st_mode & 0o777, 0o600)
        [backup] = self.backups()
        self.assertRegex(backup.name, r"^seed-\d{8}T\d{6}Z\.sql$")
        self.assertEqual(backup.read_text(), OLD_SEED)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertIn(f"Saved the database into seed.sql. The old one is .generated/seed-backups/{backup.name}.",
                      result.stdout)
        self.assertCleanedUp()
        self.assertEqual({Path(call["kubeconfig"]).resolve() for call in self.calls()},
                         {(self.repo / ".generated/kubeconfig").resolve()})

    def test_the_pod_passes_restricted_pod_security_and_gets_the_password_on_stdin(self):
        self.run_playbook()
        run = next(call["args"] for call in self.calls() if call["args"][0] == "run")
        self.assertEqual(run[1], "hospital-db-dump")
        self.assertEqual(run[-4:], ["--command", "--", "sleep", "3600"])
        for env in ("PGHOST=hospitalsystem-db.abc123.eu-north-1.rds.amazonaws.com", "PGPORT=5432",
                    "PGDATABASE=hospital", "PGUSER=postgres", "PGSSLMODE=require"):
            self.assertIn(f"--env={env}", run)
        overrides = json.loads(next(arg for arg in run if arg.startswith("--overrides=")).split("=", 1)[1])
        [container] = overrides["spec"]["containers"]
        self.assertEqual(container["name"], "hospital-db-dump")  # merged into the container kubectl run makes
        self.assertTrue(overrides["spec"]["securityContext"]["runAsNonRoot"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        execs = [call for call in self.calls() if call["args"][0] == "exec"]
        self.assertEqual([call["stdin"] for call in execs], ["fake-password\n"] * 2)
        self.assertFalse(any("fake-password" in arg for call in self.calls() for arg in call["args"]))

    def test_first_dump_writes_seed_sql_without_a_backup(self):
        result = self.run_playbook(seed=None)
        self.assertOk(result)
        self.assertEqual(self.seed(), DUMP)
        self.assertEqual(self.backups(), [])
        self.assertIn("Saved the database into seed.sql.", result.stdout)
        self.assertNotIn("The old one is", result.stdout)

    def test_failed_or_cut_off_dump_leaves_seed_sql_alone(self):
        for mode in ("fail", "cut"):
            with self.subTest(mode=mode):
                result = self.run_playbook(env={"FAKE_DUMP": mode})
                self.assertOk(result, False)
                self.assertIn("seed.sql was left as it is", result.stdout)
                if mode == "fail":
                    self.assertIn("pg_dump: error: SSL connection has been closed unexpectedly", result.stdout)
                self.assertEqual(self.seed(), OLD_SEED)
                self.assertEqual(self.backups(), [])
                self.assertCleanedUp()

    def test_database_without_tables_leaves_seed_sql_alone(self):
        result = self.run_playbook(env={"FAKE_TABLES": "0"})
        self.assertOk(result)
        self.assertIn("The database has no tables, so seed.sql was left as it is.", result.stdout)
        self.assertNotIn("exec pg_dump", self.steps())
        self.assertEqual(self.seed(), OLD_SEED)
        self.assertCleanedUp()

    def test_unreadable_database_stops_and_says_why(self):
        result = self.run_playbook(env={"FAKE_TABLES": "fail"})
        self.assertOk(result, False)
        self.assertIn('Could not read the database: psql: error: password authentication failed', result.stdout)
        self.assertNotIn("exec pg_dump", self.steps())
        self.assertEqual(self.seed(), OLD_SEED)
        self.assertCleanedUp()

    def test_pod_left_by_an_interrupted_run_is_replaced(self):
        result = self.run_playbook(leftover_pod=True)
        self.assertOk(result)
        self.assertEqual(self.steps()[:3], ["get nodes", "delete pod", "run"])
        self.assertEqual(self.seed(), DUMP)

    def test_cluster_without_nodes_stops_before_starting_a_pod(self):
        result = self.run_playbook(env={"FAKE_NODES": ""})
        self.assertOk(result, False)
        self.assertIn("ansible-playbook ansible/playbooks/resume.yml", result.stdout)
        self.assertEqual(self.steps(), ["get nodes"])
        self.assertEqual(self.seed(), OLD_SEED)

    def test_missing_password_stops_before_kubectl(self):
        result = self.run_playbook(password=None)
        self.assertOk(result, False)
        self.assertIn("HOSPITALSYSTEM_DB_PASSWORD isn't set", result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.seed(), OLD_SEED)


if __name__ == "__main__":
    main()
