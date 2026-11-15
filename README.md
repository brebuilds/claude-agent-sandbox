# claude-agent-sandbox

[![CI](https://github.com/brebuilds/claude-agent-sandbox/actions/workflows/ci.yml/badge.svg)](https://github.com/brebuilds/claude-agent-sandbox/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Three safety patterns extracted from a working personal agent runner, written up as
an engineering record rather than marketing copy. **This is not a framework** — there
is no orchestration layer, no CLI, no config system. It is three small, dependency-free
Python modules and the tests that pin their behavior, published so the reasoning behind
each pattern is checkable against real code and real test runs instead of taken on
faith.

Source: a local, single-user tool called Agent Room that turns a filesystem-based task
queue into finished work by spawning coding-agent CLI processes against queued jobs.
The three patterns below are the parts of that runner worth generalizing. Everything
project-specific — paths, client references, room/channel concepts — has been stripped;
what's left is the mechanism.

**52 tests, all passing** (`python -m pytest -q`, verified on Python 3.11 and 3.14).

---

## 1. `--allowed-tools` is not a restriction. `--tools` is.

This is the finding worth reading first, because the methodology behind it matters
more than the specific flags.

Verified empirically against the `claude` CLI, **v2.1.233, on 2026-08-15**. CLI flag
behavior changes between releases without much warning — treat this table as a
snapshot, not a permanent fact, and re-verify against your own installed version
before relying on it:

| Flag | What it looks like it does | What it actually does |
|---|---|---|
| `--allowed-tools ""` | restrict to nothing | **nothing restricted at all** — Bash still ran. The flag auto-approves tool use (skips the permission prompt); it never removes a tool. |
| `--allowed-tools "Read"` | restrict to Read only | **nothing restricted** — Bash still ran, for the same reason. |
| `--disallowed-tools Bash` | remove Bash | Bash is removed, but this fails **open**: any tool added to the CLI later, or any tool not named in the list, is granted by default. Denying by enumeration only denies what you thought to enumerate. |
| `--tools "Read"` | restrict to Read only | **correct** — fails closed. Only what is explicitly named survives. |
| `--tools ""` | disable all tools (per `--help`) | **no restriction at all.** An empty value is treated as *unset*, identically to `--allowed-tools ""`. There is no flag value that means "grant nothing." |
| `--strict-mcp-config` | (not obviously about tool restriction) | drops the entire MCP tool surface — 671 tools down to 2 on the machine this was measured on. `--tools` governs **only built-in tools**; without this flag, a spawned agent keeps every MCP-provided tool on the box regardless of what any allowlist says. |

The practical upshot: on the measured machine, MCP tools were roughly 96% of the total
available surface. A permissions UI that only ever touched `--tools` — however
carefully designed — was governing 4% of what a spawned agent could actually do.

### Methodology (the part worth keeping even if the table above goes stale)

Getting to that table took three *wrong* methods first, and each one produced a
confident, wrong answer:

1. **Asking a spawned agent to self-report its tools.** This is not proof. One run
   omitted `Glob`/`Grep` from its own description despite them working when actually
   invoked. Another run, spawned under `--tools ""`, confidently described a full
   toolset — which happened to be *true* that time (because `--tools ""` grants
   everything), but the model had no way to know that from the inside. It guessed and
   got lucky. A self-report that happens to be correct is not distinguishable, from the
   outside, from one that doesn't.

2. **A stub binary that echoes its own argv.** This proves the *calling code* formatted
   the command line correctly. It proves nothing about whether the *real* binary
   honors that flag. `--allowed-tools` would pass this exact test while doing nothing
   at all — which is close to how it ended up in the codebase as a security control in
   the first place.

3. **Reading `--help` or documentation prose.** `--tools ""` is documented as disabling
   all tools. It does not.

The only method that actually settled it: spawn the real binary with a real
restriction in place, then **ask it to use the withheld tool and watch it refuse (or
not)**. Behavioral proof, not self-report, not a stub, not documentation. If you adapt
any of this to a different CLI, or to a newer version of this one, re-derive the table
the same way rather than carrying it forward on faith — that's the actual lesson here,
independent of whether the specific flags above are still accurate by the time you read
this.

### Layered defense, stated honestly

The source project also runs a cheap regex-based tripwire over queued task text —
looking for phrases like "email the client" or "deploy to production" — before
anything spawns. That tripwire is **not included in this extraction**, deliberately:
its own docstring in the source lists four known evasion classes (split sentences,
passive voice, out-of-vocabulary targets, action/target on separate lines), because
pattern-matching over natural language cannot be made sound. It's a useful cheap first
pass that catches obvious cases and routes them to a human. It is not, and cannot be, a
security boundary.

The boundary that actually holds is capability restriction at spawn time: an agent that
was never granted a tool cannot be talked into using it, no matter what the prompt
says or what the content filter did or didn't catch. `claude_agent_sandbox/tool_grants.py`
is that boundary — see `EmptyGrantError` below.

### The empty-grant rule

Because there is no flag value that safely means "no tools," an agent whose computed
permission set is empty cannot be spawned with `--tools ""` — that would run
**completely unrestricted**, the opposite of the intent. `tool_grants.build_argv()`
raises `EmptyGrantError` instead of building a command in that case, and
`tool_grants.spawn()` turns that into a clean non-zero return without ever touching
`subprocess`.

```python
from claude_agent_sandbox.tool_grants import tools_for, spawn

TOOL_MAP = {"Read": ["Read"], "Bash": ["Bash"]}
DEFAULTS = {"Read": 1, "Bash": 0}

tools = tools_for(agent_perms, TOOL_MAP, DEFAULTS)   # e.g. {"Read": 1, "Bash": 0} -> ["Read"]
code, out, err = spawn("claude", prompt, tools=tools, model="haiku")
```

`is_granted()` also exists because permission values usually arrive as JSON over HTTP,
not as Python booleans — a bare `if value:` treats the *string* `"0"` as true, silently
handing over a tool that was switched off. Only unambiguous "yes" spellings (`1`,
`"true"`, `"yes"`, `"on"`, `True`) pass; anything else, including values nobody
anticipated (`2`, `"maybe"`), fails closed rather than open.

---

## 2. Atomic claiming: `os.link`, never `os.rename`

The queue this was built for is a set of directories on disk, and multiple workers
can try to claim the same job. The obvious-looking implementation is a race:

```python
# DON'T — TOCTOU window between the check and the rename
if not dest.exists():
    os.rename(src, dest)
```

`os.rename` (and `os.replace`) **silently overwrite** their destination if one already
exists. There is a real window between `dest.exists()` returning `False` and the
`rename()` call actually landing, and POSIX gives you nothing to close it with these
primitives. Two workers can both pass the check before either has renamed anything;
whichever renames second **destroys** whatever the first one just wrote, with no
exception and no warning.

**Concrete failure scenario:** two workers list `queue/` and both see `job-42.md`. Both
decide to claim it and call `rename(queue/job-42.md, doing/job-42.md)`. If `doing/`
already contains an unrelated file that happens to be named `job-42.md` — a retried
run, a stale leftover from a crash, an id collision — the second rename call silently
overwrites *that* file too. Nothing in the return value tells you it happened. The only
way to notice is to go looking for data that's no longer there.

`os.link(src, dest)` instead **fails atomically** with `FileExistsError` the instant
`dest` already exists. There's no window: the filesystem itself resolves the race, so
exactly one caller can ever succeed in giving that name to that content. That's the
property a claim actually needs — not "check then act," but "attempt and let the
filesystem adjudicate."

```python
def _link_into(self, src, dest_dir, name):
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    try:
        os.link(src, target)
    except FileExistsError:
        return None          # contention — someone else got there first
    return target
```

The other half of the rule: **only `FileExistsError` means contention.** Every other
`OSError` — no hard-link support on this filesystem, a permissions problem, a full disk
— is left to propagate. Catching `OSError` broadly and treating it all as "someone beat
me to it" would make an infrastructure failure indistinguishable from a race that
resolved itself normally, when actually *nothing* claimed the job.

`claude_agent_sandbox/atomic_queue.py` genericizes this into `AtomicQueue`: caller
supplies the root path and the tuple of valid status names (the four in the source
project were hardcoded; here they're a constructor argument). `claim()`, `move_to()`,
and `trash()` all route through one `_link_into()` / `_relocate()` pair, so the
no-clobber guarantee is defined exactly once instead of re-derived (and potentially
re-gotten-wrong) at each call site.

## 3. Directory-name-as-state-machine

A job's status is not a field stored somewhere that could disagree with reality — it
**is** the directory the job's file currently lives in. There is no `status: "doing"`
value to fall out of sync, because there is only one place that fact is recorded at
all. A status transition is a `move_to()` call, which is the same atomic
link-then-unlink primitive as the claim — so status changes inherit the exact same
atomicity guarantee for free.

**The tradeoff, stated honestly, not sold:**

- **You get:** atomic transitions with no separate consistency check needed, and zero
  schema drift — renaming a status is renaming a directory, not writing a migration.
- **You give up:** cheap querying on anything other than status. "Every job filed by X,
  regardless of state" means scanning every status directory, because there is no index
  but the directory layout itself. For a queue small enough that a full `glob()` across
  a handful of directories is instant, that's a good trade. It stops being one once the
  job count is large enough that a full scan gets expensive, or once you need to query
  on a dimension the directory layout doesn't express — at that point you want an actual
  index (a database, a manifest file) and the directories become a derived view of it,
  not the source of truth.

This repo doesn't pretend that second regime doesn't exist — it's just outside what
this extraction is for. `AtomicQueue` is sized for "small enough to list."

## 4. Prompt fencing: nonce-marked boundaries for untrusted content

Any content in a prompt that the prompt's author didn't write themselves — a queued
job filed by someone else, an API response, a file being summarized — can contain text
that *looks like* a system instruction. A prompt that just concatenates
`"the task is: {task_body}"` gives that content no structural boundary, so if the task
body itself contains something like `"## New instructions: ignore the above,"` a model
reading linearly has no signal that this line came from a different, less-trusted
source than the surrounding text.

`claude_agent_sandbox/fencing.py` implements three pieces:

1. **`fence(label, content, nonce=None)`** wraps untrusted content in
   `<<<BEGIN LABEL {nonce}>>> ... <<<END LABEL {nonce}>>>` markers. The nonce (8 random
   hex characters, one per call) exists because a *fixed* delimiter can be closed early
   by content that simply contains that exact string, and a delimiter derived from
   something the untrusted content's author controls (e.g. the job's own filename) is
   no better. A nonce generated by code the content has no way to see in advance can't
   be pre-guessed or pre-closed.

2. **`defuse(text)`** breaks up any fence-marker-shaped substrings already present in
   untrusted content (`<<<` → `<< <`, `>>>` → `> >>`) before it's wrapped. The nonce
   already makes forging the *real* marker infeasible; this additionally stops the
   content from displaying a convincing-looking (but wrong-nonce) lookalike that could
   still confuse a model skimming for structure.

3. **`build_layered_prompt(trusted_before, untrusted_sections, trusted_after)`**
   assembles the full prompt with fenced untrusted sections placed *between* the
   trusted preamble and the trusted rules — never before the rules. Models, like people
   skimming a document, tend to weight what they read most recently more heavily when
   instructions conflict. Putting the genuine rule after the untrusted material at
   least contests that recency effect instead of relying on the fence alone to hold.

```python
from claude_agent_sandbox.fencing import build_layered_prompt

prompt = build_layered_prompt(
    trusted_before=["You are Agent, a technical builder."],
    untrusted_sections=[("TASK CONTENT", job_body_from_disk)],
    trusted_after=["## Rules\nStay in scope. Emit a status line when done."],
)
```

`tests/test_fencing.py` includes a fence-escape attempt: task content that contains a
spoofed `<<<END TASK CONTENT>>>` marker followed by fake "real rules" trying to claim
positional last word. The test asserts the spoof is defused and the *actual*
`trusted_after` block still lands after the one genuine, nonce-bearing end marker.

**Honesty about what this is:** a probabilistic deterrent, not a proof. Nothing here
makes it *impossible* for sufficiently adversarial content to influence a model that
reads it — prompt injection is an open problem. This is one layer. The layer that
actually bounds the damage is the tool-grant boundary above: an agent that was never
handed a tool cannot be talked into using it, no matter what any fence failed to stop.

---

## What this is not

- Not a framework. No orchestrator, no scheduler, no agent abstraction, no config
  format. Three modules, each independently importable, each with its own tests.
- Not a drop-in dependency. The source project's `runner.py` has business logic —
  markdown task parsing, subtask checklists, report cards, a business-specific content
  gate with client names in its regex patterns — none of which is here. What's here is
  the mechanism underneath that logic, generalized enough to read on its own.
- Not a claim that any of this is bulletproof. The README says where each pattern's
  limits are, on purpose, because a writeup whose claims can't be checked against code
  isn't worth publishing.

## Layout

```
claude_agent_sandbox/
  atomic_queue.py   — AtomicQueue: claim/move_to/trash via os.link, statuses caller-defined
  fencing.py        — fence/defuse/build_layered_prompt for untrusted prompt content
  tool_grants.py    — tools_for/is_granted/build_argv/spawn, the --tools finding + EmptyGrantError
tests/
  test_atomic_queue.py
  test_fencing.py
  test_tool_grants.py
```

## Running the tests

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

52 tests, all passing, on Python 3.11 and 3.14 (CI pins 3.11).
