from __future__ import annotations

import hashlib
import json
import os
import string
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PromptVersion:
    prompt_id: str
    version: int
    content: str
    variables: tuple[str, ...]
    created_by: str
    change_note: str
    created_at: str
    checksum: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PromptVersion":
        copied = dict(value)
        copied["variables"] = tuple(copied["variables"])
        return cls(**copied)


@dataclass(frozen=True)
class Deployment:
    prompt_id: str
    environment: str
    version: int
    actor: str
    deployed_at: str
    action: str = "promote"
    previous_version: int | None = None


class PromptRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write(self._empty_state())

    def create_prompt(self, prompt_id: str, owner: str) -> None:
        if not prompt_id.strip() or not owner.strip():
            raise ValueError("prompt_id and owner are required")
        with self._lock:
            state = self._read()
            if prompt_id in state["prompts"]:
                raise ValueError(f"prompt already exists: {prompt_id}")
            state["prompts"][prompt_id] = {"owner": owner, "versions": []}
            self._write(state)

    def create_version(
        self,
        prompt_id: str,
        content: str,
        created_by: str,
        change_note: str,
    ) -> PromptVersion:
        if not content.strip():
            raise ValueError("prompt content cannot be empty")
        if not created_by.strip() or not change_note.strip():
            raise ValueError("created_by and change_note are required")
        variables = extract_variables(content)
        with self._lock:
            state = self._read()
            prompt = self._prompt(state, prompt_id)
            next_version = len(prompt["versions"]) + 1
            version = PromptVersion(
                prompt_id=prompt_id,
                version=next_version,
                content=content,
                variables=variables,
                created_by=created_by,
                change_note=change_note,
                created_at=utc_now(),
                checksum=version_checksum(prompt_id, next_version, content, variables),
            )
            prompt["versions"].append(asdict(version))
            self._write(state)
            return version

    def get_version(self, prompt_id: str, version: int) -> PromptVersion:
        state = self._read()
        prompt = self._prompt(state, prompt_id)
        try:
            stored = prompt["versions"][version - 1]
        except (IndexError, TypeError) as exc:
            raise ValueError(f"unknown version {prompt_id}@{version}") from exc
        result = PromptVersion.from_dict(stored)
        expected = version_checksum(result.prompt_id, result.version, result.content, result.variables)
        if result.checksum != expected:
            raise ValueError(f"checksum mismatch for {prompt_id}@{version}")
        return result

    def list_versions(self, prompt_id: str) -> list[PromptVersion]:
        prompt = self._prompt(self._read(), prompt_id)
        return [PromptVersion.from_dict(value) for value in prompt["versions"]]

    def render(self, prompt_id: str, version: int, values: dict[str, Any]) -> str:
        prompt = self.get_version(prompt_id, version)
        expected = set(prompt.variables)
        supplied = set(values)
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        if missing or unknown:
            raise ValueError(f"variable mismatch; missing={missing}, unknown={unknown}")
        try:
            return prompt.content.format(**values)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"could not render {prompt_id}@{version}: {exc}") from exc

    def approve(self, prompt_id: str, version: int, reviewer: str) -> None:
        prompt = self.get_version(prompt_id, version)
        if not reviewer.strip():
            raise ValueError("reviewer is required")
        if reviewer == prompt.created_by:
            raise ValueError("prompt author cannot approve their own version")
        key = f"{prompt_id}@{version}"
        with self._lock:
            state = self._read()
            approvals = state["approvals"].setdefault(key, [])
            if any(item["reviewer"] == reviewer for item in approvals):
                return
            approvals.append({"reviewer": reviewer, "approved_at": utc_now()})
            self._write(state)

    def promote(self, prompt_id: str, version: int, environment: str, actor: str) -> Deployment:
        target = self.get_version(prompt_id, version)
        if environment not in {"development", "staging", "production"}:
            raise ValueError("environment must be development, staging, or production")
        if not actor.strip():
            raise ValueError("actor is required")
        with self._lock:
            state = self._read()
            deployments = state["deployments"].setdefault(prompt_id, {})
            if environment == "staging" and deployments.get("development", {}).get("version") != version:
                raise ValueError("version must be deployed to development before staging")
            if environment == "production":
                if deployments.get("staging", {}).get("version") != version:
                    raise ValueError("version must be deployed to staging before production")
                approvals = state["approvals"].get(f"{prompt_id}@{version}", [])
                if not any(item["reviewer"] != target.created_by for item in approvals):
                    raise ValueError("production promotion requires independent approval")

            previous = deployments.get(environment, {}).get("version")
            deployment = Deployment(
                prompt_id=prompt_id,
                environment=environment,
                version=version,
                actor=actor,
                deployed_at=utc_now(),
                previous_version=previous,
            )
            serialized = asdict(deployment)
            deployments[environment] = serialized
            history_key = f"{prompt_id}:{environment}"
            state["deployment_history"].setdefault(history_key, []).append(serialized)
            self._write(state)
            return deployment

    def deployed_version(self, prompt_id: str, environment: str) -> PromptVersion:
        state = self._read()
        try:
            version = int(state["deployments"][prompt_id][environment]["version"])
        except KeyError as exc:
            raise ValueError(f"nothing deployed for {prompt_id} in {environment}") from exc
        return self.get_version(prompt_id, version)

    def render_environment(self, prompt_id: str, environment: str, values: dict[str, Any]) -> str:
        deployed = self.deployed_version(prompt_id, environment)
        return self.render(prompt_id, deployed.version, values)

    def rollback(self, prompt_id: str, environment: str, actor: str) -> Deployment:
        if not actor.strip():
            raise ValueError("actor is required")
        history_key = f"{prompt_id}:{environment}"
        with self._lock:
            state = self._read()
            history = state["deployment_history"].get(history_key, [])
            if not history:
                raise ValueError(f"no deployment history for {prompt_id} in {environment}")
            current_version = history[-1]["version"]
            previous = next(
                (item["version"] for item in reversed(history[:-1]) if item["version"] != current_version),
                None,
            )
            if previous is None:
                raise ValueError("no previous version available for rollback")
            deployment = Deployment(
                prompt_id=prompt_id,
                environment=environment,
                version=previous,
                actor=actor,
                deployed_at=utc_now(),
                action="rollback",
                previous_version=current_version,
            )
            serialized = asdict(deployment)
            state["deployments"].setdefault(prompt_id, {})[environment] = serialized
            history.append(serialized)
            self._write(state)
            return deployment

    def _prompt(self, state: dict[str, Any], prompt_id: str) -> dict[str, Any]:
        try:
            return state["prompts"][prompt_id]
        except KeyError as exc:
            raise ValueError(f"unknown prompt: {prompt_id}") from exc

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, state: dict[str, Any]) -> None:
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp_path, self.path)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {"prompts": {}, "deployments": {}, "deployment_history": {}, "approvals": {}}


def extract_variables(content: str) -> tuple[str, ...]:
    try:
        fields = {
            field_name.split(".")[0].split("[")[0]
            for _, field_name, _, _ in string.Formatter().parse(content)
            if field_name
        }
    except ValueError as exc:
        raise ValueError(f"invalid prompt template: {exc}") from exc
    return tuple(sorted(fields))


def version_checksum(prompt_id: str, version: int, content: str, variables: tuple[str, ...]) -> str:
    material = json.dumps(
        {"prompt_id": prompt_id, "version": version, "content": content, "variables": variables},
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
