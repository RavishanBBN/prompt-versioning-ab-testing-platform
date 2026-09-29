from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Experiment:
    id: str
    prompt_id: str
    variants: dict[str, int]
    allocation: dict[str, float]
    salt: str
    status: str = "draft"
    created_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Experiment":
        return cls(
            id=str(value["id"]),
            prompt_id=str(value["prompt_id"]),
            variants={str(name): int(version) for name, version in value["variants"].items()},
            allocation={str(name): float(weight) for name, weight in value["allocation"].items()},
            salt=str(value["salt"]),
            status=str(value.get("status", "draft")),
            created_at=str(value.get("created_at", "")),
        )

    def validate(self) -> None:
        if not self.id.strip() or not self.prompt_id.strip() or not self.salt:
            raise ValueError("experiment id, prompt id, and salt are required")
        if len(self.variants) < 2:
            raise ValueError("an experiment needs at least two variants")
        if set(self.variants) != set(self.allocation):
            raise ValueError("variant and allocation labels must match")
        if any(version < 1 for version in self.variants.values()):
            raise ValueError("prompt versions must be positive")
        if any(weight <= 0 for weight in self.allocation.values()):
            raise ValueError("allocation weights must be positive")
        if not math.isclose(sum(self.allocation.values()), 1.0, abs_tol=1e-9):
            raise ValueError("allocation weights must sum to 1")
        if self.status not in {"draft", "running", "paused", "completed"}:
            raise ValueError("invalid experiment status")


@dataclass(frozen=True)
class Assignment:
    experiment_id: str
    identity: str
    variant: str
    prompt_version: int
    bucket: float


class ExperimentCatalog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"experiments": {}})

    def create(self, experiment: Experiment) -> None:
        experiment.validate()
        if experiment.status != "draft":
            raise ValueError("new experiments must start as draft")
        state = self._read()
        if experiment.id in state["experiments"]:
            raise ValueError(f"experiment already exists: {experiment.id}")
        value = asdict(experiment)
        value["created_at"] = experiment.created_at or utc_now()
        state["experiments"][experiment.id] = value
        self._write(state)

    def get(self, experiment_id: str) -> Experiment:
        try:
            return Experiment.from_dict(self._read()["experiments"][experiment_id])
        except KeyError as exc:
            raise ValueError(f"unknown experiment: {experiment_id}") from exc

    def set_status(self, experiment_id: str, status: str) -> Experiment:
        current = self.get(experiment_id)
        transitions = {
            "draft": {"running"},
            "running": {"paused", "completed"},
            "paused": {"running", "completed"},
            "completed": set(),
        }
        if status not in transitions[current.status]:
            raise ValueError(f"cannot move experiment from {current.status} to {status}")
        state = self._read()
        state["experiments"][experiment_id]["status"] = status
        self._write(state)
        return self.get(experiment_id)

    def assign(self, experiment_id: str, identity: str) -> Assignment:
        experiment = self.get(experiment_id)
        if experiment.status != "running":
            raise ValueError("experiment must be running before assignment")
        if not identity.strip():
            raise ValueError("stable user or request identity is required")
        material = f"{experiment.id}:{experiment.salt}:{identity}".encode("utf-8")
        integer = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        bucket = integer / 2**64
        cumulative = 0.0
        selected = ""
        for label in sorted(experiment.variants):
            cumulative += experiment.allocation[label]
            if bucket < cumulative:
                selected = label
                break
        if not selected:  # Floating-point defense for a bucket extremely close to one.
            selected = sorted(experiment.variants)[-1]
        return Assignment(
            experiment_id=experiment.id,
            identity=identity,
            variant=selected,
            prompt_version=experiment.variants[selected],
            bucket=round(bucket, 12),
        )

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, value: dict[str, Any]) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)
