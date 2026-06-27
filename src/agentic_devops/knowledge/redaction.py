"""Secret redaction at ingest (Knowledge Memory, Phase C).

The safety gate that makes crawling real repos / AWS accounts possible: every
document and every fact deposit passes through here *before* it is persisted or
embedded, so a secret never lands in the store. We document the *mechanism and
location* of a secret, never its value.

Two detection tiers:

- **Tier 1 — high-confidence patterns** (AWS keys, GitHub/Slack/Google tokens,
  JWTs, bearer tokens, PEM private keys, ``secret=…`` assignments). These are
  unambiguous, so they are redacted **inline** and ingestion proceeds — the value
  is replaced with a typed placeholder ``«REDACTED:<kind>»``.

- **Tier 2 — high-entropy heuristic** (the catch-all for novel/secret-shaped
  blobs). Tuned **conservative** to minimize false positives: it ignores known-
  safe shapes (git SHAs, UUIDs, hex hashes) and only flags mixed-class,
  sufficiently-random tokens. In ``fail_closed`` mode a Tier-2 hit **quarantines**
  the document (it is not ingested; a human reviews); in ``best_effort`` mode it
  is redacted inline like Tier 1.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

_PLACEHOLDER = "«REDACTED:{kind}»"


class RedactionQuarantine(Exception):
    """Raised by callers that must reject (not silently redact) a quarantined input
    — e.g. a fact deposit. Carries a human-readable summary of what tripped it."""

    def __init__(self, summary: str) -> None:
        super().__init__(f"quarantined: suspected secret ({summary})")
        self.summary = summary


@dataclass
class RedactionResult:
    text: str
    findings: dict[str, int] = field(default_factory=dict)  # kind -> count
    quarantine: bool = False

    @property
    def total(self) -> int:
        return sum(self.findings.values())

    @property
    def summary(self) -> str:
        return ", ".join(f"{k}×{v}" for k, v in sorted(self.findings.items())) or "none"


@dataclass
class _Detector:
    kind: str
    pattern: re.Pattern
    group: int = 0  # group to replace (0 = whole match); others keep surrounding context


# Tier-1 patterns. ``«`` excluded from value classes so a placeholder isn't
# re-matched on a second pass (redaction stays idempotent).
_TIER1: list[_Detector] = [
    _Detector(
        "pem_private_key",
        re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----", re.DOTALL),
    ),
    _Detector("aws_access_key", re.compile(
        r"\b(?:AKIA|ASIA|AROA|AIDA|AGPA|AIPA|ANPA|ANVA|ASCA|ACCA)[A-Z0-9]{16}\b")),
    _Detector("github_token", re.compile(
        r"\b(?:gh[opsur]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,})\b")),
    _Detector("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    _Detector("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    _Detector("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # Keep the "Bearer " prefix, redact the token.
    _Detector("bearer_token", re.compile(
        r"(?i)\bbearer\s+([A-Za-z0-9._~+/=\-]{12,})"), group=1),
    # secret/password/api_key/token = <value> — keep the key+operator, redact value.
    _Detector("secret_assignment", re.compile(
        r"(?i)([A-Za-z0-9_.\-]*(?:secret|passwd|password|api[_-]?key|apikey|client[_-]?secret|access[_-]?token|auth[_-]?token|token)[A-Za-z0-9_.\-]*\s*[:=]\s*['\"]?)([^\s'\"«»]{6,})"),
        group=2),
]

_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")
_SAFE_SHAPES = [
    re.compile(r"^[a-fA-F0-9]{40}$"),  # git SHA-1
    re.compile(r"^[a-fA-F0-9]{64}$"),  # SHA-256
    re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),  # UUID
]


def _shannon(s: str) -> float:
    n = len(s)
    if n == 0:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


class Redactor:
    """Scans text for secrets; redacts Tier-1 inline and applies the configured
    posture to Tier-2 (quarantine vs inline). Stateless and reusable."""

    def __init__(
        self,
        *,
        mode: str = "fail_closed",
        entropy_enabled: bool = True,
        entropy_threshold: float = 4.0,
        entropy_min_len: int = 20,
        entropy_max_len: int = 200,
    ) -> None:
        self.mode = mode if mode in ("fail_closed", "best_effort") else "fail_closed"
        self.entropy_enabled = entropy_enabled
        self.entropy_threshold = entropy_threshold
        self.entropy_min_len = entropy_min_len
        self.entropy_max_len = entropy_max_len

    def scan(self, text: str) -> RedactionResult:
        if not text:
            return RedactionResult(text=text)
        findings: dict[str, int] = {}
        out = text

        for det in _TIER1:
            def repl(m: re.Match, _kind=det.kind, _grp=det.group) -> str:
                findings[_kind] = findings.get(_kind, 0) + 1
                ph = _PLACEHOLDER.format(kind=_kind)
                if _grp == 0:
                    return ph
                whole = m.group(0)
                s, e = m.start(_grp) - m.start(0), m.end(_grp) - m.start(0)
                return whole[:s] + ph + whole[e:]
            out = det.pattern.sub(repl, out)

        quarantine = False
        if self.entropy_enabled:
            hits = self._entropy_hits(out)
            if hits:
                findings["high_entropy"] = findings.get("high_entropy", 0) + len(hits)
                if self.mode == "best_effort":
                    ph = _PLACEHOLDER.format(kind="high_entropy")
                    for tok in hits:
                        out = out.replace(tok, ph)
                else:  # fail_closed
                    quarantine = True

        return RedactionResult(text=out, findings=findings, quarantine=quarantine)

    def _entropy_hits(self, text: str) -> list[str]:
        """Distinct high-entropy tokens that aren't known-safe shapes. Conservative:
        requires mixed character classes (a digit AND a letter) and a length window,
        so hashes, UUIDs, long identifiers, and prose don't trip it."""
        seen: dict[str, None] = {}
        for m in _TOKEN_RE.finditer(text):
            tok = m.group(0)
            if not (self.entropy_min_len <= len(tok) <= self.entropy_max_len):
                continue
            if not (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)):
                continue
            if any(p.match(tok) for p in _SAFE_SHAPES):
                continue
            if _shannon(tok) >= self.entropy_threshold:
                seen.setdefault(tok, None)
        return list(seen)


def apply_redaction(raw: str, redactor: Optional[Redactor]) -> tuple[Optional[str], RedactionResult]:
    """Convenience for document entry points: returns ``(redacted_text, result)``,
    or ``(None, result)`` when the input is quarantined (caller must not persist it).
    A ``None`` redactor is a pass-through (redaction disabled)."""
    if redactor is None:
        return raw, RedactionResult(text=raw)
    result = redactor.scan(raw)
    return (None if result.quarantine else result.text), result
