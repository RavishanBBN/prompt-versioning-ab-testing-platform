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


@dataclass(frozen=True)
class AllocationHealth:
    status: str
    observed: dict[str, int]
    expected: dict[str, float]
    chi_square: float | None
    degrees_freedom: int
    p_value: float | None
    alpha: float


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

    def allocation_health(
        self,
        experiment_id: str,
        allocation: dict[str, float],
        alpha: float = 0.01,
        minimum_total: int = 100,
    ) -> AllocationHealth:
        if self.store.exposure_store is not None:
            records = self.store.exposure_store.records(experiment_id)
        else:
            records = self.store.records(experiment_id)
        observed = {name: 0 for name in allocation}
        for record in records:
            variant = str(record["variant"])
            if variant not in observed:
                raise ValueError(f"unknown observed variant: {variant}")
            observed[variant] += 1
        return check_sample_ratio(observed, allocation, alpha, minimum_total)

    def choose_winner(
        self,
        experiment_id: str,
        control: str,
        primary_metric: str = "quality",
        minimum_samples: int = 30,
        minimum_effect: float = 0.0,
        max_cost_increase: float = 0.10,
        expected_allocation: dict[str, float] | None = None,
        sample_ratio_alpha: float = 0.01,
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
        if expected_allocation is not None:
            health = self.allocation_health(
                experiment_id,
                expected_allocation,
                alpha=sample_ratio_alpha,
                minimum_total=minimum_samples * len(expected_allocation),
            )
            if health.status == "mismatch":
                return WinnerDecision(
                    "invalid_experiment", None, primary_metric, None, None,
                    f"sample ratio mismatch detected (p={health.p_value})",
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


def check_sample_ratio(
    observed: dict[str, int],
    allocation: dict[str, float],
    alpha: float = 0.01,
    minimum_total: int = 100,
) -> AllocationHealth:
    """Run a chi-square goodness-of-fit check against planned allocation."""
    if set(observed) != set(allocation):
        raise ValueError("observed and allocation labels must match")
    if len(allocation) < 2:
        raise ValueError("at least two variants are required")
    if any(count < 0 for count in observed.values()):
        raise ValueError("observed counts cannot be negative")
    if any(weight <= 0 for weight in allocation.values()) or not math.isclose(
        sum(allocation.values()), 1.0, abs_tol=1e-9
    ):
        raise ValueError("allocation weights must be positive and sum to 1")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")

    total = sum(observed.values())
    expected = {name: total * weight for name, weight in allocation.items()}
    degrees_freedom = len(allocation) - 1
    if total < minimum_total:
        return AllocationHealth(
            "insufficient_data", dict(observed), expected, None,
            degrees_freedom, None, alpha,
        )

    statistic = sum(
        (observed[name] - expected[name]) ** 2 / expected[name]
        for name in allocation
    )
    p_value = _regularized_gamma_q(degrees_freedom / 2, statistic / 2)
    return AllocationHealth(
        "mismatch" if p_value < alpha else "healthy",
        dict(observed),
        {name: round(value, 6) for name, value in expected.items()},
        round(statistic, 6),
        degrees_freedom,
        round(p_value, 8),
        alpha,
    )


def _regularized_gamma_q(shape: float, value: float) -> float:
    """Regularized upper incomplete gamma used by chi-square survival."""
    if shape <= 0 or value < 0:
        raise ValueError("gamma arguments are outside the supported domain")
    if value == 0:
        return 1.0
    epsilon = 3e-14
    tiny = 1e-300
    maximum_iterations = 200
    log_scale = -value + shape * math.log(value) - math.lgamma(shape)

    if value < shape + 1:
        term = 1 / shape
        series = term
        rising_shape = shape
        for _ in range(maximum_iterations):
            rising_shape += 1
            term *= value / rising_shape
            series += term
            if abs(term) < abs(series) * epsilon:
                lower = series * math.exp(log_scale)
                return max(0.0, min(1.0, 1 - lower))
        raise ArithmeticError("gamma series did not converge")

    denominator = value + 1 - shape
    c_value = 1 / tiny
    d_value = 1 / max(abs(denominator), tiny)
    if denominator < 0:
        d_value = -d_value
    fraction = d_value
    for index in range(1, maximum_iterations + 1):
        coefficient = -index * (index - shape)
        denominator += 2
        d_value = coefficient * d_value + denominator
        if abs(d_value) < tiny:
            d_value = tiny
        c_value = denominator + coefficient / c_value
        if abs(c_value) < tiny:
            c_value = tiny
        d_value = 1 / d_value
        delta = d_value * c_value
        fraction *= delta
        if abs(delta - 1) < epsilon:
            return max(0.0, min(1.0, math.exp(log_scale) * fraction))
    raise ArithmeticError("gamma continued fraction did not converge")
