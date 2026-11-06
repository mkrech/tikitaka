# tikitaka

Orchestrator for a **builder/reviewer workflow** with coding agents in arbitrary harnesses
(Claude Code, OpenCode, Kiro, …), **OpenSpec** for the specification and **human gates**.

```
Spec builder ⇄ Spec reviewer (max. 2 rounds)  ──▶  HUMAN GATE spec
      ▲                                              │ changes → feedback → builder → checks → 1 review round → gate
      └──────────────────────────────────────────────┘ approve ↓
Code builder ⇄ Code reviewer (max. 2 rounds)  ──▶  HUMAN GATE code
      ▲                                              │ changes / spec_change (back to spec)
      └──────────────────────────────────────────────┘ approve → openspec archive → done
```

- **Deterministic checks** (`openspec validate`, tests) run before every LLM review. Red checks go straight back to the builder and do not count as a round.
- Reviews come back **structured** with a severity (MCP tool `submit_review`, otherwise a JSON block in the text). Only `blocker`/`major` trigger a new round, `minor` ends up in the gate document.
- Builders may reject reviewer findings with a justification (`wont_fix`), **human feedback is binding**.
- No consensus after the maximum number of rounds → **conflict → human gate**. The same applies if the builder changes nothing at all in a phase
  or if a review contradicts itself even after being asked again.
- Every round is a **git commit** in the target project. Human feedback is recorded in the commit message.
- State lives in `<project>/.tikitaka/runs/<change>/state.json` → `resume` is possible at any time.

## Quickstart

Run everything from the tikitaka directory. The target project (`../tikitaka-smoke` below) is a sibling directory.
Comments sit on their own lines on purpose: an interactive zsh does not treat a trailing `# …` as a comment
and would fail with `missing delimiter for 'g' glob qualifier`.

```bash
# Check harnesses, tools and model IDs (add --probe to send each model a tiny request)
uv run tikitaka doctor

# Create the target project (git + openspec + .tikitaka.yaml)
uv run tikitaka init ../tikitaka-smoke

# Start a new run – or continue an existing one for the same change (a new --idea is then ignored)
uv run tikitaka run ../tikitaka-smoke --change add-hello --idea "minimal hello in python"

# The same with the idea from a file
uv run tikitaka run ../tikitaka-smoke --change add-hello --idea-file idee.md

# Start despite uncommitted changes in the target project (they get committed)
uv run tikitaka run ../tikitaka-smoke --change add-hello --idea "…" --allow-dirty

# Continue a run (after Ctrl+C, an agent error or while waiting at a human gate)
uv run tikitaka resume ../tikitaka-smoke --change add-hello

# Show the runs of a project
uv run tikitaka status ../tikitaka-smoke

# Coach MCP server (stdio) for Claude Code / OpenCode – see "Coach" below for registering it
uv run tikitaka coach
```

At a human gate, open `<project>/.tikitaka/runs/<change>/gate-<phase>-<n>.md`, set `decision:` to `approve`,
`changes` (feedback under `## Dein Feedback`) or `reject` and save – the run continues.

## Setup

```bash
uv sync
npm install -g @fission-ai/openspec                # if not installed yet
npm install -g @anthropic-ai/sandbox-runtime      # `srt`, only needed for the pi-srt harness
uv run tikitaka doctor               # checks harnesses, tools and whether the configured models exist
uv run tikitaka doctor --probe       # additionally sends every model one tiny request (finds blocked models, expired logins)
```

`run` and `resume` check the model IDs against the harness (`models_cmd`, e.g. `opencode models` or `pi --list-models`) before they
start and stop with suggestions when one is unknown (`--skip-preflight` to bypass).

Roles and harnesses are configured in [workflow.yaml](workflow.yaml). Switching the combination = changing a single line:

```yaml
agents:
  spec_builder:  { harness: claude,   prompt: prompts/spec-builder.md }
  spec_reviewer: { harness: opencode, prompt: prompts/spec-reviewer.md, readonly: true }
```

The prompts for the four roles live in [prompts/](prompts/).

### Harnesses

| Harness | Command | Notes |
|---|---|---|
| `claude` | `claude -p` | MCP via `--mcp-config`, sessions via `--session-id`/`--resume` |
| `opencode` | `opencode run` | background service, so MCP comes from a project file (see below) |
| `pi` | `pi -p` | no background service, whole agent in the subprocess |
| `pi-srt` | `srt --settings … pi -p` | same as `pi`, plus sandbox (see below) |
| `kiro` | `kiro-cli chat --no-interactive` | flags may differ between Kiro versions |

**Pi specifics** (`pi`, `pi-srt`):

- `models_cmd` is `pi --list-models`, so a model must be written as `provider/model` (e.g. `custom-openai/DeepSeek-V4.1-Flash-1017`).
  Models come from `~/.pi/agent/models.json`; `pi --list-models` prints the available ones.
- The harness passes `--approve` because Pi only loads the project's `.pi/mcp.json` after project trust; print mode cannot answer the trust prompt.
- `--tools` is an **allowlist**, not a denylist: an MCP tool that no entry names is never declared to the model, even with
  `exposure: "direct"`. The read-only harness therefore lists `mcp__tikitaka__*` explicitly next to `read,grep,find,ls`.
- The reviewer gets no `bash`, so it cannot write anything; the diff is handed to it as a file.
- Sessions run with `--session-id <uuid>` so a follow-up question can resume the same conversation.

### Sandboxing the agents (`pi-srt`)

The harness `pi-srt` runs Pi inside Anthropic's [Sandbox Runtime](https://github.com/anthropics/sandbox-runtime)
(`srt`): every agent call is a single sandboxed process tree with filesystem and network isolation, no container needed.

```bash
npm install -g @anthropic-ai/sandbox-runtime   # provides `srt`
uv run tikitaka doctor                        # also checks `srt` and the settings file
```

The rules live in the repo: [sandbox/srt-settings.json](sandbox/srt-settings.json). The harness references them via
`sandbox_settings`, which is resolved relative to `workflow.yaml` and passed as `{srt_settings}`:

```yaml
harnesses:
  pi-srt:
    cmd: [srt, --settings, "{srt_settings}", pi, -p, "{prompt}"]
    sandbox_settings: sandbox/srt-settings.json
```

What the shipped settings do:

| Rule | Effect |
|---|---|
| `filesystem.allowWrite: ["."]` | agents write only inside the target project (the run's cwd) – the MCP server's `mcp/` files are in there too |
| `filesystem.allowWrite: ["~/.pi"]` | Pi keeps its sessions and settings; `~/.pi/agent/auth.json` is `denyWrite` |
| `filesystem.denyRead` | `~/.ssh`, `~/.aws`, `~/.config/gh` are unreadable |
| `network.allowedDomains` | only the model provider is reachable – everything else gets `403 host is not on the allow list` |

The deterministic checks (`openspec validate`, `pytest`) still run outside the sandbox: they are shell commands from
your `workflow.yaml`, not model output. If you want them isolated too, wrap them in `srt` as well.

**`NO_PROXY` caveat:** `srt` always sets `NO_PROXY` to include the private ranges (`10/8`, `172.16/12`, `192.168/16`).
A client that interprets these CIDR entries (curl does) then dials a LAN model server directly, which the sandbox forbids.
Pi's undici proxy agent matches `NO_PROXY` entries as host names only, so it goes through srt's filtering proxy and works.
If a model server stops answering after an undici update, override `NO_PROXY`/`no_proxy` to `localhost,127.0.0.1,::1`
for the harness command.

## Example: calculator

```bash
uv run tikitaka init ../calculator
```

```bash
uv run tikitaka run ../calculator --change add-calculator --idea "A command-line calculator in Python (uv, pytest). Basic arithmetic operations, parentheses, operator precedence, decimals. Error message on division by 0 and on invalid input. Interactive REPL mode and one-shot invocation: calc '2+3*4'."
```

Instead of `--idea` you can also pass `--idea-file <file>`. If the change already exists, `run` resumes the run and ignores the
new idea. The path to `workflow.yaml` comes from `--config`, otherwise from `TIKITAKA_CONFIG`, otherwise from the current directory.

The global code checks in `workflow.yaml` (`uv run pytest -q`) suit a Python project.
Per project they can be overridden (see below).

### Project configuration and smoke checks

`init` creates a commented-out `.tikitaka.yaml` in the target project. It may override `checks`,
`max_agent_rounds`, `rounds_after_human`, `max_check_fixes`, `blocking_severities`, `timeout_seconds`,
`human_gates`, `archive_on_done` and, per role, `agents.<role>.model` / `.harness`.
Prompts and harness definitions stay global. `checks.<phase>` replaces the global checks of exactly that phase.

```yaml
agents:
  spec_reviewer: { model: "hagrid/GLM-5.3-Flash-EXL3" }
  code_reviewer: { model: "hagrid/GLM-5.3-Flash-EXL3" }
```

Besides shell commands (which must exit 0) there are **smoke checks**: a single command that runs for several
inputs and whose output is inspected – e.g. "no invalid input may produce a traceback":

```yaml
checks:
  code:
    - uv run pytest -q
    - name: no tracebacks on invalid input
      run: uv run -q calc {input}        # {input} is substituted shell-safely
      inputs: ["2²", "1/0", "abc", "((1", "1.2.3", ".5", ""]
      must_not_contain: [Traceback]
      # must_contain: [...]   check_exit: true  (require exit 0; default: exit code ignored)
```

If a smoke check fails, the builder receives the affected inputs together with their output as feedback.

### Reviews across multiple rounds

From round 2 on, the reviewer receives all findings from previous rounds and must report any that are still open again.
If it nevertheless drops an earlier minor finding, that finding shows up in the gate document under
**"Frühere Minor-Findings – nicht erneut gemeldet"** (the literal heading the code emits), so that nothing disappears silently
(matched by text or ID + file).

From the second review of a phase on, the reviewer only inspects the **diff since its last review**
(`<step>.diff`); the full diff of the phase is kept next to it as `<step>.full.diff` for reference.
After a human gate it also receives your feedback and checks whether it has been implemented.
Generated files (`__pycache__/`, `*.pyc`, `.venv/`, `node_modules/`, lockfiles …) are excluded from the
reviewer diff.

### MCP server for the agents

Builders and reviewers report their results through the MCP server `tikitaka`
([mcp_server.py](src/tikitaka/mcp_server.py)) instead of a JSON block in the text. tikitaka starts
it for each agent invocation and registers it with the harness (claude: `--mcp-config`; OpenCode: see below).
The tool arguments are schema-checked – an invalid `verdict` or a
wrong `severity` is rejected by the server immediately and the agent corrects itself.

| Tool | Role | Purpose |
|---|---|---|
| `submit_review(verdict, summary, findings)` | Reviewer | Deliver the review |
| `submit_build(summary, addressed, wont_fix, questions)` | Builder | Deliver the summary |
| `get_previous_findings()` | Reviewer | Findings from previous rounds |
| `get_human_feedback()` | both | Feedback from the last human gate |
| `ask_human(question)` | both | Question to you – collected and shown in the gate document |

The results are stored in `<run>/mcp/<step>.result.json`, questions in `<run>/mcp/questions.jsonl`.
If an agent does not call the tool (or the harness has no MCP settings), the JSON block in the text
is still used, including a follow-up request. To disable: `mcp: false` in `workflow.yaml`.

**OpenCode (V2):** its background service starts MCP servers once per project and ignores the environment of the
client (`OPENCODE_CONFIG_CONTENT`), and a private `--standalone` server does not know your Kiro or GitHub Copilot login.
So the harness definition (`mcp_config_file` / `mcp_config_content`) makes tikitaka write
`<project>/.opencode/opencode.json` with the server. The file is excluded locally via `.git/info/exclude`, removed
when the run ends, and a file that does not belong to tikitaka is never overwritten (the run stops with a hint instead).
Because the server lives longer than one agent call, it reads the current step and role from `<run>/mcp/current.json`
and rejects the wrong tool for the role. In OpenCode's Code Mode the agent finds the tools via `search` and calls
`tools.tikitaka.submit_review(...)` inside `execute`; the prompts say so.

**Pi:** Pi has no background service – the agent runs entirely in the subprocess tikitaka starts. Its MCP servers come from
`<project>/.pi/mcp.json`, using the same `mcpServers` format as Claude. Pi only loads that file once the project is trusted,
so the harness runs with `--approve`; `exposure: "direct"` puts the tools straight into the model's tool list instead of
behind a codemode script. The file is excluded locally via `.git/info/exclude`, removed when the run ends, and a foreign
file is never overwritten. The server reads step and role from `<run>/mcp/current.json`, exactly like OpenCode's.
Note that `--tools` is an allowlist: an MCP tool that no entry names is not declared, so the read-only harness lists
`mcp__tikitaka__*` explicitly.

### Coach: steering tikitaka from Claude Code or OpenCode

`tikitaka coach` is a second MCP server – for **you** instead of for the agents. In a
Claude Code or OpenCode session it lets you ask things like "How is calculator-4 doing?", "What is waiting at the gate?" or "Start a run
for …". The server provides facts from the run files; interpretation is up to the model of your session.

| Tool | Purpose |
|---|---|
| `list_runs(project)` | Runs of a project, currently running / waiting for you |
| `status(project, change)` | Phase, current agent (runtime, log activity), open gate, minutes per role, latest checks/reviews |
| `log_tail(project, change, lines, step?)` | End of an agent log |
| `gate_briefing(project, change)` | Everything needed for the gate decision: conflict, findings, agent questions (deduplicated), builder, checks, removed tests, write-protection violations |
| `run_smoke(project, extra_inputs?)` | Try the program with the smoke inputs from `.tikitaka.yaml` (plus your own) |
| `compare_runs(runs)` | Compare runs: agent minutes, cycles, decisions, findings, follow-up requests, errors |
| `list_models(filter?)` | Model IDs known to OpenCode (`provider/model`) |
| `get_agents(project)` / `set_agents(project, models, harness?)` | Read/set the models per role of a project – also `builder`, `reviewer`, `all`; unknown models are rejected with suggestions. Takes effect from the next start or `resume_run`, not for a run in progress |
| `start_run` / `resume_run` / `stop_run` | Start runs in the background (optionally with `models`), resume them, stop them (like Ctrl+C) |
| `decide(project, change, decision, confirmed_by_human, feedback?)` | Write the decision into the gate – **only after your confirmation** |

Background runs write their output to `<run>/orchestrator.log`. Registering it (adjust the paths):

```bash
claude mcp add tikitaka-coach -- uv run --directory /path/to/tikitaka tikitaka --config /path/to/tikitaka/workflow.yaml coach
```

OpenCode (`~/.config/opencode/opencode.jsonc`):

```jsonc
"mcp": {
  "tikitaka-coach": {
    "type": "local",
    "command": ["uv", "run", "--directory", "/path/to/tikitaka", "tikitaka",
                "--config", "/path/to/tikitaka/workflow.yaml", "coach"]
  }
}
```

### Robustness

- **Missing review JSON:** First only the result is requested again (a matter of seconds), only then is the review repeated.
  If the harness has `resume_args` (opencode, claude, pi), the reviewer's session is resumed through its own ID
  for this – it still has its entire review in context, and a parallel session in the same directory stays
  untouched. Otherwise it receives the end of its answer.
- **Contradictory review:** `approve` with blocking findings, or `changes_requested` without any, leads to a
  follow-up question. If `changes_requested` remains without a blocking finding, it goes to the gate as a conflict; if `approve` remains with
  blocking findings, the findings count.
- **Builder without changes:** If nothing has been changed in the whole phase yet, there is no review but an immediate
  conflict. Later rounds may be empty, for instance when the builder rejects all findings with a justification.
- **Removed tests:** The code gate lists deleted test files and those that shrank by at least 5 lines net.
  This is a hint to check, not an error: renamed files and changes to test contents are not detected.
- **Commit range in the reviewer prompt:** Everything is already committed; the prompt names the range (`abc123..HEAD`)
  so that a `git diff` without a range does not leave the reviewer empty-handed.
- **Builder timeout:** If the builder has already changed files, the checks still run – if they are green, it proceeds to the review.
  Without changes, a timeout remains an agent error (`resume` once it is fixed).
- **Checks** run with empty stdin – a program that starts a REPL when called without arguments cannot hang.
- **`.gitignore`:** tikitaka adds missing standard entries in the target project (`.tikitaka/`, `.kiro/`, `__pycache__/`,
  `*.pyc`, `.venv/`, `.pytest_cache/`, `node_modules/`, `dist/`, `build/`, `.DS_Store`, `.env`, `.env.*` except
  `.env.example`), but never overwrites anything.
- **One run per project:** Runs share the working tree and the commits. A second one is rejected while the first is
  active, including while it waits at the human gate. Ctrl+C or `stop_run` frees the project again.
- **Clean start:** A new run does not start if the project has uncommitted changes, because the first commit would
  pick them up. Exempt are tikitaka's own files and what `init` creates fresh (`openspec/`, `AGENTS.md`).
  To bypass deliberately: `tikitaka run … --allow-dirty` (not via the coach). `resume` does not check this.
- **Check timeout:** If a check runs longer than 15 minutes, it is terminated together with its child processes and counts as red.
- **Output that is not UTF-8** is replaced instead of aborting the run.
- **Permissions of non-interactive agents:** An agent started by tikitaka cannot answer `ask` prompts of its harness; the
  request is rejected and the agent then guesses. The OpenCode harness therefore runs with `--auto` (approves everything
  that is not explicitly `deny`ed in your OpenCode config). `blocked_patterns` recognises such rejections in the output:
  tikitaka warns, and from 3 rejections in one call it stops the agent as an agent error with a hint. A builder that
  changed nothing gets that hint in the conflict reason.
- **Transient provider errors:** Output matching `transient_patterns` (HTTP 401/429/5xx, "Model unavailable", …) is
  retried once after 5 seconds before the run stops as an agent error.
- **Text instead of tool calls:** If the output contains tool calls as text (`<invoke name=…>`) that were never executed,
  tikitaka warns – the model does not cope with the harness (`doctor --probe` detects this as well).
- **Agent errors do not count as a cycle:** After `resume`, the same cycle continues (`c1`, not `c2`); the error output
  shows the error lines instead of repeated status lines.
- **`state.json`** is written atomically, so `status`, the coach and parallel readers never see half a file.

### Write protection

`readonly` only sets harness flags, and a reviewer with a shell can get around them. That is why the orchestrator
checks after every agent invocation (also after a crash, timeout or Ctrl+C) what has actually changed. On timeout and
abort it first terminates the agent together with its child processes (SIGTERM first, SIGKILL after 5 seconds):

- **Read-only agents:** Changed or new files, their own commits and branch switches are reset to the state before the
  invocation. The changes are kept as `<run>/guard/<step>.patch`.
- **All agents:** `.tikitaka.yaml` is restored; the agent's version is kept at
  `<run>/guard/<step>.tikitaka.yaml`. No agent changes the rules by which it is checked.

The run then continues, so the review still counts. Every violation appears in the gate document, in
`gate_briefing` and in `compare_runs`. `set_agents` is rejected while an agent is working.

Limits: Git-ignored files and branches other than the current one are not checked. If an agent exits
normally, a process it started in the background may keep running after the check. The reviewer's test runs
may only create ignored files, otherwise there is a warning (e.g. add `.coverage` to `.gitignore`).
Files changed by hand in the project are reset during a review as well (and then preserved in the patch), as is
`.tikitaka.yaml` during any agent run.

With the `pi-srt` harness the sandbox backs this up at OS level: a read-only agent has no `bash`, `edit` or `write`
tool, and even if a tool tried to write outside the project, Seatbelt refuses it (`Operation not permitted`).
The git-based guard still runs – it is the second line of defence and also covers harnesses without a sandbox.

### Human gate

At the gate the orchestrator stops, sends a macOS notification and writes
`../calculator/.tikitaka/runs/add-calculator/gate-spec-1.md` with a summary, open findings,
the builder's assumptions and links to the spec files. At the top of the document:

```yaml
---
decision: pending   # approve | changes | reject
---
```

Set it to `changes`, write your feedback under `## Dein Feedback`, save – and the workflow continues.
With Ctrl+C you can abort the waiting and resume later:

```bash
uv run tikitaka resume ../calculator --change add-calculator
```

```bash
uv run tikitaka status ../calculator
```

## Files of a run

`<project>/.tikitaka/runs/<change>/` (gitignored):

| File | Content |
|---|---|
| `state.json` | Phase, cycle, results, history |
| `logs/<step>.log` / `.prompt.md` | Output and prompt of each agent invocation |
| `<step>-review.json` / `-builder.json` | Structured responses |
| `<step>.full.diff` | Full diff of the phase; on the first review of a phase, the reviewer's diff |
| `<step>.diff` | From the second review on: diff since the last review |
| `<step>-checks-<n>.log` | Check output per attempt |
| `gate-<phase>-<n>.md` | Human gate documents |
| `mcp/` | Results, context and questions of the agents via the MCP server |
| `guard/<step>.*` | Changes reset due to write-protection violations |
| `../../lock` (i.e. `.tikitaka/lock`) | Lock on the project holding the active run |
| `orchestrator.log` / `orchestrator.pid` | Output and process ID of runs started by the coach |

## Renaming

The project used to be called `agent-tandem`. Existing target projects keep working: `.agent-tandem.yaml` is read as long as
there is no `.tikitaka.yaml`, and runs under `.agent-tandem/runs/` (and the even older `.agentflow/runs/`) can be
resumed. New runs go to `.tikitaka/`. The `agent-tandem` command remains as an alias for now, so that existing
coach integrations keep starting.

## Tests

```bash
uv run pytest -q
```

The tests use a fake harness ([tests/fake_agent.py](tests/fake_agent.py)) and play through the complete flow
including human feedback, check fixes and conflict – without real agents.
