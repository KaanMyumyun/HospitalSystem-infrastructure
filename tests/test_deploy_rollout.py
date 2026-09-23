"""Exercise the actual SSM rollout loop without contacting Kubernetes."""

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
import sys
import textwrap


SOURCE = (Path(__file__).resolve().parents[1] / "terraform/ops.tf").read_text()
ROLLOUT = textwrap.dedent(
    SOURCE[SOURCE.index("    deadline = time.monotonic() + 360"):SOURCE.index("    PY\n")]
)


def deployment(observed, failed=False):
    return {
        "metadata": {"generation": 12},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": observed,
            "updatedReplicas": 1,
            "replicas": 1,
            "availableReplicas": 1,
            "conditions": [{"reason": "ProgressDeadlineExceeded"}] if failed else [],
        },
    }


class RolloutTests(TestCase):
    def run_rollout(self, responses, clock=(0, 1, 2)):
        self.call = Mock(side_effect=responses)
        self.sleep = Mock()
        namespace = {
            "DEPLOYMENTS": ["hospital-backend"],
            "call": self.call,
            "sys": sys,
            "time": SimpleNamespace(monotonic=Mock(side_effect=clock), sleep=self.sleep),
        }
        with patch("builtins.print"):
            exec(compile(ROLLOUT, "terraform/ops.tf:rollout", "exec"), namespace)

    def test_stale_failure_waits_for_corrected_release(self):
        self.run_rollout([deployment(11, failed=True), deployment(12)])
        self.assertEqual(self.call.call_count, 2)
        self.sleep.assert_called_once_with(5)

    def test_current_generation_failure_is_reported(self):
        with self.assertRaisesRegex(SystemExit, "exceeded its progress deadline"):
            self.run_rollout([deployment(12, failed=True)])
        self.sleep.assert_not_called()

    def test_stale_status_still_times_out(self):
        with self.assertRaisesRegex(SystemExit, "did not finish within 6 minutes"):
            self.run_rollout([deployment(11, failed=True)], clock=(0, 361))

    def test_current_success_finishes_without_waiting(self):
        self.run_rollout([deployment(12)])
        self.assertEqual(self.call.call_count, 1)
        self.sleep.assert_not_called()


if __name__ == "__main__":
    main()
