from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from exposures import JsonlExposureStore


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Outcome:
    experiment_id: str
    identity: str
    variant: str
    quality_score: float
    converted: bool
    cost_usd: float
    latency_ms: float
    created_at: str = ""

    def validate(self) -> None:
        if not self.experiment_id.strip() or not self.identity.strip() or not self.variant.strip():
            raise ValueError("experiment, identity, and variant are required")
        if not 0 <= self.quality_score <= 1:
            raise ValueError("quality score must be between 0 and 1")
        if self.cost_usd < 0 or self.latency_ms < 0:
            raise ValueError("cost and latency cannot be negative")


@dataclass(frozen=True)
class VariantMetrics:
    variant: str
    samples: int
    mean_quality: float
    quality_standard_error: float
    conversion_rate: float
    conversion_interval_95: tuple[float, float]
    average_cost_usd: float
    average_latency_ms: float


@dataclass(frozen=True)
class WinnerDecision:
    status: str
    winner: str | None
    primary_metric: str
    effect: float | None
    confidence_interval_95: tuple[float, float] | None
    reason: str


class JsonlOutcomeStore:
    def __init__(self, path: Path, exposure_store: JsonlExposureStore | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.exposure_store = exposure_store

    def append(self, outcome: Outcome) -> None:
        outcome.validate()
        if self.exposure_store is not None:
            exposure = self.exposure_store.find(outcome.experiment_id, outcome.identity)
            if exposure is None:
                raise ValueError("outcome requires a recorded exposure")
            if exposure["variant"] != outcome.variant:
                raise ValueError("outcome variant does not match recorded exposure")
        if any(
            item["experiment_id"] == outcome.experiment_id and item["identity"] == outcome.identity
            for item in self.records()
        ):
            raise ValueError("one outcome per experiment identity is allowed")
        value = asdict(outcome)
        value["created_at"] = outcome.created_at or utc_now()
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(value, sort_keys=True) + "\n")

    def records(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        return [item for item in records if experiment_id is None or item["experiment_id"] == experiment_id]


class ExperimentAnalyzer:
    def __init__(self, store: JsonlOutcomeStore):
        self.store = store

    def summarize(self, experiment_id: str) -> dict[str, VariantMetrics]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in self.store.records(experiment_id):
            groups[record["variant"]].append(record)
        summaries: dict[str, VariantMetrics] = {}
        for variant, records in sorted(groups.items()):
            qualities = [float(item["quality_score"]) for item in records]
            conversions = [bool(item["converted"]) for item in records]
            n = len(records)
            mean_quality = statistics.fmean(qualities)
            quality_se = statistics.stdev(qualities) / math.sqrt(n) if n > 1 else 0.0
            conversion = sum(conversions) / n
            summaries[variant] = VariantMetrics(
                variant=variant,
                samples=n,
                mean_quality=round(mean_quality, 6),
                quality_standard_error=round(quality_se, 6),
                conversion_rate=round(conversion, 6),
                conversion_interval_95=wilson_interval(sum(conversions), n),
                average_cost_usd=round(statistics.fmean(float(item["cost_usd"]) for item in records), 8),
                average_latency_ms=round(statistics.fmean(float(item["latency_ms"]) for item in records), 3),
            )
        return summaries

    def choose_winner(
        self,
        experiment_id: str,
        control: str,
        primary_metric: str = "quality",
        minimum_samples: int = 30,
        minimum_effect: float = 0.0,
        max_cost_increase: float = 0.10,
    ) -> WinnerDecision:
        if primary_metric not in {"quality", "conversion"}:
            raise ValueError("primary_metric must be quality or conversion")
        summaries = self.summarize(experiment_id)
        if control not in summaries:
            raise ValueError(f"control variant not found: {control}")
        if len(summaries) < 2:
            return WinnerDecision("insufficient_data", None, primary_metric, None, None, "fewer than two variants")
        if any(item.samples < minimum_samples for item in summaries.values()):
            return WinnerDecision(
                "insufficient_data", None, primary_metric, None, None,
                f"every variant needs at least {minimum_samples} independent samples",
            )

        baseline = summaries[control]
        candidates: list[tuple[float, str, float, tuple[float, float]]] = []
        for name, candidate in summaries.items():
            if name == control:
                continue
            effect, interval = difference_interval(baseline, candidate, primary_metric)
            cost_limit = baseline.average_cost_usd * (1 + max_cost_increase)
            if interval[0] > minimum_effect and candidate.average_cost_usd <= cost_limit:
                candidates.append((interval[0], name, effect, interval))

        if not candidates:
            return WinnerDecision(
                "no_winner", None, primary_metric, None, None,
                "no candidate cleared the effect confidence bound and cost guardrail",
            )
        _, name, effect, interval = max(candidates)
        return WinnerDecision(
            "winner", name, primary_metric, round(effect, 6), interval,
            f"{name} beat {control} with a positive 95% lower bound and acceptable cost",
        )


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return (0.0, 0.0)
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials) / denominator
    return (round(max(0.0, center - margin), 6), round(min(1.0, center + margin), 6))


def difference_interval(
    control: VariantMetrics,
    candidate: VariantMetrics,
    metric: str,
    z: float = 1.96,
) -> tuple[float, tuple[float, float]]:
    if metric == "quality":
        effect = candidate.mean_quality - control.mean_quality
        se = math.sqrt(candidate.quality_standard_error**2 + control.quality_standard_error**2)
    else:
        effect = candidate.conversion_rate - control.conversion_rate
        se = math.sqrt(
            candidate.conversion_rate * (1 - candidate.conversion_rate) / candidate.samples
            + control.conversion_rate * (1 - control.conversion_rate) / control.samples
        )
    return effect, (round(effect - z * se, 6), round(effect + z * se, 6))
