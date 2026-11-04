import sys
from pathlib import Path

import yaml

from tikitaka import cli
from tikitaka.config import AgentConfig, Config
from tikitaka.harness import blocked_matcher, error_tail, has_fake_tool_calls
from tikitaka.preflight import model_problems, probe, probe_agents

ROOT = Path(__file__).resolve().parents[1]


def _list_cmd(*models: str) -> list[str]:
    return [sys.executable, "-c", "print('\\n'.join(%r))" % list(models)]


def _config(harness: dict, **models: str | None) -> Config:
    agents = {
        role: AgentConfig(harness="h", prompt=ROOT / "prompts" / f"{role.replace('_', '-')}.md",
                          readonly=role.endswith("reviewer"), model=models.get(role))
        for role in ("spec_builder", "spec_reviewer", "code_builder", "code_reviewer")
    }
    return Config(harnesses={"h": harness}, agents=agents, notify=False)


def test_unknown_model_is_reported_with_suggestions() -> None:
    cfg = _config(
        {"cmd": ["x"], "models_cmd": _list_cmd("hagrid/DeepSeek-V4.1-Flash-1017", "kiro/claude-sonnet-4.5")},
        spec_builder="kiro/claude-sonnet-4.5",
        spec_reviewer="hagrid-remote-DeepSeek-V4.1-Flash-1017/DeepSeek-V4.1-Flash-1017",
        code_reviewer="kiro/claude-sonnet-4.5#high",  # Variante zählt zum Basis-Modell
    )
    problems = model_problems(cfg)
    assert len(problems) == 1
    assert problems[0].startswith("spec_reviewer:") and "hagrid/DeepSeek-V4.1-Flash-1017" in problems[0]


def test_models_are_not_checked_without_models_cmd_or_when_listing_fails() -> None:
    assert model_problems(_config({"cmd": ["x"]}, spec_builder="egal/egal")) == []
    failing = {"cmd": ["x"], "models_cmd": [sys.executable, "-c", "exit(1)"]}
    assert model_problems(_config(failing, spec_builder="egal/egal")) == []
    missing = {"cmd": ["x"], "models_cmd": ["tikitaka-gibt-es-nicht"]}
    assert model_problems(_config(missing, spec_builder="egal/egal")) == []


def test_probe_detects_working_and_broken_models() -> None:
    agent = AgentConfig(harness="h", prompt=ROOT / "prompts" / "spec-builder.md", model="a/b")
    ok, _ = probe({"cmd": [sys.executable, "-c", "print('ok')"]}, agent)
    assert ok

    broken = {"cmd": [sys.executable, "-c", "print('Error: Model unavailable: a/b'); exit(1)"]}
    ok, detail = probe(broken, agent)
    assert not ok and "Model unavailable" in detail

    fake_calls = {"cmd": [sys.executable, "-c", "print('<invoke name=\"fs_read\">')"]}
    ok, detail = probe(fake_calls, agent)
    assert not ok and "Text" in detail

    ok, detail = probe({"cmd": [sys.executable, "-c", "pass"]}, agent)
    assert not ok and "leer" in detail


def test_probe_runs_each_model_once(tmp_path: Path) -> None:
    log = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('ok')"
    counter = tmp_path / "counter"
    harness = {"cmd": [sys.executable, "-c", log, str(counter)], "model_args": ["--model", "{model}"]}
    cfg = _config(harness, spec_builder="a/b", spec_reviewer="a/b", code_builder="a/b", code_reviewer="c/d")
    results = probe_agents(cfg)
    assert set(results) == {("h", "a/b"), ("h", "c/d")} and all(ok for ok, _ in results.values())
    assert counter.read_text() == "xx"


def test_error_tail_prefers_errors_and_collapses_repeats() -> None:
    output = "> build · auto\n" * 11 + "Error: Internal error\n"
    assert error_tail(output) == ["Error: Internal error"]
    assert error_tail("> build · auto\n> build · auto\nfertig\n") == ["> build · auto", "fertig"]
    assert error_tail("") == []


def test_harness_signals() -> None:
    matcher = blocked_matcher({"blocked_patterns": ["permission requested: .*auto-rejecting"]})
    assert matcher and matcher.search("! permission requested: shell (ls); auto-rejecting")
    assert not matcher.search("Created file successfully")
    assert blocked_matcher({}) is None
    assert has_fake_tool_calls('Ich lese\n<invoke name="fs_read">\n<parameter name="path">x</parameter>')
    assert not has_fake_tool_calls("normale Ausgabe")


def test_run_stops_before_starting_when_a_model_is_unknown(tmp_path: Path, capsys) -> None:
    workflow = {
        "agents": {
            role: {"harness": "h", "model": "falsch/modell", "prompt": str(ROOT / "prompts" / f"{role.replace('_', '-')}.md")}
            for role in ("spec_builder", "spec_reviewer", "code_builder", "code_reviewer")
        },
        "harnesses": {"h": {"cmd": [sys.executable, "-c", "raise SystemExit('darf nicht laufen')"],
                            "models_cmd": _list_cmd("richtig/modell")}},
        "notify": False,
    }
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump(workflow))
    project = tmp_path / "projekt"
    project.mkdir()

    code = cli.main(["--config", str(config), "run", str(project), "--change", "c", "--idea", "x"])
    assert code == 2
    err = capsys.readouterr().err
    assert "falsch/modell" in err and "richtig/modell" in err
    assert not (project / ".tikitaka").exists()  # kein halber Lauf


def test_doctor_reports_unknown_models(tmp_path: Path, capsys) -> None:
    workflow = {
        "agents": {
            role: {"harness": "h", "model": "falsch/modell", "prompt": str(ROOT / "prompts" / f"{role.replace('_', '-')}.md")}
            for role in ("spec_builder", "spec_reviewer", "code_builder", "code_reviewer")
        },
        "harnesses": {"h": {"cmd": [sys.executable, "-c", "pass"], "models_cmd": _list_cmd("richtig/modell")}},
    }
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump(workflow))
    assert cli.main(["--config", str(config), "doctor"]) == 1
    assert "falsch/modell" in capsys.readouterr().out
