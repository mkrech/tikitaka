"""Coach-MCP-Server: tikitaka aus Claude Code, OpenCode & Co. heraus beobachten und steuern.

Start (stdio):  tikitaka --config /pfad/zu/workflow.yaml coach

Der Server liefert Fakten (Status, Zeiten, Findings, Smoke-Ergebnisse). Die Einordnung – echter Bug oder
Haarspalterei, welches Feedback – macht das Modell, das ihn nutzt. Entscheidungen am Human Gate schreibt
`decide` nur mit ausdrücklicher Bestätigung des Menschen.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import coach

INSTRUCTIONS = """\
Werkzeuge zum Beobachten und Steuern von tikitaka (Builder/Reviewer-Workflow mit Human Gates).
Typischer Ablauf: list_runs/status → bei waiting_for_human: gate_briefing lesen, bei Code-Gates run_smoke
ausprobieren, Findings einordnen und dem Menschen eine Empfehlung samt Feedback-Text vorschlagen.
decide nur aufrufen, nachdem der Mensch die konkrete Entscheidung (und ggf. den Feedback-Text) bestätigt hat.
"""


def build_server(config: Path) -> MCPServer:
    server = MCPServer("tikitaka-coach", instructions=INSTRUCTIONS)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except coach.CoachError as exc:
            raise ToolError(str(exc)) from None  # erreicht das Modell mit Text als Tool-Fehler

    @server.tool(description="Alle tikitaka-Läufe eines Projekts: Phase, läuft gerade, wartet auf den Menschen.")
    def list_runs(project: str) -> list[dict]:
        return call(coach.list_runs, project)

    @server.tool(description=(
        "Status eines Laufs: Phase, Zyklus, aktueller Agent (Laufzeit, Log-Aktivität), offenes Human Gate, "
        "Minuten pro Rolle, letzte Checks und letztes Review."
    ))
    def status(project: str, change: str) -> dict:
        return call(coach.status, project, change)

    @server.tool(description="Ende eines Agenten-Logs (Standard: das zuletzt geschriebene). Gut, um Hänger zu erkennen.")
    def log_tail(project: str, change: str, lines: int = 40, step: str | None = None) -> str:
        return call(coach.log_tail, project, change, lines, step)

    @server.tool(description=(
        "Alles für die Entscheidung am offenen Human Gate: Konflikt, Reviewer-Urteil, blockierende und Minor-Findings, "
        "frühere nicht erneut gemeldete Minors, Fragen der Agenten, Builder-Antwort, Checks, entfernte Tests, Schreibschutz-Verletzungen, "
        "das Gate-Dokument."
    ))
    def gate_briefing(project: str, change: str) -> dict:
        return call(coach.gate_briefing, project, change)

    @server.tool(description=(
        "Probiert das Programm aus: führt die Smoke-Checks aus der .tikitaka.yaml des Projekts aus, "
        "optional mit zusätzlichen Eingaben. Liefert Exit-Code, Ausgabe und ok je Eingabe."
    ))
    def run_smoke(project: str, extra_inputs: list[str] | None = None) -> list[dict]:
        return call(coach.run_smoke, project, extra_inputs)

    @server.tool(description=(
        "Vergleicht Läufe: Agentenminuten pro Rolle, Zyklen, menschliche Entscheidungen, Findings nach Severity, "
        "Nachforderungen/Retries, Agent-Fehler, Schreibschutz-Verletzungen, Fragen der Agenten. runs = [{project, change}, ...]."
    ))
    def compare_runs(runs: list[dict]) -> list[dict]:
        return call(coach.compare_runs, runs)

    @server.tool(description=(
        "Modell-IDs (provider/model), die ein Harness kennt, optional gefiltert, z.B. filter='deepseek'. "
        "harness optional (Standard: der Harness der meisten Rollen in workflow.yaml, meist pi). "
        "Vor set_agents aufrufen, um gültige IDs zu bekommen."
    ))
    def list_models(filter: str | None = None, harness: str | None = None) -> list[str]:
        return call(coach.list_models, config, filter, harness)

    @server.tool(description="Welche Rolle (spec/code builder/reviewer) in einem Projekt mit welchem Harness und Modell läuft.")
    def get_agents(project: str) -> dict:
        return call(coach.effective_agents, config, project)

    @server.tool(description=(
        "Setzt Modelle je Rolle für ein Projekt (in dessen .tikitaka.yaml). Ein laufender Lauf behält seine Modelle: "
        "Die Änderung gilt erst ab dem nächsten Start oder resume_run, also ggf. vorher stop_run. "
        "Abgelehnt, solange ein Agent arbeitet. "
        "models: {rolle: 'provider/model'} mit Rollen spec_builder, spec_reviewer, code_builder, code_reviewer "
        "oder builder / reviewer / all für mehrere. harness optional (Standard: wie bisher, meist pi). "
        "Modelle, die der Harness nicht kennt, werden mit Vorschlägen abgelehnt."
    ))
    def set_agents(project: str, models: dict[str, str], harness: str | None = None) -> dict:
        return call(coach.set_agents, config, project, models, harness)

    @server.tool(description=(
        "Startet einen neuen Lauf im Hintergrund (Projekt muss mit `tikitaka init` angelegt sein). "
        "change ist die OpenSpec-Change-ID, idea die Anforderung. models optional wie bei set_agents."
    ))
    def start_run(project: str, change: str, idea: str, models: dict[str, str] | None = None) -> dict:
        return call(coach.start_run, config, project, change, idea, models)

    @server.tool(description="Setzt einen unterbrochenen oder abgebrochenen Lauf im Hintergrund fort.")
    def resume_run(project: str, change: str) -> dict:
        return call(coach.resume_run, config, project, change)

    @server.tool(description="Hält einen laufenden Lauf an (wie Ctrl+C). Er lässt sich mit resume_run fortsetzen.")
    def stop_run(project: str, change: str) -> dict:
        return call(coach.stop_run, project, change)

    @server.tool(description=(
        "Schreibt die Entscheidung ins offene Human Gate. NUR aufrufen, wenn der Mensch genau diese Entscheidung "
        "und diesen Feedback-Text ausdrücklich bestätigt hat; confirmed_by_human muss dann true sein. "
        "decision: approve | changes | reject | spec_change (nur Code-Gate). changes/spec_change brauchen feedback."
    ))
    def decide(project: str, change: str, decision: str, confirmed_by_human: bool, feedback: str = "") -> dict:
        if not confirmed_by_human:
            raise ToolError("Erst die Entscheidung mit dem Menschen abstimmen, dann mit confirmed_by_human=true aufrufen.")
        return call(coach.decide, project, change, decision, feedback)

    return server


def main(config: str | Path) -> None:
    build_server(Path(config).resolve()).run()
