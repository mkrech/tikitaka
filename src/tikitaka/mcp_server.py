"""MCP-Server, über den Builder und Reviewer ihre Ergebnisse an tikitaka melden.

tikitaka startet ihn pro Agentenaufruf über den Harness (stdio), z.B.:

    python -m tikitaka.mcp_server --run-dir <run> --step code-c3-r1-review --role code_reviewer

Ergebnisse landen als Dateien im Run-Ordner – der Orchestrator liest sie nach dem Aufruf:

    mcp/<step>.result.json    submit_review / submit_build (Schema-geprüft)
    mcp/<step>.context.json   vom Orchestrator geschrieben: frühere Findings, Human-Feedback
    mcp/questions.jsonl       ask_human
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

SERVER_NAME = "tikitaka"
QUESTIONS_FILE = "questions.jsonl"


class Finding(BaseModel):
    id: str = Field(description="Stabile ID wie R1. Ein erneut gemeldetes Finding behält seine ID.")
    severity: Literal["blocker", "major", "minor"]
    issue: str = Field(description="Was ist das Problem")
    file: str = Field("", description="Betroffene Datei")
    spec_ref: str = Field("", description="Requirement- oder Scenario-Name")
    suggestion: str = Field("", description="Konkreter Verbesserungsvorschlag")


class WontFix(BaseModel):
    id: str = Field(description="ID des abgelehnten Findings")
    reason: str = Field(description="Nachvollziehbare Begründung")


def mcp_dir(run_dir: Path) -> Path:
    return run_dir / "mcp"


def result_path(run_dir: Path, step: str) -> Path:
    return mcp_dir(run_dir) / f"{step}.result.json"


def context_path(run_dir: Path, step: str) -> Path:
    return mcp_dir(run_dir) / f"{step}.context.json"


def read_result(run_dir: Path, step: str) -> dict | None:
    path = result_path(run_dir, step)
    return json.loads(path.read_text()) if path.exists() else None


def read_questions(run_dir: Path, step_prefix: str) -> list[dict]:
    path = mcp_dir(run_dir) / QUESTIONS_FILE
    if not path.exists():
        return []
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in entries if e.get("step", "").startswith(step_prefix)]


def _write_json(path: Path, data: object) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def build_server(run_dir: Path, step: str, role: str) -> MCPServer:
    mcp_dir(run_dir).mkdir(parents=True, exist_ok=True)
    reviewer = role.endswith("reviewer")
    server = MCPServer(
        SERVER_NAME,
        instructions=(
            "Werkzeuge des tikitaka-Workflows. Melde dein Ergebnis am Ende mit "
            + ("submit_review" if reviewer else "submit_build")
            + ". Fragen, die nur der Mensch entscheiden kann, stellst du mit ask_human."
        ),
    )

    def context() -> dict:
        path = context_path(run_dir, step)
        return json.loads(path.read_text()) if path.exists() else {}

    if reviewer:
        @server.tool(description=(
            "Liefert dein Review an tikitaka. Rufe es am Ende genau einmal auf. "
            "verdict ist approve, wenn es keine blocker/major-Findings gibt."
        ))
        def submit_review(
            verdict: Literal["approve", "changes_requested"], summary: str, findings: list[Finding],
        ) -> str:
            _write_json(result_path(run_dir, step), {
                "verdict": verdict, "summary": summary, "findings": [f.model_dump() for f in findings],
            })
            return f"Review gespeichert ({verdict}, {len(findings)} Findings). Du bist fertig."

        @server.tool(description="Findings aus früheren Runden dieses Zyklus (JSON-Liste mit round).")
        def get_previous_findings() -> str:
            return json.dumps(context().get("previous_findings", []), ensure_ascii=False)
    else:
        @server.tool(description="Liefert deine Zusammenfassung an tikitaka. Rufe es am Ende genau einmal auf.")
        def submit_build(
            summary: str,
            addressed: list[str] | None = None,
            wont_fix: list[WontFix] | None = None,
            questions: list[str] | None = None,
        ) -> str:
            _write_json(result_path(run_dir, step), {
                "summary": summary,
                "addressed": addressed or [],
                "wont_fix": [w.model_dump() for w in wont_fix or []],
                "questions": questions or [],
            })
            return "Zusammenfassung gespeichert. Du bist fertig."

    @server.tool(description="Feedback des Menschen aus dem letzten Human Gate (verbindlich), falls vorhanden.")
    def get_human_feedback() -> str:
        return context().get("human_feedback") or "Kein Human-Feedback in diesem Zyklus."

    @server.tool(description=(
        "Stellt dem Menschen eine Frage, die nur er entscheiden kann (Produktentscheidung, Mehrdeutigkeit). "
        "Die Frage erscheint am nächsten Human Gate. Triff bis dahin eine vernünftige Annahme und arbeite weiter."
    ))
    def ask_human(question: str) -> str:
        entry = {"at": datetime.now().isoformat(timespec="seconds"), "step": step, "role": role, "question": question}
        with (mcp_dir(run_dir) / QUESTIONS_FILE).open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return "Frage notiert – sie erscheint am nächsten Human Gate. Triff eine vernünftige Annahme und arbeite weiter."

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="tikitaka MCP-Server (stdio)")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--step", required=True)
    parser.add_argument("--role", required=True)
    args = parser.parse_args(argv)
    build_server(Path(args.run_dir), args.step, args.role).run()


if __name__ == "__main__":
    main()
