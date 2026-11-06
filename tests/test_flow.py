import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tikitaka.config import AgentConfig, Config, load_config
from tikitaka.gates import GateDecision, ScriptedGate, parse_gate_file
from tikitaka.harness import build_command, sandbox_profile_path, strip_ansi
from tikitaka import orchestrator
from tikitaka.orchestrator import DirtyProject, Orchestrator, ProjectBusy, acquire_project_lock, dirty_files
from tikitaka.review import Review, dropped_findings, extract_json, parse_review

ROOT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_agent.py")


def make_config() -> Config:
    harness = {"cmd": [sys.executable, str(FAKE), "{prompt_file}"], "write_args": [], "readonly_args": []}
    agents = {
        role: AgentConfig(harness="fake", prompt=ROOT / "prompts" / f"{role.replace('_', '-')}.md",
                          readonly=role.endswith("reviewer"))
        for role in ("spec_builder", "spec_reviewer", "code_builder", "code_reviewer")
    }
    return Config(
        harnesses={"fake": harness},
        agents=agents,
        checks={
            "spec": ["test -f openspec/changes/{change}/proposal.md"],
            "code": [f"{sys.executable} -c 'import calc; assert calc.add(2, 3) == 5'"],
        },
        notify=False,
        timeout_seconds=60,
    )


def test_full_flow_with_human_gates(tmp_path: Path) -> None:
    gate = ScriptedGate([
        GateDecision("changes", "Bitte Klammern unterstützen"),  # Spec-Gate 1
        GateDecision("approve", ""),                             # Spec-Gate 2
        GateDecision("approve", ""),                             # Code-Gate 1
    ])
    orch = Orchestrator(make_config(), tmp_path, "add-calculator", idea="Taschenrechner", gate=gate)

    assert orch.run() == "done"
    assert orch.state["gates"] == {"spec": 2, "code": 1}

    proposal = (tmp_path / "openspec/changes/add-calculator/proposal.md").read_text()
    assert "feedback=reviewer" in proposal and "feedback=human" in proposal
    assert "a + b" in (tmp_path / "calc.py").read_text()

    log = subprocess.run(["git", "log", "--format=%B"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "Human feedback:\nBitte Klammern unterstützen" in log

    run_dir = tmp_path / ".tikitaka/runs/add-calculator"
    assert (run_dir / "gate-spec-1.md").exists()
    assert (run_dir / "logs/code-c3-r1-fix1-build.log").exists()  # Check-Fix lief
    state = json.loads((run_dir / "state.json").read_text())
    assert state["results"]["spec"]["minors"][0]["severity"] == "minor"

    # Minor aus Runde 1, das in Runde 2 nicht mehr gemeldet wurde, bleibt im Gate sichtbar
    gate1 = (run_dir / "gate-spec-1.md").read_text()
    assert "Frühere Minor-Findings" in gate1 and "Unicode-Ziffern ungeklärt" in gate1 and "(Runde 1)" in gate1

    # Ab Runde 2 bekommt der Reviewer nur den Diff seit seinem letzten Review, der Gesamtdiff liegt daneben
    assert "+call 1" in (run_dir / "spec-c1-r2.full.diff").read_text()
    assert "+call 1" not in (run_dir / "spec-c1-r2.diff").read_text()
    assert "+call 2" in (run_dir / "spec-c1-r2.diff").read_text()

    # Nach dem Human Gate sieht der Reviewer das Human-Feedback
    assert "Bitte Klammern unterstützen" in (run_dir / "logs/spec-c2-r1-review.prompt.md").read_text()

    # Standard-.gitignore im Zielprojekt
    assert {"__pycache__/", "*.pyc", ".venv/"} <= set((tmp_path / ".gitignore").read_text().splitlines())

    # ANSI-Farbcodes landen nicht in Logs
    review_log = (run_dir / "logs/spec-c1-r1-review.log").read_text()
    assert "farbige Ausgabe" in review_log and "\x1b" not in review_log


def test_conflict_goes_to_gate_and_reject_aborts(tmp_path: Path) -> None:
    cfg = make_config()
    cfg.max_agent_rounds = 1  # Spec-Reviewer blockt in Runde 1 -> Konflikt
    gate = ScriptedGate([GateDecision("reject", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "aborted"
    assert orch.state["results"]["spec"]["status"] == "conflict"
    gate_doc = (tmp_path / ".tikitaka/runs/c/gate-spec-1.md").read_text()
    assert "Konflikt" in gate_doc and "Division durch 0" in gate_doc


def test_agent_failure_stops_without_gate_and_resumes(tmp_path: Path) -> None:
    cfg = make_config()
    good = cfg.harnesses["fake"]
    cfg.harnesses["fake"] = {"cmd": [sys.executable, "-c", "print('401 OAuth token invalid'); exit(1)"]}
    gate = ScriptedGate([])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "error"
    assert orch.state["phase"] == "spec" and gate.seen == []

    cfg.harnesses["fake"] = good  # z.B. nach erneutem Login
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    assert Orchestrator(cfg, tmp_path, "c", gate=gate).run() == "done"


def test_agent_failure_does_not_count_as_a_cycle(tmp_path: Path) -> None:
    cfg = make_config()
    good = cfg.harnesses["fake"]
    cfg.harnesses["fake"] = {"cmd": [sys.executable, "-c", "print('Error: Model unavailable: x/y'); exit(1)"]}
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=ScriptedGate([]))
    assert orch.run() == "error"
    assert orch.state["cycle"] == 0

    cfg.harnesses["fake"] = good
    orch = Orchestrator(cfg, tmp_path, "c", gate=ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")]))
    assert orch.run() == "done"
    # derselbe Zyklus läuft weiter: kein c2 nach dem Fehlversuch
    assert (tmp_path / ".tikitaka/runs/c/logs/spec-c1-r1-build.log").exists()
    assert not (tmp_path / ".tikitaka/runs/c/logs/spec-c2-r1-build.log").exists()


def test_agent_is_stopped_when_the_harness_keeps_rejecting_permissions(tmp_path: Path) -> None:
    cfg = make_config()
    line = "! permission requested: shell (ls); auto-rejecting"
    cfg.harnesses["fake"] = {
        "cmd": [sys.executable, "-c", f"import time\nfor _ in range(3): print({line!r}, flush=True)\ntime.sleep(60)"],
        "blocked_patterns": ["permission requested: .*auto-rejecting"],
    }
    started = time.monotonic()
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=ScriptedGate([]))
    assert orch.run() == "error"
    assert time.monotonic() - started < 30  # nicht erst nach dem Timeout
    error = next(e for e in orch.state["history"] if e["event"] == "agent_error")["error"]
    assert "Berechtigung" in error and "--auto" in error
    assert orch.state["cycle"] == 0


def test_missing_review_json_is_requested_without_full_retry(tmp_path: Path) -> None:
    cfg = make_config()
    cfg.harnesses["nojson"] = {**cfg.harnesses["fake"], "cmd": [*cfg.harnesses["fake"]["cmd"], "nojson"]}
    cfg.agents["spec_reviewer"].harness = "nojson"
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    assert Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate).run() == "done"
    logs = tmp_path / ".tikitaka/runs/c/logs"
    assert (logs / "spec-c1-r1-review-repair.log").exists()
    assert not (logs / "spec-c1-r1-review-retry.log").exists()
    # ohne continue_args: Nachforderung mit dem Ende der vorigen Antwort
    assert "endete ohne den geforderten JSON-Block" in (logs / "spec-c1-r1-review-repair.prompt.md").read_text()


def test_missing_review_json_continues_session_when_harness_supports_it(tmp_path: Path) -> None:
    cfg = make_config()
    cfg.harnesses["nojson"] = {
        **cfg.harnesses["fake"], "cmd": [*cfg.harnesses["fake"]["cmd"], "nojson"], "session_args": ["--sid={session_uuid}"], "resume_args": ["--resume", "--sid={session_uuid}"],
    }
    cfg.agents["spec_reviewer"].harness = "nojson"
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"
    assert orch.state["results"]["spec"]["summary"] == "aus Sitzung"
    assert not (tmp_path / ".tikitaka/runs/c/logs/spec-c1-r1-review-retry.log").exists()
    # Nachforderung über die eigene Sitzungs-ID, nie über "die letzte Sitzung"
    calls = (tmp_path / ".tikitaka/runs/c/fake-argv.log").read_text().splitlines()
    sid = next(line for line in calls if line.startswith("spec_reviewer --sid=")).split()[1]
    assert f"spec_reviewer --resume {sid}" in calls


def test_transient_provider_error_is_retried_once(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config()
    cfg.harnesses["fake"]["transient_patterns"] = ["HTTP (401|429)"]
    monkeypatch.setattr(orchestrator, "TRANSIENT_DELAY_SECONDS", 0)
    real, calls = orchestrator.run_agent, []

    def flaky(*args, **kwargs):
        calls.append(kwargs["log_file"].name)
        if len(calls) == 1:
            return orchestrator.AgentResult(1, "Error: Provider request failed with HTTP 401: unauthorized\n")
        return real(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "run_agent", flaky)
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"
    assert calls[:2] == ["spec-c1-r1-build.log"] * 2  # derselbe Schritt, zweiter Versuch
    assert any(e["event"] == "agent_retry" for e in orch.state["history"])


def test_non_transient_error_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config()
    cfg.harnesses["fake"] = {
        "cmd": [sys.executable, "-c", "print('Error: kaputt'); exit(1)"], "transient_patterns": ["HTTP 401"],
    }
    monkeypatch.setattr(orchestrator, "TRANSIENT_DELAY_SECONDS", 0)
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=ScriptedGate([]))
    assert orch.run() == "error"
    assert not any(e["event"] == "agent_retry" for e in orch.state["history"])


def test_mcp_config_for_opencode_v1_and_v2_placeholders(tmp_path: Path) -> None:
    orch = Orchestrator(make_config(), tmp_path, "c", idea="x", gate=ScriptedGate([]))
    launch = orch._mcp_launch("spec_builder", "spec-c1-r1-build", None)
    v1 = json.loads(launch["{mcp_opencode_config}"])["mcp"]["tikitaka"]
    v2 = json.loads(launch["{mcp_opencode_v2_config}"])["mcp"]["servers"]["tikitaka"]
    assert v1["command"] == v2["command"] and v2["type"] == "local"
    assert "enabled" not in v2  # in V2 heißt es `disabled`


def test_results_and_questions_via_mcp_server(tmp_path: Path) -> None:
    cfg = make_config()
    cfg.harnesses["fake"]["mcp_env"] = {"TIKITAKA_FAKE_MCP": "{mcp_opencode_config}"}
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"

    run_dir = tmp_path / ".tikitaka/runs/c"
    # Builder- und Reviewer-Ergebnisse kommen aus submit_build/submit_review, nicht aus dem Text-Block
    assert json.loads((run_dir / "spec-c1-r1-builder.json").read_text())["summary"] == "spec call 1"
    review = json.loads((run_dir / "spec-c1-r1-review.json").read_text())
    assert review["summary"] == "Lücke" and review["findings"][0]["issue"] == "Division durch 0 fehlt"
    assert orch.state["results"]["spec"]["summary"] == "ok"

    # ask_human landet gesammelt im Gate-Dokument
    gate_doc = (run_dir / "gate-spec-1.md").read_text()
    assert "Fragen der Agenten an dich" in gate_doc and "Soll ^ unterstützt werden?" in gate_doc

    # Reviewer-Kontext für get_previous_findings wird bereitgestellt
    context = json.loads((run_dir / "mcp/spec-c1-r2-review.context.json").read_text())
    assert context["previous_findings"][0]["issue"] == "Division durch 0 fehlt"


def test_reviewer_prompt_names_commit_range(tmp_path: Path) -> None:
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    Orchestrator(make_config(), tmp_path, "c", idea="x", gate=gate).run()
    prompt = (tmp_path / ".tikitaka/runs/c/logs/spec-c1-r2-review.prompt.md").read_text()
    assert "..HEAD`" in prompt and "`git diff` ohne Bereich ist leer" in prompt


def test_review_retry_gets_second_attempt_note(tmp_path: Path) -> None:
    cfg = make_config()
    # Reviewer liefert nie ein Ergebnis -> Review, Nachforderung, Retry, dann Konflikt
    cfg.harnesses["silent"] = {"cmd": [sys.executable, "-c", "print('lese noch ...')"]}
    cfg.agents["spec_reviewer"].harness = "silent"
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=ScriptedGate([GateDecision("reject", "")]))
    assert orch.run() == "aborted"
    logs = tmp_path / ".tikitaka/runs/c/logs"
    assert "zweiter Versuch" in (logs / "spec-c1-r1-review-retry.prompt.md").read_text()
    assert "zweiter Versuch" not in (logs / "spec-c1-r1-review.prompt.md").read_text()


def test_builder_timeout_with_changes_continues_to_checks(tmp_path: Path) -> None:
    cfg = make_config()
    cfg.timeout_seconds = 2
    cfg.harnesses["slow"] = {**cfg.harnesses["fake"], "cmd": [*cfg.harnesses["fake"]["cmd"], "slow"]}
    cfg.agents["spec_builder"].harness = "slow"
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"
    assert any(h["event"] == "agent_soft_timeout" for h in orch.state["history"])


def _with_mode(cfg: Config, role: str, mode: str) -> None:
    cfg.harnesses[mode] = {**cfg.harnesses["fake"], "cmd": [*cfg.harnesses["fake"]["cmd"], mode]}
    cfg.agents[role].harness = mode


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def test_reviewer_changes_are_rolled_back_and_reported(tmp_path: Path) -> None:
    cfg = make_config()
    _with_mode(cfg, "spec_reviewer", "tamper")
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"

    # Weder Commit noch Dateien des Reviewers bleiben – auch nicht über das `git add -A` der nächsten Runde
    assert "reviewer commit" not in _git_out(tmp_path, "log", "--format=%s")
    assert not (tmp_path / "scratch.txt").exists()
    assert not (tmp_path / "committed_by_reviewer.txt").exists()
    assert "reviewer edit" not in (tmp_path / "openspec/changes/c/proposal.md").read_text()

    violations = orch.state["guard"]
    assert [v["step"] for v in violations] == ["spec-c1-r1-review", "spec-c1-r2-review"]
    assert {"scratch.txt", "committed_by_reviewer.txt", "openspec/changes/c/proposal.md"} == set(violations[0]["files"])

    run_dir = tmp_path / ".tikitaka/runs/c"
    patch = (run_dir / "guard/spec-c1-r1-review.patch").read_bytes()
    assert b"reviewer edit" in patch and b"committed_by_reviewer.txt" in patch
    gate_doc = (run_dir / "gate-spec-1.md").read_text()
    assert "Schreibschutz verletzt" in gate_doc and "guard/spec-c1-r1-review.patch" in gate_doc
    # Das Review selbst zählt weiter: Runde 1 hat einen Blocker gemeldet, Runde 2 freigegeben
    assert orch.state["results"]["spec"]["status"] == "approved"


def test_reviewer_branch_switch_is_undone(tmp_path: Path) -> None:
    cfg = make_config()
    _with_mode(cfg, "code_reviewer", "tamper_branch")
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("reject", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    orch._ensure_git()
    start_branch = orch._current_branch()
    assert orch.run() == "aborted"
    assert orch._current_branch() == start_branch
    assert "reviewer commit" not in _git_out(tmp_path, "log", "--format=%s")


def test_crashing_reviewer_is_rolled_back(tmp_path: Path) -> None:
    cfg = make_config()
    cfg.harnesses["crash"] = {"cmd": [sys.executable, "-c", "open('junk.txt', 'w').write('x'); exit(1)"]}
    cfg.agents["spec_reviewer"].harness = "crash"
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=ScriptedGate([]))
    assert orch.run() == "error"
    assert not (tmp_path / "junk.txt").exists()
    assert orch.state["guard"][0]["files"] == ["junk.txt"]


def test_builder_cannot_change_project_config(tmp_path: Path) -> None:
    original = "max_agent_rounds: 2\n"
    (tmp_path / ".tikitaka.yaml").write_text(original)
    cfg = make_config()
    _with_mode(cfg, "spec_builder", "tamper")
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"

    assert (tmp_path / ".tikitaka.yaml").read_text() == original
    # Nur der initiale Commit enthält die Datei, keine Runde hat sie geändert
    assert _git_out(tmp_path, "log", "--format=%s", "--", ".tikitaka.yaml").splitlines() == ["tikitaka: initial commit"]
    run_dir = tmp_path / ".tikitaka/runs/c"
    assert "max_agent_rounds: 99" in (run_dir / "guard/spec-c1-r1-build.tikitaka.yaml").read_text()
    assert "Schreibschutz verletzt" in (run_dir / "gate-spec-1.md").read_text()


def test_clean_run_has_no_guard_violations(tmp_path: Path) -> None:
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(make_config(), tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"
    assert "guard" not in orch.state
    assert not (tmp_path / ".tikitaka/runs/c/guard").exists()
    assert "Schreibschutz" not in (tmp_path / ".tikitaka/runs/c/gate-spec-1.md").read_text()


def test_dropped_findings_match_by_id_and_file() -> None:
    earlier = [
        {"id": "R3", "file": "a.py", "severity": "minor", "issue": "nutzt .12g"},
        {"id": "R4", "file": "b.py", "severity": "minor", "issue": "pyc im Repo"},
    ]
    current = [{"id": "R3", "file": "a.py", "severity": "minor", "issue": "nutzt weiterhin .12g"}]
    assert [f["id"] for f in dropped_findings(earlier, current)] == ["R4"]

    # Zeilennummern verschieben sich zwischen Runden, mehrere Dateien mit " / " getrennt
    earlier = [{"id": "R3", "file": "src/fmt.py:8", "severity": "minor", "issue": "-0"},
               {"id": "R1", "file": "src/ev.py:188 / src/fmt.py:7", "severity": "minor", "issue": "inf"}]
    current = [{"id": "R3", "file": "src/fmt.py:13", "severity": "minor", "issue": "weiterhin -0"},
               {"id": "R1", "file": "src/fmt.py:9", "severity": "minor", "issue": "inf bleibt"}]
    assert dropped_findings(earlier, current) == []


def test_check_gets_empty_stdin(tmp_path: Path) -> None:
    orch = Orchestrator(make_config(), tmp_path, "c", idea="x")
    ok, _ = orch._run_check({"run": "cat", "check_exit": True})  # würde mit Terminal-stdin hängen
    assert ok


def test_project_config_overrides_checks_with_smoke_check(tmp_path: Path) -> None:
    (tmp_path / ".tikitaka.yaml").write_text(
        "max_agent_rounds: 1\n"
        "checks:\n"
        "  code:\n"
        "    - name: keine Tracebacks\n"
        "      run: echo {input}\n"
        "      inputs: ['ok', 'Traceback (most recent call last)', \"it's; touch pwned\"]\n"
        "      must_not_contain: [Traceback]\n"
    )
    orch = Orchestrator(make_config(), tmp_path, "c", idea="x", gate=ScriptedGate([]))
    assert orch.cfg.max_agent_rounds == 1
    assert orch.cfg.checks["spec"] == make_config().checks["spec"]  # nicht überschriebene Phase bleibt

    ok, output = orch._run_checks("code")
    assert not ok
    assert "# keine Tracebacks" in output and "enthält verbotenes 'Traceback'" in output
    assert not (tmp_path / "pwned").exists()  # Eingaben werden shell-sicher übergeben


def test_project_config_rejects_unknown_keys(tmp_path: Path) -> None:
    (tmp_path / ".tikitaka.yaml").write_text("harnesses: {}\n")
    with pytest.raises(ValueError, match="nicht erlaubte Schlüssel"):
        Orchestrator(make_config(), tmp_path, "c", idea="x")


def test_strip_ansi() -> None:
    assert strip_ansi("\x1b[0m\x1b[91m\x1b[1mError: \x1b[0mtext\x1b]0;title\x07") == "Error: text"


def test_parse_gate_file() -> None:
    assert parse_gate_file("---\ndecision: pending\n---") is None
    d = parse_gate_file("---\ndecision: changes  # x\n---\n## Dein Feedback\n<!-- hint -->\nMehr Tests\n")
    assert d == GateDecision("changes", "Mehr Tests")


def test_review_parsing() -> None:
    out = 'bla\n```json\n{"verdict": "changes_requested", "findings": [{"severity": "MAJOR"}]}\n```'
    r = parse_review(out)
    assert r.valid and r.findings[0] == {"severity": "major", "id": "R1"}
    assert not parse_review("kein json").valid
    assert extract_json('```json\n{"a": 1}\n```\n```json\n{"a": 2}\n```') == {"a": 2}


def test_existing_agent_tandem_project_keeps_working(tmp_path: Path) -> None:
    (tmp_path / ".agent-tandem.yaml").write_text("max_agent_rounds: 2\ntimeout_seconds: 77\n")
    legacy = tmp_path / ".agent-tandem/runs/c"
    legacy.mkdir(parents=True)
    state = {"change": "c", "idea": "x", "phase": "spec", "cycle": 0, "gates": {"spec": 0, "code": 0},
             "bases": {}, "results": {}, "pending_feedback": None, "history": []}
    (legacy / "state.json").write_text(json.dumps(state))

    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(make_config(), tmp_path, "c", gate=gate)
    assert orch.cfg.timeout_seconds == 77 and orch.run_dir == legacy
    assert orch.run() == "done"
    assert not (tmp_path / ".tikitaka/runs/c").exists()
    assert not (tmp_path / ".tikitaka.yaml").exists()


def test_new_config_wins_over_legacy_and_new_runs_use_new_dir(tmp_path: Path) -> None:
    (tmp_path / ".agent-tandem.yaml").write_text("timeout_seconds: 77\n")
    (tmp_path / ".tikitaka.yaml").write_text("timeout_seconds: 88\n")
    orch = Orchestrator(make_config(), tmp_path, "neu", idea="x", gate=ScriptedGate([]))
    assert orch.cfg.timeout_seconds == 88
    assert orch.run_dir == tmp_path / ".tikitaka/runs/neu"


def test_guard_protects_legacy_project_config(tmp_path: Path) -> None:
    (tmp_path / ".agent-tandem.yaml").write_text("timeout_seconds: 60\n")
    cfg = make_config()
    cfg.harnesses["edit"] = {"cmd": [sys.executable, "-c", "open('.agent-tandem.yaml', 'w').write('timeout_seconds: 1\\n'); exit(1)"]}
    cfg.agents["spec_builder"].harness = "edit"
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=ScriptedGate([]))
    assert orch.run() == "error"
    assert (tmp_path / ".agent-tandem.yaml").read_text() == "timeout_seconds: 60\n"
    assert orch.state["guard"][0]["files"] == [".agent-tandem.yaml"]


def _committed_repo(path: Path) -> None:
    _git_out(path, "init", "-q")
    (path / "app.py").write_text("x = 1\n")
    _git_out(path, "add", "-A")
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "start")


def test_second_run_in_same_project_is_refused_until_first_ends(tmp_path: Path) -> None:
    first = acquire_project_lock(tmp_path)
    orch = Orchestrator(make_config(), tmp_path, "b", idea="x", gate=ScriptedGate([]))
    with pytest.raises(ProjectBusy, match="bereits ein Lauf aktiv"):
        orch.run()
    first.close()
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    assert Orchestrator(make_config(), tmp_path, "b", idea="x", gate=gate).run() == "done"
    # Die Sperre ist nach dem Lauf wieder frei und nennt den letzten Lauf
    lock = acquire_project_lock(tmp_path)
    assert "b, PID" in (tmp_path / ".tikitaka/lock").read_text()
    lock.close()


def test_lock_is_held_while_a_run_is_active(tmp_path: Path) -> None:
    class CheckingGate(ScriptedGate):
        def wait(self, *args, **kwargs):
            with pytest.raises(ProjectBusy, match="c, PID"):
                acquire_project_lock(tmp_path)
            return super().wait(*args, **kwargs)

    gate = CheckingGate([GateDecision("approve", ""), GateDecision("approve", "")])
    assert Orchestrator(make_config(), tmp_path, "c", idea="x", gate=gate).run() == "done"


def test_new_run_refuses_dirty_project(tmp_path: Path) -> None:
    _committed_repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 2\n")
    (tmp_path / "notes.txt").write_text("privat\n")
    orch = Orchestrator(make_config(), tmp_path, "c", idea="x", gate=ScriptedGate([]))
    with pytest.raises(DirtyProject, match="app.py.*notes.txt"):
        orch.run()
    # Nichts angefasst, kein halber Lauf bleibt zurück
    assert _git_out(tmp_path, "log", "--format=%s").splitlines() == ["start"]
    assert not orch.state_file.exists()


def test_new_run_ignores_files_from_tikitaka_and_init(tmp_path: Path) -> None:
    _committed_repo(tmp_path)
    (tmp_path / ".tikitaka.yaml").write_text("timeout_seconds: 60\n")
    (tmp_path / "AGENTS.md").write_text("x\n")
    (tmp_path / "openspec").mkdir()
    (tmp_path / "openspec/project.md").write_text("x\n")
    (tmp_path / ".tikitaka/runs/c").mkdir(parents=True)
    (tmp_path / ".kiro/agents").mkdir(parents=True)  # legt der Kiro-Provider von OpenCode selbst an
    (tmp_path / ".kiro/agents/default.json").write_text("{}\n")
    assert dirty_files(tmp_path) == []
    # Eigene Änderungen in openspec/ zählen, sobald es bereits versioniert ist
    _git_out(tmp_path, "add", "openspec")
    assert dirty_files(tmp_path) == ["openspec/project.md"]


def test_allow_dirty_and_resume_skip_the_check(tmp_path: Path) -> None:
    _committed_repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 2\n")
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("reject", "")])
    assert Orchestrator(make_config(), tmp_path, "c", idea="x", gate=gate, allow_dirty=True).run() == "aborted"
    assert "x = 2" in _git_out(tmp_path, "show", "HEAD:app.py")

    # Beim Fortsetzen sind offene Änderungen erlaubt, z.B. ein Builder-Stand mit roten Checks
    (tmp_path / "app.py").write_text("x = 3\n")
    orch = Orchestrator(make_config(), tmp_path, "c", gate=ScriptedGate([]))
    assert not orch.is_new
    assert orch.run() == "aborted"  # Phase ist schon beendet, aber die Prüfung schlägt nicht an


def test_env_files_are_ignored_but_not_the_example(tmp_path: Path) -> None:
    gate = ScriptedGate([GateDecision("reject", "")])
    Orchestrator(make_config(), tmp_path, "c", idea="x", gate=gate).run()
    for name in (".env", ".env.local", ".env.example"):
        (tmp_path / name).write_text("X=1\n")
    untracked = _git_out(tmp_path, "status", "--porcelain").splitlines()
    assert untracked == ["?? .env.example"]


def test_check_timeout_fails_check_and_stops_children(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "CHECK_TIMEOUT", 1)
    orch = Orchestrator(make_config(), tmp_path, "c", idea="x", gate=ScriptedGate([]))
    started = time.monotonic()
    # Hintergrundprozess hält die Pipe offen, ein einfaches Beenden der Shell würde hier hängen
    ok, output = orch._run_check({"run": "echo vorher; sleep 40 & wait"})
    assert time.monotonic() - started < 15
    assert not ok and "Timeout nach 1s" in output and "vorher" in output


def test_check_output_that_is_not_utf8_does_not_crash(tmp_path: Path) -> None:
    orch = Orchestrator(make_config(), tmp_path, "c", idea="x", gate=ScriptedGate([]))
    ok, output = orch._run_check({"run": "printf 'a\\377b'", "check_exit": True})
    assert ok and "a�b" in output


def test_agent_output_and_files_that_are_not_utf8(tmp_path: Path) -> None:
    cfg = make_config()
    _with_mode(cfg, "spec_builder", "latin1")
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"
    run_dir = tmp_path / ".tikitaka/runs/c"
    # Der Lesethread lief weiter: die Antwort hinter dem Byte kam an, der Builder hat strukturiert geantwortet
    assert json.loads((run_dir / "spec-c1-r1-builder.json").read_text())["summary"] == "spec call 1"
    assert "�" in (run_dir / "logs/spec-c1-r1-build.log").read_text()


def test_builder_without_changes_goes_to_gate_without_review(tmp_path: Path) -> None:
    _committed_repo(tmp_path)  # ohne .gitignore: Unsere Vorbereitung darf nicht als Arbeit des Builders zählen
    cfg = make_config()
    cfg.harnesses["idle"] = {"cmd": [sys.executable, "-c", "print('nichts zu tun')"]}
    cfg.agents["spec_builder"].harness = "idle"
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=ScriptedGate([GateDecision("reject", "")]))
    assert orch.run() == "aborted"
    assert "nichts geändert" in orch.state["results"]["spec"]["reason"]
    run_dir = tmp_path / ".tikitaka/runs/c"
    assert "nichts geändert" in (run_dir / "gate-spec-1.md").read_text()
    assert not (run_dir / "logs/spec-c1-r1-review.log").exists()
    assert "tikitaka: prepare project" in _git_out(tmp_path, "log", "--format=%s")


def test_later_round_without_changes_still_reviews(tmp_path: Path) -> None:
    cfg = make_config()
    script = (
        "import os, pathlib, sys\n"
        "d = pathlib.Path('openspec/changes/c'); d.mkdir(parents=True, exist_ok=True)\n"
        "p = d / 'proposal.md'\n"
        "if not p.exists(): p.write_text('v1')\n"
        "print('```json\\n{\"summary\": \"ok\", \"wont_fix\": [{\"id\": \"R1\", \"reason\": \"passt so\"}]}\\n```')\n"
    )
    cfg.harnesses["once"] = {"cmd": [sys.executable, "-c", script]}
    cfg.agents["spec_builder"].harness = "once"
    gate = ScriptedGate([GateDecision("reject", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "aborted"
    assert (tmp_path / ".tikitaka/runs/c/logs/spec-c1-r2-review.log").exists()


def test_contradicting_review_is_asked_again(tmp_path: Path) -> None:
    cfg = make_config()
    _with_mode(cfg, "spec_reviewer", "contradict")
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("approve", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "done"
    assert orch.state["results"]["spec"]["status"] == "approved"
    assert orch.state["results"]["spec"]["summary"] == "korrigiert"
    prompt = (tmp_path / ".tikitaka/runs/c/logs/spec-c1-r1-review-verdict.prompt.md").read_text()
    assert "`changes_requested`, aber kein Finding" in prompt and "Kleinigkeit" in prompt


def test_review_contradicting_itself_twice_is_a_conflict(tmp_path: Path) -> None:
    cfg = make_config()
    _with_mode(cfg, "spec_reviewer", "contradict_always")
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=ScriptedGate([GateDecision("reject", "")]))
    assert orch.run() == "aborted"
    result = orch.state["results"]["spec"]
    assert result["status"] == "conflict" and "widerspricht sich" in result["reason"]
    assert result["minors"][0]["issue"] == "Kleinigkeit"


def test_contradiction_rules() -> None:
    blockers = ["blocker", "major"]
    major = [{"severity": "major"}]
    minor = [{"severity": "minor"}]
    assert Review("approve", findings=major).contradiction(blockers)
    assert Review("changes_requested", findings=minor).contradiction(blockers)
    assert Review("changes_requested", findings=[]).contradiction(blockers)
    assert Review("approve", findings=minor).contradiction(blockers) is None
    assert Review("changes_requested", findings=major).contradiction(blockers) is None
    # Blockierend ist, was die Konfiguration sagt
    assert Review("changes_requested", findings=major).contradiction(["blocker"])


def test_gate_reports_deleted_and_shrunk_tests(tmp_path: Path) -> None:
    _git_out(tmp_path, "init", "-q")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_old.py").write_text("def test_a(): pass\n")
    (tmp_path / "tests/test_big.py").write_text("".join(f"# {i}\n" for i in range(20)))
    (tmp_path / "tests/test_keep.py").write_text("def test_b(): pass\n")
    (tmp_path / "docs.md").write_text("".join(f"# {i}\n" for i in range(20)))
    _git_out(tmp_path, "add", "-A")
    _git_out(tmp_path, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "start")

    cfg = make_config()
    _with_mode(cfg, "code_builder", "drop_tests")
    cfg.checks["code"] = []
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("reject", "")])
    orch = Orchestrator(cfg, tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "aborted"

    changes = orch.state["results"]["code"]["test_changes"]
    assert changes["deleted"] == ["tests/test_old.py"]
    assert changes["shrunk"] == [{"file": "tests/test_big.py", "added": 1, "removed": 20}]
    doc = (tmp_path / ".tikitaka/runs/c/gate-code-1.md").read_text()
    assert "Tests entfernt" in doc and "gelöscht: `tests/test_old.py`" in doc and "+1 / -20" in doc


def test_gate_is_quiet_when_tests_are_untouched(tmp_path: Path) -> None:
    gate = ScriptedGate([GateDecision("approve", ""), GateDecision("reject", "")])
    orch = Orchestrator(make_config(), tmp_path, "c", idea="x", gate=gate)
    assert orch.run() == "aborted"
    assert orch.state["results"]["code"]["test_changes"] == {"deleted": [], "shrunk": []}
    assert "Tests entfernt" not in (tmp_path / ".tikitaka/runs/c/gate-code-1.md").read_text()


def test_workflow_gives_each_harness_a_session_of_its_own() -> None:
    """Die Formate der echten CLIs: claude verlangt eine UUID, opencode eine ID, die mit "ses" beginnt."""
    cfg = load_config(ROOT / "workflow.yaml")
    agent = AgentConfig(harness="x", prompt=Path("p"))
    sid = "f49d9242-2d61-4740-a36d-85a18f64ee04"
    for name, start, again in (
        ("claude", ["--session-id", sid], ["--resume", sid]),
        ("opencode", ["--session", f"ses_{sid.replace('-', '')}"], ["--session", f"ses_{sid.replace('-', '')}"]),
    ):
        harness = cfg.harnesses[name]
        first = build_command(harness, agent, "p", Path("f"), session_id=sid)
        second = build_command(harness, agent, "p", Path("f"), session_id=sid, resume=True)
        assert " ".join(start) in " ".join(first) and " ".join(again) in " ".join(second)
        assert "--continue" not in second


def test_sandbox_profile_is_resolved_from_the_config_dir(tmp_path: Path) -> None:
    """`sandbox_profile` wird relativ zur workflow.yaml aufgelöst und landet als {sandbox_profile} im Kommando."""
    prompts = ROOT / "prompts"
    roles = "".join(
        f"  {role}: {{ harness: pi-seatbelt, prompt: {prompts / (role.replace('_', '-') + '.md')} }}\n"
        for role in ("spec_builder", "spec_reviewer", "code_builder", "code_reviewer")
    )
    config = tmp_path / "workflow.yaml"
    config.write_text(
        "agents:\n" + roles
        + "harnesses:\n  pi-seatbelt:\n    cmd: [sh, -c, 'exec sandbox-exec -f \"$0\" pi \"$@\"', \"{sandbox_profile}\", -p, \"{prompt}\"]\n"
        + "    sandbox_profile: sandbox/pi.sb\n"
    )
    cfg = load_config(config)
    harness = cfg.harnesses["pi-seatbelt"]
    assert harness["sandbox_profile"] == str((tmp_path / "sandbox/pi.sb").resolve())

    cmd = build_command(harness, AgentConfig(harness="pi-seatbelt", prompt=Path("p")), "prompt", Path("f"))
    assert cmd[3] == str((tmp_path / "sandbox/pi.sb").resolve())
    assert cmd[-2:] == ["-p", "prompt"] and "{sandbox_profile}" not in " ".join(cmd)


def test_workflow_sandbox_profiles_exist() -> None:
    cfg = load_config(ROOT / "workflow.yaml")
    for name, harness in cfg.harnesses.items():
        path = sandbox_profile_path(harness)
        if path is not None:
            assert path.exists(), f"{name}: {path} fehlt"


def test_gate_accepts_common_spellings() -> None:
    # "approved" ist die naheliegendste Eingabe und muss wie "approve" wirken.
    assert parse_gate_file("---\ndecision: approved\n---") == GateDecision("approve", "")
    assert parse_gate_file("---\ndecision: APPROVE\n---") == GateDecision("approve", "")
    assert parse_gate_file("---\ndecision: approve  # approved | changes | reject\n---") == GateDecision("approve", "")
    assert parse_gate_file("---\ndecision: changes_requested\n---\n## Dein Feedback\nMehr Tests\n").decision == "changes"
    assert parse_gate_file("---\ndecision: spec-change\n---\n## Dein Feedback\nAnders\n").decision == "spec_change"
    assert parse_gate_file("---\ndecision: rejected\n---").decision == "reject"
    assert parse_gate_file("---\ndecision: pending\n---") is None
    assert parse_gate_file("---\ndecision: vielleicht\n---") is None
