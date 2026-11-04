# tikitaka

Orchestrator für einen **Builder/Reviewer-Workflow** mit Coding-Agents in beliebigen Harnesses
(Claude Code, OpenCode, Kiro, …), **OpenSpec** für die Spezifikation und **Human Gates**.

```
Spec-Builder ⇄ Spec-Reviewer (max. 2 Runden)  ──▶  HUMAN GATE Spec
      ▲                                              │ changes → Feedback → Builder → Checks → 1 Review-Runde → Gate
      └──────────────────────────────────────────────┘ approve ↓
Code-Builder ⇄ Code-Reviewer (max. 2 Runden)  ──▶  HUMAN GATE Code
      ▲                                              │ changes / spec_change (zurück zur Spec)
      └──────────────────────────────────────────────┘ approve → openspec archive → done
```

- Vor jedem LLM-Review laufen **deterministische Checks** (`openspec validate`, Tests). Rote Checks gehen direkt zurück an den Builder und zählen nicht als Runde.
- Reviews kommen **strukturiert** mit Severity (MCP-Tool `submit_review`, sonst JSON-Block im Text). Nur `blocker`/`major` lösen eine neue Runde aus, `minor` landet im Gate-Dokument.
- Builder dürfen Reviewer-Findings begründet ablehnen (`wont_fix`), **Human-Feedback ist verbindlich**.
- Kein Konsens nach max. Runden → **Konflikt → Human Gate**. Dasselbe gilt, wenn der Builder in einer Phase gar nichts ändert
  oder ein Review sich auch auf Rückfrage widerspricht.
- Jede Runde ist ein **Git-Commit** im Zielprojekt. Human-Feedback steht in der Commit-Message.
- Zustand in `<projekt>/.tikitaka/runs/<change>/state.json` → `resume` jederzeit möglich.

## Setup

```bash
uv sync
npm install -g @fission-ai/openspec   # falls noch nicht installiert
uv run tikitaka doctor               # prüft Harnesses + Tools
```

Rollen und Harnesses stehen in [workflow.yaml](workflow.yaml). Kombination wechseln = eine Zeile ändern:

```yaml
agents:
  spec_builder:  { harness: claude,   prompt: prompts/spec-builder.md }
  spec_reviewer: { harness: opencode, prompt: prompts/spec-reviewer.md, readonly: true }
```

Die Prompts der vier Rollen liegen in [prompts/](prompts/).

## Beispiel: Taschenrechner

```bash
uv run tikitaka init ../calculator
```

```bash
uv run tikitaka run ../calculator --change add-calculator --idea "Ein Kommandozeilen-Taschenrechner in Python (uv, pytest). Grundrechenarten, Klammern, Punkt-vor-Strich, Dezimalzahlen. Fehlermeldung bei Division durch 0 und ungültiger Eingabe. Interaktiver REPL-Modus und Einmalaufruf: calc '2+3*4'."
```

Statt `--idea` geht auch `--idea-file <datei>`. Gibt es den Change schon, setzt `run` den Lauf fort und ignoriert die
neue Idee. Der Pfad zur `workflow.yaml` kommt aus `--config`, sonst aus `TIKITAKA_CONFIG`, sonst aus dem aktuellen Verzeichnis.

Die globalen Code-Checks in `workflow.yaml` (`uv run pytest -q`) passen zu einem Python-Projekt.
Pro Projekt lassen sie sich überschreiben (siehe unten).

### Projekt-Konfiguration und Smoke-Checks

`init` legt im Zielprojekt eine auskommentierte `.tikitaka.yaml` an. Sie darf `checks`,
`max_agent_rounds`, `rounds_after_human`, `max_check_fixes`, `blocking_severities`, `timeout_seconds`,
`human_gates`, `archive_on_done` und je Rolle `agents.<rolle>.model` / `.harness` überschreiben.
Prompts und Harness-Definitionen bleiben global. `checks.<phase>` ersetzt die globalen Checks genau dieser Phase.

```yaml
agents:
  spec_reviewer: { model: "hagrid-remote-GLM-5.3-Flash-EXL3/GLM-5.3-Flash-EXL3" }
  code_reviewer: { model: "hagrid-remote-GLM-5.3-Flash-EXL3/GLM-5.3-Flash-EXL3" }
```

Neben Shell-Befehlen (müssen Exit 0 liefern) gibt es **Smoke-Checks**: ein Befehl, der für mehrere
Eingaben läuft und dessen Ausgabe geprüft wird – z.B. „keine Fehleingabe darf einen Traceback zeigen“:

```yaml
checks:
  code:
    - uv run pytest -q
    - name: keine Tracebacks bei Fehleingaben
      run: uv run -q calc {input}        # {input} wird shell-sicher eingesetzt
      inputs: ["2²", "1/0", "abc", "((1", "1.2.3", ".5", ""]
      must_not_contain: [Traceback]
      # must_contain: [...]   check_exit: true  (Exit 0 verlangen; Standard: Exit-Code egal)
```

Schlägt ein Smoke-Check fehl, bekommt der Builder die betroffenen Eingaben samt Ausgabe als Feedback.

### Reviews über mehrere Runden

Ab Runde 2 bekommt der Reviewer alle Findings der Vorrunden und muss noch offene erneut melden.
Lässt er ein früheres Minor-Finding trotzdem fallen, erscheint es im Gate-Dokument unter
**„Frühere Minor-Findings – nicht erneut gemeldet“**, damit nichts still verschwindet
(abgeglichen über Text oder ID + Datei).

Ab dem zweiten Review einer Phase prüft der Reviewer nur den **Diff seit seinem letzten Review**
(`<schritt>.diff`); der Gesamtdiff der Phase liegt als `<schritt>.full.diff` zum Nachschlagen daneben.
Nach einem Human Gate bekommt er außerdem dein Feedback und prüft, ob es umgesetzt ist.
Generierte Dateien (`__pycache__/`, `*.pyc`, `.venv/`, `node_modules/`, Lockfiles …) sind aus dem
Reviewer-Diff ausgeschlossen.

### MCP-Server für die Agenten

Builder und Reviewer melden ihre Ergebnisse über den MCP-Server `tikitaka`
([mcp_server.py](src/tikitaka/mcp_server.py)) statt als JSON-Block im Text. tikitaka startet
ihn pro Agentenaufruf und meldet ihn dem Harness an (opencode: `OPENCODE_CONFIG_CONTENT`,
claude: `--mcp-config`). Die Tool-Argumente sind schema-geprüft – ein ungültiges `verdict` oder eine
falsche `severity` lehnt der Server sofort ab und der Agent korrigiert sich.

| Tool | Rolle | Zweck |
|---|---|---|
| `submit_review(verdict, summary, findings)` | Reviewer | Review abliefern |
| `submit_build(summary, addressed, wont_fix, questions)` | Builder | Zusammenfassung abliefern |
| `get_previous_findings()` | Reviewer | Findings der Vorrunden |
| `get_human_feedback()` | beide | Feedback aus dem letzten Human Gate |
| `ask_human(question)` | beide | Frage an dich – erscheint gesammelt im Gate-Dokument |

Die Ergebnisse liegen in `<run>/mcp/<schritt>.result.json`, Fragen in `<run>/mcp/questions.jsonl`.
Ruft ein Agent das Tool nicht auf (oder hat der Harness keine `mcp_args`/`mcp_env`), greift weiter
der JSON-Block im Text samt Nachforderung. Abschalten: `mcp: false` in `workflow.yaml`.

### Coach: tikitaka aus Claude Code oder OpenCode steuern

`tikitaka coach` ist ein zweiter MCP-Server – für **dich** statt für die Agenten. Damit fragst du in einer
Claude-Code- oder OpenCode-Sitzung z.B. „Wie läuft calculator-4?“, „Was steht am Gate?“ oder „Starte einen Lauf
für …“. Der Server liefert Fakten aus den Run-Dateien; die Einordnung macht das Modell deiner Sitzung.

| Tool | Zweck |
|---|---|
| `list_runs(project)` | Läufe eines Projekts, läuft gerade / wartet auf dich |
| `status(project, change)` | Phase, aktueller Agent (Laufzeit, Log-Aktivität), offenes Gate, Minuten pro Rolle, letzte Checks/Reviews |
| `log_tail(project, change, lines, step?)` | Ende eines Agenten-Logs |
| `gate_briefing(project, change)` | Alles für die Gate-Entscheidung: Konflikt, Findings, Fragen der Agenten (dedupliziert), Builder, Checks, entfernte Tests, Schreibschutz-Verletzungen |
| `run_smoke(project, extra_inputs?)` | Programm mit den Smoke-Eingaben aus `.tikitaka.yaml` (plus eigenen) ausprobieren |
| `compare_runs(runs)` | Läufe vergleichen: Agentenminuten, Zyklen, Entscheidungen, Findings, Nachforderungen, Fehler |
| `list_models(filter?)` | Modell-IDs, die OpenCode kennt (`provider/model`) |
| `get_agents(project)` / `set_agents(project, models, harness?)` | Modelle je Rolle eines Projekts lesen/setzen – auch `builder`, `reviewer`, `all`; unbekannte Modelle werden mit Vorschlägen abgelehnt. Gilt erst ab dem nächsten Start oder `resume_run`, nicht für einen laufenden Lauf |
| `start_run` / `resume_run` / `stop_run` | Läufe im Hintergrund starten (optional mit `models`), fortsetzen, anhalten (wie Ctrl+C) |
| `decide(project, change, decision, confirmed_by_human, feedback?)` | Entscheidung ins Gate schreiben – **nur nach deiner Bestätigung** |

Hintergrund-Läufe schreiben ihre Ausgabe nach `<run>/orchestrator.log`. Einbinden (Pfade anpassen):

```bash
claude mcp add tikitaka-coach -- uv run --directory /pfad/zu/tikitaka tikitaka --config /pfad/zu/tikitaka/workflow.yaml coach
```

OpenCode (`~/.config/opencode/opencode.jsonc`):

```jsonc
"mcp": {
  "tikitaka-coach": {
    "type": "local",
    "command": ["uv", "run", "--directory", "/pfad/zu/tikitaka", "tikitaka",
                "--config", "/pfad/zu/tikitaka/workflow.yaml", "coach"]
  }
}
```

### Robustheit

- **Fehlendes Review-JSON:** Erst wird nur das Ergebnis nachgefordert (Sekunden), erst danach das Review wiederholt.
  Hat der Harness `resume_args` (opencode, claude), wird dafür die Sitzung des Reviewers über ihre eigene ID
  fortgesetzt – er hat seine ganze Prüfung noch im Kontext, und eine parallele Sitzung im selben Verzeichnis bleibt
  unberührt. Sonst bekommt er das Ende seiner Antwort.
- **Widersprüchliches Review:** `approve` mit blockierenden Findings oder `changes_requested` ohne sie führt zu einer
  Rückfrage. Bleibt `changes_requested` ohne blockierendes Finding, geht es als Konflikt ans Gate; bleibt `approve` mit
  blockierenden Findings, zählen die Findings.
- **Builder ohne Änderungen:** Ist in der ganzen Phase noch nichts geändert, gibt es kein Review, sondern direkt einen
  Konflikt. Spätere Runden dürfen leer sein, etwa wenn der Builder alle Findings begründet ablehnt.
- **Entfernte Tests:** Das Code-Gate nennt gelöschte Testdateien und solche, die netto mindestens 5 Zeilen kleiner
  wurden. Das ist ein Hinweis zum Prüfen, kein Fehler: Umbenannte Dateien und Änderungen an Testinhalten erkennt er nicht.
- **Commit-Bereich im Reviewer-Prompt:** Alles ist bereits committet; der Prompt nennt den Bereich (`abc123..HEAD`),
  damit ein `git diff` ohne Bereich den Reviewer nicht ins Leere laufen lässt.
- **Builder-Timeout:** Hat der Builder bereits Dateien geändert, laufen trotzdem die Checks – sind sie grün, geht es zum Review.
  Ohne Änderungen bleibt ein Timeout ein Agent-Fehler (`resume` nach Behebung).
- **Checks** laufen mit leerem stdin – ein Programm, das ohne Argument eine REPL startet, kann nicht hängen.
- **`.gitignore`:** tikitaka ergänzt im Zielprojekt fehlende Standardeinträge (`.tikitaka/`, `__pycache__/`,
  `*.pyc`, `.venv/`, `.pytest_cache/`, `node_modules/`, `dist/`, `build/`, `.DS_Store`, `.env`, `.env.*` außer
  `.env.example`), überschreibt aber nichts.
- **Ein Lauf pro Projekt:** Läufe teilen sich Working Tree und Commits. Ein zweiter wird abgelehnt, solange der erste
  aktiv ist, auch während er am Human Gate wartet. Mit Ctrl+C oder `stop_run` wird das Projekt wieder frei.
- **Sauberer Start:** Ein neuer Lauf startet nicht, wenn das Projekt offene Änderungen hat, weil der erste Commit sie
  mitnehmen würde. Ausgenommen sind die Dateien von tikitaka und was `init` frisch anlegt (`openspec/`, `AGENTS.md`).
  Bewusst übergehen: `tikitaka run … --allow-dirty` (nicht über den Coach). `resume` prüft das nicht.
- **Check-Timeout:** Läuft ein Check länger als 15 Minuten, wird er samt Kindprozessen beendet und zählt als rot.
- **Ausgaben, die kein UTF-8 sind,** werden ersetzt, statt den Lauf abzubrechen.

### Schreibschutz

`readonly` setzt nur Harness-Flags, und ein Reviewer mit Shell kommt daran vorbei. Deshalb prüft der Orchestrator
nach jedem Agentenaufruf (auch nach Absturz, Timeout oder Ctrl+C), was sich wirklich geändert hat. Bei Timeout und
Abbruch beendet er vorher den Agenten samt Kindprozessen (erst SIGTERM, nach 5 Sekunden SIGKILL):

- **Readonly-Agenten:** Geänderte oder neue Dateien, eigene Commits und Branch-Wechsel werden auf den Stand vor dem
  Aufruf zurückgesetzt. Die Änderungen liegen als `<run>/guard/<schritt>.patch`.
- **Alle Agenten:** `.tikitaka.yaml` wird wiederhergestellt, die Fassung des Agenten liegt unter
  `<run>/guard/<schritt>.tikitaka.yaml`. Kein Agent ändert die Regeln, nach denen er geprüft wird.

Der Lauf geht danach weiter, das Review zählt also trotzdem. Jede Verletzung steht im Gate-Dokument, in
`gate_briefing` und in `compare_runs`. `set_agents` ist abgelehnt, solange ein Agent arbeitet.

Grenzen: Gitignorierte Dateien und andere Branches als der aktuelle werden nicht geprüft. Beendet sich ein Agent
normal, kann ein von ihm im Hintergrund gestarteter Prozess nach der Prüfung weiterlaufen. Testläufe des Reviewers
dürfen nur ignorierte Dateien erzeugen, sonst gibt es eine Warnung (z.B. `.coverage` in die `.gitignore` aufnehmen).
Von Hand geänderte Dateien im Projekt werden während eines Reviews mit zurückgesetzt (dann im Patch), ebenso
`.tikitaka.yaml` während eines beliebigen Agentenlaufs.

### Human Gate

Am Gate hält der Orchestrator an, schickt eine macOS-Benachrichtigung und schreibt
`../calculator/.tikitaka/runs/add-calculator/gate-spec-1.md` mit Zusammenfassung, offenen Findings,
Annahmen des Builders und Links zu den Spec-Dateien. Oben im Dokument:

```yaml
---
decision: pending   # approve | changes | reject
---
```

Auf `changes` setzen, unter `## Dein Feedback` schreiben, speichern – der Workflow läuft weiter.
Mit Ctrl+C kannst du das Warten abbrechen und später fortsetzen:

```bash
uv run tikitaka resume ../calculator --change add-calculator
```

```bash
uv run tikitaka status ../calculator
```

## Dateien eines Laufs

`<projekt>/.tikitaka/runs/<change>/` (gitignored):

| Datei | Inhalt |
|---|---|
| `state.json` | Phase, Zyklus, Ergebnisse, Verlauf |
| `logs/<schritt>.log` / `.prompt.md` | Ausgabe und Prompt jedes Agentenaufrufs |
| `<schritt>-review.json` / `-builder.json` | strukturierte Antworten |
| `<schritt>.full.diff` | Gesamtdiff der Phase; beim ersten Review einer Phase der Diff des Reviewers |
| `<schritt>.diff` | Ab dem zweiten Review: Diff seit dem letzten Review |
| `<schritt>-checks-<n>.log` | Ausgabe der Checks je Versuch |
| `gate-<phase>-<n>.md` | Human-Gate-Dokumente |
| `mcp/` | Ergebnisse, Kontext und Fragen der Agenten über den MCP-Server |
| `guard/<schritt>.*` | Zurückgesetzte Änderungen bei Schreibschutz-Verletzungen |
| `../../lock` (also `.tikitaka/lock`) | Sperre des Projekts mit dem aktiven Lauf |
| `orchestrator.log` / `orchestrator.pid` | Ausgabe und Prozess-ID von Läufen, die der Coach gestartet hat |

## Umbenennung

Das Projekt hieß früher `agent-tandem`. Bestehende Zielprojekte laufen weiter: `.agent-tandem.yaml` wird gelesen, solange es
keine `.tikitaka.yaml` gibt, und Läufe unter `.agent-tandem/runs/` (und den noch älteren `.agentflow/runs/`) lassen sich
fortsetzen. Neue Läufe landen in `.tikitaka/`. Der Befehl `agent-tandem` bleibt vorerst als Alias, damit bestehende
Coach-Einbindungen weiter starten.

## Tests

```bash
uv run pytest -q
```

Die Tests nutzen einen Fake-Harness ([tests/fake_agent.py](tests/fake_agent.py)) und spielen den kompletten Ablauf
inkl. Human-Feedback, Check-Fix und Konflikt durch – ohne echte Agenten.
