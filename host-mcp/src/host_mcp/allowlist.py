"""Declarative, safe command allow-list — the heart of the host MCP.

Commands are defined in YAML as fixed argv templates with typed, constrained
arguments. There is no shell and no arbitrary execution: an argument can only
fill a whole `{placeholder}` token, and only after passing its constraints.
Each check declares the minimum *profile* it requires; the server runs at one
active profile and exposes only the checks at or below it.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PROFILES = {"read-only": 0, "diagnostic": 1, "elevated": 2}
_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")
_DEFAULT_TIMEOUT = 20


@dataclass
class ArgSpec:
    type: str = "string"  # "string" | "int"
    required: bool = False
    default: Any = None
    pattern: Optional[str] = None
    enum: Optional[list] = None
    min: Optional[int] = None
    max: Optional[int] = None
    description: str = ""

    def json_schema(self) -> dict[str, Any]:
        if self.type == "int":
            schema: dict[str, Any] = {"type": "integer"}
            if self.min is not None:
                schema["minimum"] = self.min
            if self.max is not None:
                schema["maximum"] = self.max
        else:
            schema = {"type": "string"}
            if self.pattern:
                schema["pattern"] = self.pattern
            if self.enum:
                schema["enum"] = self.enum
        if self.description:
            schema["description"] = self.description
        return schema

    def validate(self, name: str, value: Any) -> tuple[Any, Optional[str]]:
        if value is None:
            if self.required:
                return None, f"missing required argument {name!r}"
            value = self.default
            if value is None:
                return None, f"argument {name!r} has no value"
        if self.type == "int":
            try:
                ival = int(value)
            except (TypeError, ValueError):
                return None, f"argument {name!r} must be an integer"
            if self.min is not None and ival < self.min:
                ival = self.min
            if self.max is not None and ival > self.max:
                ival = self.max
            return ival, None
        sval = str(value)
        if self.enum and sval not in self.enum:
            return None, f"argument {name!r} must be one of {self.enum}"
        if self.pattern and not re.match(self.pattern, sval):
            return None, f"argument {name!r} failed validation"
        return sval, None


@dataclass
class Check:
    name: str
    description: str
    profile: str = "read-only"
    argv: Optional[list[str]] = None
    platform: Optional[dict[str, list[str]]] = None
    args: dict[str, ArgSpec] = field(default_factory=dict)

    def base_argv(self) -> Optional[list[str]]:
        if self.platform:
            return self.platform.get(platform.system())
        return self.argv

    def json_schema(self) -> dict[str, Any]:
        props = {n: spec.json_schema() for n, spec in self.args.items()}
        required = [n for n, spec in self.args.items() if spec.required]
        schema: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        return schema


def _truncate(text: str, max_lines: int = 60, max_chars: int = 4000) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines truncated)"]
    out = "\n".join(lines)
    return out if len(out) <= max_chars else out[:max_chars] + "\n... (truncated)"


class Allowlist:
    def __init__(
        self,
        checks: dict[str, Check],
        active_profile: str = "diagnostic",
        audit_path: Optional[Path] = None,
    ) -> None:
        if active_profile not in PROFILES:
            raise ValueError(f"unknown profile {active_profile!r}; valid: {list(PROFILES)}")
        self._checks = checks
        self.active_profile = active_profile
        self._audit_path = audit_path

    @classmethod
    def from_dict(
        cls, data: dict, active_profile: Optional[str] = None, audit_path: Optional[Path] = None
    ) -> "Allowlist":
        checks: dict[str, Check] = {}
        for name, raw in (data.get("checks") or {}).items():
            args = {
                arg_name: ArgSpec(**arg_raw) for arg_name, arg_raw in (raw.get("args") or {}).items()
            }
            checks[name] = Check(
                name=name,
                description=raw.get("description", name),
                profile=raw.get("profile", "read-only"),
                argv=raw.get("argv"),
                platform=raw.get("platform"),
                args=args,
            )
        profile = active_profile or data.get("profile", "diagnostic")
        return cls(checks, active_profile=profile, audit_path=audit_path)

    def _allowed(self, check: Check) -> bool:
        return PROFILES.get(check.profile, 99) <= PROFILES[self.active_profile]

    def available_checks(self) -> list[Check]:
        return [c for c in self._checks.values() if self._allowed(c)]

    def build_argv(self, name: str, args: dict[str, Any]) -> tuple[Optional[list[str]], Optional[str]]:
        check = self._checks.get(name)
        if check is None or not self._allowed(check):
            return None, f"unknown or unavailable check {name!r}"
        base = check.base_argv()
        if base is None:
            return None, f"check {name!r} is not supported on {platform.system()}"

        validated: dict[str, Any] = {}
        for arg_name, spec in check.args.items():
            value, err = spec.validate(arg_name, args.get(arg_name))
            if err:
                return None, err
            validated[arg_name] = value

        argv: list[str] = []
        for token in base:
            match = _PLACEHOLDER.match(token)
            if match:
                key = match.group(1)
                if key not in validated:
                    return None, f"check {name!r} references unknown argument {key!r}"
                argv.append(str(validated[key]))
            else:
                argv.append(token)
        return argv, None

    def run(self, name: str, args: dict[str, Any], timeout: int = _DEFAULT_TIMEOUT) -> str:
        argv, error = self.build_argv(name, args or {})
        if error:
            self._audit({"ts": time.time(), "check": name, "args": args, "error": error})
            return f"ERROR: {error}"

        started = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)  # type: ignore[arg-type]
            returncode: Optional[int] = proc.returncode
            output = proc.stdout or ""
            if proc.returncode != 0:
                err = (proc.stderr or "").strip()
                output = (output + ("\n" + err if err else "")).strip() or f"(exit {proc.returncode})"
        except FileNotFoundError:
            returncode, output = None, f"command not found: {argv[0]!r}"  # type: ignore[index]
        except subprocess.TimeoutExpired:
            returncode, output = None, f"timed out after {timeout}s"

        self._audit(
            {
                "ts": time.time(),
                "check": name,
                "args": args,
                "argv": argv,
                "returncode": returncode,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        )
        return f"$ {' '.join(argv)}\n\n{_truncate(output)}"  # type: ignore[arg-type]

    def _audit(self, record: dict[str, Any]) -> None:
        if self._audit_path is None:
            return
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
