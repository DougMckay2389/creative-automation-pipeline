"""The compliance gate: brand rules and legal rules, scored on what shipped.

Two principles run through this file, and they are the ones worth defending.

**1. Measure the render, not the brief.**
Whether the logo is present, whether the message is legible, whether the
palette is on brand -- these are properties of pixels. A checker that reads
the campaign YAML and pronounces the creative compliant will go green while
the artwork is wrong, which is worse than no checker at all.

**2. A rule that always fires is worse than no rule.**
BRAND-001 does not test for exact hex equality. Any gradient, resize or JPEG
round-trip moves every pixel off-swatch, so exact matching flags 100% of
assets, everyone learns to ignore the report, and the gate becomes theatre.
It measures perceptual distance to the nearest approved swatch instead, with a
tolerance the brand team owns.

Nothing here auto-approves anything. The output is a sorted queue for humans
plus a remediation list -- "your reviewers stop seeing the half that were
never going to pass" is a very different pitch from "AI approves your ads".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    BLOCKER = "blocker"      # do not ship
    MAJOR = "major"          # a human must look
    MINOR = "minor"          # note it, keep moving


class Verdict(str, Enum):
    PASS = "pass"
    REVIEW = "review"
    BLOCK = "block"


@dataclass
class Finding:
    rule_id: str
    severity: Severity
    message: str
    routes_to: str           # which desk fixes it

    def as_dict(self) -> dict:
        return {"rule": self.rule_id, "severity": self.severity.value,
                "message": self.message, "routes_to": self.routes_to}


@dataclass
class CheckResult:
    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)

    def routing(self) -> list[str]:
        return sorted({f.routes_to for f in self.findings})


# --------------------------------------------------------------------------
# Colour distance
# --------------------------------------------------------------------------

def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def redmean_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """A cheap perceptual colour distance.

    Plain Euclidean distance in RGB says pure blue and pure green are as
    different as two greys a shade apart, which is not how eyes work. Redmean
    weights the channels by the average red level and is the standard
    low-dependency approximation -- close enough to CIE76 for a brand
    tolerance, and it needs no colour-science library.
    """
    rm = (a[0] + b[0]) / 2
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return (((2 + rm / 256) * dr * dr) + (4 * dg * dg) +
            ((2 + (255 - rm) / 256) * db * db)) ** 0.5


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------

RULES = {
    "BRAND-001": "Dominant colours sit within the approved palette",
    "BRAND-002": "Logo is present on the creative",
    "BRAND-003": "Logo clearspace meets the brand minimum",
    "LEGAL-001": "Campaign copy contains no prohibited terms",
    "SPEC-001":  "Delivered pixel dimensions match the requested spec",
    "SPEC-002":  "Campaign message is present",
    "SPEC-003":  "Message type size is at or above the legibility floor",
}


def rule_catalogue() -> list[str]:
    return sorted(RULES)


def evaluate(comp, variant, brand: dict, prohibited: list[str]) -> CheckResult:
    """Run every rule. A rule that raises flags for review -- never passes.

    Fail-open is the one unacceptable failure mode in a compliance tool: a
    crash must not become a green light.
    """
    findings: list[Finding] = []

    def guard(fn):
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            findings.append(Finding(
                "SYS-001", Severity.MAJOR,
                f"check raised and could not complete: {type(exc).__name__}: {exc}",
                "engineering"))

    # --- SPEC-001: did we deliver the pixels we promised? -----------------
    def spec_dims():
        if (comp.width, comp.height) != (variant.ratio.width, variant.ratio.height):
            findings.append(Finding(
                "SPEC-001", Severity.BLOCKER,
                f"delivered {comp.width}x{comp.height}, spec is "
                f"{variant.ratio.width}x{variant.ratio.height}",
                "engineering"))
    guard(spec_dims)

    # --- SPEC-002 / SPEC-003: message present and legible -----------------
    def spec_message():
        if not (comp.message or "").strip():
            findings.append(Finding(
                "SPEC-002", Severity.BLOCKER, "no campaign message rendered", "creative"))
            return
        floor = float(brand.get("min_message_height", 0.045)) * min(comp.width, comp.height)
        if comp.message_px_height < floor:
            findings.append(Finding(
                "SPEC-003", Severity.MAJOR,
                f"message cap height {comp.message_px_height:.0f}px is below the "
                f"{floor:.0f}px legibility floor for this spec",
                "creative"))
    guard(spec_message)

    # --- BRAND-002 / BRAND-003: logo -------------------------------------
    def brand_logo():
        if comp.logo_box is None:
            findings.append(Finding(
                "BRAND-002", Severity.BLOCKER, "no logo on the creative", "brand"))
            return
        min_clear = float((brand.get("logo") or {}).get("min_clearspace", 0.5))
        if comp.logo_clearspace_ratio < min_clear:
            findings.append(Finding(
                "BRAND-003", Severity.MINOR,
                f"logo clearspace {comp.logo_clearspace_ratio:.2f} of logo height, "
                f"brand minimum is {min_clear:.2f}",
                "brand"))
    guard(brand_logo)

    # --- BRAND-001: palette ----------------------------------------------
    def brand_palette():
        swatches = [hex_to_rgb(s["hex"]) for s in (brand.get("palette") or [])
                    if s.get("hex")]
        if not swatches:
            return                       # brand kit defines no palette: nothing to check
        if comp.dominant_hex is None:
            # Not "the image has no colours" -- it means measurement did not
            # run. Treat a missing measurement as a defect, never as a pass.
            raise ValueError("dominant colours were never measured for this creative")
        if not comp.dominant_hex:
            findings.append(Finding(
                "BRAND-001", Severity.MAJOR,
                "no dominant colours could be measured; palette not verified", "engineering"))
            return
        tol = float(brand.get("palette_tolerance", 120.0))
        off = []
        for hx in comp.dominant_hex:
            rgb = hex_to_rgb(hx)
            nearest = min(redmean_distance(rgb, s) for s in swatches)
            if nearest > tol:
                off.append((hx, nearest))
        coverage = 1.0 - (len(off) / len(comp.dominant_hex))
        if coverage < float(brand.get("min_palette_coverage", 0.55)):
            worst = ", ".join(f"{h} (Δ{d:.0f})" for h, d in off[:3])
            findings.append(Finding(
                "BRAND-001", Severity.MINOR,
                f"{coverage:.0%} of dominant colours are on palette; off-palette: {worst}",
                "brand"))
    guard(brand_palette)

    # --- LEGAL-001: prohibited copy --------------------------------------
    def legal_copy():
        hay = (comp.message or "").lower()
        hits = [t for t in prohibited if t and t.lower() in hay]
        if hits:
            findings.append(Finding(
                "LEGAL-001", Severity.BLOCKER,
                f"prohibited term(s) in campaign copy: {', '.join(hits)}", "legal"))
    guard(legal_copy)

    # --- anything the composer itself flagged -----------------------------
    for w in getattr(comp, "warnings", []) or []:
        findings.append(Finding("SYS-002", Severity.MAJOR, w, "engineering"))

    if any(f.severity is Severity.BLOCKER for f in findings):
        verdict = Verdict.BLOCK
    elif findings:
        verdict = Verdict.REVIEW
    else:
        verdict = Verdict.PASS
    return CheckResult(verdict=verdict, findings=findings)


# --------------------------------------------------------------------------
# Pre-flight: the cheapest check is the one that runs before you spend money
# --------------------------------------------------------------------------

def preflight_brief(brief) -> list[Finding]:
    """Catch what we can before a single generative call is made.

    At Firefly's documented 4 requests/minute, a wasted call is a wasted
    minute. Copy that can never clear legal should never reach the generator.
    """
    findings: list[Finding] = []
    for m in brief.markets:
        hay = m.message.lower()
        hits = [t for t in brief.prohibited_terms if t and t.lower() in hay]
        if hits:
            findings.append(Finding(
                "LEGAL-001", Severity.BLOCKER,
                f"[{m.locale}] prohibited term(s) in brief copy: {', '.join(hits)}",
                "legal"))
    for p in brief.products:
        if p.asset and not p.has_asset():
            findings.append(Finding(
                "SYS-003", Severity.MINOR,
                f"[{p.id}] brief points at '{p.asset}' which is not on disk; "
                f"this product will be generated",
                "engineering"))
    return findings
