"""Fake-Harness für Tests: verhält sich je nach TIKITAKA_ROLE wie Builder oder Reviewer.

Ablauf-Skript:
- Spec-Reviewer: 1. Review -> blocker, danach approve (mit minor)
- Code-Builder: 1. Aufruf liefert kaputten Code (Checks rot), danach korrekt
- Code-Reviewer: approve

Optionaler Modus als 2. Argument:
- nojson: Reviewer liefert erst kein JSON, erst auf die Nachforderung hin
- slow:   Builder ändert Dateien, schläft dann aber über das Timeout hinaus
- tamper: Reviewer ändert Dateien und committet, Builder ändert die Projekt-Konfiguration
- tamper_branch: Reviewer committet auf einem neuen Branch und bleibt dort
- contradict: Reviewer antwortet changes_requested ohne Blocker, auf Rückfrage approve (contradict_always: bleibt dabei)
- drop_tests: Code-Builder löscht und verkleinert Testdateien
- latin1: Builder schreibt eine Datei und eine Ausgabe, die kein UTF-8 sind
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

role = os.environ["TIKITAKA_ROLE"]
change = os.environ["TIKITAKA_CHANGE"]
source = os.environ.get("TIKITAKA_FEEDBACK_SOURCE", "")
run_dir = Path(os.environ["TIKITAKA_RUN_DIR"])
prompt = Path(sys.argv[1]).read_text()
mode = sys.argv[2] if len(sys.argv) > 2 else ""
assert Path(os.environ["PWD"]).resolve() == Path.cwd().resolve(), "PWD zeigt nicht auf das Projekt"

with (run_dir / "fake-argv.log").open("a") as f:
    f.write(f"{role} {' '.join(sys.argv[3:])}\n")

if mode == "nojson" and role.endswith("reviewer"):
    if "--resume" in sys.argv:  # fortgesetzte Sitzung
        assert "noch nicht abgeschlossen" in prompt
        print('```json\n{"verdict": "approve", "summary": "aus Sitzung", "findings": []}\n```')
    elif "endete ohne den geforderten JSON-Block" in prompt:
        print('```json\n{"verdict": "approve", "summary": "nachgeliefert", "findings": []}\n```')
    else:
        print("Ich lese noch den Diff ...\ndiff --git a/x b/x")
    sys.exit(0)

if mode.startswith("contradict") and role == "spec_reviewer":
    if "Dein Review widerspricht sich" in prompt and mode == "contradict":
        print('```json\n{"verdict": "approve", "summary": "korrigiert", "findings": []}\n```')
    else:
        print('```json\n{"verdict": "changes_requested", "summary": "unklar", '
              '"findings": [{"id": "R1", "severity": "minor", "issue": "Kleinigkeit"}]}\n```')
    sys.exit(0)

counter_file = run_dir / f"fake-{role}.count"
calls = int(counter_file.read_text()) + 1 if counter_file.exists() else 1
counter_file.write_text(str(calls))


def git(*args: str) -> None:
    subprocess.run(["git", "-c", "user.name=fake", "-c", "user.email=fake@localhost", *args], check=True)


if mode.startswith("tamper") and role.endswith("reviewer"):
    if mode == "tamper_branch":
        git("checkout", "-q", "-b", "reviewer-branch")
    Path("committed_by_reviewer.txt").write_text("x\n")
    git("add", "-A")
    git("commit", "-q", "-m", "reviewer commit")
    Path("scratch.txt").write_bytes(b"\xff not utf-8\n")
    with (Path("openspec/changes") / change / "proposal.md").open("a") as f:
        f.write("reviewer edit\n")
elif mode == "tamper" and role.endswith("builder"):
    Path(".tikitaka.yaml").write_text("max_agent_rounds: 99\n")
elif mode == "drop_tests" and role == "code_builder":
    Path("tests/test_old.py").unlink()
    Path("tests/test_big.py").write_text("x = 1\n")
elif mode == "latin1" and role.endswith("builder"):
    Path("latin1.txt").write_bytes(b"caf\xe9\n")  # gültig als Text, aber kein UTF-8
    sys.stdout.buffer.write(b"Ausgabe mit \xff\n")
    sys.stdout.buffer.flush()


def mcp_call(tool: str, args: dict) -> None:
    """Ruft ein Tool des echten tikitaka-MCP-Servers auf – so, wie es opencode/claude täten."""
    import asyncio

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    command = json.loads(os.environ["TIKITAKA_FAKE_MCP"])["mcp"]["tikitaka"]["command"]

    async def go() -> None:
        async with Client(StdioServerParameters(command=command[0], args=command[1:])) as client:
            result = await client.call_tool(tool, args)
            assert not result.is_error, result.content

    asyncio.run(go())


def emit(data: dict) -> None:
    if os.environ.get("TIKITAKA_FAKE_MCP"):
        mcp_call("submit_review" if role.endswith("reviewer") else "submit_build", data)
        # Widersprechender Text-Block: Das MCP-Ergebnis muss Vorrang haben
        print('Fertig.\n```json\n{"verdict": "changes_requested", "summary": "TEXT-FALLBACK", "findings": []}\n```')
        return
    print("Fertig.\n```json\n" + json.dumps(data) + "\n```")


if role == "spec_builder":
    d = Path("openspec/changes") / change
    d.mkdir(parents=True, exist_ok=True)
    with (d / "proposal.md").open("a") as f:
        f.write(f"call {calls} feedback={source}\n")
    if mode == "slow":
        time.sleep(30)
    if os.environ.get("TIKITAKA_FAKE_MCP") and calls == 1:
        mcp_call("ask_human", {"question": "Soll ^ unterstützt werden?"})
    emit({"summary": f"spec call {calls}", "addressed": [], "wont_fix": [], "questions": []})

elif role == "spec_reviewer":
    assert "{{" not in prompt, "Platzhalter nicht ersetzt"
    print("\x1b[1m\x1b[31mfarbige Ausgabe\x1b[0m")  # muss aus Logs/Output gefiltert werden
    if calls == 1:
        emit({"verdict": "changes_requested", "summary": "Lücke",
              "findings": [{"id": "R1", "severity": "blocker", "issue": "Division durch 0 fehlt"},
                           {"id": "R2", "severity": "minor", "issue": "Unicode-Ziffern ungeklärt"}]})
    else:
        if calls == 2:
            assert "Unicode-Ziffern ungeklärt" in prompt, "frühere Findings fehlen im Prompt"
        emit({"verdict": "approve", "summary": "ok",
              "findings": [{"id": "R1", "severity": "minor", "issue": "Formulierung"}]})

elif role == "code_builder":
    body = "def add(a, b):\n    return a - b\n" if calls == 1 else "def add(a, b):\n    return a + b\n"
    Path("calc.py").write_text(body)
    emit({"summary": f"code call {calls}", "addressed": [], "wont_fix": [], "questions": []})

elif role == "code_reviewer":
    emit({"verdict": "approve", "summary": "passt", "findings": []})
