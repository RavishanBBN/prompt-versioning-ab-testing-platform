from __future__ import annotations

import json
import random
import tempfile
from dataclasses import asdict
from pathlib import Path

from exposures import JsonlExposureStore
from experiments import Experiment, ExperimentCatalog
from metrics import ExperimentAnalyzer, JsonlOutcomeStore, Outcome
from prompt_registry import PromptRegistry


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = PromptRegistry(root / "registry.json")
        registry.create_prompt("support-reply", owner="support-ai")
        first = registry.create_version(
            "support-reply", "Answer {question} clearly.", "maya", "initial prompt"
        )
        second = registry.create_version(
            "support-reply", "Answer {question} clearly and include the next action.", "maya", "make replies actionable"
        )
        registry.promote("support-reply", first.version, "development", "maya")
        registry.promote("support-reply", first.version, "staging", "maya")
        registry.approve("support-reply", first.version, "liam")
        registry.promote("support-reply", first.version, "production", "maya")
        registry.promote("support-reply", second.version, "development", "maya")
        registry.promote("support-reply", second.version, "staging", "maya")
        registry.approve("support-reply", second.version, "liam")

        catalog = ExperimentCatalog(root / "experiments.json")
        experiment = Experiment(
            id="reply-copy-v2",
            prompt_id="support-reply",
            variants={"control": first.version, "candidate": second.version},
            allocation={"control": 0.5, "candidate": 0.5},
            salt="demo-2026-10",
            primary_metric="quality",
            minimum_samples=100,
            minimum_effect=0.02,
            max_cost_increase=0.10,
        )
        catalog.create(experiment)
        catalog.set_status("reply-copy-v2", "running")
        exposures = JsonlExposureStore(root / "exposures.jsonl")
        outcomes = JsonlOutcomeStore(root / "outcomes.jsonl", exposures)
        randomizer = random.Random(12)
        for number in range(300):
            identity = f"user-{number}"
            assignment = catalog.assign("reply-copy-v2", identity)
            exposures.record(assignment)
            uplift = 0.09 if assignment.variant == "candidate" else 0.0
            quality = min(1.0, max(0.0, randomizer.gauss(0.72 + uplift, 0.08)))
            outcomes.append(
                Outcome(
                    experiment_id="reply-copy-v2",
                    identity=identity,
                    variant=assignment.variant,
                    quality_score=quality,
                    converted=randomizer.random() < 0.35 + uplift,
                    cost_usd=0.0021 if assignment.variant == "candidate" else 0.002,
                    latency_ms=randomizer.gauss(480, 35),
                )
            )

        analyzer = ExperimentAnalyzer(outcomes)
        summary = {name: asdict(value) for name, value in analyzer.summarize("reply-copy-v2").items()}
        allocation_health = analyzer.allocation_health(
            experiment.id, experiment.allocation, experiment.sample_ratio_alpha
        )
        decision = analyzer.choose_for_experiment(experiment)
        catalog.set_status(experiment.id, "completed")
        if decision.status == "winner":
            registry.promote("support-reply", second.version, "production", "maya")
        print(
            json.dumps(
                {
                    "allocation_health": asdict(allocation_health),
                    "metrics": summary,
                    "decision": asdict(decision),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
