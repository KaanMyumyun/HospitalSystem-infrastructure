"""Exercise the shared YAML reader and alarm query through their public Bash API."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory)

    def read(self, command):
        return subprocess.run(
            ['bash', '-ec', 'source "$REPO_ROOT/scripts/lib/config.sh"; ' + command],
            env={**os.environ, 'REPO_ROOT': str(ROOT), 'GROUP_VARS_DIR': self.directory},
            text=True, capture_output=True, timeout=10,
        )

    def test_yaml_quotes_comments_generated_precedence_and_environment(self):
        (self.path / 'main.yml').write_text(
            'region: old\nname: \'quoted: value # literal\' # comment\n'
            'plain: some-value # comment\nflag: false\nport: 8443\n'
        )
        (self.path / 'terraform.yml').write_text('"region": "custom-region"\n')
        result = self.read(
            'printf "%s\\n" "$(group_var region)" "$(group_var name)" '
            '"$(group_var plain)" "$(group_var flag)" "$(group_var port)"; '
            'REGION=override; printf "%s\\n" "${REGION:-$(group_var region)}"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            'custom-region', 'quoted: value # literal', 'some-value', 'false', '8443', 'override',
        ])

    def test_missing_generated_file_and_key_are_empty(self):
        (self.path / 'main.yml').write_text('ingress_name: custom-ingress\n')
        result = self.read('printf "[%s][%s]" "$(group_var aws_region)" "$(group_var ingress_name)"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '[][custom-ingress]')

    def test_values_are_data_and_preserve_equals_backslashes_spaces(self):
        (self.path / 'main.yml').write_text(
            "value: '  a=b\\c $(touch should-not-exist) `false`  '\n"
        )
        result = self.read('printf "[%s]" "$(group_var value)"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '[  a=b\\c $(touch should-not-exist) `false`  ]')
        self.assertFalse((ROOT / 'should-not-exist').exists())

    def test_ansible_structures_and_templates_are_not_shell_defaults(self):
        (self.path / 'main.yml').write_text('items: [a, b]\nimage: "{{ repo }}:latest"\n')
        result = self.read('printf "[%s][%s]" "$(group_var items)" "$(group_var image)"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '[][]')

    def test_bad_yaml_and_multiline_values_fail_before_using_partial_config(self):
        for source in ('broken: [', '- list', 'first: fine\nvalue: |\n  one\n  two\n'):
            with self.subTest(source=source):
                (self.path / 'main.yml').write_text(source)
                result = self.read('printf SHOULD_NOT_RUN')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('SHOULD_NOT_RUN', result.stdout)
                self.assertIn('Could not load operational configuration', result.stderr)


class AlarmQueryTests(unittest.TestCase):
    """The cleanup scripts select monitoring.yml's alarms with this query."""

    def query(self, root=ROOT):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        return subprocess.run(
            ['bash', '-ec', 'source "$REPO_ROOT/scripts/lib/config.sh"; alb_alarm_query hs'],
            env={**os.environ, 'REPO_ROOT': str(root), 'GROUP_VARS_DIR': directory},
            text=True, capture_output=True, timeout=10,
        )

    def root_with_alarms(self, alarms):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / 'scripts').symlink_to(ROOT / 'scripts')
        (root / 'config').mkdir()
        (root / 'config/alb-alarms.json').write_text(json.dumps(alarms))
        return root

    @unittest.skipUnless(importlib.util.find_spec('jmespath'), 'needs the jmespath package')
    def test_selects_every_configured_alarm_and_nothing_else(self):
        import jmespath

        result = self.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        alarms = json.loads((ROOT / 'config/alb-alarms.json').read_text())
        # Named the way ansible/tasks/alb-alarm.yml names them.
        expected = [f"hs-{alarm['suffix']}" + ('targetgroup-k8s-back-1' if alarm['scope'] == 'target_group' else '')
                    for alarm in alarms]
        decoys = ['hs-nodegroup-missing-nodes'] + [
            name for alarm in alarms for name in (f"hs2-{alarm['suffix']}", f"shop-{alarm['suffix']}")
        ] + [f"hs-{alarm['suffix']}-by-hand" for alarm in alarms if alarm['scope'] != 'target_group']
        listed = {'MetricAlarms': [{'AlarmName': name} for name in decoys + expected]}
        self.assertEqual(jmespath.search(result.stdout.strip(), listed), expected)

    def test_names_that_could_break_the_query_or_unknown_scopes_fail(self):
        for alarm in ({'suffix': "x') || `true` || ('", 'scope': 'load_balancer'},
                      {'suffix': 'x', 'scope': 'listener'}):
            with self.subTest(alarm=alarm):
                result = self.query(self.root_with_alarms([alarm]))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
