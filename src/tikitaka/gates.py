"""Human Gates: der Orchestrator hält an, du entscheidest in einer Markdown-Datei."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DECISIONS = ("approve", "changes", "reject", "spec_change")
FEEDBACK_HEADING = "## Dein Feedback"
_DECISION = re.compile(r"^decision:\s*([a-z_]+)", re.M)
_COMMENT = re.compile(r"<!--.*?-->", re.S)


@dataclass
class GateDecision:
    decision: str
    feedback: str


class Gate(Protocol):
    def wait(self, gate_file: Path, title: str, options: tuple[str, ...]) -> GateDecision: ...


def parse_gate_file(text: str, options: tuple[str, ...] = DECISIONS) -> GateDecision | None:
    m = _DECISION.search(text)
    if not m or m.group(1) not in options:
        return None
    feedback = text.split(FEEDBACK_HEADING, 1)[1] if FEEDBACK_HEADING in text else ""
    return GateDecision(m.group(1), _COMMENT.sub("", feedback).strip())


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = f'display notification "{message}" with title "{title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


class FileGate:
    """Wartet, bis in der Gate-Datei `decision:` gesetzt und gespeichert wurde."""

    def __init__(self, notify_user: bool = True, poll_seconds: float = 2.0):
        self.notify_user = notify_user
        self.poll_seconds = poll_seconds

    def wait(self, gate_file: Path, title: str, options: tuple[str, ...]) -> GateDecision:
        print(f"\n⏸  HUMAN GATE: {title}")
        print(f"   Öffne {gate_file}")
        print(f"   Setze 'decision:' auf {' | '.join(options)} und speichere.")
        print("   (Ctrl+C beendet – später mit `tikitaka resume` fortsetzen)\n")
        if self.notify_user:
            notify("tikitaka", f"Human Gate: {title}")
        while True:
            decision = parse_gate_file(gate_file.read_text(), options)
            if decision:
                if decision.decision in ("changes", "spec_change") and not decision.feedback:
                    print("   ⚠  'changes' ohne Feedback – bitte unter '## Dein Feedback' etwas eintragen.")
                    time.sleep(self.poll_seconds * 5)
                    continue
                print(f"   → Entscheidung: {decision.decision}")
                return decision
            time.sleep(self.poll_seconds)


class ScriptedGate:
    """Für Tests/Automatisierung: liefert vorgegebene Entscheidungen der Reihe nach."""

    def __init__(self, decisions: list[GateDecision]):
        self.decisions = list(decisions)
        self.seen: list[str] = []

    def wait(self, gate_file: Path, title: str, options: tuple[str, ...]) -> GateDecision:
        self.seen.append(title)
        return self.decisions.pop(0)
