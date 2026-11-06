import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import Client

from tikitaka import orchestrator
from tikitaka.config import AgentConfig, Config
from tikitaka.gates import GateDecision, ScriptedGate
from tikitaka.mcp_server import build_server, current_path, mcp_dir, read_result, read_questions, result_path
from tikitaka.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_agent.py")


def _call(run_dir: Path, tool: str, args: dict, **server_args) -> str:
    async def go() -> str:
        async with Client(build_server(run_dir, **server_args)) as client:
            result = await client.call_tool(tool, args)
            return result.content[0].text

    return asyncio.run(go())


def _set_current(run_dir: Path, step: str, role: str) -> None:
    mcp_dir(run_dir).mkdir(parents=True, exist_ok=True)
    current_path(run_dir).write_text(json.dumps({"step": step, "role": role}))


def test_dynamic_server_follows_current_step_and_role(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    review = {"verdict": "approve", "summary": "ok", "findings": []}

    _set_current(run_dir, "spec-c1-r1-review", "spec_reviewer")
    assert "Review gespeichert" in _call(run_dir, "submit_review", review)
    assert read_result(run_dir, "spec-c1-r1-review")["summary"] == "ok"

    # Derselbe Server (neu gestartet nur zur Prüfung) bedient danach den Builder-Schritt
    _set_current(run_dir, "code-c1-r1-build", "code_builder")
    assert "Zusammenfassung gespeichert" in _call(run_dir, "submit_build", {"summary": "gebaut"})
    assert read_result(run_dir, "code-c1-r1-build")["summary"] == "gebaut"

    _call(run_dir, "ask_human", {"question": "Python 3.8?"})
    assert [q["role"] for q in read_questions(run_dir, "code-c1-")] == ["code_builder"]


def test_dynamic_server_rejects_the_wrong_tool_for_the_role(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _set_current(run_dir, "code-c1-r1-build", "code_builder")
    answer = _call(run_dir, "submit_review", {"verdict": "approve", "summary": "x", "findings": []})
    assert answer.startswith("Fehler") and not result_path(run_dir, "code-c1-r1-build").exists()

    _set_current(run_dir, "spec-c1-r1-review", "spec_reviewer")
    assert _call(run_dir, "submit_build", {"summary": "x"}).startswith("Fehler")
    assert not result_path(run_dir, "spec-c1-r1-review").exists()


def test_static_server_only_offers_the_tools_of_its_role(tmp_path: Path) -> None:
    async def names(role: str) -> set[str]:
        async with Client(build_server(tmp_path, "s", role)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert "submit_review" in asyncio.run(names("spec_reviewer"))
    assert "submit_review" not in asyncio.run(names("spec_builder"))
    assert "submit_build" in asyncio.run(names("spec_builder"))


def _config_with_mcp_file(relative: str = ".opencode/opencode.json") -> Config:
    harness = {
        "cmd": [sys.executable, str(FAKE), "{prompt_file}"], "write_args": [], "readonly_args": [],
        "mcp_config_file": relative, "mcp_config_content": "{mcp_opencode_v2_static_config}",
    }
    agents = {
        role: AgentConfig(harness="fake", prompt=ROOT / "prompts" / f"{role.replace('_', '-')}.md",
                          readonly=role.endswith("reviewer"))
        for role in ("spec_builder", "spec_reviewer", "code_builder", "code_reviewer")
    }
    return Config(
        harnesses={"fake": harness}, agents=agents, notify=False, timeout_seconds=60,
        checks={"spec": ["test -f openspec/changes/{change}/proposal.md"],
                "code": [f"{sys.executable} -c 'import calc; assert calc.add(2, 3) == 5'"]},
    )


@pytest.fixture(autouse=True)
def _no_settle_wait(monkeypatch):
    monkeypatch.setattr(orchestrator, "MCP_CONFIG_SETTLE_SECONDS", 0)


def test_run_installs_the_project_mcp_config_and_removes_it_afterwards(tmp_path: Path) -> None:
    seen: list[dict] = []
    config_file = tmp_path / ".opencode/opencode.json"

    class Spy(ScriptedGate):
        def wait(self, *args, **kwargs):  # am Human Gate ist die Konfiguration noch da
            seen.append(json.loads(config_file.read_text()))
            return super().wait(*args, **kwargs)

    gate = Spy([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(_config_with_mcp_file(), tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"

    server = seen[0]["mcp"]["servers"]["tikitaka"]
    assert server["type"] == "local" and "--run-dir" in server["command"] and "--step" not in server["command"]
    assert not config_file.exists() and not config_file.parent.exists()
    # lokal ignoriert statt in .gitignore oder Commits
    assert ".opencode/opencode.json" in (tmp_path / ".git/info/exclude").read_text()
    assert ".opencode" not in (tmp_path / ".gitignore").read_text()


def test_foreign_project_mcp_config_is_never_overwritten(tmp_path: Path) -> None:
    config_file = tmp_path / ".opencode/opencode.json"
    config_file.parent.mkdir()
    config_file.write_text('{"mcp": {"servers": {"mein-server": {"type": "local", "command": ["x"]}}}}\n')
    orch = Orchestrator(_config_with_mcp_file(), tmp_path, "c", idea="x", gate=ScriptedGate([]))
    assert orch.run() == "error"
    assert "mein-server" in config_file.read_text()  # unverändert, auch nach dem Lauf
    error = next(e for e in orch.state["history"] if e["event"] == "agent_error")["error"]
    assert "gehört nicht zu tikitaka" in error


def _config_with_pi_mcp_file() -> Config:
    cfg = _config_with_mcp_file(".pi/mcp.json")
    cfg.harnesses["fake"] = {**cfg.harnesses["fake"], "mcp_config_content": "{mcp_pi_config}"}
    return cfg


def test_pi_project_mcp_config_is_installed_and_removed(tmp_path: Path) -> None:
    seen: list[dict] = []
    config_file = tmp_path / ".pi/mcp.json"

    class Spy(ScriptedGate):
        def wait(self, *args, **kwargs):
            seen.append(json.loads(config_file.read_text()))
            return super().wait(*args, **kwargs)

    gate = Spy([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(_config_with_pi_mcp_file(), tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"

    # Pi erwartet mcpServers (wie Claude), aber als JSON-Inhalt; Tools direkt für das Modell
    server = seen[0]["mcpServers"]["tikitaka"]
    assert server["exposure"] == "direct" and "--run-dir" in server["args"] and "--step" not in server["args"]
    assert not config_file.exists() and not config_file.parent.exists()
    assert ".pi/mcp.json" in (tmp_path / ".git/info/exclude").read_text()
