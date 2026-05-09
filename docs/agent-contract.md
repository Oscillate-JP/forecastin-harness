# Agent contract

Every task an agent works on is described by a **task packet** — a YAML file
that the harness reads and renders into an agent-specific prompt. The same
packet drives Claude, Codex, and Gemini, so the same scope produces
equivalent prompts across providers.

The canonical schema is in `templates/task_packet.yaml`; this document
explains what each field is for and how agents are expected to behave.

## Packet schema

| Field | Type | Required | Purpose |
|---|---|---|---|
| `task_id` | string | optional | Identifier (e.g. `FOR-235`). Pre-filled by lane registration. |
| `branch` | string | optional | Lane branch name. Pre-filled at render time. |
| `base_sha` | string | optional | SHA the lane was branched from. Pre-filled at render time. |
| `mission` | string | **required** | One paragraph describing the outcome. |
| `scope` | string | **required** | What is in *and* out for this slice. |
| `files.allowed` | list[str] | **required** | Files the agent may modify. Globs allowed. |
| `files.forbidden` | list[str] | **required** | Files the agent must not modify. Globs allowed. |
| `acceptance` | list[str] | **required** | Conditions that must hold for the slice to be done. |
| `tests` | list[str] | **required** | Concrete test commands the agent must run. |
| `stop_conditions` | list[str] | **required** | Any one ends the session, success or otherwise. |
| `evidence` | list[str] | **required** | Output the agent must capture before stopping. |

Single-string values are accepted in place of one-element lists, for
ergonomic short packets.

## Behavioural rules baked into every prompt

1. **Stay inside the allowed file list.** Touching anything in the forbidden
   list — or anything not in the allowed list when ambiguous — is treated as
   a stop condition. The agent reports the conflict instead of expanding
   scope.
2. **Never push.** The harness does not push and the agent must not push.
   Branch publishing is operator-driven.
3. **Never bypass git hooks.** No `--no-verify`, no `--no-gpg-sign`, no
   editing of `scripts/ci.sh` or `.git/hooks/`.
4. **Tests must pass before declaring done.** "Passes locally" means the
   exact commands in `tests` complete successfully. The agent must capture
   pre-state and post-state output as the `evidence` field requires.
5. **Stop on a forbidden file change request.** If the only way to satisfy
   the mission requires touching a forbidden file, the agent stops and
   explains.

## Output contract

Every agent reply must contain, in order:

1. **Summary** — one paragraph.
2. **Changed files** — bulleted, with rationale.
3. **Tests run** — exact commands and tail of output.
4. **Evidence** — pre-state and post-state output as required.
5. **Blockers** — list, or `NONE`.
6. **Next action** — one line: `READY_FOR_REVIEW` or a precise question /
   request.

This output is captured and stored in `<state>/tasks/<task_id>.result.json`
by the operator (the harness library exposes the schema; v0.1 does not yet
ingest results automatically — that is a follow-up).

## Per-agent template differences

The three templates (`claude_prompt.md`, `codex_prompt.md`, `gemini_prompt.md`)
are deliberately near-identical. The differences are stylistic, to fit each
provider's conventions:

* **Claude** uses Markdown headings and conversational hints (it processes
  Markdown structure best).
* **Codex** prefers labelled blocks (`SUMMARY`, `CHANGES`, …) because its
  fine-tunes work better with explicit caps-lock section labels.
* **Gemini** uses bold-labelled section names with explicit "you may" /
  "you must not" wording, which Gemini consistently respects.

If a future provider needs a fourth template, drop a new
`<agent>_prompt.md` next to the existing three, register the agent name in
`SUPPORTED_AGENTS` in `prompts.py`, and add a parametrised test entry. No
other changes are required.

## Why this is a packet, not a free-form brief

A packet exists for two reasons:

1. **Reproducibility.** A packet plus a base SHA plus a config is enough
   to reconstruct exactly what an agent was told to do, even months later.
   Free-form briefs decay.
2. **Cross-agent parity.** Without a packet, the same operator gives a
   subtly different brief to each provider, and the resulting diffs are
   not comparable. Packets force a canonical scope so different agents are
   actually competing on the same problem.
