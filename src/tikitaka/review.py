"""Strukturierte Ausgaben von Buildern und Reviewern lesen und formatieren."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def extract_json(output: str) -> dict | None:
    """Nimmt den letzten ```json-Block aus der Agentenausgabe."""
    for block in reversed(_JSON_BLOCK.findall(output)):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


@dataclass
class Review:
    verdict: str
    summary: str = ""
    findings: list[dict] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.verdict in ("approve", "changes_requested")

    def blocking(self, severities: list[str]) -> list[dict]:
        return [f for f in self.findings if f.get("severity") in severities]

    def non_blocking(self, severities: list[str]) -> list[dict]:
        return [f for f in self.findings if f.get("severity") not in severities]

    def contradiction(self, severities: list[str]) -> str | None:
        """Passt das Urteil nicht zu den Findings, die Erklärung dazu – sonst None."""
        blocking = self.blocking(severities)
        if self.verdict == "approve" and blocking:
            return f"`approve`, aber {len(blocking)} Finding(s) mit Severity {'/'.join(severities)}"
        if self.verdict == "changes_requested" and not blocking:
            return f"`changes_requested`, aber kein Finding mit Severity {'/'.join(severities)}"
        return None


def parse_review(output: str) -> Review:
    data = extract_json(output)
    if data is None:
        return Review(verdict="invalid", summary="Reviewer hat keinen gültigen ```json-Block geliefert.")
    return review_from_data(data)


def review_from_data(data: dict) -> Review:
    """Review aus einem Dict – vom MCP-Tool submit_review oder aus dem JSON-Block im Text."""
    findings = [f for f in data.get("findings") or [] if isinstance(f, dict)]
    for i, f in enumerate(findings, 1):
        f.setdefault("id", f"R{i}")
        f["severity"] = str(f.get("severity", "major")).lower()
    return Review(
        verdict=str(data.get("verdict", "invalid")).lower(),
        summary=str(data.get("summary", "")),
        findings=findings,
    )


def finding_key(f: dict) -> tuple[str, str]:
    return str(f.get("file", "")).strip(), " ".join(str(f.get("issue", "")).lower().split())


_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?")


def _files(f: dict) -> frozenset[str]:
    """Dateien eines Findings ohne Zeilennummern – die verschieben sich zwischen Runden."""
    raw = _LINE_SUFFIX.sub("", str(f.get("file", "")))
    return frozenset(p.strip() for p in re.split(r"[,/]\s+|\s+/\s+", raw) if p.strip())


def _match_keys(f: dict) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = {("text", *finding_key(f))}
    if f.get("id"):
        fid = str(f["id"]).strip().upper()
        keys |= {("id", fid, path) for path in _files(f)} or {("id", fid, "")}
    return keys


def dropped_findings(earlier: list[dict], current: list[dict]) -> list[dict]:
    """Findings aus früheren Runden, die der Reviewer nicht erneut gemeldet hat (ohne Duplikate).

    Als erneut gemeldet gilt ein Finding mit gleichem Text oder gleicher ID in derselben Datei –
    Reviewer formulieren wiederholte Findings oft leicht um („nutzt weiterhin …“).
    """
    seen: set[tuple[str, ...]] = set().union(*(_match_keys(f) for f in current))
    out = []
    for f in earlier:
        keys = _match_keys(f)
        if not keys & seen:
            seen |= keys
            out.append(f)
    return out


def format_findings(findings: list[dict]) -> str:
    if not findings:
        return "_keine_"
    lines = []
    for f in findings:
        loc = f" ({f['file']})" if f.get("file") else ""
        ref = f" [Spec: {f['spec_ref']}]" if f.get("spec_ref") else ""
        rnd = f" _(Runde {f['round']})_" if f.get("round") else ""
        lines.append(f"- **{f['id']}** `{f['severity']}`{rnd}{loc}{ref}: {f.get('issue', '')}")
        if f.get("suggestion"):
            lines.append(f"  - Vorschlag: {f['suggestion']}")
    return "\n".join(lines)


def format_builder_response(data: dict | None) -> str:
    if not data:
        return "_Builder hat keine strukturierte Antwort geliefert._"
    parts = [f"Zusammenfassung: {data.get('summary', '-')}"]
    if data.get("addressed"):
        parts.append("Umgesetzt: " + ", ".join(map(str, data["addressed"])))
    for w in data.get("wont_fix") or []:
        parts.append(f"Abgelehnt {w.get('id', '?')}: {w.get('reason', '')}")
    for q in data.get("questions") or []:
        parts.append(f"Frage/Annahme: {q}")
    return "\n".join(parts)
