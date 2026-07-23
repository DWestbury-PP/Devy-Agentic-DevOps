"""Declarative, safe command allow-list — the heart of the host MCP.

Commands are defined in YAML as fixed argv templates with typed, constrained
arguments. There is no shell and no arbitrary execution: an argument can only
fill a whole `{placeholder}` token, and only after passing its constraints.
Each check declares the minimum *profile* it requires; the server runs at one
active profile and exposes only the checks at or below it.
"""

from __future__ import annotations

import json
import os
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
    type: str = "string"  # "string" | "int" | "path"
    required: bool = False
    default: Any = None
    pattern: Optional[str] = None
    enum: Optional[list] = None
    min: Optional[int] = None
    max: Optional[int] = None
    # For type == "path": the allow-listed directory roots a path may resolve
    # inside. The value is realpath-resolved (collapsing `..` and symlinks) and
    # rejected unless it lands within one of these roots — the guard that turns a
    # file read into a *scoped* one, never an arbitrary `cat`.
    roots: Optional[list[str]] = None
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
        if self.type == "path":
            return self._validate_path(name, str(value))
        sval = str(value)
        if self.enum and sval not in self.enum:
            return None, f"argument {name!r} must be one of {self.enum}"
        if self.pattern and not re.match(self.pattern, sval):
            return None, f"argument {name!r} failed validation"
        return sval, None

    def _validate_path(self, name: str, value: str) -> tuple[Any, Optional[str]]:
        # Reject control characters outright, then confine to the allow-listed
        # roots. realpath resolves `..` and symlinks BEFORE the prefix check, so
        # neither traversal nor a symlink inside a root can escape it. Both sides
        # are realpath'd (e.g. macOS /var -> /private/var) so the compare is sound.
        if any(c in value for c in ("\n", "\r", "\x00")):
            return None, f"argument {name!r} failed validation"
        if not self.roots:
            return None, f"argument {name!r} has no allow-listed roots configured"
        real = os.path.realpath(value)
        for root in self.roots:
            rroot = os.path.realpath(root)
            if real == rroot or real.startswith(rroot + os.sep):
                return real, None
        return None, f"argument {name!r} must resolve inside an allow-listed directory"


@dataclass
class Check:
    name: str
    description: str
    profile: str = "read-only"
    argv: Optional[list[str]] = None
    platform: Optional[dict[str, list[str]]] = None
    args: dict[str, ArgSpec] = field(default_factory=dict)
    # Optional per-check subprocess timeout (seconds). A few checks (notably the
    # macOS unified-log query, `log show`) legitimately take longer than the
    # 20s default when scanning a wide time window.
    timeout: Optional[int] = None
    # A state-CHANGING check (restart a service, prune images, …) as opposed to a
    # read-only diagnostic. Mutating checks are exposed ONLY when the server is
    # started with mutations explicitly enabled (see Allowlist.allow_mutations) —
    # a dedicated, default-off switch independent of the read profile. The
    # allow-list still permits only reversible, enumerated verbs; there is never a
    # shell, and no path/volume deletion.
    mutating: bool = False

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
        allow_mutations: bool = False,
    ) -> None:
        if active_profile not in PROFILES:
            raise ValueError(f"unknown profile {active_profile!r}; valid: {list(PROFILES)}")
        self._checks = checks
        self.active_profile = active_profile
        self._audit_path = audit_path
        # The dedicated, single-purpose mutation gate (default OFF). Independent of
        # the read profile: no mutating check is ever exposed, resolved, or run
        # unless this is true — so a SecOps team controls "can this host act?" with
        # one auditable boolean, and the sidecar is incapable of mutation by default.
        self.allow_mutations = allow_mutations

    @classmethod
    def from_dict(
        cls,
        data: dict,
        active_profile: Optional[str] = None,
        audit_path: Optional[Path] = None,
        allow_mutations: bool = False,
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
                timeout=raw.get("timeout"),
                mutating=bool(raw.get("mutating", False)),
            )
        profile = active_profile or data.get("profile", "diagnostic")
        return cls(checks, active_profile=profile, audit_path=audit_path, allow_mutations=allow_mutations)

    def _allowed(self, check: Check) -> bool:
        # Two independent gates: the read profile (how much it can SEE) and the
        # mutation switch (whether it can ACT at all). A mutating check needs BOTH
        # its profile satisfied AND mutations explicitly enabled — so mutations are
        # off by default even at the elevated profile, and never at read-only.
        if PROFILES.get(check.profile, 99) > PROFILES[self.active_profile]:
            return False
        if check.mutating and not self.allow_mutations:
            return False
        return True

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

        check = self._checks.get(name)
        if check is not None and check.timeout:
            timeout = check.timeout

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
