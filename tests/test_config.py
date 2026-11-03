"""Exercise the shared Python YAML reader and alarm query."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from lib import config
from lib.commands import ScriptError


class PythonConfigTests(unittest.TestCase):
    """The same cases for scripts/lib/config.py."""

    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_yaml_quotes_comments_generated_precedence_and_environment(self):
        (self.path / 'main.yml').write_text(
            'region: old\nname: \'quoted: value # literal\' # comment\n'
            'plain: some-value # comment\nflag: false\nport: 8443\n'
        )
        (self.path / 'terraform.yml').write_text('"region": "custom-region"\n')
        values = config.read_group_vars(self.path)
        self.assertEqual([values[name] for name in ('region', 'name', 'plain', 'flag', 'port')],
                         ['custom-region', 'quoted: value # literal', 'some-value', 'false', '8443'])
        with mock.patch.dict(os.environ, {'GROUP_VARS_DIR': str(self.path), 'REGION': 'override'}):
            self.assertEqual(config.setting('REGION', 'region'), 'override')
            self.assertEqual(config.setting('REGION_NOT_SET_IN_THIS_TEST', 'region'), 'custom-region')

    def test_missing_generated_file_and_key_are_empty(self):
        (self.path / 'main.yml').write_text('ingress_name: custom-ingress\n')
        with mock.patch.dict(os.environ, {'GROUP_VARS_DIR': str(self.path)}):
            self.assertEqual([config.group_var('aws_region'), config.group_var('ingress_name')],
                             ['', 'custom-ingress'])

    def test_values_are_data_and_preserve_equals_backslashes_spaces(self):
        (self.path / 'main.yml').write_text("value: '  a=b\\c $(touch should-not-exist) `false`  '\n")
        self.assertEqual(config.read_group_vars(self.path)['value'], '  a=b\\c $(touch should-not-exist) `false`  ')

    def test_ansible_structures_and_templates_are_not_defaults(self):
        (self.path / 'main.yml').write_text('items: [a, b]\nimage: "{{ repo }}:latest"\n')
        self.assertEqual(config.read_group_vars(self.path), {})

    def test_bad_yaml_and_multiline_values_fail_before_using_partial_config(self):
        for source in ('broken: [', '- list', 'first: fine\nvalue: |\n  one\n  two\n'):
            with self.subTest(source=source):
                (self.path / 'main.yml').write_text(source)
                with self.assertRaisesRegex(ScriptError, 'Could not load operational configuration'):
                    config.read_group_vars(self.path)

    @unittest.skipUnless(importlib.util.find_spec('jmespath'), 'needs the jmespath package')
    def test_alarm_query_selects_every_configured_alarm_and_nothing_else(self):
        import jmespath

        alarms = json.loads((ROOT / 'config/alb-alarms.json').read_text())
        expected = [f"hs-{alarm['suffix']}" + ('targetgroup-k8s-back-1' if alarm['scope'] == 'target_group' else '')
                    for alarm in alarms]
        decoys = ['hs-nodegroup-missing-nodes'] + [
            name for alarm in alarms for name in (f"hs2-{alarm['suffix']}", f"shop-{alarm['suffix']}")
        ] + [f"hs-{alarm['suffix']}-by-hand" for alarm in alarms if alarm['scope'] != 'target_group']
        listed = {'MetricAlarms': [{'AlarmName': name} for name in decoys + expected]}
        self.assertEqual(jmespath.search(config.alb_alarm_query('hs'), listed), expected)

    def test_alarm_names_that_could_break_the_query_or_unknown_scopes_fail(self):
        for alarm in ({'suffix': "x') || `true` || ('", 'scope': 'load_balancer'},
                      {'suffix': 'x', 'scope': 'listener'}):
            with self.subTest(alarm=alarm):
                (self.path / 'alarms.json').write_text(json.dumps([alarm]))
                with self.assertRaises(ScriptError):
                    config.alb_alarm_query('hs', self.path / 'alarms.json')


if __name__ == '__main__':
    unittest.main()
