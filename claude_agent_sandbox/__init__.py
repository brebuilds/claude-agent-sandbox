"""claude_agent_sandbox — three extracted safety patterns for filesystem-queued LLM agents.

- atomic_queue: atomic job claiming (os.link, not os.rename) over a
  directory-name-as-state-machine job queue.
- fencing: nonce-fenced markers for untrusted content in prompts, plus positional
  last-word ordering for trusted instructions.
- tool_grants: translating stored permission switches into an enforceable tool
  allowlist, and refusing to spawn on an empty grant.

See the top-level README for where these came from and what they are (and are not).
"""

from . import atomic_queue, fencing, tool_grants  # noqa: F401

__all__ = ["atomic_queue", "fencing", "tool_grants"]
