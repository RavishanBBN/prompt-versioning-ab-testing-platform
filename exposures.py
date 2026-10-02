from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiments import Assignment


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Exposure:
    experiment_id: str
    identity: str
    variant: str
    prompt_version: int
    bucket: float
    exposed_at: str


class JsonlExposureStore:
    """Append-only first-exposure log for independent experiment units."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, assignment: Assignment) -> Exposure:
        exposure = Exposure(
            experiment_id=assignment.experiment_id,
            identity=assignment.identity,
            variant=assignment.variant,
            prompt_version=assignment.prompt_version,
            bucket=assignment.bucket,
            exposed_at=utc_now(),
        )
        with self._lock:
            existing = self.find(assignment.experiment_id, assignment.identity)
            if existing is not None:
                if (
                    existing["variant"] != assignment.variant
                    or int(existing["prompt_version"]) != assignment.prompt_version
                ):
                    raise ValueError("identity already has a conflicting exposure")
                return Exposure(**existing)
            with self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(asdict(exposure), sort_keys=True) + "\n")
        return exposure

    def records(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        return [
            item
            for item in records
            if experiment_id is None or item["experiment_id"] == experiment_id
        ]

    def find(self, experiment_id: str, identity: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.records(experiment_id)
                if item["identity"] == identity
            ),
            None,
        )
