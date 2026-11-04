from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

ROLES = ("spec_builder", "spec_reviewer", "code_builder", "code_reviewer")
PROJECT_CONFIG = ".tikitaka.yaml"
# Bis zur Umbenennung hieß das Projekt agent-tandem, davor agentflow: bestehende Läufe und Konfigurationen bleiben gültig
LEGACY_PROJECT_CONFIG = ".agent-tandem.yaml"
RUN_DIRS = (".tikitaka/runs", ".agent-tandem/runs", ".agentflow/runs")
# Was ein Zielprojekt in seiner .tikitaka.yaml überschreiben darf. Bei agents nur harness/model je Rolle –
# Prompts und Harness-Definitionen bleiben global.
PROJECT_KEYS = (
    "agents", "checks", "max_agent_rounds", "rounds_after_human", "max_check_fixes",
    "blocking_severities", "timeout_seconds", "human_gates", "archive_on_done",
)
AGENT_OVERRIDE_KEYS = ("harness", "model")

# Ein Check ist entweder ein Shell-Befehl (muss Exit 0 liefern) oder ein Smoke-Check:
#   {name, run, inputs?, must_not_contain?, must_contain?, check_exit?}
Check = str | dict


@dataclass
class AgentConfig:
    harness: str
    prompt: Path
    readonly: bool = False
    model: str | None = None


@dataclass
class Config:
    harnesses: dict[str, dict]
    agents: dict[str, AgentConfig]
    max_agent_rounds: int = 2
    rounds_after_human: int = 1
    max_check_fixes: int = 2
    blocking_severities: list[str] = field(default_factory=lambda: ["blocker", "major"])
    timeout_seconds: int = 1800
    human_gates: dict[str, bool] = field(
        default_factory=lambda: {"after_spec": True, "after_code": True, "on_conflict": True}
    )
    checks: dict[str, list[Check]] = field(default_factory=dict)
    archive_on_done: bool = False
    notify: bool = True
    # Ergebnisse über den tikitaka-MCP-Server statt JSON im Text (für Harnesses mit mcp_args/mcp_env)
    mcp: bool = True

    def gate(self, name: str) -> bool:
        return bool(self.human_gates.get(name, False))


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    base = path.parent

    agents: dict[str, AgentConfig] = {}
    for role in ROLES:
        spec = (raw.get("agents") or {}).get(role)
        if not spec:
            raise ValueError(f"{path}: agents.{role} fehlt")
        prompt = Path(spec["prompt"])
        agents[role] = AgentConfig(
            harness=spec["harness"],
            prompt=prompt if prompt.is_absolute() else base / prompt,
            readonly=bool(spec.get("readonly", role.endswith("reviewer"))),
            model=spec.get("model"),
        )

    harnesses = raw.get("harnesses") or {}
    for role, agent in agents.items():
        if agent.harness not in harnesses:
            raise ValueError(f"{path}: Harness '{agent.harness}' für {role} ist nicht definiert")
        if not agent.prompt.exists():
            raise ValueError(f"{path}: Prompt-Datei {agent.prompt} für {role} fehlt")

    known = {f for f in Config.__dataclass_fields__ if f not in ("harnesses", "agents")}
    extra = {k: v for k, v in raw.items() if k in known}
    return Config(harnesses=harnesses, agents=agents, **extra)


def project_config_path(project: Path) -> Path:
    """Die Projekt-Konfiguration: .tikitaka.yaml, sonst eine vorhandene alte .agent-tandem.yaml."""
    legacy = project / LEGACY_PROJECT_CONFIG
    return legacy if legacy.exists() and not (project / PROJECT_CONFIG).exists() else project / PROJECT_CONFIG


def apply_project_config(cfg: Config, project: Path) -> Config:
    """Überlagert die globale Config mit <projekt>/.tikitaka.yaml (falls vorhanden)."""
    path = project_config_path(project)
    if not path.exists():
        return cfg
    raw = yaml.safe_load(path.read_text()) or {}
    unknown = set(raw) - set(PROJECT_KEYS)
    if unknown:
        raise ValueError(f"{path}: nicht erlaubte Schlüssel {sorted(unknown)} – erlaubt: {', '.join(PROJECT_KEYS)}")
    overrides = {k: raw[k] for k in PROJECT_KEYS if k in raw}
    # checks und human_gates pro Eintrag überschreiben, nicht komplett ersetzen
    if "checks" in overrides:
        overrides["checks"] = {**cfg.checks, **(overrides["checks"] or {})}
    if "human_gates" in overrides:
        overrides["human_gates"] = {**cfg.human_gates, **(overrides["human_gates"] or {})}
    if "agents" in overrides:
        overrides["agents"] = _override_agents(cfg, overrides["agents"] or {}, path)
    return replace(cfg, **overrides)


def _override_agents(cfg: Config, raw: dict, path: Path) -> dict[str, AgentConfig]:
    agents = dict(cfg.agents)
    for role, spec in raw.items():
        if role not in ROLES:
            raise ValueError(f"{path}: unbekannte Rolle '{role}' – erlaubt: {', '.join(ROLES)}")
        spec = spec or {}
        bad = set(spec) - set(AGENT_OVERRIDE_KEYS)
        if bad:
            raise ValueError(f"{path}: agents.{role} darf nur {', '.join(AGENT_OVERRIDE_KEYS)} setzen, nicht {sorted(bad)}")
        harness = spec.get("harness", agents[role].harness)
        if harness not in cfg.harnesses:
            raise ValueError(f"{path}: Harness '{harness}' für {role} ist in workflow.yaml nicht definiert")
        agents[role] = replace(agents[role], harness=harness, model=spec.get("model", agents[role].model))
    return agents


PROJECT_TEMPLATE = """\
# tikitaka – projektspezifische Einstellungen (überschreiben workflow.yaml)
#
# checks.<phase> ersetzt die globalen Checks dieser Phase komplett.
# Ein Check ist ein Shell-Befehl (muss Exit 0 liefern) oder ein Smoke-Check:
#
#   - name: keine Tracebacks bei Fehleingaben
#     run: uv run -q calc {input}          # {input} wird shell-sicher ersetzt, {change} = Change-ID
#     inputs: ["1/0", "abc", "((1"]
#     must_not_contain: [Traceback]
#     # must_contain: [...]   check_exit: true  (Exit-Code 0 verlangen; Standard: egal)

# checks:
#   spec:
#     - openspec validate {change} --strict
#   code:
#     - uv run pytest -q

# max_agent_rounds: 2
# human_gates: { after_spec: true, after_code: true, on_conflict: true }

# Modelle pro Rolle (nur harness/model; Modell-IDs: `opencode models`)
# agents:
#   spec_reviewer: { model: "hagrid/DeepSeek-V4.1-Flash-1017" }
#   code_builder:  { harness: opencode, model: "lmstudio/qwen3.8-27b-splash" }
"""
