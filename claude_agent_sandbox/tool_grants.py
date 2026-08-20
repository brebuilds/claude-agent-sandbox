"""tool_grants.py — turning stored on/off permission switches into an enforceable
tool allowlist for a spawned coding-agent CLI, and refusing to spawn on an empty
grant instead of silently running unrestricted.

======================================================================================
FINDING: --allowed-tools is not a restriction. --tools is.
======================================================================================
Verified EMPIRICALLY against the `claude` CLI, v2.1.233, on 2026-08-15. CLI flag
behavior can and does change between releases — re-verify against your own installed
version before relying on any of this; do not trust it on the strength of the flag
names alone (that's the whole lesson below).

    --allowed-tools ""       -> Bash STILL RAN. This flag AUTO-APPROVES tool use
    --allowed-tools "Read"   -> Bash STILL RAN.    (skips the permission prompt) —
                                                    it never actually removes a tool.
    --disallowed-tools Bash  -> Bash removed. But denying by ENUMERATION fails OPEN:
                                any tool added to the CLI after this list was written
                                is granted by default, because it was never named.
    --tools "Read"           -> Bash removed. Fail-CLOSED: only what is explicitly
                                named survives.
    --tools ""                -> NO RESTRICTION AT ALL, despite --help documenting it
                                as disabling all tools. An empty value here is treated
                                as UNSET, exactly like an empty --allowed-tools. There
                                is no flag value that means "grant nothing" — which is
                                why an empty permission set below refuses to spawn
                                rather than passing "".
    --strict-mcp-config       -> drops the whole MCP tool surface (671 tools -> 2 on
                                the box this was measured on). --tools governs ONLY
                                the built-in tools; without this flag a spawned agent
                                keeps every MCP-provided tool on the machine —
                                external deploy tools, e-commerce/CRM integrations,
                                data-deletion actions, messaging integrations —
                                regardless of what any permissions UI says. On the
                                box this was measured on, MCP tools were ~96% of the
                                available surface, so omitting this flag left nearly
                                all of it open no matter how the built-in allowlist
                                was configured.

--------------------------------------------------------------------------------------
METHODOLOGY — this is the more valuable part.
--------------------------------------------------------------------------------------
Getting to the table above took three wrong methods first, and each wrong method
produced a *confident, wrong* answer:

  1. Asking a spawned agent to LIST which tools it had access to. This under- and
     over-reported: one run omitted Glob/Grep from its self-report despite them
     working when actually invoked; another run, spawned under `--tools ""`,
     confidently described a full toolset it did not have restricted at all (which
     was in fact correct that time, but only because `--tools ""` turns out to grant
     everything — the model had no way to know that from the inside, it just guessed
     and happened to be right).

  2. A stub binary that echoes its own argv. This proves the CALLING CODE formatted
     the command line correctly. It proves nothing about whether the REAL binary
     honors that flag. `--allowed-tools` would pass this exact test while doing
     nothing at all — which is precisely how it passed an earlier code review as a
     working security control.

  3. Reading `--help` output or documentation prose. `--tools ""` is documented as
     disabling all tools. It does not.

The only method that actually settled it: spawn the real binary with a real
restriction in place, then ASK IT TO USE the withheld tool and watch it either refuse
or succeed. Behavioral proof, not self-report, not a stub, not documentation. If you
adapt this pattern to a different CLI or a new version of this one, re-derive the
table the same way — don't carry this table forward on faith.

--------------------------------------------------------------------------------------
Layered defense, stated honestly.
--------------------------------------------------------------------------------------
A companion regex-based content filter (not included in this extraction — see the
README) that scans task text for phrases like "email the client" or "deploy to
production" is a useful cheap tripwire, but it is exactly that: a tripwire, not a
wall. Pattern-matching over natural language cannot be made sound — split sentences,
passive voice, and out-of-vocabulary targets all evade it, and no amount of
additional regex patterns closes that gap completely. The boundary that actually
holds is capability restriction at spawn time: an agent that was never granted a tool
cannot be talked into using it by anything the prompt says, however the filter did or
didn't catch it. Use a content filter as a cheap first pass to catch the obvious
cases early and route them to a human — not as the actual security boundary.
"""

from __future__ import annotations

import subprocess
from typing import Dict, Iterable, List, Optional

TRUE_STRINGS = {"1", "true", "yes", "on"}

# The base flags every spawn should carry: --strict-mcp-config is what drops the MCP
# surface (see the finding above) — it is not optional hardening, it is the
# difference between the tool allowlist meaning something and meaning almost nothing.
DEFAULT_BASE_FLAGS = ("--print", "--strict-mcp-config")


def is_granted(value) -> bool:
    """True only for values that unambiguously mean yes. Everything else fails closed.

    Permission values typically arrive over HTTP/JSON from some UI, so nothing
    guarantees they're a Python bool. A bare `if value:` treats the STRING "0" as
    true and silently hands over a tool that was switched off. An unrecognized value
    (2, "maybe", None) is a NO, not a maybe — fail closed on the unexpected, not open.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in TRUE_STRINGS
    if isinstance(value, (int, float)):
        return value == 1
    return False


def tools_for(perms: Optional[Dict], tool_map: Dict[str, List[str]],
              defaults: Dict) -> List[str]:
    """Expand a {switch_name: value} permission dict into a sorted, de-duplicated
    list of concrete CLI tool names, via `tool_map` ({switch_name: [tool names...]}).

    Falls back to `defaults` when `perms` is missing/empty, so a record created
    before some switch existed behaves identically to one created after — an absent
    key should not silently mean "everything", and it should not silently mean
    "nothing" either; it should mean whatever the caller's stated defaults are.
    """
    perms = perms or defaults
    out: List[str] = []
    for name, granted in perms.items():
        if is_granted(granted):
            out.extend(tool_map.get(name, []))
    return sorted(set(out))


class EmptyGrantError(RuntimeError):
    """Raised when asked to build or run a spawn with zero granted tools.

    There is no CLI flag value that means "no tools" (see the finding above —
    `--tools ""` is treated as unset, not as an empty grant). So the only way to
    honor an empty permission set is to refuse to spawn at all; anything else would
    silently run the agent completely unrestricted, which is the exact opposite of
    what an empty permission set is supposed to mean.
    """


def build_argv(binary: str, tools: Iterable[str], prompt: str,
               model: Optional[str] = None,
               base_flags: Iterable[str] = DEFAULT_BASE_FLAGS) -> List[str]:
    """Build the argv for a fail-closed spawn of `binary`.

    `--tools` is passed as an ALLOWLIST, not `--allowed-tools` — see the finding
    above for why the latter would not restrict anything at all.

    Raises EmptyGrantError instead of building a command when `tools` is empty.
    """
    tools = sorted(set(tools or []))
    if not tools:
        raise EmptyGrantError(
            "refusing to build a spawn command with an empty tool grant: an empty "
            "--tools value is treated as UNSET by the CLI (verified against "
            "v2.1.233), so this would run completely unrestricted rather than with "
            "no tools at all"
        )
    argv = [binary, *base_flags]
    if model:
        argv += ["--model", model]
    argv += ["--tools", ",".join(tools)]
    argv += ["--", prompt]
    return argv


def spawn(binary: str, prompt: str, tools: Iterable[str], model: Optional[str] = None,
          timeout_s: int = 900, cwd: Optional[str] = None):
    """Run one prompt through `binary` with a fail-closed tool grant.

    Returns (returncode, stdout, stderr). An empty `tools` grant returns a
    synthetic non-zero result (-3, "", <reason>) without ever invoking the
    subprocess — see EmptyGrantError. A missing binary returns (-2, "", <reason>)
    rather than raising, so a caller iterating over many jobs can keep going.
    """
    try:
        argv = build_argv(binary, tools, prompt, model=model)
    except EmptyGrantError as e:
        return -3, "", str(e)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                           stdin=subprocess.DEVNULL, cwd=cwd)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired as e:
        return -1, (e.stdout or ""), f"timeout after {timeout_s}s"
    except FileNotFoundError:
        return -2, "", f"binary not found at {binary}"
