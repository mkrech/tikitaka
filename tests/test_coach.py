import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from mcp import Client

from tikitaka import cli, coach
from tikitaka.coach_server import build_server
from tikitaka.orchestrator import acquire_project_lock

ROOT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_agent.py")


def write_config(tmp_path: Path) -> Path:
    config = {
        "agents": {
            role: {"harness": "fake", "prompt": str(ROOT / "prompts" / f"{role.replace('_', '-')}.md")}
            for role in ("spec_builder", "spec_reviewer", "code_builder", "code_reviewer")
        },
        "harnesses": {"fake": {"cmd": [sys.executable, str(FAKE), "{prompt_file}"]}},
        "checks": {
            "spec": ["test -f openspec/changes/{change}/proposal.md"],
            "code": [f"{sys.executable} -c 'import calc; assert calc.add(2, 3) == 5'"],
        },
        "notify": False,
        "timeout_seconds": 60,
    }
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def wait_until(predicate, timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.3)
    raise AssertionError("Bedingung nicht rechtzeitig erfüllt")


def test_coach_drives_a_background_run_through_both_gates(tmp_path: Path) -> None:
    config, project = write_config(tmp_path), tmp_path / "proj"
    project.mkdir()

    started = coach.start_run(config, project, "add-calc", "Taschenrechner")
    assert started["started"]
    with pytest.raises(coach.CoachError, match="existiert schon"):
        coach.start_run(config, project, "add-calc", "nochmal")

    wait_until(lambda: coach.status(project, "add-calc")["waiting_for_human"])
    st = coach.status(project, "add-calc")
    assert st["waiting_for_human"]["kind"] == "spec" and st["running"]
    assert st["last_review"]["verdict"] == "approve"
    briefing = coach.gate_briefing(project, "add-calc")
    assert briefing["status"] == "approved" and "Human Gate" in briefing["gate_document"]

    with pytest.raises(coach.CoachError, match="spec_change"):
        coach.decide(project, "add-calc", "spec_change", "x")  # am Spec-Gate nicht erlaubt
    assert coach.decide(project, "add-calc", "approve")["orchestrator_running"]

    wait_until(lambda: (coach.status(project, "add-calc")["waiting_for_human"] or {}).get("kind") == "code")
    with pytest.raises(coach.CoachError, match="Feedback"):
        coach.decide(project, "add-calc", "changes", "  ")
    coach.decide(project, "add-calc", "approve")

    wait_until(lambda: coach.status(project, "add-calc")["phase"] == "done")
    wait_until(lambda: not coach.status(project, "add-calc")["running"])

    runs = coach.list_runs(project)
    assert runs == [{**runs[0], "change": "add-calc", "phase": "done", "running": False, "waiting_for_human": False}]
    metrics = coach.run_metrics(project, "add-calc")
    assert metrics["human_decisions"] == ["approve", "approve"]
    assert metrics["findings_by_phase"]["spec"]["blocker"] == 1
    # agent_start-Events liefern für jeden Schritt eine Dauer, auch für den allerersten
    assert all(step["minutes"] is not None for step in coach.timeline(json.loads(
        (project / ".tikitaka/runs/add-calc/state.json").read_text())))
    assert "Fertig." in coach.log_tail(project, "add-calc", lines=5)


def test_run_smoke_uses_project_smoke_checks(tmp_path: Path) -> None:
    (tmp_path / ".tikitaka.yaml").write_text(
        "checks:\n  code:\n    - name: echo\n      run: echo {input}\n"
        "      inputs: ['ok']\n      must_not_contain: [Traceback]\n"
    )
    results = coach.run_smoke(tmp_path, extra_inputs=["Traceback (most recent call last)", "x" * 100])
    assert [r["ok"] for r in results] == [True, False, True]
    assert results[2]["input"].endswith("(100 Zeichen)")


def test_log_tail_escapes_nul_bytes(tmp_path: Path) -> None:
    run_dir = tmp_path / ".tikitaka/runs/c"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({"change": "c", "phase": "code", "cycle": 1, "gates": {}, "history": []}))
    (run_dir / "logs/code-c1-r1-review.log").write_text("Zeichen '\x00'\n")
    assert "\\x00" in coach.log_tail(tmp_path, "c") and "\x00" not in coach.log_tail(tmp_path, "c")


def _config_with_model_list(tmp_path: Path) -> Path:
    """Config mit einem zweiten Harness `pi`, dessen models_cmd zwei Modelle auflistet."""
    config = write_config(tmp_path)
    cfg = yaml.safe_load(config.read_text())
    cfg["harnesses"]["pi"] = dict(cfg["harnesses"]["fake"],
                                  models_cmd=[sys.executable, "-c", "print('lmstudio/qwen'); print('hagrid/deepseek-v4')"])
    config.write_text(yaml.safe_dump(cfg))
    return config


def test_list_models_uses_the_models_cmd_of_the_harness(tmp_path: Path) -> None:
    config = _config_with_model_list(tmp_path)
    assert coach.list_models(config, harness="pi") == ["hagrid/deepseek-v4", "lmstudio/qwen"]
    assert coach.list_models(config, "qwen", harness="pi") == ["lmstudio/qwen"]
    # Standard ist der Harness der meisten Rollen – hier `fake`, der keine Modelle auflisten kann
    with pytest.raises(coach.CoachError, match="fake"):
        coach.list_models(config)
    with pytest.raises(coach.CoachError, match="nicht definiert"):
        coach.list_models(config, harness="gibtsnicht")


def test_set_agents_writes_project_config_and_validates_models(tmp_path: Path) -> None:
    config = _config_with_model_list(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".tikitaka.yaml").write_text("# Kommentar bleibt\ntimeout_seconds: 99\n")

    with pytest.raises(coach.CoachError, match="Meintest du: hagrid/deepseek-v4"):
        coach.set_agents(config, project, {"reviewer": "hagrid/deepseek-v3"}, harness="pi")

    agents = coach.set_agents(config, project, {"reviewer": "hagrid/deepseek-v4", "code_builder": "lmstudio/qwen#high"},
                              harness="pi")
    assert agents["spec_reviewer"] == agents["code_reviewer"] == {"harness": "pi", "model": "hagrid/deepseek-v4"}
    assert agents["code_builder"]["model"] == "lmstudio/qwen#high" and agents["spec_builder"]["harness"] == "fake"

    text = (project / ".tikitaka.yaml").read_text()
    assert "# Kommentar bleibt" in text and "timeout_seconds: 99" in text

    # Erneutes Setzen ersetzt den agents-Block, statt ihn zu verdoppeln
    coach.set_agents(config, project, {"spec_reviewer": "lmstudio/qwen"})
    text = (project / ".tikitaka.yaml").read_text()
    assert text.count("agents:") == 1
    assert coach.effective_agents(config, project)["spec_reviewer"]["model"] == "lmstudio/qwen"
    assert coach.effective_agents(config, project)["code_reviewer"]["model"] == "hagrid/deepseek-v4"

    with pytest.raises(coach.CoachError, match="Meintest du: hagrid/deepseek-v4"):
        coach.set_agents(config, project, {"reviewer": "hagrid/deepseek"})
    with pytest.raises(coach.CoachError, match="Unbekannte Rolle"):
        coach.set_agents(config, project, {"tester": "lmstudio/qwen"})


def test_set_agents_refused_while_agent_is_working(tmp_path: Path) -> None:
    config, project = write_config(tmp_path), tmp_path / "proj"
    run_dir = project / ".tikitaka/runs/c"
    run_dir.mkdir(parents=True)
    (run_dir / coach.PID_FILE).write_text(str(os.getpid()))  # lebender Prozess als Orchestrator
    state = {"change": "c", "phase": "code", "cycle": 1, "gates": {},
             "history": [{"at": "2026-01-01T00:00:00", "event": "agent_start", "role": "code_builder", "step": "s"}]}
    (run_dir / "state.json").write_text(json.dumps(state))
    with pytest.raises(coach.CoachError, match="arbeitet gerade ein Agent"):
        coach.set_agents(config, project, {"all": "m"}, validate=False)
    assert not (project / ".tikitaka.yaml").exists()

    # Am Gate (Agent fertig, Orchestrator wartet) bleibt es erlaubt
    state["history"].append({"at": "2026-01-01T00:00:01", "event": "agent", "role": "code_builder", "step": "s"})
    (run_dir / "state.json").write_text(json.dumps(state))
    assert coach.set_agents(config, project, {"all": "m"}, validate=False)["code_builder"]["model"] == "m"


def test_stop_run_ends_agent_and_children_before_guard(tmp_path: Path) -> None:
    # Der Reviewer ignoriert SIGINT (wie manche Harnesses) und startet einen Kindprozess, der immer wieder
    # eine Datei schreibt. Nur ein gezieltes Beenden der Prozessgruppe stoppt beide.
    writer = "import pathlib, time\nwhile True:\n    pathlib.Path('junk.txt').write_text('x')\n    time.sleep(0.05)"
    agent = (
        "import signal, subprocess, sys\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        f"subprocess.run([sys.executable, '-c', {writer!r}])"
    )
    config = write_config(tmp_path)
    cfg = yaml.safe_load(config.read_text())
    cfg["harnesses"]["looper"] = {"cmd": [sys.executable, "-c", agent]}
    cfg["agents"]["spec_reviewer"]["harness"] = "looper"
    config.write_text(yaml.safe_dump(cfg))
    project = tmp_path / "proj"
    project.mkdir()

    coach.start_run(config, project, "c", "x")
    wait_until(lambda: (project / "junk.txt").exists())
    assert coach.stop_run(project, "c")["stopped"]
    wait_until(lambda: not coach.status(project, "c")["running"])

    assert not (project / "junk.txt").exists()
    time.sleep(0.5)  # lebte der Kindprozess noch, wäre die Datei jetzt wieder da
    assert not (project / "junk.txt").exists()
    state = json.loads((project / ".tikitaka/runs/c/state.json").read_text())
    assert state["guard"][0]["files"] == ["junk.txt"]


def test_cli_started_run_is_seen_and_stoppable(tmp_path: Path) -> None:
    """Ein Lauf aus dem Terminal (`tikitaka run`, ohne orchestrator.pid) wird über die Projektsperre erkannt."""
    config = write_config(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tikitaka.cli", "--config", str(config), "run", str(project), "--change", "c", "--idea", "x"],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        run_dir = project / ".tikitaka/runs/c"
        wait_until(lambda: (run_dir / "state.json").exists() and coach.status(project, "c")["running"])
        assert not (run_dir / coach.PID_FILE).exists()
        assert coach.list_runs(project)[0]["running"]
        assert coach._lock_pid(project / ".tikitaka/runs/anderer") is None  # Sperre gehört zu Change c

        assert coach.stop_run(project, "c")["pid"] == proc.pid
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    # Der Eintrag in .tikitaka/lock bleibt stehen, zählt aber nicht mehr
    assert "PID" in (project / ".tikitaka/lock").read_text()
    assert not coach.status(project, "c")["running"]


def test_project_config_rejects_bad_agent_overrides(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    (tmp_path / ".tikitaka.yaml").write_text("agents:\n  code_builder: { prompt: x.md }\n")
    with pytest.raises(coach.CoachError, match="nur harness, model"):
        coach.effective_agents(config, tmp_path)
    (tmp_path / ".tikitaka.yaml").write_text("agents:\n  code_builder: { harness: kiro }\n")
    with pytest.raises(coach.CoachError, match="nicht definiert"):
        coach.effective_agents(config, tmp_path)


def test_mcp_server_exposes_tools_and_guards_decide(tmp_path: Path) -> None:
    server = build_server(write_config(tmp_path))

    async def go() -> None:
        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"status", "gate_briefing", "run_smoke", "decide", "start_run", "compare_runs",
                    "list_models", "get_agents", "set_agents"} <= names
            agents = await client.call_tool("get_agents", {"project": str(tmp_path)})
            assert not agents.is_error and "spec_builder" in agents.content[0].text
            denied = await client.call_tool("decide", {
                "project": str(tmp_path), "change": "c", "decision": "approve", "confirmed_by_human": False,
            })
            assert denied.is_error and "abstimmen" in denied.content[0].text
            missing = await client.call_tool("status", {"project": str(tmp_path), "change": "nope"})
            assert missing.is_error and "Kein Lauf" in missing.content[0].text

    asyncio.run(go())


def test_start_run_refuses_busy_or_dirty_project(tmp_path: Path) -> None:
    config, project = write_config(tmp_path), tmp_path / "proj"
    project.mkdir()
    lock = acquire_project_lock(project)
    with pytest.raises(coach.CoachError, match="bereits ein Lauf aktiv"):
        coach.start_run(config, project, "c", "x")
    lock.close()

    for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "--allow-empty", "-m", "s"]):
        subprocess.run(["git", *args], cwd=project, check=True)
    (project / "offen.txt").write_text("x\n")
    with pytest.raises(coach.CoachError, match="offen.txt"):
        coach.start_run(config, project, "c", "x")
    assert not (project / ".tikitaka/runs/c").exists()


def test_cli_reports_dirty_project_and_passes_allow_dirty(tmp_path: Path, capsys, monkeypatch) -> None:
    config, project = write_config(tmp_path), tmp_path / "proj"
    project.mkdir()
    for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "--allow-empty", "-m", "s"]):
        subprocess.run(["git", *args], cwd=project, check=True)
    (project / "offen.txt").write_text("x\n")
    base = ["--config", str(config), "run", str(project), "--change", "c", "--idea", "x"]
    assert cli.main(base) == 2
    assert "--allow-dirty" in capsys.readouterr().err

    seen = {}
    monkeypatch.setattr(cli.Orchestrator, "run", lambda self: seen.setdefault("allow", self.allow_dirty) and "done")
    assert cli.main([*base, "--allow-dirty"]) == 0
    assert seen["allow"] is True
