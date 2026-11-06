"""Startet einen Agenten in seinem Harness (claude, opencode, kiro, ...) als Subprozess."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import AgentConfig

# CSI- (Farben, Cursor) und OSC-Sequenzen (Titel, Links)
TERM_GRACE_SECONDS = 5

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


@dataclass
class AgentResult:
    returncode: int
    output: str
    timed_out: bool = False
    blocked: int = 0  # Aktionen, die der Harness wegen fehlender Berechtigung abgelehnt hat
    aborted: bool = False  # tikitaka hat den Agenten wegen zu vieler Ablehnungen beendet


# Ab so vielen Berechtigungs-Ablehnungen in einem Aufruf beendet tikitaka den Agenten
BLOCKED_ABORT_AFTER = 3
# Ein Agent, der Tool-Aufrufe als Text ausgibt, statt sie auszuführen (und danach Ergebnisse erfindet)
FAKE_TOOL_CALL = re.compile(r"<invoke name=|<parameter name=|<function_calls>")


def blocked_matcher(harness: dict) -> re.Pattern | None:
    """Muster aus `blocked_patterns` des Harness: Ausgaben, die eine verweigerte Berechtigung melden."""
    patterns = harness.get("blocked_patterns") or []
    return re.compile("|".join(f"(?:{p})" for p in patterns)) if patterns else None


def has_fake_tool_calls(output: str) -> bool:
    return bool(FAKE_TOOL_CALL.search(output))


def is_transient_failure(harness: dict, res: AgentResult) -> bool:
    """Scheiterte der Aufruf an einem Fehler, der laut `transient_patterns` oft beim zweiten Versuch verschwindet
    (Anmelde-Token, Rate-Limit, Anbieter-Fehler)?"""
    patterns = harness.get("transient_patterns") or []
    if not patterns or res.returncode == 0 or res.timed_out or res.aborted:
        return False
    return bool(re.search("|".join(f"(?:{p})" for p in patterns), res.output))


def error_tail(output: str, count: int = 3) -> list[str]:
    """Die aussagekräftigsten Zeilen einer Agentenausgabe: Fehlerzeilen, sonst das Ende ohne Wiederholungen."""
    lines: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    errors = [line for line in lines if re.search(r"\b(error|fehler|exception|failed)\b", line, re.I)]
    return (errors or lines)[-count:]


def render_prompt(template: str, values: dict[str, object]) -> str:
    """Ersetzt {{name}}-Platzhalter. Einfache Klammern (z.B. JSON) bleiben unberührt."""
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def project_env(cwd: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Umgebung für Agenten/Checks im Zielprojekt – ohne das venv von tikitaka selbst."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    # PWD mitsetzen: manche Harnesses (z.B. opencode) lesen das Projekt aus PWD statt aus dem echten cwd
    return {**env, **(extra or {}), "PWD": str(cwd)}


def agent_home(harness: dict) -> Path | None:
    """Eigenes Konfigurationsverzeichnis für die Agenten eines Harness (`agent_home.dir`), falls konfiguriert."""
    spec = harness.get("agent_home")
    return Path(os.path.expanduser(str(spec["dir"]))) if spec and spec.get("dir") else None


def ensure_agent_home(harness: dict) -> Path | None:
    """Legt `agent_home.dir` an und verlinkt die Dateien aus `agent_home.link` aus `agent_home.link_from`
    (z.B. models.json und auth.json aus ~/.pi/agent). Vorhandene Dateien bleiben unberührt."""
    home = agent_home(harness)
    if home is None:
        return None
    spec = harness["agent_home"]
    home.mkdir(parents=True, exist_ok=True)
    source = Path(os.path.expanduser(str(spec["link_from"]))) if spec.get("link_from") else None
    for name in spec.get("link") or []:
        target = home / name
        if source is not None and not target.exists() and not target.is_symlink():
            target.symlink_to(source / name)
    return home


def harness_env(harness: dict) -> dict[str, str]:
    """Umgebungsvariablen aus `env` im Harness (~ und {agent_home} aufgelöst); legt agent_home bei Bedarf an."""
    env = harness.get("env") or {}
    if not env:
        return {}
    home = ensure_agent_home(harness)
    subst = {"{agent_home}": str(home or "")}
    return {key: os.path.expanduser(_substitute(value, subst)) for key, value in env.items()}


def can_continue(harness: dict) -> bool:
    return bool(harness.get("resume_args"))


def supports_mcp(harness: dict) -> bool:
    return bool(harness.get("mcp_args") or harness.get("mcp_env") or harness.get("mcp_config_file"))


def _substitute(value: object, subst: dict[str, str]) -> str:
    text = str(value)
    for key, replacement in subst.items():
        text = text.replace(key, replacement)
    return text


def build_command(
    harness: dict, agent: AgentConfig, prompt: str, prompt_file: Path, *,
    session_id: str | None = None, resume: bool = False, mcp: dict[str, str] | None = None,
) -> list[str]:
    args = list(harness["cmd"])
    args += harness.get("readonly_args" if agent.readonly else "write_args") or []
    if agent.model:
        args += harness.get("model_args") or []
    if session_id:
        args += harness.get("resume_args" if resume else "session_args") or []
    if mcp is not None:
        args += harness.get("mcp_args") or []
    subst = {
        "{prompt}": prompt, "{prompt_file}": str(prompt_file), "{model}": agent.model or "",
        "{session_uuid}": session_id or "", "{session_hex}": (session_id or "").replace("-", ""),
        "{sandbox_profile}": str(harness.get("sandbox_profile") or ""), **(mcp or {}),
    }
    return [_substitute(arg, subst) for arg in args]


def mcp_env(harness: dict, mcp: dict[str, str] | None) -> dict[str, str]:
    """Umgebungsvariablen, über die ein Harness den MCP-Server findet (z.B. OPENCODE_CONFIG_CONTENT)."""
    if mcp is None:
        return {}
    return {key: _substitute(value, mcp) for key, value in (harness.get("mcp_env") or {}).items()}


def harness_available(harness: dict) -> bool:
    return shutil.which(str(harness["cmd"][0])) is not None


def sandbox_profile_path(harness: dict) -> Path | None:
    """Das Sandbox-Profil (`sandbox_profile` im Harness), falls der Harness sandboxed läuft."""
    path = harness.get("sandbox_profile")
    return Path(path) if path else None


def _stop_group(proc: subprocess.Popen) -> None:
    """Beendet den Agenten samt Kindprozessen: erst freundlich, nach TERM_GRACE_SECONDS hart."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=TERM_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


def run_shell(cmd: str, cwd: Path, timeout: int) -> tuple[int | None, str]:
    """Führt einen Check aus und liefert Exit-Code und Ausgabe. Exit-Code None heißt: Timeout."""
    proc = subprocess.Popen(
        cmd, shell=True, cwd=cwd, env=project_env(cwd),
        stdin=subprocess.DEVNULL,  # z.B. eine REPL ohne Argument darf nicht auf Eingaben warten
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
        start_new_session=True,  # eigene Prozessgruppe: Timeout und Abbruch erreichen auch Kindprozesse
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        return proc.returncode, output
    except subprocess.TimeoutExpired:
        _stop_group(proc)
        try:
            output, _ = proc.communicate(timeout=TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            output = ""
        return None, output
    except BaseException:
        _stop_group(proc)
        raise


def run_agent(
    harness: dict,
    agent: AgentConfig,
    prompt: str,
    *,
    cwd: Path,
    log_file: Path,
    env: dict[str, str],
    timeout: int,
    session_id: str | None = None,
    resume: bool = False,
    mcp: dict[str, str] | None = None,
) -> AgentResult:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file = log_file.with_suffix(".prompt.md")
    prompt_file.write_text(prompt)
    cmd = build_command(harness, agent, prompt, prompt_file, session_id=session_id, resume=resume, mcp=mcp)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=project_env(cwd, {**harness_env(harness), **env, **mcp_env(harness, mcp)}),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",  # Ein Byte, das kein UTF-8 ist, darf den Lesethread nicht beenden
        bufsize=1,
        start_new_session=True,  # eigene Prozessgruppe: Abbruch und Timeout erreichen auch Kindprozesse
    )
    lines: list[str] = []
    blocked_re = blocked_matcher(harness)
    blocked = 0
    aborted = threading.Event()

    def pump() -> None:
        nonlocal blocked
        with log_file.open("w") as log:
            for line in proc.stdout:  # type: ignore[union-attr]
                clean = strip_ansi(line)  # Log, state.json und JSON-Parsing ohne Farbcodes
                lines.append(clean)
                log.write(clean)
                log.flush()
                sys.stdout.write("    │ " + line)  # Terminal behält die Farben
                sys.stdout.flush()
                if blocked_re and blocked_re.search(clean):
                    blocked += 1
                    if blocked >= BLOCKED_ABORT_AFTER and not aborted.is_set():
                        aborted.set()  # der Agent rät sonst ohne Zugriff weiter
                        _stop_group(proc)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_group(proc)
    except BaseException:  # z.B. Ctrl+C: Der Agent darf nach dem Abbruch nichts mehr ändern
        _stop_group(proc)
        raise
    reader.join(timeout=5)
    return AgentResult(proc.returncode, "".join(lines), timed_out, blocked, aborted.is_set())
