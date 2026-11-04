"""Prüfungen vor dem Start: Gibt es die konfigurierten Modelle, und antworten sie wirklich?"""

from __future__ import annotations

import difflib
import re
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from .config import AgentConfig, Config
from .harness import (
    AgentResult, build_command, error_tail, has_fake_tool_calls, is_transient_failure, project_env, strip_ansi,
)

LIST_TIMEOUT = 30
PROBE_TIMEOUT = 90
PROBE_RETRY_DELAY = 3
PROBE_PROMPT = "Antworte nur mit dem Wort: ok"


def available_models(harness: dict) -> set[str] | None:
    """Modell-IDs, die der Harness kennt (`models_cmd`, eine ID pro Zeile). None: nicht prüfbar."""
    cmd = harness.get("models_cmd")
    if not cmd:
        return None
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=LIST_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    models = {line.strip() for line in strip_ansi(proc.stdout).splitlines() if re.fullmatch(r"[^\s]+/[^\s]+", line.strip())}
    return models or None


def _base_id(model: str) -> str:
    return model.split("#", 1)[0]  # provider/model#variant


def model_problems(cfg: Config) -> list[str]:
    """Konfigurierte Modelle, die ihr Harness nicht kennt – mit Vorschlägen. Nicht prüfbare werden übersprungen."""
    cache: dict[str, set[str] | None] = {}
    problems = []
    for role, agent in cfg.agents.items():
        if not agent.model:
            continue
        if agent.harness not in cache:
            cache[agent.harness] = available_models(cfg.harnesses[agent.harness])
        known = cache[agent.harness]
        if known is None or _base_id(agent.model) in known:
            continue
        close = difflib.get_close_matches(_base_id(agent.model), sorted(known), n=3, cutoff=0.5)
        hint = f" – meintest du: {', '.join(close)}?" if close else ""
        problems.append(f"{role}: Modell '{agent.model}' ist im Harness '{agent.harness}' nicht bekannt{hint}")
    return problems


def probe(harness: dict, agent: AgentConfig, timeout: int = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Ein Mini-Aufruf mit dem Modell des Agenten (bei vorübergehenden Fehlern ein zweiter). Liefert (ok, Detail)."""
    for attempt in (1, 2):
        returncode, output = _probe_once(harness, agent, timeout)
        if returncode == 0 or attempt == 2 or not is_transient_failure(harness, AgentResult(returncode, output)):
            break
        time.sleep(PROBE_RETRY_DELAY)
    if returncode is None:
        return False, output
    if returncode != 0:
        return False, " | ".join(error_tail(output)) or f"Exit-Code {returncode}"
    if not output.strip():
        return False, "leere Antwort"
    if has_fake_tool_calls(output):
        return False, "Tool-Aufrufe nur als Text"
    return True, ""


def _probe_once(harness: dict, agent: AgentConfig, timeout: int) -> tuple[int | None, str]:
    """Exit-Code und Ausgabe; Exit-Code None heißt: kein Aufruf möglich (Ausgabe = Grund)."""
    with tempfile.TemporaryDirectory(prefix="tikitaka-probe-") as tmp:
        cwd = Path(tmp)
        cmd = build_command(harness, replace(agent, readonly=True), PROBE_PROMPT, cwd / "prompt.md")
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, env=project_env(cwd), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None, f"keine Antwort nach {timeout}s"
        except OSError as exc:
            return None, str(exc)
    return proc.returncode, strip_ansi(proc.stdout + proc.stderr)


def probe_agents(cfg: Config, timeout: int = PROBE_TIMEOUT) -> dict[tuple[str, str | None], tuple[bool, str]]:
    """Probt jede Kombination aus Harness und Modell einmal (mehrere Rollen teilen sich oft ein Modell)."""
    results: dict[tuple[str, str | None], tuple[bool, str]] = {}
    for agent in cfg.agents.values():
        key = (agent.harness, agent.model)
        if key not in results:
            results[key] = probe(cfg.harnesses[agent.harness], agent, timeout)
    return results
