"""Coach: tikitaka von außen beobachten und steuern.

Reine Funktionen über die Dateien eines Laufs (state.json, Logs, Gate-Dokumente) – die Grundlage für den
Coach-MCP-Server (coach_server.py). Der Orchestrator bleibt unverändert: Läufe werden als eigener
Hintergrundprozess gestartet, Gate-Entscheidungen landen wie von Hand in der Gate-Datei.
"""

from __future__ import annotations

import difflib
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .config import PROJECT_CONFIG, ROLES, RUN_DIRS, apply_project_config, load_config, project_config_path
from .gates import FEEDBACK_HEADING, parse_gate_file
from .harness import project_env, strip_ansi
from .mcp_server import read_questions
from .orchestrator import ProjectBusy, acquire_project_lock, dirty_files, dirty_message

PID_FILE = "orchestrator.pid"
LOG_FILE = "orchestrator.log"
SEVERITIES = ("blocker", "major", "minor")


class CoachError(Exception):
    """Fehler, der dem Aufrufer als verständliche Meldung zurückgegeben wird."""


# ----------------------------------------------------------------------- Läufe


def find_run_dir(project: str | Path, change: str) -> Path:
    root = Path(project).expanduser().resolve()
    for runs in RUN_DIRS:
        candidate = root / runs / change
        if (candidate / "state.json").exists() or (candidate / PID_FILE).exists():
            return candidate
    raise CoachError(f"Kein Lauf '{change}' in {root} gefunden.")


def _state(run_dir: Path) -> dict:
    return json.loads((run_dir / "state.json").read_text())


def _pid_alive(run_dir: Path) -> int | None:
    pid_file = run_dir / PID_FILE
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return None
    try:
        # Selbst gestartete, beendete Läufe einsammeln – sonst bleiben sie als Zombie "am Leben"
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return None
    except ChildProcessError:
        pass  # nicht unser Kindprozess (z.B. aus einer früheren Coach-Sitzung)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return pid


def list_runs(project: str | Path) -> list[dict]:
    root = Path(project).expanduser().resolve()
    out = []
    for runs in RUN_DIRS:
        for state_file in sorted((root / runs).glob("*/state.json")):
            run_dir = state_file.parent
            s = _state(run_dir)
            gate = pending_gate(run_dir, s)
            out.append({
                "change": s["change"],
                "phase": s["phase"],
                "cycle": s["cycle"],
                "running": _pid_alive(run_dir) is not None,
                "waiting_for_human": gate is not None,
                "last_event": s["history"][-1]["at"] if s["history"] else None,
            })
    return out


# ------------------------------------------------------------------ Zeitachse


def timeline(state: dict) -> list[dict]:
    """Agentenaufrufe mit Dauer. Nutzt agent_start-Events; ältere Läufe ohne sie: vorheriges Event."""
    steps: list[dict] = []
    started: dict[str, datetime] = {}
    prev: datetime | None = None
    for h in state["history"]:
        at = datetime.fromisoformat(h["at"])
        if h["event"] == "agent_start":
            started[h["step"]] = at
        elif h["event"] == "agent":
            start = started.pop(h["step"], None) or prev
            steps.append({
                "step": h["step"],
                "role": h["role"],
                "end": h["at"],
                "minutes": round((at - start).total_seconds() / 60, 1) if start else None,
                "exit": h.get("exit"),
                "timed_out": h.get("timed_out", False),
            })
        if h["event"] != "agent_start":
            prev = at
    return steps


def _minutes_by_role(steps: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for st in steps:
        entry = out.setdefault(st["role"], {"calls": 0, "minutes": 0.0})
        entry["calls"] += 1
        entry["minutes"] = round(entry["minutes"] + (st["minutes"] or 0), 1)
    return out


# ------------------------------------------------------------------- Status


def pending_gate(run_dir: Path, state: dict | None = None) -> dict | None:
    state = state or _state(run_dir)
    phase = state["phase"]
    if not phase.endswith("_gate"):
        return None
    kind = phase.removesuffix("_gate")
    n = state["gates"][kind] + 1
    gate_file = run_dir / f"gate-{kind}-{n}.md"
    if not gate_file.exists():
        return None
    options = ("approve", "changes", "reject") + (("spec_change",) if kind == "code" else ())
    if parse_gate_file(gate_file.read_text(), options) is not None:
        return None  # Entscheidung steht schon drin, Orchestrator hat sie nur noch nicht gelesen
    return {"kind": kind, "number": n, "file": str(gate_file), "options": list(options)}


def _current_step(run_dir: Path, state: dict) -> dict | None:
    started = {h["step"]: h for h in state["history"] if h["event"] == "agent_start"}
    for h in state["history"]:
        if h["event"] == "agent":
            started.pop(h["step"], None)
    if not started:
        return None
    step, event = list(started.items())[-1]
    log = run_dir / "logs" / f"{step}.log"
    now = datetime.now()
    info = {
        "step": step,
        "role": event["role"],
        "running_minutes": round((now - datetime.fromisoformat(event["at"])).total_seconds() / 60, 1),
    }
    if log.exists():
        info["log_lines"] = sum(1 for _ in log.open(errors="replace"))
        info["log_idle_seconds"] = int(now.timestamp() - log.stat().st_mtime)
    return info


def _latest(run_dir: Path, pattern: str) -> Path | None:
    files = sorted(run_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _severity_counts(findings: list[dict]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f.get("severity") == s) for s in SEVERITIES}


def status(project: str | Path, change: str) -> dict:
    run_dir = find_run_dir(project, change)
    if not (run_dir / "state.json").exists():  # gerade gestartet, Orchestrator noch nicht so weit
        running = _pid_alive(run_dir) is not None
        return {"change": change, "phase": "starting" if running else "failed_to_start", "running": running,
                "waiting_for_human": None, "log": str(run_dir / LOG_FILE)}
    s = _state(run_dir)
    steps = timeline(s)
    out: dict = {
        "change": change,
        "phase": s["phase"],
        "cycle": s["cycle"],
        "running": _pid_alive(run_dir) is not None,
        "waiting_for_human": pending_gate(run_dir, s),
        "current_step": _current_step(run_dir, s),
        "minutes_by_role": _minutes_by_role(steps),
        "recent_steps": steps[-6:],
    }
    review = _latest(run_dir, "*-review.json")
    if review:
        data = json.loads(review.read_text())
        out["last_review"] = {
            "file": review.name, "verdict": data.get("verdict"), "summary": data.get("summary"),
            "severities": _severity_counts(data.get("findings", [])),
        }
    checks = _latest(run_dir, "*-checks-*.log")
    if checks:
        text = checks.read_text()
        out["last_checks"] = {"file": checks.name, "green": "✗" not in text,
                              "summary": [line for line in text.splitlines() if "passed" in line or "✗" in line][:5]}
    errors = [h for h in s["history"] if h["event"] == "agent_error"]
    if errors:
        out["last_agent_error"] = errors[-1].get("error", "")[:600]
    return out


def log_tail(project: str | Path, change: str, lines: int = 40, step: str | None = None) -> str:
    run_dir = find_run_dir(project, change)
    if step:
        log = run_dir / "logs" / f"{step}.log"
    else:
        log = _latest(run_dir, "logs/*.log")
        if log is None:
            raise CoachError("Noch keine Agenten-Logs.")
    if not log.exists():
        raise CoachError(f"Log {log.name} nicht gefunden.")
    text = log.read_text(errors="replace").replace("\x00", "\\x00")
    return f"# {log.name}\n" + "\n".join(text.splitlines()[-max(1, min(lines, 400)):])


# ------------------------------------------------------------- Gate-Briefing


def gate_briefing(project: str | Path, change: str) -> dict:
    """Alles, was du für eine Gate-Entscheidung brauchst – strukturiert und als Gate-Dokument."""
    run_dir = find_run_dir(project, change)
    s = _state(run_dir)
    gate = pending_gate(run_dir, s)
    if gate is None:
        raise CoachError(f"Lauf '{change}' wartet gerade nicht an einem Human Gate (Phase: {s['phase']}).")
    kind = gate["kind"]
    result = s["results"].get(kind, {})
    questions, seen = [], set()
    for q in read_questions(run_dir, f"{kind}-c{s['cycle']}-"):
        key = " ".join(q["question"].lower().split())
        if key not in seen:  # Builder und Reviewer stellen oft dieselbe Frage
            seen.add(key)
            questions.append({"role": q["role"], "question": q["question"]})
    return {
        **gate,
        "status": result.get("status"),
        "conflict_reason": result.get("reason") if result.get("status") == "conflict" else None,
        "reviewer_summary": result.get("summary"),
        "open_blocking": result.get("open", []),
        "minors": result.get("minors", []),
        "earlier_minors_not_repeated": result.get("earlier_minors", []),
        "agent_questions": questions,
        "builder": result.get("builder"),
        "check_output": (result.get("check_output") or "")[-3000:],
        "test_changes": result.get("test_changes"),
        "guard_violations": [v for v in s.get("guard", []) if v["step"].startswith(f"{kind}-c{s['cycle']}-")],
        "gate_document": Path(gate["file"]).read_text(),
    }


# ---------------------------------------------------------------- Ausprobieren


def run_smoke(project: str | Path, extra_inputs: list[str] | None = None, timeout: int = 60) -> list[dict]:
    """Führt die Smoke-Checks aus der .tikitaka.yaml des Projekts aus – optional mit weiteren Eingaben."""
    root = Path(project).expanduser().resolve()
    cfg_file = project_config_path(root)
    raw = yaml.safe_load(cfg_file.read_text()) if cfg_file.exists() else {}
    smoke = [c for c in ((raw or {}).get("checks") or {}).get("code") or [] if isinstance(c, dict) and "{input}" in c.get("run", "")]
    if not smoke:
        raise CoachError(f"Kein Smoke-Check mit {{input}} in {cfg_file} – dort unter checks.code eintragen.")
    results = []
    for check in smoke:
        forbidden = check.get("must_not_contain") or []
        for inp in [*(check.get("inputs") or []), *(extra_inputs or [])]:
            cmd = check["run"].replace("{input}", shlex.quote(str(inp)))
            try:
                p = subprocess.run(cmd, shell=True, cwd=root, env=project_env(root), stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=timeout, errors="replace")
                output, code = strip_ansi(p.stdout + p.stderr), p.returncode
            except subprocess.TimeoutExpired:
                output, code = f"(Timeout nach {timeout}s)", None
            results.append({
                "check": check.get("name", check["run"]),
                "input": inp if len(str(inp)) <= 60 else f"{str(inp)[:40]}… ({len(str(inp))} Zeichen)",
                "exit": code,
                "output": output.strip().replace("\x00", "\\x00")[-500:],
                "ok": code is not None and not any(f in output for f in forbidden),
            })
    return results


# ----------------------------------------------------------------- Vergleich


def run_metrics(project: str | Path, change: str) -> dict:
    run_dir = find_run_dir(project, change)
    s = _state(run_dir)
    steps = timeline(s)
    findings: dict[str, dict[str, int]] = {}
    for f in run_dir.glob("*-review.json"):
        kind = f.name.split("-")[0]
        counts = _severity_counts(json.loads(f.read_text()).get("findings", []))
        findings[kind] = {sev: findings.get(kind, {}).get(sev, 0) + n for sev, n in counts.items()}
    by_role = _minutes_by_role(steps)
    return {
        "project": str(Path(project).expanduser().resolve()),
        "change": change,
        "phase": s["phase"],
        "cycles": s["cycle"],
        "agent_minutes_total": round(sum(r["minutes"] for r in by_role.values()), 1),
        "minutes_by_role": by_role,
        "human_decisions": [h["decision"] for h in s["history"] if h["event"] == "human_gate"],
        "findings_by_phase": findings,
        "review_repairs": sum(1 for st in steps if st["step"].endswith("-repair")),
        "review_retries": sum(1 for st in steps if st["step"].endswith("-retry")),
        "agent_errors": sum(1 for h in s["history"] if h["event"] == "agent_error"),
        "guard_violations": len(s.get("guard", [])),
        "agent_questions": sum(1 for _ in read_questions(run_dir, "")),
        "mcp_results": len(glob.glob(str(run_dir / "mcp" / "*.result.json"))),
    }


def compare_runs(runs: list[dict]) -> list[dict]:
    """runs: [{"project": ..., "change": ...}, ...]"""
    out = []
    for r in runs:
        try:
            out.append(run_metrics(r["project"], r["change"]))
        except CoachError as exc:
            out.append({"project": r["project"], "change": r["change"], "error": str(exc)})
    return out


# ------------------------------------------------------------------ Modelle

ROLE_GROUPS = {
    "builder": ("spec_builder", "code_builder"),
    "reviewer": ("spec_reviewer", "code_reviewer"),
    "all": ROLES,
}


def list_models(name_filter: str | None = None) -> list[str]:
    """Modell-IDs, die OpenCode kennt (Format provider/model) – optional gefiltert."""
    if not shutil.which("opencode"):
        raise CoachError("opencode ist nicht installiert oder nicht im PATH.")
    try:
        p = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise CoachError("`opencode models` hat nicht rechtzeitig geantwortet.") from None
    models = [line.strip() for line in strip_ansi(p.stdout).splitlines() if "/" in line and " " not in line.strip()]
    if name_filter:
        models = [m for m in models if name_filter.lower() in m.lower()]
    return models


def effective_agents(config: str | Path, project: str | Path) -> dict[str, dict]:
    """Welche Rolle mit welchem Harness/Modell läuft – workflow.yaml plus .tikitaka.yaml des Projekts."""
    root = Path(project).expanduser().resolve()
    try:
        cfg = apply_project_config(load_config(config), root)
    except ValueError as exc:
        raise CoachError(str(exc)) from None
    return {role: {"harness": a.harness, "model": a.model} for role, a in cfg.agents.items()}


def _expand_roles(models: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, model in models.items():
        roles = ROLE_GROUPS.get(key, (key,))
        for role in roles:
            if role not in ROLES:
                raise CoachError(f"Unbekannte Rolle '{key}' – erlaubt: {', '.join(ROLES)}, builder, reviewer, all.")
            out[role] = model
    return out


def _replace_top_level_block(text: str, key: str, block: str) -> str:
    """Ersetzt den YAML-Block `key:` auf oberster Ebene; Kommentare und andere Schlüssel bleiben erhalten."""
    out, skipping = [], False
    for line in text.splitlines():
        if re.match(rf"^{re.escape(key)}:", line):
            skipping = True
            continue
        if skipping and (not line.strip() or line[0].isspace()):
            continue
        skipping = False
        out.append(line)
    rest = "\n".join(out).strip("\n")
    return (rest + "\n\n" if rest else "") + block


def _agent_running(root: Path) -> str | None:
    """Change eines Laufs, in dem gerade ein Agent arbeitet – sonst None."""
    for runs in RUN_DIRS:
        for state_file in (root / runs).glob("*/state.json"):
            if _pid_alive(state_file.parent) is None:
                continue
            s = _state(state_file.parent)
            events = [h["event"] for h in s["history"] if h["event"] in ("agent_start", "agent")]
            if events and events[-1] == "agent_start":
                return s["change"]
    return None


def set_agents(
    config: str | Path, project: str | Path, models: dict[str, str], harness: str | None = None,
    validate: bool = True,
) -> dict[str, dict]:
    """Setzt Modelle (und optional den Harness) je Rolle in der .tikitaka.yaml des Projekts.

    models: {"spec_reviewer": "provider/model", ...} – auch "builder", "reviewer" oder "all" für mehrere Rollen.
    """
    root = Path(project).expanduser().resolve()
    if not root.exists():
        raise CoachError(f"Projekt {root} existiert nicht.")
    # Der Schreibschutz setzt Änderungen an der Projekt-Konfiguration während eines Agentenlaufs zurück
    active = _agent_running(root)
    if active:
        raise CoachError(f"In Lauf '{active}' arbeitet gerade ein Agent – Modelle erst danach ändern (am Gate oder nach stop_run). Sie gelten ab dem nächsten Start oder resume_run.")
    wanted = _expand_roles(models)
    current = effective_agents(config, root)
    if validate:
        known: list[str] | None = None
        for role, model in wanted.items():
            if (harness or current[role]["harness"]) != "opencode":
                continue
            known = known if known is not None else list_models()
            if model not in known:
                close = difflib.get_close_matches(model, known, n=3, cutoff=0.3)
                raise CoachError(f"OpenCode kennt das Modell '{model}' nicht."
                                 + (f" Meintest du: {', '.join(close)}?" if close else " Siehe list_models."))
    cfg_file = project_config_path(root)
    text = cfg_file.read_text() if cfg_file.exists() else ""
    existing = ((yaml.safe_load(text) or {}).get("agents") or {}) if text else {}
    for role, model in wanted.items():
        entry = dict(existing.get(role) or {})
        entry["model"] = model
        if harness:
            entry["harness"] = harness
        existing[role] = entry
    block = yaml.safe_dump({"agents": existing}, sort_keys=False, allow_unicode=True)
    cfg_file.write_text(_replace_top_level_block(text, "agents", block))
    return effective_agents(config, root)


# ----------------------------------------------------------- Steuern


def _spawn(config: Path, args: list[str], run_dir: Path) -> int:
    run_dir.mkdir(parents=True, exist_ok=True)
    log = (run_dir / LOG_FILE).open("a")
    log.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} {' '.join(args[:2])} =====\n")
    log.flush()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tikitaka.cli", "--config", str(config), *args],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True,
    )
    (run_dir / PID_FILE).write_text(str(proc.pid))
    return proc.pid


def _require_free(root: Path) -> None:
    """Früh und verständlich ablehnen: Ein Hintergrundlauf meldet den Fehler sonst nur in seinem Log."""
    try:
        acquire_project_lock(root).close()
    except ProjectBusy as exc:
        raise CoachError(str(exc)) from None


def start_run(
    config: str | Path, project: str | Path, change: str, idea: str, models: dict[str, str] | None = None,
) -> dict:
    root = Path(project).expanduser().resolve()
    if not root.exists():
        raise CoachError(f"Projekt {root} existiert nicht – erst `tikitaka init {root}`.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", change):
        raise CoachError("Change-ID nur aus Kleinbuchstaben, Ziffern und Bindestrichen, z.B. add-power.")
    run_dir = root / RUN_DIRS[0] / change
    if any((root / runs / change / "state.json").exists() for runs in RUN_DIRS) or _pid_alive(run_dir):
        raise CoachError(f"Lauf '{change}' existiert schon – zum Fortsetzen resume_run verwenden.")
    _require_free(root)
    files = dirty_files(root)
    if files:
        raise CoachError(dirty_message(root, files))
    agents = set_agents(config, root, models) if models else effective_agents(config, root)
    pid = _spawn(Path(config), ["run", str(root), "--change", change, "--idea", idea], run_dir)
    return {"started": True, "pid": pid, "log": str(run_dir / LOG_FILE), "agents": agents}


def resume_run(config: str | Path, project: str | Path, change: str) -> dict:
    run_dir = find_run_dir(project, change)
    if _pid_alive(run_dir):
        raise CoachError(f"Lauf '{change}' läuft bereits.")
    if _state(run_dir)["phase"] in ("done", "aborted"):
        raise CoachError(f"Lauf '{change}' ist beendet ({_state(run_dir)['phase']}).")
    _require_free(Path(project).expanduser().resolve())
    pid = _spawn(Path(config), ["resume", str(Path(project).expanduser().resolve()), "--change", change], run_dir)
    return {"resumed": True, "pid": pid, "log": str(run_dir / LOG_FILE)}


def stop_run(project: str | Path, change: str) -> dict:
    run_dir = find_run_dir(project, change)
    pid = _pid_alive(run_dir)
    if pid is None:
        raise CoachError(f"Lauf '{change}' läuft nicht.")
    os.killpg(os.getpgid(pid), signal.SIGINT)  # wie Ctrl+C: Zustand bleibt, resume möglich
    return {"stopped": True, "pid": pid}


def decide(project: str | Path, change: str, decision: str, feedback: str = "") -> dict:
    run_dir = find_run_dir(project, change)
    gate = pending_gate(run_dir)
    if gate is None:
        raise CoachError(f"Lauf '{change}' wartet nicht an einem Human Gate.")
    if decision not in gate["options"]:
        raise CoachError(f"Ungültige Entscheidung '{decision}' – erlaubt: {', '.join(gate['options'])}.")
    if decision in ("changes", "spec_change") and not feedback.strip():
        raise CoachError(f"'{decision}' braucht konkretes Feedback für den Builder.")
    gate_file = Path(gate["file"])
    text = re.sub(r"^decision:\s*\S+", f"decision: {decision}", gate_file.read_text(), count=1, flags=re.M)
    if feedback.strip():
        if FEEDBACK_HEADING not in text:
            text += f"\n{FEEDBACK_HEADING}\n"
        text = text.rstrip("\n") + "\n\n" + feedback.strip() + "\n"
    gate_file.write_text(text)
    running = _pid_alive(run_dir) is not None
    return {
        "decided": decision,
        "gate_file": str(gate_file),
        "orchestrator_running": running,
        "next": "Orchestrator übernimmt die Entscheidung in wenigen Sekunden." if running
        else "Orchestrator läuft nicht – mit resume_run fortsetzen.",
    }
