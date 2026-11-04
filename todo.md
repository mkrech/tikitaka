# ToDo – tikitaka Review

Stand: 2026-11-04 · Basis: `uv run pytest -q` → 51 Tests grün.
Die Punkte sind nach Priorität sortiert; Datei-/Funktionsangaben beziehen sich auf den aktuellen Stand.

## Prio 1 – Korrektheit und Robustheit

- [ ] **MCP-Untergrenze korrigieren** (`pyproject.toml`)
  - `mcp>=1.10` → `mcp>=2.0`
  - Begründung: Der Code importiert `mcp.server.mcpserver` (`mcp_server.py`, `coach_server.py`). Das gibt es erst ab mcp 2.x; in 1.10 heißt das Modul `mcp.server.fastmcp`.
  - Danach `uv lock` / `uv sync` prüfen.

- [x] **`state.json` atomar schreiben** (`src/tikitaka/orchestrator.py`, `Orchestrator._save`)
  - Aktuell `write_text()`; parallele Leser (`coach.status`, `coach.list_runs`, `cli status`) können eine halb geschriebene Datei erwischen.
  - Lösung: in temp-Datei schreiben und mit `Path.replace()` umbenennen – wie `mcp_server._write_json`.
  - Test ergänzen, der das atomare Schreiben absichert.

- [ ] **`change` zentral validieren** (`src/tikitaka/cli.py`, `src/tikitaka/orchestrator.py`)
  - Regex wie im Coach: `[a-z0-9][a-z0-9-]*`.
  - Gilt mindestens für `run` und `resume`; der Coach validiert bereits.
  - Verhindert Path-Traversal über `run_dir`, Shell-Injection über `{change}` in Checks (`Orchestrator._run_check`) und kaputte AppleScript-Strings.
  - Zusätzlich: `notify()` in `gates.py` sollte Anführungszeichen/Backslashes escapen.

- [ ] **`coach.run_smoke` an die Orchestrator-Checks angleichen** (`src/tikitaka/coach.py`)
  - Aktuell werden `check_exit`, `must_contain` und die `{change}`-Ersetzung ignoriert.
  - Folge: Der Coach kann einen Smoke-Check als „ok“ melden, den der Orchestrator rot bewertet.
  - Lösung: gemeinsame Check-Funktion aus `Orchestrator._run_check` extrahieren und von beiden nutzen.

- [ ] **`openspec archive`-Fehler sichtbar machen** (`src/tikitaka/orchestrator.py`, `Orchestrator._finish`)
  - Der Exit-Code wird nur geloggt; danach wird trotzdem committed und `phase = "done"` gesetzt.
  - Entscheiden: bei Fehler nicht `done` setzen oder mindestens deutlich warnen und im Gate/Status anzeigen.
  - Verhalten in README dokumentieren.

## Prio 2 – Kleinere Verbesserungen

- [ ] **`project_env`: tikitaka-venv aus `PATH` entfernen** (`src/tikitaka/harness.py`)
  - Aktuell wird nur `VIRTUAL_ENV` entfernt; Checks könnten den `python` von tikitaka statt den des Zielprojekts verwenden.

- [ ] **`coach._pid_alive`: PID-Wiederverwendung absichern** (`src/tikitaka/coach.py`)
  - Eine fremde, wiederverwendete PID kann einen beendeten Lauf als „läuft“ anzeigen.
  - Z. B. Startzeit oder Prozess-Kommando zusätzlich prüfen.

- [ ] **TOCTOU in `_require_free`/`start_run` schließen** (`src/tikitaka/coach.py`)
  - Zwischen Lock-Prüfung und `_spawn` kann ein anderer Lauf starten.
  - Lock bis zum erfolgreichen Spawn halten oder den Fehler sauber weiterreichen.

- [ ] **`coach.status`: „Checks grün“ strukturiert bestimmen** (`src/tikitaka/coach.py`)
  - Aktuell Heuristik `"✗" not in text`; ein Testname oder eine Ausgabe mit „✗“ verfälscht das Ergebnis.
  - Besser: Status aus dem Check-Log/den Check-Ergebnissen ableiten.

- [ ] **`dirty_files`: Rename-Einträge korrekt parsen** (`src/tikitaka/orchestrator.py`)
  - `git status --porcelain` liefert bei Renames `R  old -> new`; die aktuelle Pfad-Extraktion ist dafür nicht ausgelegt.

- [ ] **`_ensure_git`: Verhalten in Unterverzeichnissen eines Repos klären** (`src/tikitaka/orchestrator.py`)
  - Liegt das Zielprojekt in einem bestehenden Repo, wird durch `git init` ein verschachteltes Repo angelegt.
  - Entweder bewusst dokumentieren oder erkennen und sauber behandeln.

- [ ] **Builder-Branch-Wechsel prüfen** (`src/tikitaka/orchestrator.py`, Guard)
  - Nur Readonly-Agenten werden vollständig zurückgesetzt. Ein Builder kann den Branch wechseln; das ist in der README als Grenze dokumentiert.
  - Entscheiden, ob das für Builder ebenfalls abgesichert werden soll.

- [ ] **`run_agent`: Live-Logs bei gepufferten Harnesses verbessern** (`src/tikitaka/harness.py`)
  - Kindprozesse können ihre Ausgabe blockweise puffern; das Log erscheint dann erst am Ende.
  - Ggf. `PYTHONUNBUFFERED=1` oder harness-spezifische Flags setzen.

- [ ] **Test-Suite-Laufzeit reduzieren** (`tests/`)
  - Der Lauf dauert ~90 s, vor allem durch echte Timeout-/Sleep-Tests.
  - Kleinere Timeouts per `monkeypatch` bzw. gezieltere Tests prüfen.

## Optional / Ideen

- [ ] `doctor` prüft die installierte `mcp`-Version und warnt verständlich bei < 2.0.
- [ ] CI einrichten (z. B. GitHub Actions) mit `uv run pytest -q`.
- [ ] `ruff` und/oder `mypy` einführen.
- [ ] Sprachliche Konsistenz klären: README ist englisch, Code-Kommentare und Prompts sind deutsch.

## Definition of Done

- [ ] `uv run pytest -q` ist grün.
- [ ] `uv run tikitaka doctor` läuft ohne Fehler.
- [ ] Für jeden Fix gibt es einen Test (oder eine nachvollziehbare Begründung, warum nicht).
- [ ] README wurde bei Verhaltensänderungen aktualisiert.
