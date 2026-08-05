"""Machine-checkable rules for pipeline output.

These do not measure whether the copy is *good* — no automated check does. They
catch the failures that are objectively wrong, so that when you read the output
side by side you are judging craft rather than hunting for broken references.

The fabrication checks matter most: the system prompts forbid inventing
testimonials, review counts and results, and the landing page had exactly that
problem before it was rewritten. A model that ignores those instructions is
disqualifying regardless of how good the prose reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Finding:
    severity: str   # "fail" | "warn"
    rule: str
    detail: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def fail(self, rule: str, detail: str):
        self.findings.append(Finding("fail", rule, detail))

    def warn(self, rule: str, detail: str):
        self.findings.append(Finding("warn", rule, detail))

    @property
    def failures(self):
        return [f for f in self.findings if f.severity == "fail"]

    @property
    def warnings(self):
        return [f for f in self.findings if f.severity == "warn"]


# Claims a pre-launch product cannot make. Case-insensitive substring or regex.
FABRICATION_PATTERNS = [
    (r"\btrusted by\b", "invented social proof"),
    (r"\b\d[\d,]*\+?\s+(happy\s+)?(customers|clients|businesses|users)\b", "invented customer count"),
    (r"\b\d(\.\d)?\s*(\/\s*5|stars?)\b", "invented rating"),
    (r"\b\d[\d,]*\s+reviews?\b", "invented review count"),
    (r"\baward[- ]winning\b", "unverifiable award claim"),
    (r"\b(voted|rated)\s+(the\s+)?(best|number one|#1)\b", "unverifiable ranking"),
    (r"\bas seen (in|on)\b", "unverifiable press claim"),
    (r"\b\d+%\s+of\s+(our\s+)?(customers|clients)\b", "invented statistic"),
]

# Phrases the system prompts explicitly ban.
BANNED_PHRASES = [
    "unlock", "elevate", "game-changer", "game changer",
    "in today's fast-paced world", "look no further", "dive in",
    "revolutionise", "revolutionize", "supercharge",
]

# American spellings that shouldn't appear in British-English copy.
US_SPELLINGS = [
    "customize", "customized", "organize", "organized", "personalize",
    "personalized", "optimize", "optimized", "color", "colors", "favorite",
    "center", "flavor", "flavors", "specialty", "maximize", "analyze",
]


def _walk_strings(node, path="") -> list[tuple[str, str]]:
    out = []
    if isinstance(node, str):
        out.append((path, node))
    elif isinstance(node, dict):
        for k, v in node.items():
            out.extend(_walk_strings(v, f"{path}.{k}" if path else k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_walk_strings(v, f"{path}[{i}]"))
    return out


def check_text_hygiene(outputs: dict, report: Report) -> None:
    for path, text in _walk_strings(outputs):
        low = text.lower()
        for pattern, why in FABRICATION_PATTERNS:
            m = re.search(pattern, low)
            if m:
                report.fail("fabrication", f"{path}: {why} — {m.group(0)!r}")
        for phrase in BANNED_PHRASES:
            if phrase in low:
                report.warn("banned-phrase", f"{path}: {phrase!r}")
        for word in US_SPELLINGS:
            if re.search(rf"\b{word}\b", low):
                report.warn("us-spelling", f"{path}: {word!r}")


def check_step3(step3: dict, formats: dict, report: Report) -> None:
    stages = step3.get("funnel_stages") or []
    if not stages:
        report.fail("step3", "no funnel stages")
    for stage in stages:
        for fid in stage.get("formats", []):
            if fid not in formats:
                report.fail("step3", f"unknown format id {fid!r} in stage {stage.get('stage')!r}")
        if not stage.get("kpi"):
            report.warn("step3", f"stage {stage.get('stage')!r} has no KPI")

    split = step3.get("budget_split") or []
    total = sum(b.get("percent", 0) for b in split)
    if split and total != 100:
        report.fail("step3", f"budget_split sums to {total}, not 100")


def check_budget_honesty(step3: dict, budget, report: Report) -> None:
    """A £0 brief must not produce a plan that quietly assumes ad spend."""
    if budget not in (0, "0", None):
        return
    blob = " ".join(t for _p, t in _walk_strings(step3)).lower()
    if re.search(r"£\s*[1-9]", blob) or "ad spend" in blob or "ad budget" in blob:
        report.warn(
            "zero-budget",
            "plan references spend despite a £0 budget — check it is organic-only",
        )


def check_voice_gating(step3: dict, brand_voice: dict, report: Report) -> None:
    """meme-overlay is only appropriate when the brand voice is playful."""
    chosen = {f for s in step3.get("funnel_stages", []) for f in s.get("formats", [])}
    if "meme-overlay" in chosen and brand_voice.get("humor") != "Playful":
        report.fail(
            "voice-gating",
            f"chose meme-overlay but brand humour is {brand_voice.get('humor')!r}",
        )


def check_step5(step5: dict, timeframe_days: int, asset_ids: set[str], report: Report) -> None:
    calendar = step5.get("30_day_calendar") or []
    if not calendar:
        report.fail("step5", "empty calendar")
    for entry in calendar:
        day = entry.get("day")
        if isinstance(day, int) and day > timeframe_days:
            report.fail("step5", f"calendar day {day} exceeds the {timeframe_days}-day timeframe")
        if asset_ids and entry.get("asset_id") not in asset_ids:
            report.fail("step5", f"calendar references unknown asset_id {entry.get('asset_id')!r}")
    if not step5.get("launch_checklist"):
        report.warn("step5", "empty launch checklist")
    if not step5.get("success_metrics"):
        report.warn("step5", "no success metrics")


def check_assets(assets: list[dict], report: Report) -> None:
    if len(assets) < 15:
        report.warn("assets", f"{len(assets)} assets — the blueprint's DoD is 15+")
    for a in assets:
        content = a.get("content", {})
        if not content.get("hook") or not content.get("body") or not content.get("cta"):
            report.fail("assets", f"asset {a.get('format')} is missing hook/body/cta")
        if a.get("type") in ("ad_copy", "email", "landing_copy") and content.get("hashtags"):
            report.warn("assets", f"{a.get('type')} has hashtags, which it should not")
        if a.get("type") == "email":
            hook = content.get("hook", "")
            if len(hook) > 42:
                report.warn("assets", f"email subject is {len(hook)} chars (guidance: under 42)")
