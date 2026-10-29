"""Deterministic numeric grounding of a submitted RCA conclusion (2026-10-19, measurement only).

Every number the conclusion states (a quantity with a unit, a time of day, or a bare count >= 10) is looked up in the
observations the episode saw before its final submit:
  verified_cited     the value appears in an observation that carries one of the cited support refs
  verified_anywhere  the value appears in some other observation before the submit
  derived            a ratio, percent change or difference of two grounded numbers the conclusion states
  unchecked          a multiple ("53x") stated without its operands (not decidable mechanically, never flagged)
  unverified         none of the above: the number is not in anything the agent observed

This is an existence check, not a semantic judgement: it says nothing about whether the observations support the
inference (the evidence judge does that). Identifiers (UUIDs, hashes, IPs, versions, dates, ref strings) are removed
before extraction. Tolerances follow the precision the claim was written with ("2.1s" matches 2.05–2.15 s or the same
duration in ms; "86MB" matches 85.5–86.5 MB or the same size in bytes/KiB/MiB).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# identifiers that contain digits but are not measurements
_ID_PATTERNS = [
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",   # uuid
    r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b",                                # ip[:port]
    r"\bv?\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.]+)?\b",                          # version
    r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{6,}\b",                   # hex hash / k8s suffix
    r"\b[A-Za-z_][\w.-]*\d[\w.-]*\b",                                       # word containing digits (ORA-00001, f41-r, http2)
]
_DATE = r"\b\d{4}-\d{2}-\d{2}(?=\D|$)"
_TIME = r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?\s*(?:Z|UTC|KST)?(?![\d:])"
_QTY = (r"(?<![\w.#/,])([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*"
        r"(ns|us|µs|μs|ms|msec|밀리초|s|sec|secs|seconds?|초|min|mins|minutes?|분|h|hrs?|hours?|시간|%|퍼센트|"
        r"[KMGT]i?B|[kKMGT]B|[Bb]ytes?|B|x|배|rps|req/s|/s|ops/s|/min|/m|건|개|회|번|만|억)?(?![A-Za-z0-9_])")   # Korean particles may follow

_TIME_MS = {"ns": 1e-6, "us": 1e-3, "µs": 1e-3, "μs": 1e-3, "ms": 1, "msec": 1, "밀리초": 1, "s": 1000, "sec": 1000, "secs": 1000, "second": 1000, "seconds": 1000, "초": 1000,
            "min": 60000, "mins": 60000, "minute": 60000, "minutes": 60000, "분": 60000,
            "h": 3600000, "hr": 3600000, "hrs": 3600000, "hour": 3600000, "hours": 3600000, "시간": 3600000}
_SIZE_B = {"B": 1, "byte": 1, "bytes": 1, "Byte": 1, "Bytes": 1, "KB": 1e3, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
           "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


@dataclass
class NumericClaim:
    text: str
    value: float
    unit: str            # "time_ms" | "size_b" | "percent" | "ratio" | "clock_s" | "count" | "rate"
    tolerance: float     # absolute, in the unit's base
    status: str = "unverified"
    context: str = ""


@dataclass
class GroundingResult:
    claims: list[NumericClaim] = field(default_factory=list)

    @property
    def unverified(self) -> list[NumericClaim]:
        return [c for c in self.claims if c.status == "unverified"]

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"numeric_claims": len(self.claims)}
        for s in ("verified_cited", "verified_anywhere", "derived", "unchecked", "unverified"):
            out[s] = sum(c.status == s for c in self.claims)
        out["unverified_examples"] = [f"{c.text} | {c.context}" for c in self.unverified[:5]]
        return out


def _strip_ids(text: str) -> str:
    for pat in _ID_PATTERNS:
        text = re.sub(pat, " ", text)
    return re.sub(_DATE, " ", text)


def _decimals(raw: str) -> int:
    return len(raw.split(".", 1)[1]) if "." in raw else 0


def extract_claims(text: str) -> list[NumericClaim]:
    """Numbers stated in a conclusion, normalised to a base unit with a precision-based tolerance."""
    claims: list[NumericClaim] = []
    clean = re.sub(_DATE + r"[T ]?", " ", text)          # ISO dates (time part kept)
    for m in re.finditer(_TIME, clean):
        h, mi, se, frac = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        if h > 23 or mi > 59:
            continue
        secs = h * 3600 + mi * 60 + (int(se) if se else 0)
        # minute-level: series are bucketed (20 s / 1 min), so a second-precise time inside a bucket is not invented
        claims.append(NumericClaim(m.group(0).strip(), float(secs), "clock_s", 60.0,
                                   context=clean[max(0, m.start() - 40):m.end() + 40].strip()))
    rest = re.sub(_TIME, " ", clean)
    rest = _strip_ids(rest)
    for m in re.finditer(_QTY, rest):
        raw, unit = m.group(1), (m.group(2) or "")
        try:
            v = float(raw.replace(",", ""))   # prose thousands separators ("3,954ms")
        except ValueError:
            continue
        # a written value may be rounded or truncated from the observation: one step of its last digit, or 1 %
        half_step = max(10 ** (-_decimals(raw)), 0.01 * abs(v))
        ctx = rest[max(0, m.start() - 40):m.end() + 40].strip()
        if unit in _TIME_MS:
            k = _TIME_MS[unit]; claims.append(NumericClaim(m.group(0).strip(), v * k, "time_ms", half_step * k, context=ctx))
        elif unit in _SIZE_B:
            k = _SIZE_B[unit]; claims.append(NumericClaim(m.group(0).strip(), v * k, "size_b", half_step * k, context=ctx))
        elif unit in ("%", "퍼센트"):
            claims.append(NumericClaim(m.group(0).strip(), v, "percent", max(half_step, 0.5), context=ctx))
        elif unit in ("x", "배"):
            claims.append(NumericClaim(m.group(0).strip(), v, "ratio", max(half_step, 0.05 * abs(v)), context=ctx))
        elif unit in ("만", "억"):   # Korean myriads: 360만 = 3,600,000
            k = 1e4 if unit == "만" else 1e8
            claims.append(NumericClaim(m.group(0).strip(), v * k, "count", max(half_step * k, 0.01 * abs(v * k)), context=ctx))
        elif unit in ("rps", "req/s", "/s", "ops/s"):
            claims.append(NumericClaim(m.group(0).strip(), v, "rate", max(half_step, 0.02 * abs(v)), context=ctx))
        elif unit in ("/min", "/m"):   # per minute, compared as per second too
            claims.append(NumericClaim(m.group(0).strip(), v / 60, "rate", max(half_step, 0.02 * abs(v)) / 60, context=ctx))
        elif abs(v) >= 10:   # bare counts: small integers (2 pods, 3 retries) are too common to check
            claims.append(NumericClaim(m.group(0).strip(), v, "count", max(half_step, 0.01 * abs(v)), context=ctx))
    return claims


def observed_numbers(text: str) -> tuple[list[float], list[float]]:
    """(plain numbers, clock times in seconds of day) found in an observation; both UTC and KST clocks are kept."""
    # observations are mostly JSON: commas separate values, so they are never thousands separators here. Dates,
    # timestamps and identifiers are removed first so their digit groups ("2026-10-11") cannot match a quantity.
    plain = re.sub(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?", " ", text)
    plain = re.sub(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?![\d:])", " ", plain)
    for pat in _ID_PATTERNS[:4]:
        plain = re.sub(pat, " ", plain)
    nums = [float(x) for x in re.findall(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", plain)]
    clocks = []
    for m in re.finditer(r"(\d{1,2}):(\d{2}):(\d{2})", text):
        h, mi, se = map(int, m.groups())
        if h < 24 and mi < 60 and se < 60:
            s = h * 3600 + mi * 60 + se
            clocks += [s, (s + 9 * 3600) % 86400]          # observations are mostly UTC, answers may use KST
    for m in re.finditer(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])", text):
        h, mi = map(int, m.groups())
        if h < 24 and mi < 60:
            s = h * 3600 + mi * 60
            clocks += [s, (s + 9 * 3600) % 86400]
    for m in re.finditer(r"\b(1[6-9]\d{8})(\d{3})?(?:\.\d+)?\b", text):   # epoch seconds or milliseconds
        s = int(m.group(1)) % 86400
        clocks += [s, (s + 9 * 3600) % 86400]
    return nums, clocks


def _scales(claim: NumericClaim) -> list[float]:
    if claim.unit == "time_ms":
        return [1, 1e-3, 1 / 60000, 1 / 3600000, 1e3, 1e6]        # ms, s, min, h, us, ns
    if claim.unit == "size_b":
        return [1, 1e-3, 1e-6, 1e-9, 1 / 1024, 1 / 1024 ** 2, 1 / 1024 ** 3]
    if claim.unit == "percent":
        return [1, 0.01]                                          # 45 % or a 0.45 ratio
    if claim.unit == "rate":
        return [1, 60]                                            # per second or per minute
    if claim.unit == "count" and claim.value != int(claim.value):
        return [1, 1e3, 1e-3]          # a unitless decimal often borrows the s/ms unit of its neighbour
    return [1]


def _matches(claim: NumericClaim, nums: Iterable[float], clocks: Iterable[float]) -> bool:
    if claim.unit == "clock_s":
        return any(min(abs(c - claim.value), 86400 - abs(c - claim.value)) <= claim.tolerance for c in clocks)
    for k in _scales(claim):
        target, tol = claim.value * k, claim.tolerance * k
        if any(abs(n - target) <= tol + 1e-12 for n in nums):
            return True
    return False


def _derived(claim: NumericClaim, base: list[float]) -> bool:
    """A ratio ("3.2x"), percent change ("+45%") or difference ("(-65,634,304)") of two numbers the conclusion itself
    states and that are grounded. Deriving from the whole observation pool would match almost anything (hundreds of
    values give tens of thousands of pairs), so only the conclusion's own grounded numbers are used."""
    vals = sorted({abs(b) for b in base if b and math.isfinite(b)})
    t, tol = abs(claim.value), claim.tolerance
    for i, a in enumerate(vals):
        for b in vals[i + 1:]:
            if claim.unit == "ratio" and abs(b / a - t) <= tol:
                return True
            if claim.unit == "percent" and (abs((b / a - 1) * 100 - t) <= tol or abs((1 - a / b) * 100 - t) <= tol):
                return True
            if claim.unit in ("count", "size_b", "time_ms") and abs((b - a) - t) <= tol:
                return True
    return False


def conclusion_text(conclusion: dict[str, Any]) -> str:
    parts = [str(conclusion.get("summary", ""))]
    for c in conclusion.get("causes", []) or []:
        parts += [str(c.get(k, "")) for k in ("mechanism", "description", "summary", "name") if c.get(k)]
    for c in conclusion.get("external_causes", []) or []:
        parts += [str(c.get(k, "")) for k in ("mechanism", "description", "name") if c.get(k)]
    return "\n".join(p for p in parts if p)


def cited_refs(conclusion: dict[str, Any]) -> list[str]:
    refs = [r for c in conclusion.get("causes", []) or [] for r in c.get("support_refs", []) or []]
    refs += [r for c in conclusion.get("external_causes", []) or [] for r in c.get("evidence_refs", []) or []]
    return [str(r) for r in refs]


def ground(conclusion: dict[str, Any], observations: list[str]) -> GroundingResult:
    """observations: user/tool message contents before the final submit, in order."""
    refs = cited_refs(conclusion)
    cited = [o for o in observations if any(r in o for r in refs)]
    cn, cc = observed_numbers("\n".join(cited))
    an, ac = observed_numbers("\n".join(observations))
    result = GroundingResult(extract_claims(conclusion_text(conclusion)))
    for claim in result.claims:
        if _matches(claim, cn, cc):
            claim.status = "verified_cited"
        elif _matches(claim, an, ac):
            claim.status = "verified_anywhere"
    grounded = [c for c in result.claims if c.status.startswith("verified")]
    for claim in result.claims:
        if claim.status != "unverified":
            continue
        base = [g.value for g in grounded if g.unit == claim.unit or claim.unit in ("ratio", "percent")]
        if _derived(claim, base):
            claim.status = "derived"
        elif claim.unit == "ratio":
            claim.status = "unchecked"   # a multiple stated without its two operands: not decidable mechanically
    return result


def ground_evidence_item(item: dict[str, Any]) -> GroundingResult:
    """Same check on an evidence-judge item (rl_reward.build_trajectory_evidence_item)."""
    # the agent's own tool-call arguments (windows, limits) count as seen: they are not fabricated observations
    obs = [str(r.get("content", "")) + " " + json.dumps((r.get("paired_call") or {}).get("function", {}), ensure_ascii=False)
           for r in item["trajectory_evidence"]["refs"]]
    return ground(item["submitted_conclusion"], obs)
