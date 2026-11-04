"""Der Orchestrator: Spec-Loop → Human Gate → Code-Loop → Human Gate → Archiv.

Zustand liegt in <projekt>/.tikitaka/runs/<change>/state.json, damit ein Lauf
nach Abbruch (oder während eines Human Gates) mit `tikitaka resume` weitergeht.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from .config import RUN_DIRS, AgentConfig, Check, Config, apply_project_config, project_config_path
from .gates import FEEDBACK_HEADING, FileGate, Gate
from .harness import (
    AgentResult, _substitute, can_continue, error_tail, has_fake_tool_calls, is_transient_failure, render_prompt,
    run_agent, run_shell, strip_ansi, supports_mcp,
)
from .mcp_server import SERVER_NAME, context_path, current_path, mcp_dir, read_questions, read_result, result_path
from .review import (
    Review, dropped_findings, extract_json, format_builder_response, format_findings, parse_review,
    review_from_data,
)

TERMINAL = ("done", "aborted")
LABEL = {"spec": "Spec", "code": "Code"}
MAX_CHECK_OUTPUT = 8000
CHECK_TIMEOUT = 900
# So lange wartet tikitaka nach dem Schreiben der MCP-Projektkonfiguration, bis der Harness-Dienst sie geladen hat
MCP_CONFIG_SETTLE_SECONDS = 3
# Anmelde-Token, Rate-Limit und Anbieterfehler verschwinden oft beim zweiten Versuch (Muster: transient_patterns)
TRANSIENT_RETRIES = 1
TRANSIENT_DELAY_SECONDS = 5


def _is_tikitaka_mcp_config(path: Path) -> bool:
    """Besteht die Datei nur aus dem tikitaka-MCP-Server (V2-Format)? Dann gehört sie uns."""
    try:
        data = json.loads(path.read_text())
        return (
            set(data) == {"mcp"} and set(data["mcp"]) == {"servers"} and set(data["mcp"]["servers"]) == {SERVER_NAME}
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False

REPAIR_TAIL = 12000
REPAIR_PROMPT = """\
Du bist ein Reviewer. Deine vorige Antwort endete ohne den geforderten JSON-Block.
Lies KEINE Dateien und benutze KEINE Tools. Fasse nur das Ergebnis deiner vorigen Antwort zusammen.

Beende deine Antwort mit genau einem JSON-Block:

```json
{"verdict": "approve | changes_requested", "summary": "...",
 "findings": [{"id": "R1", "severity": "blocker | major | minor", "file": "...",
               "spec_ref": "...", "issue": "...", "suggestion": "..."}]}
```

Enthält deine vorige Antwort noch KEIN abgeschlossenes Urteil, gib genau dies aus:

```json
{"verdict": "incomplete", "summary": "Review nicht abgeschlossen", "findings": []}
```

Ende deiner vorigen Antwort:

"""

# Nachforderung in der fortgesetzten Reviewer-Sitzung (Harness mit resume_args)
CONTINUE_PROMPT = """\
Du hast dein Review noch nicht abgeschlossen.
Lies keine weiteren Dateien. Fälle jetzt dein Urteil auf Basis dessen, was du bereits geprüft hast,
und rufe das Tool `submit_review` auf. Steht dir dieses Tool nicht zur Verfügung, beende deine Antwort
stattdessen mit genau einem JSON-Block im geforderten Format
({"verdict": "approve | changes_requested", "summary": "...", "findings": [...]}).
"""

# Rückfrage, wenn Urteil und Findings nicht zusammenpassen
VERDICT_PROMPT = """\
Dein Review widerspricht sich: {problem}.
Lies keine weiteren Dateien. Entscheide, was gilt, und liefere das Review vollständig neu ab: Gibt es etwas, das eine
neue Runde braucht, melde es mit passender Severity und `changes_requested`, sonst `approve`.
Rufe dafür das Tool `submit_review` auf. Steht es dir nicht zur Verfügung, beende deine Antwort mit genau einem
JSON-Block im geforderten Format ({"verdict": "approve | changes_requested", "summary": "...", "findings": [...]}).

Dein bisheriges Review:

{review}
"""

# Angehängt an den Prompt, wenn ein Review samt Nachforderung ohne Ergebnis blieb
RETRY_NOTE = """

## Achtung: zweiter Versuch

Dein vorheriger Versuch dieses Reviews endete ohne Ergebnis – vermutlich mitten in einem Experiment.
Prüfe zielgerichtet, gib Ausgaben eigener Skripte nur mit `repr()` aus (keine rohen Steuerzeichen) und
rufe `submit_review` auf, **bevor** du weitere Experimente machst. Ein Urteil mit offenen Punkten ist
besser als kein Urteil.
"""

# Zielprojekt-.gitignore: wird ergänzt, nie überschrieben
DEFAULT_IGNORES = (
    ".tikitaka/", ".kiro/", "__pycache__/", "*.pyc", ".venv/", ".pytest_cache/",
    "node_modules/", "dist/", "build/", ".DS_Store", ".env", ".env.*", "!.env.example",
)
# Zählen beim Start nicht als offene Änderungen: gehören tikitaka (oder legt der Kiro-Provider selbst an) ...
OWN_PATHS = (".tikitaka/", ".tikitaka.yaml", ".agent-tandem/", ".agent-tandem.yaml", ".agentflow/", ".kiro/")
# ... oder hat `tikitaka init` frisch angelegt
INIT_PATHS = ("openspec/", "AGENTS.md")
# Für den Hinweis am Gate, wenn der Builder Tests entfernt
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$")
SHRUNK_TEST_LINES = 5
# Nicht im Reviewer-Diff: generiert, binär oder Lockfiles
DIFF_EXCLUDES = (
    "**/__pycache__/**", "**/*.pyc", "**/.venv/**", "**/node_modules/**", "**/dist/**", "**/build/**",
    "**/*.lock", "**/package-lock.json", "**/pnpm-lock.yaml",
)


class AgentError(Exception):
    """Ein Agent ist technisch gescheitert (Login, Absturz, Timeout) – kein inhaltlicher Konflikt."""


class ProjectBusy(ValueError):
    """Im Projekt läuft bereits ein anderer Lauf."""


class DirtyProject(ValueError):
    """Das Projekt hat beim Start offene Änderungen, die sonst mitcommittet würden."""


def acquire_project_lock(project: Path) -> TextIO:
    """Pro Projekt nur ein Lauf: Sie teilen sich Working Tree und Commits, und der Schreibschutz des einen
    würde die Arbeit des anderen zurücksetzen. Das Betriebssystem gibt die Sperre beim Prozessende frei."""
    path = project / ".tikitaka" / "lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.seek(0)
        holder = lock.read().strip() or "unbekannt"
        lock.close()
        raise ProjectBusy(
            f"In {project} ist bereits ein Lauf aktiv ({holder}), auch das Warten am Human Gate zählt dazu. "
            "Erst beenden oder anhalten."
        ) from None
    return lock


def dirty_files(project: Path) -> list[str]:
    """Offene Änderungen, die der erste Commit eines neuen Laufs mitnehmen würde. Ohne Commits oder Repo: keine."""
    if subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True).returncode:
        return []
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True, errors="replace",
    ).stdout
    out = []
    for line in status.splitlines():
        path = line[3:].strip('"')
        if path in OWN_PATHS or (line.startswith("??") and path in INIT_PATHS):
            continue
        out.append(path)
    return out


def dirty_message(project: Path, files: list[str]) -> str:
    shown = ", ".join(files[:10]) + (f" und {len(files) - 10} weitere" if len(files) > 10 else "")
    return f"{project} hat offene Änderungen, die mitcommittet würden: {shown}. Erst committen oder stashen."


class Orchestrator:
    def __init__(
        self,
        cfg: Config,
        project: str | Path,
        change: str,
        *,
        idea: str | None = None,
        gate: Gate | None = None,
        allow_dirty: bool = False,
    ):
        self.project = Path(project).resolve()
        self.cfg = apply_project_config(cfg, self.project)
        self.change = change
        # Ein bestehender Lauf bleibt, wo er ist, auch unter einem alten Verzeichnisnamen
        existing = (self.project / runs / change for runs in RUN_DIRS)
        self.run_dir = next((d for d in existing if (d / "state.json").exists()), self.project / RUN_DIRS[0] / change)
        self.state_file = self.run_dir / "state.json"
        self.gate = gate or FileGate(notify_user=cfg.notify)
        self.allow_dirty = allow_dirty
        self.is_new = not self.state_file.exists()
        self._blocked_steps: list[str] = []
        self._mcp_config_installed: Path | None = None
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text())
        else:
            if not idea:
                raise ValueError(f"Kein Lauf für '{change}' gefunden – für einen neuen Lauf --idea angeben.")
            self.state = {
                "change": change,
                "idea": idea,
                "phase": "spec",
                "cycle": 0,
                "gates": {"spec": 0, "code": 0},
                "bases": {},
                "results": {},
                "pending_feedback": None,
                "history": [],
            }

    # ------------------------------------------------------------------ Ablauf

    def run(self) -> str:
        lock = acquire_project_lock(self.project)
        try:
            lock.truncate(0)
            lock.write(f"{self.change}, PID {os.getpid()}\n")
            lock.flush()
            return self._run_locked()
        finally:
            self._remove_mcp_config()
            lock.close()

    def _run_locked(self) -> str:
        # Nur beim Start: Beim Fortsetzen sind offene Änderungen normal, z.B. ein Builder-Stand mit roten Checks
        if self.is_new and not self.allow_dirty:
            files = dirty_files(self.project)
            if files:
                raise DirtyProject(dirty_message(self.project, files) + " Oder bewusst mit --allow-dirty starten.")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_git()
        if self.is_new:  # Eigene Vorbereitung (.gitignore, Konfiguration) nicht dem Builder zuschreiben
            self._commit("tikitaka: prepare project")
        self._save()
        while self.state["phase"] not in TERMINAL:
            phase = self.state["phase"]
            try:
                if phase in ("spec", "code"):
                    self._run_phase(phase)
                else:
                    self._run_gate(phase.removesuffix("_gate"))
            except AgentError as exc:
                self._log("agent_error", error=str(exc))
                print(f"\n✖ Agent-Fehler: {exc}")
                print(f"  Ursache beheben, dann: uv run tikitaka resume {self.project} --change {self.change}")
                return "error"
            self._save()
        print(f"\n■ Lauf '{self.change}' beendet: {self.state['phase']}")
        return self.state["phase"]

    def _run_phase(self, kind: str) -> None:
        self.state["cycle"] += 1
        cycle = self.state["cycle"]
        feedback = self.state.get("pending_feedback")
        from_human = bool(feedback and feedback["source"] == "human")
        max_rounds = self.cfg.rounds_after_human if from_human else self.cfg.max_agent_rounds
        self.state["bases"].setdefault(kind, self._head())
        self._save()

        print(f"\n═══ Phase {LABEL[kind]} · Zyklus {cycle} · max. {max_rounds} Runde(n) ═══")
        self._blocked_steps = []
        try:
            result = self._loop(kind, cycle, feedback, max_rounds)
        except AgentError:
            # Ein technischer Fehler ist kein Zyklus: Nach `resume` läuft derselbe Zyklus weiter statt c+1
            self.state["cycle"] = cycle - 1
            self._save()
            raise
        if kind == "code":
            result["test_changes"] = self._test_changes(self.state["bases"][kind])
        self.state["results"][kind] = result
        self.state["pending_feedback"] = None
        self._log(f"{kind}_loop", status=result["status"], reason=result.get("reason", ""))

        if result["status"] == "approved":
            if self.cfg.gate(f"after_{kind}"):
                self.state["phase"] = f"{kind}_gate"
            else:
                self._advance(kind)
        elif self.cfg.gate("on_conflict"):
            self.state["phase"] = f"{kind}_gate"
        else:
            print(f"✖ Konflikt ohne Human Gate: {result.get('reason')}")
            self.state["phase"] = "aborted"

    def _advance(self, kind: str) -> None:
        if kind == "spec":
            self.state["phase"] = "code"
        else:
            self._finish()

    def _finish(self) -> None:
        if self.cfg.archive_on_done:
            try:
                p = subprocess.run(
                    ["openspec", "archive", self.change, "--yes"],
                    cwd=self.project, capture_output=True, text=True,
                )
                self._log("archive", exit=p.returncode, output=(p.stdout + p.stderr)[-500:])
                self._commit(f"tikitaka: archive {self.change}")
            except FileNotFoundError:
                self._log("archive", exit=127, output="openspec nicht gefunden")
        self.state["phase"] = "done"

    # ------------------------------------------------------- Builder/Reviewer

    def _loop(self, kind: str, cycle: int, feedback: dict | None, max_rounds: int) -> dict:
        severities = self.cfg.blocking_severities
        blocking: list[dict] = []
        minors: list[dict] = []
        review: Review | None = None
        resp: dict | None = None
        earlier: list[dict] = []  # alle Findings früherer Runden dieses Zyklus, mit "round"
        human_feedback = feedback["text"] if feedback and feedback["source"] == "human" else None

        for rnd in range(1, max_rounds + 1):
            tag = f"{kind}-c{cycle}-r{rnd}"
            print(f"\n── {LABEL[kind]} Runde {rnd}/{max_rounds} ──")

            resp = self._builder(kind, tag, rnd, max_rounds, feedback)
            if self._nothing_built(kind):
                print("  ✗ Der Builder hat nichts geändert – kein Review")
                reason = "Der Builder hat in dieser Phase noch nichts geändert."
                if self._blocked_steps:
                    reason += (
                        f" Der Harness hat Aktionen wegen fehlender Berechtigung abgelehnt ({', '.join(self._blocked_steps)})"
                        " – der Agent braucht eine nicht-interaktive Freigabe (z.B. --auto in write_args)."
                    )
                return {
                    "status": "conflict",
                    "reason": reason,
                    "builder": resp,
                }
            ok, check_output = self._checks(kind, tag, rnd, max_rounds)
            if not ok:
                return {
                    "status": "conflict",
                    "reason": "Automatische Checks bleiben rot.",
                    "check_output": check_output,
                    "builder": resp,
                }

            message = f"tikitaka({kind}): {self.change} cycle {cycle} round {rnd}"
            if feedback and feedback["source"] == "human":
                message += "\n\nHuman feedback:\n" + feedback["text"]
            self._commit(message)

            diff_file, full_diff, diff_range = self._write_diffs(kind, tag)
            review = self._reviewer(
                kind, tag, rnd, max_rounds, diff_file, full_diff, diff_range, resp, check_output, earlier,
                human_feedback,
            )
            if not review.valid:
                return {"status": "conflict", "reason": review.summary, "builder": resp}
            self.state["reviewed"][kind] = self._head()

            blocking = review.blocking(severities)
            minors = review.non_blocking(severities)
            print(f"  ◆ Review: {review.verdict} · {len(blocking)} blockierend · {len(review.findings)} gesamt")
            if not blocking and review.verdict != "approve":
                return {
                    "status": "conflict",
                    "reason": f"Das Review widerspricht sich: {review.contradiction(severities)}.",
                    "summary": review.summary,
                    "minors": minors,
                    "earlier_minors": self._dropped_minors(earlier, minors, severities),
                    "builder": resp,
                    "check_output": check_output,
                }
            if not blocking:
                return {
                    "status": "approved",
                    "summary": review.summary,
                    "minors": minors,
                    "earlier_minors": self._dropped_minors(earlier, minors, severities),
                    "builder": resp,
                    "check_output": check_output,
                }
            earlier += [{**f, "round": rnd} for f in review.findings]
            feedback = {"source": "reviewer", "text": format_findings(blocking)}

        return {
            "status": "conflict",
            "reason": f"Nach {max_rounds} Runde(n) noch {len(blocking)} blockierende Finding(s).",
            "summary": review.summary if review else "",
            "open": blocking,
            "minors": minors,
            "earlier_minors": self._dropped_minors(earlier, blocking + minors, severities),
            "builder": resp,
        }

    @staticmethod
    def _dropped_minors(earlier: list[dict], current: list[dict], severities: list[str]) -> list[dict]:
        earlier_minors = [f for f in earlier if f.get("severity") not in severities]
        return dropped_findings(earlier_minors, current)

    def _builder(self, kind: str, tag: str, rnd: int, max_rounds: int, feedback: dict | None) -> dict:
        values = {
            "change": self.change,
            "idea": self.state["idea"],
            "round": rnd,
            "max_rounds": max_rounds,
            "feedback": feedback["text"] if feedback else "Keines – erste Runde.",
            "feedback_source": feedback["source"] if feedback else "none",
            "run_dir": self._rel(self.run_dir),
        }
        human = feedback["text"] if feedback and feedback["source"] == "human" else None
        name = f"{tag}-build"
        res = self._agent(f"{kind}_builder", name, values, soft_timeout=True, mcp_context={"human_feedback": human})
        data = read_result(self.run_dir, name) or extract_json(res.output) or {}
        if res.timed_out:
            data.setdefault("summary", "Builder lief ins Timeout – Ergebnis nur anhand der Änderungen und Checks bewertbar.")
        (self.run_dir / f"{tag}-builder.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return data

    def _reviewer(
        self, kind: str, tag: str, rnd: int, max_rounds: int,
        diff_file: Path, full_diff: Path, diff_range: str, resp: dict, check_output: str, earlier: list[dict],
        human_feedback: str | None,
    ) -> Review:
        incremental = diff_file != full_diff
        scope = "seit deinem letzten Review" if incremental else "gesamte Phase"
        values = {
            "change": self.change,
            "idea": self.state["idea"],
            "round": rnd,
            "max_rounds": max_rounds,
            "diff_file": self._rel(diff_file),
            # Bereich mit angeben: alles ist bereits committet, ein `git diff` ohne Bereich ist leer
            "diff_scope": f"{scope}, Commits `{diff_range}` – bereits committet, `git diff` ohne Bereich ist leer",
            "full_diff_file": self._rel(full_diff),
            "builder_response": format_builder_response(resp),
            "previous_findings": format_findings(earlier) if earlier else "_keine – erste Runde_",
            "human_feedback": human_feedback or "_keines in diesem Zyklus_",
            "check_output": check_output or "(keine Checks konfiguriert)",
            "run_dir": self._rel(self.run_dir),
        }
        role = f"{kind}_reviewer"
        context = {"previous_findings": earlier, "human_feedback": human_feedback}
        session = str(uuid4())  # eigene ID: `--continue` könnte eine fremde Sitzung im selben Verzeichnis treffen
        resumable = can_continue(self.cfg.harnesses[self.cfg.agents[role].harness])

        def run(name: str, **kwargs) -> tuple[Review, str]:
            output = self._agent(role, name, values, mcp_context=context, session_id=session, **kwargs).output
            data = read_result(self.run_dir, name)  # submit_review über MCP hat Vorrang vor JSON im Text
            return (review_from_data(data) if data else parse_review(output)), output

        review, output = run(f"{tag}-review")
        if not review.valid:
            # Erst nur das Ergebnis nachfordern (Sekunden), erst danach das ganze Review wiederholen (Minuten).
            # Kann der Harness die Sitzung fortsetzen, hat der Reviewer dabei seine ganze Arbeit im Kontext.
            print("  ↻ Kein gültiges Review-Ergebnis – fordere nur das Ergebnis nach")
            if resumable:
                review, _ = run(f"{tag}-review-repair", prompt=CONTINUE_PROMPT, resume=True)
            else:
                review, _ = run(f"{tag}-review-repair", prompt=REPAIR_PROMPT + output[-REPAIR_TAIL:])
        if not review.valid:
            print("  ↻ Nachforderung ohne Ergebnis – wiederhole das Review")
            agent = self.cfg.agents[role]
            retry_prompt = render_prompt(agent.prompt.read_text(), values) + RETRY_NOTE
            session = str(uuid4())  # neuer Versuch, neue Sitzung
            review, _ = run(f"{tag}-review-retry", prompt=retry_prompt)
        problem = review.contradiction(self.cfg.blocking_severities) if review.valid else None
        if problem:
            print(f"  ↻ Review widerspricht sich ({problem}) – frage nach")
            current = json.dumps(
                {"verdict": review.verdict, "summary": review.summary, "findings": review.findings},
                indent=2, ensure_ascii=False,
            )
            prompt = VERDICT_PROMPT.replace("{problem}", problem).replace("{review}", current)
            fixed, _ = run(f"{tag}-review-verdict", prompt=prompt, resume=resumable)
            if fixed.valid:
                review = fixed
        (self.run_dir / f"{tag}-review.json").write_text(
            json.dumps(review.__dict__, indent=2, ensure_ascii=False)
        )
        return review

    def _agent(
        self, role: str, name: str, values: dict, *,
        prompt: str | None = None, soft_timeout: bool = False, session_id: str | None = None, resume: bool = False,
        mcp_context: dict | None = None,
    ) -> AgentResult:
        agent = self.cfg.agents[role]
        harness = self.cfg.harnesses[agent.harness]
        if prompt is None:
            prompt = render_prompt(agent.prompt.read_text(), values)
        mcp = self._mcp_launch(role, name, mcp_context) if self.cfg.mcp and supports_mcp(harness) else None
        if mcp is not None:
            self._install_mcp_config(harness, mcp)
        print(f"  ▶ {role} [{agent.harness}{' · ' + agent.model if agent.model else ''}] {name}")
        self._log("agent_start", role=role, step=name)
        env = {
            "TIKITAKA_ROLE": role,
            "TIKITAKA_CHANGE": self.change,
            "TIKITAKA_ROUND": str(values.get("round", "")),
            "TIKITAKA_FEEDBACK_SOURCE": str(values.get("feedback_source", "")),
            "TIKITAKA_RUN_DIR": str(self.run_dir),
        }
        snapshot = self._guard_snapshot()
        try:
            for attempt in range(1, TRANSIENT_RETRIES + 2):
                res = run_agent(
                    harness, agent, prompt,
                    cwd=self.project,
                    log_file=self.run_dir / "logs" / f"{name}.log",
                    env=env,
                    timeout=self.cfg.timeout_seconds,
                    session_id=session_id,
                    resume=resume,
                    mcp=mcp,
                )
                if attempt > TRANSIENT_RETRIES or not is_transient_failure(harness, res):
                    break
                reason = (error_tail(res.output) or [f"Exit-Code {res.returncode}"])[-1]
                print(f"  ↻ Vorübergehender Fehler ({reason}) – neuer Versuch in {TRANSIENT_DELAY_SECONDS}s")
                self._log("agent_retry", role=role, step=name, reason=reason)
                time.sleep(TRANSIENT_DELAY_SECONDS)
        except FileNotFoundError as exc:
            raise SystemExit(f"Harness '{agent.harness}' nicht gefunden: {exc}") from exc
        finally:
            # Auch bei Absturz oder Ctrl+C: sonst fegt der nächste `git add -A` die Änderungen mit in den Commit
            self._guard_check(role, name, agent, snapshot)
        self._log("agent", role=role, step=name, exit=res.returncode, timed_out=res.timed_out)
        if res.blocked:
            self._blocked_steps.append(name)
            self._log("permission_blocked", role=role, step=name, count=res.blocked)
        if res.aborted:
            raise AgentError(
                f"{role} [{agent.harness}] abgebrochen: {res.blocked} Aktionen wegen fehlender Berechtigung abgelehnt.\n"
                "  Der Harness läuft nicht-interaktiv und kann nicht nachfragen. Erlaube die Aktionen "
                "(z.B. --auto in write_args/readonly_args von workflow.yaml)\n"
                "  oder prüfe die Berechtigungen des Harness."
                f"\n  Log: {self.run_dir / 'logs' / f'{name}.log'}"
            )
        if res.blocked:
            print(f"  ⚠ {role}: {res.blocked} Aktion(en) wegen fehlender Berechtigung abgelehnt – siehe Log")
        if has_fake_tool_calls(res.output):
            self._log("fake_tool_calls", role=role, step=name)
            print(f"  ⚠ {role}: Tool-Aufrufe stehen nur als Text in der Ausgabe und wurden nicht ausgeführt – "
                  "das Modell kommt mit dem Harness vermutlich nicht zurecht")
        if res.timed_out and soft_timeout and self._has_changes():
            # Builder hat gearbeitet, aber nicht rechtzeitig abgeschlossen – die Checks entscheiden
            print(f"  ⚠ Timeout nach {self.cfg.timeout_seconds}s, aber es gibt Änderungen – weiter mit den Checks")
            self._log("agent_soft_timeout", role=role, step=name)
            return res
        if res.returncode != 0 or res.timed_out:
            reason = f"Timeout nach {self.cfg.timeout_seconds}s" if res.timed_out else f"Exit-Code {res.returncode}"
            tail = error_tail(res.output)
            raise AgentError(
                f"{role} [{agent.harness}] {reason}\n  " + "\n  ".join(tail)
                + f"\n  Log: {self.run_dir / 'logs' / f'{name}.log'}"
            )
        return res

    # ------------------------------------------------------------ Schreibschutz

    def _guard_snapshot(self) -> dict:
        config = project_config_path(self.project)
        return {
            "head": self._head(),
            "branch": self._current_branch(),
            "config": config.read_bytes() if config.exists() else None,
        }

    def _guard_check(self, role: str, step: str, agent: AgentConfig, before: dict) -> None:
        """Harness-Flags sind kein Schutz: Prüft nach dem Agentenlauf, was er wirklich verändert hat."""
        if agent.readonly:
            self._restore_tree(role, step, before)
        self._restore_config(role, step, before["config"])

    def _current_branch(self) -> str:
        return self._git("symbolic-ref", "-q", "--short", "HEAD", check=False).strip()

    def _restore_tree(self, role: str, step: str, before: dict) -> None:
        """Setzt das Repo auf den Stand vor einem Readonly-Agenten zurück. Die Änderungen bleiben als Patch erhalten."""
        head, branch = self._head(), self._current_branch()
        if head == before["head"] and branch == before["branch"] and not self._has_changes():
            return
        self._git("add", "-A")
        files = self._git("diff", "--cached", "--name-only", before["head"]).splitlines()
        patch = self._guard_path(f"{step}.patch")
        with patch.open("wb") as out:  # Bytes: Dateien müssen nicht UTF-8 sein
            subprocess.run(
                ["git", "diff", "--cached", "--binary", before["head"]],
                cwd=self.project, stdout=out, check=True,
            )
        if branch != before["branch"]:
            self._git("checkout", "-q", "-f", before["branch"] or before["head"])
        self._git("reset", "-q", "--hard", before["head"])
        self._report_violation(role, step, "Readonly-Agent hat das Repo verändert", files, patch)

    def _restore_config(self, role: str, step: str, before: bytes | None) -> None:
        """Kein Agent darf die Regeln ändern, nach denen seine Arbeit geprüft wird."""
        config = project_config_path(self.project)
        now = config.read_bytes() if config.exists() else None
        if now == before:
            return
        saved = self._guard_path(f"{step}{config.name}")
        saved.write_bytes(now or b"")
        if before is None:
            config.unlink()
        else:
            config.write_bytes(before)
        self._report_violation(role, step, "Projekt-Konfiguration geändert", [config.name], saved)

    def _guard_path(self, name: str) -> Path:
        directory = self.run_dir / "guard"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    def _report_violation(self, role: str, step: str, reason: str, files: list[str], saved: Path) -> None:
        print(f"  ⚠ Schreibschutz: {role} – {reason}, zurückgesetzt: {', '.join(files) or '(nur Commits)'}")
        event = {"role": role, "step": step, "reason": reason, "files": files, "saved": self._rel(saved)}
        self.state.setdefault("guard", []).append(event)
        self._log("guard_violation", **event)

    # ------------------------------------------------------------------ Checks

    def _checks(self, kind: str, tag: str, rnd: int, max_rounds: int) -> tuple[bool, str]:
        for attempt in range(self.cfg.max_check_fixes + 1):
            ok, output = self._run_checks(kind)
            (self.run_dir / f"{tag}-checks-{attempt}.log").write_text(output)
            print(f"  ✓ Checks grün" if ok else f"  ✗ Checks rot (Versuch {attempt + 1})")
            if ok or attempt == self.cfg.max_check_fixes:
                return ok, output
            fix = {
                "source": "checks",
                "text": "Die automatischen Checks sind fehlgeschlagen. Behebe die Ursache:\n\n```\n" + output + "\n```",
            }
            self._builder(kind, f"{tag}-fix{attempt + 1}", rnd, max_rounds, fix)
        return False, ""

    def _run_checks(self, kind: str) -> tuple[bool, str]:
        outputs = []
        for check in self.cfg.checks.get(kind) or []:
            ok, output = self._run_check(check)
            outputs.append(output)
            if not ok:
                return False, "\n\n".join(outputs)[-MAX_CHECK_OUTPUT:]
        return True, "\n\n".join(outputs)[-MAX_CHECK_OUTPUT:]

    def _run_check(self, check: Check) -> tuple[bool, str]:
        if isinstance(check, str):
            check = {"run": check, "check_exit": True}
        template = check["run"].replace("{change}", self.change)
        inputs = check.get("inputs")
        forbidden = check.get("must_not_contain") or []
        required = check.get("must_contain") or []
        header = f"# {check['name']}\n" if check.get("name") else ""
        lines = []
        ok = True
        for inp in inputs if inputs is not None else [None]:
            cmd = template if inp is None else template.replace("{input}", shlex.quote(str(inp)))
            code, raw = run_shell(cmd, self.project, CHECK_TIMEOUT)
            out = strip_ansi(raw)
            if code is None:
                problems = [f"Timeout nach {CHECK_TIMEOUT}s"]
            else:
                problems = [f"Exit-Code {code}"] if check.get("check_exit") and code else []
            problems += [f"enthält verbotenes '{s}'" for s in forbidden if s in out]
            problems += [f"'{s}' fehlt in der Ausgabe" for s in required if s not in out]
            status = "✗ " + "; ".join(problems) if problems else "✓"
            # Bei Smoke-Checks nur fehlschlagende Eingaben ausführlich zeigen
            detail = out.strip() if problems or inputs is None else ""
            lines.append(f"$ {cmd}\n(exit {code}) {status}" + (f"\n{detail}" if detail else ""))
            ok &= not problems
        return ok, header + "\n".join(lines)

    # ------------------------------------------------------------- Human Gate

    def _run_gate(self, kind: str) -> None:
        n = self.state["gates"][kind] + 1
        gate_file = self.run_dir / f"gate-{kind}-{n}.md"
        if not gate_file.exists():
            gate_file.write_text(self._gate_document(kind, n))
        options = ("approve", "changes", "reject") + (("spec_change",) if kind == "code" else ())
        decision = self.gate.wait(gate_file, f"{LABEL[kind]} – {self.change} (#{n})", options)
        self.state["gates"][kind] = n
        self._log("human_gate", kind=kind, decision=decision.decision)

        if decision.decision == "approve":
            self._advance(kind)
        elif decision.decision in ("changes", "spec_change"):
            self.state["pending_feedback"] = {"source": "human", "text": decision.feedback}
            self.state["phase"] = "spec" if decision.decision == "spec_change" else kind
        else:
            self.state["phase"] = "aborted"

    def _gate_document(self, kind: str, n: int) -> str:
        result = self.state["results"].get(kind, {})
        options = "approve | changes | reject" + (" | spec_change" if kind == "code" else "")
        out = [
            "---",
            f"decision: pending   # {options}",
            "---",
            "",
            f"# Human Gate: {LABEL[kind]} – `{self.change}` (#{n})",
            "",
        ]
        if result.get("status") == "conflict":
            out += [f"> ⚠️ **Konflikt:** {result.get('reason', '')}", ""]
        violations = [
            v for v in self.state.get("guard", []) if v["step"].startswith(f"{kind}-c{self.state['cycle']}-")
        ]
        if violations:
            out += ["## ⚠️ Schreibschutz verletzt", "", "> Die Änderungen wurden zurückgesetzt und gesichert.", ""]
            out += [
                f"- **{v['role']}** ({v['step']}): {v['reason']} – {', '.join(v['files']) or '(nur Commits)'}"
                f" – gesichert in `{v['saved']}`"
                for v in violations
            ]
            out.append("")
        out += ["## Agenten-Ergebnis", "", f"- Status: **{result.get('status', '?')}**"]
        if result.get("summary"):
            out.append(f"- Reviewer: {result['summary']}")
        out.append("")

        if kind == "spec":
            change_dir = self.project / "openspec" / "changes" / self.change
            files = sorted(p for p in change_dir.rglob("*") if p.is_file()) if change_dir.exists() else []
            out += ["## Spec-Dateien", ""]
            out += [f"- [{self._rel(p)}](../../../{self._rel(p)})" for p in files] or ["_keine gefunden_"]
            out += ["", f"Ansehen: `openspec show {self.change}`", ""]
        else:
            stat = self._git("diff", "--stat", f"{self.state['bases']['code']}..HEAD", check=False)
            out += ["## Änderungen", "", "```", stat.strip() or "(keine)", "```", ""]

        tests = result.get("test_changes") or {}
        if tests.get("deleted") or tests.get("shrunk"):
            out += ["## ⚠️ Tests entfernt – bitte prüfen", "", "> Grüne Checks sagen wenig, wenn Tests fehlen.", ""]
            out += [f"- gelöscht: `{path}`" for path in tests.get("deleted", [])]
            out += [f"- verkleinert: `{s['file']}` (+{s['added']} / -{s['removed']} Zeilen)" for s in tests.get("shrunk", [])]
            out.append("")
        if result.get("check_output"):
            out += ["## Checks", "", "```", result["check_output"][-3000:], "```", ""]
        if result.get("open"):
            out += ["## Offene blockierende Findings", "", format_findings(result["open"]), ""]
        if result.get("minors"):
            out += ["## Minor-Findings (nicht umgesetzt)", "", format_findings(result["minors"]), ""]
        if result.get("earlier_minors"):
            out += [
                "## Frühere Minor-Findings – nicht erneut gemeldet",
                "",
                "> Der Reviewer hat diese in einer früheren Runde gemeldet, in der letzten aber nicht mehr.",
                "> Sie können erledigt sein – oder einfach übersehen. Bitte prüfen.",
                "",
                format_findings(result["earlier_minors"]),
                "",
            ]
        questions = read_questions(self.run_dir, f"{kind}-c{self.state['cycle']}-")
        if questions:
            out += ["## Fragen der Agenten an dich", ""]
            out += [f"- **{q['role']}** ({q['step']}): {q['question']}" for q in questions]
            out += ["", "> Antworten gehören ins Feedback unten (`decision: changes`).", ""]
        out += ["## Builder: Annahmen, Fragen, Abgelehntes", "", format_builder_response(result.get("builder")), ""]
        out += [
            "## Entscheidungen",
            "",
            "- `approve` – weiter zur nächsten Phase",
            "- `changes` – dein Feedback geht an den Builder, danach Checks + Agenten-Review, dann wieder hierher",
        ]
        if kind == "code":
            out.append("- `spec_change` – zurück zur Spec-Phase mit deinem Feedback")
        out += [
            "- `reject` – Lauf abbrechen",
            "",
            FEEDBACK_HEADING,
            "",
            "<!-- Konkretes Feedback hier eintragen. Es ist für den Builder verbindlich. -->",
            "",
        ]
        return "\n".join(out)

    # --------------------------------------------------------------------- Git

    def _git(self, *args: str, check: bool = True) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.project, capture_output=True, text=True, errors="replace", check=check
        ).stdout

    def _ensure_git(self) -> None:
        if not (self.project / ".git").exists():
            self._git("init", "-q")
        gitignore = self.project / ".gitignore"
        lines = gitignore.read_text().splitlines() if gitignore.exists() else []
        missing = [entry for entry in DEFAULT_IGNORES if entry not in lines]
        if missing:
            gitignore.write_text("\n".join([*lines, *missing]) + "\n")
        if subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.project, capture_output=True).returncode:
            self._commit("tikitaka: initial commit", allow_empty=True)

    def _commit(self, message: str, allow_empty: bool = False) -> None:
        self._git("add", "-A")
        if not allow_empty and not self._git("status", "--porcelain").strip():
            return
        identity: list[str] = []
        if not self._git("config", "user.email", check=False).strip():
            identity = ["-c", "user.name=tikitaka", "-c", "user.email=tikitaka@localhost"]
        args = [*identity, "commit", "-q", "-m", message] + (["--allow-empty"] if allow_empty else [])
        self._git(*args)

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def _nothing_built(self, kind: str) -> bool:
        """Noch keine Änderung in der ganzen Phase. Spätere Runden dürfen leer sein (`wont_fix`)."""
        return self._head() == self.state["bases"][kind] and not self._has_changes()

    def _test_changes(self, base: str) -> dict:
        """Gelöschte oder deutlich verkleinerte Testdateien seit `base` – ein Hinweis, kein Urteil."""
        deleted = [
            path for path in self._git("diff", "--name-only", "--diff-filter=D", base, check=False).splitlines()
            if TEST_PATH.search(path)
        ]
        shrunk = []
        for line in self._git("diff", "--numstat", "--diff-filter=M", base, check=False).splitlines():
            added, removed, path = line.split("\t", 2)
            if added == "-" or not TEST_PATH.search(path):  # "-": Binärdatei
                continue
            if int(removed) - int(added) >= SHRUNK_TEST_LINES:
                shrunk.append({"file": path, "added": int(added), "removed": int(removed)})
        if deleted or shrunk:
            print(f"  ⚠ Tests entfernt: {len(deleted)} gelöscht, {len(shrunk)} verkleinert")
            self._log("tests_removed", deleted=deleted, shrunk=shrunk)
        return {"deleted": deleted, "shrunk": shrunk}

    def _has_changes(self) -> bool:
        return bool(self._git("status", "--porcelain", check=False).strip())

    def _diff(self, base: str) -> str:
        excludes = [f":(glob,exclude){pattern}" for pattern in DIFF_EXCLUDES]
        return self._git("diff", f"{base}..HEAD", "--", ".", *excludes)

    def _write_diffs(self, kind: str, tag: str) -> tuple[Path, Path, str]:
        """Gesamtdiff der Phase und Diff seit dem letzten Review (beim ersten Review identisch).

        Liefert zusätzlich den Commit-Bereich des zu prüfenden Diffs, z.B. "a1b2c3d..HEAD".
        """
        base = self.state["bases"][kind]
        full = self.run_dir / f"{tag}.full.diff"
        full.write_text(self._diff(base))
        reviewed = self.state.setdefault("reviewed", {}).get(kind)
        if not reviewed:
            return full, full, f"{base[:10]}..HEAD"
        incremental = self.run_dir / f"{tag}.diff"
        incremental.write_text(self._diff(reviewed))
        return incremental, full, f"{reviewed[:10]}..HEAD"

    # --------------------------------------------------------------------- MCP

    def _mcp_launch(self, role: str, step: str, context: dict | None) -> dict[str, str]:
        """Bereitet den tikitaka-MCP-Server für einen Agentenaufruf vor und liefert die Platzhalter
        für mcp_args/mcp_env/mcp_config_content: {mcp_claude_config} (Datei), {mcp_opencode_config} und
        {mcp_opencode_v2_config} (JSON-Inhalt je OpenCode-Version) sowie {mcp_opencode_v2_static_config}
        (ohne Schritt: der Server liest ihn aus mcp/current.json)."""
        directory = mcp_dir(self.run_dir)
        directory.mkdir(parents=True, exist_ok=True)
        result_path(self.run_dir, step).unlink(missing_ok=True)  # nie ein altes Ergebnis übernehmen
        context_path(self.run_dir, step).write_text(json.dumps(context or {}, indent=2, ensure_ascii=False))
        current = current_path(self.run_dir)
        current.with_suffix(".tmp").write_text(json.dumps({"step": step, "role": role}))
        current.with_suffix(".tmp").replace(current)
        base = [sys.executable, "-m", "tikitaka.mcp_server", "--run-dir", str(self.run_dir)]
        command = [*base, "--step", step, "--role", role]
        claude_config = directory / f"{step}.claude.json"
        claude_config.write_text(json.dumps(
            {"mcpServers": {SERVER_NAME: {"command": command[0], "args": command[1:]}}}, indent=2,
        ))
        opencode_config = {"mcp": {SERVER_NAME: {"type": "local", "command": command, "enabled": True}}}
        # OpenCode V2: Server stehen unter mcp.servers, `enabled` heißt dort `disabled`
        opencode_v2_config = {"mcp": {"servers": {SERVER_NAME: {"type": "local", "command": command}}}}
        opencode_v2_static = {"mcp": {"servers": {SERVER_NAME: {"type": "local", "command": base}}}}
        return {
            "{mcp_claude_config}": str(claude_config),
            "{mcp_opencode_config}": json.dumps(opencode_config),
            "{mcp_opencode_v2_config}": json.dumps(opencode_v2_config),
            "{mcp_opencode_v2_static_config}": json.dumps(opencode_v2_static, indent=2) + "\n",
        }

    def _install_mcp_config(self, harness: dict, mcp: dict[str, str]) -> None:
        """Harnesses, deren Hintergrunddienst die Umgebung des Clients ignoriert (OpenCode V2), lesen die
        MCP-Konfiguration aus einer Projektdatei. Sie gehört tikitaka, bleibt lokal ignoriert und wird am
        Laufende wieder entfernt; eine fremde Datei wird nie überschrieben."""
        relative = harness.get("mcp_config_file")
        if not relative:
            return
        path = self.project / relative
        content = _substitute(harness.get("mcp_config_content", ""), mcp)
        if path.exists():
            if path.read_text() == content:
                self._mcp_config_installed = path
                return
            if not _is_tikitaka_mcp_config(path):
                raise AgentError(
                    f"{relative} existiert bereits und gehört nicht zu tikitaka. Trage den MCP-Server dort selbst ein "
                    f"oder setze 'mcp: false' in workflow.yaml:\n{content}"
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self._git_exclude(relative)
        self._mcp_config_installed = path
        time.sleep(MCP_CONFIG_SETTLE_SECONDS)  # der Dienst lädt geänderte Konfiguration asynchron nach

    def _git_exclude(self, entry: str) -> None:
        """Lokal ignorieren (.git/info/exclude): nichts landet in einem Commit, die .gitignore bleibt unberührt."""
        exclude = self.project / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        if entry not in lines:
            exclude.write_text("\n".join([*lines, entry]) + "\n")

    def _remove_mcp_config(self) -> None:
        path = getattr(self, "_mcp_config_installed", None)
        if path and path.exists() and _is_tikitaka_mcp_config(path):
            path.unlink()
            try:
                path.parent.rmdir()  # nur, wenn tikitaka das Verzeichnis angelegt hat und es leer ist
            except OSError:
                pass

    # ----------------------------------------------------------------- Helfer

    def _rel(self, path: Path) -> str:
        return os.path.relpath(path, self.project)

    def _log(self, event: str, **data: object) -> None:
        self.state["history"].append({"at": datetime.now().isoformat(timespec="seconds"), "event": event, **data})
        self._save()

    def _save(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # Atomar: Coach und `status` lesen state.json, während der Lauf schreibt
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False))
        tmp.replace(self.state_file)


def list_runs(project: str | Path) -> list[dict]:
    root = Path(project).resolve()
    out = []
    for state_file in sorted(f for runs in RUN_DIRS for f in (root / runs).glob("*/state.json")):
        s = json.loads(state_file.read_text())
        out.append({"change": s["change"], "phase": s["phase"], "cycle": s["cycle"], "gates": s["gates"]})
    return out
