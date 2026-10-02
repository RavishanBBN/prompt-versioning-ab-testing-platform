import json
import tempfile
import unittest
from pathlib import Path

from exposures import JsonlExposureStore
from experiments import Assignment, Experiment, ExperimentCatalog
from metrics import (
    ExperimentAnalyzer,
    JsonlOutcomeStore,
    Outcome,
    check_sample_ratio,
    wilson_interval,
)
from prompt_registry import PromptRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "registry.json"
        self.registry = PromptRegistry(self.path)
        self.registry.create_prompt("reply", "support")

    def version(self, content: str = "Hello {name}", author: str = "maya"):
        return self.registry.create_version("reply", content, author, "improve reply")

    def test_versions_are_monotonic_and_checksum_protected(self):
        first = self.version()
        second = self.version("Welcome {name}")
        self.assertEqual((first.version, second.version), (1, 2))
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["prompts"]["reply"]["versions"][0]["content"] = "tampered"
        self.path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.registry.get_version("reply", 1)

    def test_render_requires_exact_variables(self):
        version = self.version("Hello {name}, ticket {ticket}")
        self.assertEqual(
            self.registry.render("reply", version.version, {"name": "Ana", "ticket": 42}),
            "Hello Ana, ticket 42",
        )
        with self.assertRaisesRegex(ValueError, "missing"):
            self.registry.render("reply", version.version, {"name": "Ana"})

    def test_author_cannot_self_approve(self):
        version = self.version(author="maya")
        with self.assertRaisesRegex(ValueError, "own version"):
            self.registry.approve("reply", version.version, "maya")

    def test_production_requires_sequence_and_approval(self):
        version = self.version()
        with self.assertRaisesRegex(ValueError, "staging"):
            self.registry.promote("reply", version.version, "production", "maya")
        self.registry.promote("reply", version.version, "development", "maya")
        self.registry.promote("reply", version.version, "staging", "maya")
        with self.assertRaisesRegex(ValueError, "approval"):
            self.registry.promote("reply", version.version, "production", "maya")
        self.registry.approve("reply", version.version, "liam")
        deployed = self.registry.promote("reply", version.version, "production", "maya")
        self.assertEqual(deployed.version, version.version)

    def test_rollback_restores_previous_production_version(self):
        first = self.version("Hello {name}")
        second = self.version("Welcome {name}")
        for version in (first, second):
            self.registry.promote("reply", version.version, "development", "maya")
            self.registry.promote("reply", version.version, "staging", "maya")
            self.registry.approve("reply", version.version, "liam")
            self.registry.promote("reply", version.version, "production", "maya")
        rolled_back = self.registry.rollback("reply", "production", "liam")
        self.assertEqual(rolled_back.version, first.version)
        self.assertEqual(rolled_back.action, "rollback")


class ExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.catalog = ExperimentCatalog(Path(directory.name) / "experiments.json")
        self.experiment = Experiment(
            "exp-1", "reply", {"control": 1, "candidate": 2},
            {"control": 0.5, "candidate": 0.5}, "secret-salt",
        )
        self.catalog.create(self.experiment)

    def test_assignment_requires_running_state(self):
        with self.assertRaisesRegex(ValueError, "running"):
            self.catalog.assign("exp-1", "user-1")

    def test_assignment_is_sticky(self):
        self.catalog.set_status("exp-1", "running")
        first = self.catalog.assign("exp-1", "user-1")
        second = self.catalog.assign("exp-1", "user-1")
        self.assertEqual(first, second)

    def test_hash_buckets_approximately_follow_allocation(self):
        self.catalog.set_status("exp-1", "running")
        assigned = [self.catalog.assign("exp-1", f"user-{number}").variant for number in range(1000)]
        control_share = assigned.count("control") / len(assigned)
        self.assertTrue(0.45 < control_share < 0.55)

    def test_first_exposure_is_idempotent_and_conflicts_are_rejected(self):
        self.catalog.set_status("exp-1", "running")
        assignment = self.catalog.assign("exp-1", "user-1")
        exposures = JsonlExposureStore(self.catalog.path.parent / "exposures.jsonl")
        first = exposures.record(assignment)
        second = exposures.record(assignment)
        self.assertEqual(first, second)
        self.assertEqual(len(exposures.records("exp-1")), 1)

        conflicting = assignment.__class__(
            experiment_id=assignment.experiment_id,
            identity=assignment.identity,
            variant="candidate" if assignment.variant == "control" else "control",
            prompt_version=2 if assignment.prompt_version == 1 else 1,
            bucket=assignment.bucket,
        )
        with self.assertRaisesRegex(ValueError, "conflicting exposure"):
            exposures.record(conflicting)


class MetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = JsonlOutcomeStore(Path(directory.name) / "outcomes.jsonl")
        self.analyzer = ExperimentAnalyzer(self.store)

    def add_group(self, variant: str, quality: float, cost: float, count: int = 40) -> None:
        for number in range(count):
            self.store.append(
                Outcome(
                    "exp", f"{variant}-{number}", variant,
                    quality + (0.01 if number % 2 else -0.01), number % 2 == 0,
                    cost, 400 + number,
                )
            )

    def test_duplicate_identity_is_rejected(self):
        outcome = Outcome("exp", "user-1", "control", 0.8, True, 0.01, 300)
        self.store.append(outcome)
        with self.assertRaisesRegex(ValueError, "one outcome"):
            self.store.append(outcome)

    def test_outcome_must_match_a_recorded_exposure(self):
        exposures = JsonlExposureStore(self.store.path.parent / "exposures.jsonl")
        guarded_store = JsonlOutcomeStore(
            self.store.path.parent / "guarded-outcomes.jsonl", exposures
        )
        outcome = Outcome("exp", "user-1", "control", 0.8, True, 0.01, 300)
        with self.assertRaisesRegex(ValueError, "recorded exposure"):
            guarded_store.append(outcome)

        exposures.record(Assignment("exp", "user-1", "control", 1, 0.25))
        guarded_store.append(outcome)

        mismatched = Outcome("exp", "user-2", "control", 0.8, True, 0.01, 300)
        exposures.record(Assignment("exp", "user-2", "candidate", 2, 0.75))
        with self.assertRaisesRegex(ValueError, "does not match"):
            guarded_store.append(mismatched)

    def test_wilson_interval_stays_inside_probability_bounds(self):
        low, high = wilson_interval(1, 2)
        self.assertGreaterEqual(low, 0)
        self.assertLessEqual(high, 1)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)

    def test_sample_ratio_check_detects_broken_allocation(self):
        healthy = check_sample_ratio(
            {"control": 500, "candidate": 500},
            {"control": 0.5, "candidate": 0.5},
        )
        broken = check_sample_ratio(
            {"control": 800, "candidate": 200},
            {"control": 0.5, "candidate": 0.5},
        )
        self.assertEqual(healthy.status, "healthy")
        self.assertAlmostEqual(healthy.p_value, 1.0)
        self.assertEqual(broken.status, "mismatch")
        self.assertLess(broken.p_value, broken.alpha)

    def test_sample_ratio_check_waits_for_enough_traffic(self):
        result = check_sample_ratio(
            {"control": 4, "candidate": 6},
            {"control": 0.5, "candidate": 0.5},
            minimum_total=100,
        )
        self.assertEqual(result.status, "insufficient_data")
        self.assertIsNone(result.p_value)

    def test_winner_needs_enough_independent_samples(self):
        self.add_group("control", 0.7, 0.01, count=5)
        self.add_group("candidate", 0.9, 0.01, count=5)
        decision = self.analyzer.choose_winner("exp", "control", minimum_samples=20)
        self.assertEqual(decision.status, "insufficient_data")

    def test_quality_winner_clears_confidence_and_cost_guards(self):
        self.add_group("control", 0.7, 0.01)
        self.add_group("candidate", 0.9, 0.0105)
        decision = self.analyzer.choose_winner("exp", "control", minimum_samples=30)
        self.assertEqual(decision.winner, "candidate")
        self.assertGreater(decision.confidence_interval_95[0], 0)

    def test_expensive_candidate_is_not_selected(self):
        self.add_group("control", 0.7, 0.01)
        self.add_group("candidate", 0.9, 0.02)
        decision = self.analyzer.choose_winner("exp", "control", minimum_samples=30)
        self.assertEqual(decision.status, "no_winner")


if __name__ == "__main__":
    unittest.main()
