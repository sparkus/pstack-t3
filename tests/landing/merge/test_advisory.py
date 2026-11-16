if __package__ in (None, ""):
    import sys
    from pathlib import Path

    _repo_root = Path(__file__).resolve().parent
    while _repo_root != _repo_root.parent and not (_repo_root / "tests" / "isolation.py").is_file():
        _repo_root = _repo_root.parent
    sys.path.insert(0, str(_repo_root))
from tests import isolation

isolation.install()

from ..support import (
    setUpModule,
    LandingTest as LandingTestBase,
    json,
    land,
)
from .base import MergeBase

LANE = "Selected iOS Native E2E (Observe)"

PASSED = {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}

FAILED = {"name": "test (3.12)", "status": "COMPLETED", "conclusion": "FAILURE"}

LANE_FAILED = {"name": LANE, "status": "COMPLETED", "conclusion": "FAILURE"}

LANE_CANCELLED = {"name": LANE, "status": "COMPLETED", "conclusion": "CANCELLED"}

LANE_RUNNING = {"name": LANE, "status": "IN_PROGRESS", "conclusion": ""}

PR = "https://github.com/o/r/pull/9"


class LandingTest(MergeBase, LandingTestBase):
    def init_advisory(self, suffix="(Observe)"):
        return self.land("init", "--trunk", "main", "--mode", "merge", "--check", "./check.sh", "--batch", "1",
                         "--advisory-suffix", suffix)

    def post(self, *rollup):
        (self.base / "pr-checks.json").write_text(json.dumps({"reviewDecision": "", "statusCheckRollup": list(rollup)}))

    def require(self, *contexts):
        (self.base / "protection-required.json").write_text(json.dumps({"contexts": list(contexts)}))

    def test_a_failed_advisory_check_neither_bounces_nor_blocks_the_merge(self):
        with self.fake_gh():
            self.require("test")
            self.post(PASSED, LANE_FAILED)
            self.init_advisory()
            self.queue_one()
            self.assertEqual(self.land("land"), "landed E1 (r/D1)")
            self.assertIn(f"advisory checks, never a blocker: {LANE} FAILURE", self.land("status", "E1"))

    def test_a_cancelled_advisory_check_does_not_bounce_an_entry_whose_merge_was_requested(self):
        with self.fake_gh():
            (self.base / "required-checks").write_text("")
            self.require("test")
            self.post(PASSED, LANE_RUNNING)
            self.init_advisory()
            self.queue_one()
            self.land("land")
            self.assertIn("merge requested by the queue", self.land("status", "E1"))
            self.post(PASSED, LANE_CANCELLED)
            self.assertEqual(self.land("land"), "nothing to land")
            self.assertIn(f"{LANE} CANCELLED", self.land("status", "E1"))
            self.assertTrue(self.land("lease", "list").startswith("L1 submitted"))

    def test_a_pending_advisory_check_does_not_hold_the_merge(self):
        with self.fake_gh():
            self.require("test")
            self.post(PASSED, LANE_RUNNING)
            self.init_advisory()
            self.queue_one()
            self.assertEqual(self.land("land"), "landed E1 (r/D1)")
            self.assertIn(f"{LANE} IN_PROGRESS", self.land("status", "E1"))

    def test_a_failed_check_without_the_suffix_still_bounces(self):
        with self.fake_gh():
            self.require("test")
            self.post(PASSED, FAILED, LANE_FAILED)
            self.init_advisory()
            self.queue_one()
            self.assertEqual(self.land("land"), f"bounced E1 (r/D1): required checks failed on {PR}: test (3.12)")

    def test_a_required_check_with_the_suffix_still_bounces(self):
        with self.fake_gh():
            self.require("test", LANE)
            self.post(PASSED, LANE_FAILED)
            self.init_advisory()
            self.queue_one()
            self.assertEqual(self.land("land"), f"bounced E1 (r/D1): required checks failed on {PR}: {LANE}")

    def test_a_required_check_with_the_suffix_still_holds_the_merge_while_it_runs(self):
        with self.fake_gh():
            self.require("test", LANE)
            self.post(PASSED, LANE_RUNNING)
            self.init_advisory()
            self.queue_one()
            self.assertEqual(self.land("land"), f"opened PRs that merge when their checks pass: E1 (r/D1) {PR}")
            status = self.land("status", "E1")
            self.assertIn("waiting for required checks", status)
            self.assertNotIn("advisory", status)

    def test_a_failed_advisory_check_bounces_when_the_required_checks_cannot_be_read(self):
        with self.fake_gh():
            self.post(PASSED, LANE_FAILED)
            self.init_advisory()
            self.queue_one()
            self.assertEqual(self.land("land"), f"bounced E1 (r/D1): required checks failed on {PR}: {LANE}")

    def test_without_the_setting_a_failed_observe_check_bounces_and_status_is_unchanged(self):
        with self.fake_gh():
            self.require("test")
            self.post(PASSED, LANE_FAILED)
            self.init(mode="merge")
            self.queue_one()
            self.assertEqual(self.land("land"), f"bounced E1 (r/D1): required checks failed on {PR}: {LANE}")
            self.assertNotIn("advisorySuffix", self.contract())
            self.assertNotIn("advisoryChecks", self.contract())
            self.assertNotIn("advisory", self.land("status", "E1"))
            self.assertNotIn("advisory", self.land("status"))
            self.assertNotIn("advisorySuffix", json.loads(self.land("status", "--json")))
            self.assertEqual(set(json.loads(self.land("status", "--holder", "r/D1", "--json"))[0]),
                             {"id", "state", "holder", "sha", "note"})

    def test_status_names_the_suffix_and_the_holder_json_carries_advisory_results(self):
        with self.fake_gh():
            self.require("test")
            self.post(PASSED, LANE_FAILED)
            self.init_advisory()
            self.assertIn("checks named *(Observe) are advisory unless required", self.land("status"))
            self.assertEqual(json.loads(self.land("status", "--json"))["advisorySuffix"], "(Observe)")
            self.queue_one()
            self.land("land")
            entry = json.loads(self.land("status", "--holder", "r/D1", "--json"))[0]
            self.assertEqual(entry["advisory"], [{"name": LANE, "result": "FAILURE"}])

    def test_a_rerun_keeps_the_suffix_and_an_empty_value_clears_it(self):
        with self.fake_gh():
            self.init_advisory()
            self.init(mode="merge")
            self.assertEqual(self.contract()["advisorySuffix"], "(Observe)")
            self.init_advisory("")
            self.assertNotIn("advisorySuffix", self.contract())


if __name__ == "__main__":
    import unittest

    unittest.main()
