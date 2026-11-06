from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import PROJECT_CONFIG, PROJECT_TEMPLATE, load_config, project_config_path
from .harness import harness_available, sandbox_profile_path
from .orchestrator import Orchestrator, list_runs
from .preflight import model_problems, probe_agents

DEFAULT_CONFIG = os.environ.get("TIKITAKA_CONFIG", "workflow.yaml")


def cmd_init(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    project.mkdir(parents=True, exist_ok=True)
    if not (project / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        print(f"✓ git init {project}")
    if not (project / "openspec").exists():
        if shutil.which("openspec"):
            p = subprocess.run(["openspec", "init", "--tools", "none"], cwd=project)
            print("✓ openspec init" if p.returncode == 0 else "⚠ openspec init fehlgeschlagen – bitte manuell ausführen")
        else:
            print("⚠ openspec nicht gefunden: npm install -g @fission-ai/openspec")
    project_cfg = project_config_path(project)
    if not project_cfg.exists():
        project_cfg.write_text(PROJECT_TEMPLATE)
        print(f"✓ {PROJECT_CONFIG} angelegt (Checks/Gates pro Projekt, alles auskommentiert)")
    print(f"\nNächster Schritt:\n  uv run tikitaka run {args.project} --change <id> --idea '...'")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    idea = Path(args.idea_file).read_text() if args.idea_file else args.idea
    orch = Orchestrator(cfg, args.project, args.change, idea=idea, allow_dirty=args.allow_dirty)
    if idea and orch.state["idea"] != idea:
        print("⚠ Es gibt bereits einen Lauf für diesen Change – die neue Idee wird ignoriert (resume).")
    if not args.skip_preflight:
        problems = model_problems(orch.cfg)
        if problems:
            raise ValueError("Modell-Konfiguration prüfen (oder --skip-preflight):\n  " + "\n  ".join(problems))
    phase = orch.run()
    return 0 if phase == "done" else 1


def cmd_status(args: argparse.Namespace) -> int:
    runs = list_runs(args.project)
    if not runs:
        print("Keine Läufe gefunden.")
    for r in runs:
        print(f"{r['change']:<30} phase={r['phase']:<10} zyklus={r['cycle']} gates={r['gates']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    ok = True
    used = {a.harness for a in cfg.agents.values()}
    for name, harness in cfg.harnesses.items():
        found = harness_available(harness)
        mark = "✓" if found else ("✗" if name in used else "·")
        print(f"{mark} harness {name:<10} ({harness['cmd'][0]}){'' if name in used else '  [nicht verwendet]'}")
        ok &= found or name not in used
        profile = sandbox_profile_path(harness)
        if profile is not None:
            exists = profile.exists()
            mark = "✓" if exists else ("✗" if name in used else "·")
            print(f"{mark} sandbox profile {profile}{'' if exists else '  ← fehlt'}")
            ok &= exists or name not in used
    for tool in ("git", "openspec", "uv"):
        found = shutil.which(tool) is not None
        print(f"{'✓' if found else '✗'} {tool}")
        ok &= found
    mcp_ok = _mcp_importable()
    print(f"{'✓' if mcp_ok else '✗'} MCP-Server (mcp.server.mcpserver){'' if mcp_ok else ' – uv add \"mcp>=2\"'}")
    ok &= mcp_ok or not cfg.mcp

    problems = model_problems(cfg)
    flagged = {p.split(":", 1)[0] for p in problems}
    probes = probe_agents(cfg) if args.probe else {}
    for role, agent in cfg.agents.items():
        extra = ""
        mark = " "
        if role in flagged:
            mark, ok = "✗", False
        elif args.probe:
            good, detail = probes[(agent.harness, agent.model)]
            mark, extra = ("✓", "") if good else ("✗", f"  ← {detail}")
            ok &= good
        print(f"{mark} {role:<14} → {agent.harness}{' (' + agent.model + ')' if agent.model else ''}"
              f"{' [readonly]' if agent.readonly else ''}{extra}")
    for problem in problems:
        print(f"  ✗ {problem}")
    if not args.probe:
        print("  (Mit --probe antwortet jedes Modell einmal auf einen Mini-Aufruf – findet z.B. gesperrte Modelle.)")
    return 0 if ok else 1


def _mcp_importable() -> bool:
    try:
        import mcp.server.mcpserver  # noqa: F401
    except ImportError:
        return False
    return True


def cmd_coach(args: argparse.Namespace) -> int:
    from .coach_server import main as serve

    config = Path(args.config).resolve()
    load_config(config)  # früh scheitern, falls die Konfiguration fehlt oder kaputt ist
    serve(config)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tikitaka", description="Builder/Reviewer-Workflow mit Human Gates")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Pfad zur workflow.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Zielprojekt anlegen (git + openspec)")
    p.add_argument("project")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="Neuen Lauf starten oder bestehenden fortsetzen")
    p.add_argument("project")
    p.add_argument("--change", required=True, help="OpenSpec-Change-ID, z.B. add-calculator")
    idea = p.add_mutually_exclusive_group()
    idea.add_argument("--idea", help="Beschreibung der Anforderung")
    idea.add_argument("--idea-file", help="Datei mit der Anforderung")
    p.add_argument("--allow-dirty", action="store_true", help="Neuen Lauf trotz offener Änderungen starten (werden mitcommittet)")
    p.add_argument("--skip-preflight", action="store_true", help="Modell-IDs nicht vor dem Start prüfen")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume", help="Lauf fortsetzen (z.B. nach Ctrl+C am Human Gate)")
    p.add_argument("project")
    p.add_argument("--change", required=True)
    p.add_argument("--skip-preflight", action="store_true", help="Modell-IDs nicht vor dem Start prüfen")
    p.set_defaults(func=cmd_run, idea=None, idea_file=None, allow_dirty=False)

    p = sub.add_parser("status", help="Läufe eines Projekts anzeigen")
    p.add_argument("project")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="Prüfen, ob Harnesses, Tools und Modelle in Ordnung sind")
    p.add_argument("--probe", action="store_true", help="Jedes Modell einmal mit einem Mini-Aufruf testen")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("coach", help="Coach-MCP-Server (stdio) zum Beobachten und Steuern von Läufen")
    p.set_defaults(func=cmd_coach)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n⏸  Unterbrochen. Fortsetzen mit: uv run tikitaka resume <projekt> --change <id>")
        return 130
    except ValueError as exc:
        print(f"✖ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
