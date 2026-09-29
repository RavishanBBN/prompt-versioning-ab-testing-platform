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
