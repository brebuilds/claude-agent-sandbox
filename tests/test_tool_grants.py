import pytest

from claude_agent_sandbox.tool_grants import (
    EmptyGrantError,
    build_argv,
    is_granted,
    spawn,
    tools_for,
)

TOOL_MAP = {
    "Read": ["Read"],
    "Search": ["Glob", "Grep"],
    "Write": ["Write", "Edit"],
    "Bash": ["Bash"],
    "Deploy": [],
}
DEFAULTS = {"Read": 1, "Search": 0, "Write": 1, "Bash": 0, "Deploy": 0}


def test_tools_for_with_no_perms_uses_defaults():
    assert tools_for(None, TOOL_MAP, DEFAULTS) == ["Edit", "Read", "Write"]
    assert "Bash" not in tools_for(None, TOOL_MAP, DEFAULTS)


def test_tools_for_maps_each_switch_to_its_tool_names():
    perms = {"Read": 1, "Write": 0, "Bash": 1, "Deploy": 1}
    tools = tools_for(perms, TOOL_MAP, DEFAULTS)
    assert tools == ["Bash", "Read"]
    assert "Write" not in tools and "Edit" not in tools
    # Deploy maps to no concrete tool names — granting it adds nothing.


@pytest.mark.parametrize("withheld", [0, "0", False, "false", "no", "off", "", None, "maybe", 2])
def test_a_withheld_permission_never_grants_its_tool(withheld):
    """`if granted:` treats the STRING "0" as true — this is the regression that
    finding guards against. Anything not unambiguously a yes-spelling is a no,
    including values nobody anticipated (2, "maybe"), which fail closed."""
    assert "Bash" not in tools_for({"Read": 1, "Bash": withheld}, TOOL_MAP, DEFAULTS)


@pytest.mark.parametrize("granted", [1, "1", True, "true", "TRUE", "yes", "on"])
def test_a_granted_permission_is_honoured_in_every_spelling(granted):
    assert "Bash" in tools_for({"Bash": granted}, TOOL_MAP, DEFAULTS)


def test_is_granted_rejects_unrecognized_values():
    for v in (2, "maybe", None, [], {}):
        assert is_granted(v) is False


def test_is_granted_accepts_bool_true_and_int_one():
    assert is_granted(True) is True
    assert is_granted(1) is True
    assert is_granted(1.0) is True


def test_build_argv_refuses_an_empty_tool_grant():
    with pytest.raises(EmptyGrantError):
        build_argv("claude", [], "do the thing")


def test_build_argv_uses_tools_flag_not_allowed_tools():
    """--tools is the allowlist that actually restricts. --allowed-tools must never
    appear: it auto-approves rather than restricting (see module docstring finding),
    so its presence would silently turn the wall back into a door that opens itself.
    """
    argv = build_argv("claude", ["Read", "Bash"], "do the thing", model="haiku")
    assert argv == ["claude", "--print", "--strict-mcp-config", "--model", "haiku",
                    "--tools", "Bash,Read", "--", "do the thing"]
    assert "--allowed-tools" not in argv


def test_build_argv_always_includes_strict_mcp_config():
    """Without this flag, --tools governs only built-ins and every MCP-provided tool
    on the machine remains reachable regardless of the allowlist (see finding)."""
    argv = build_argv("claude", ["Read"], "x")
    assert "--strict-mcp-config" in argv


def test_build_argv_without_model_omits_the_flag():
    argv = build_argv("claude", ["Read"], "x")
    assert "--model" not in argv


def test_spawn_refuses_an_empty_grant_without_touching_the_subprocess(tmp_path):
    code, out, err = spawn(str(tmp_path / "nonexistent-binary"), "do the thing",
                           tools=[], timeout_s=5)
    assert code != 0
    assert out == ""
    assert "refusing" in err.lower()


def test_spawn_invokes_the_binary_and_passes_the_model(tmp_path):
    stub = tmp_path / "claude"
    stub.write_text(
        '#!/bin/sh\necho "ARGS: $@"\necho \'{"status":"done","summary":"stub ran"}\'\n',
        encoding="utf-8")
    stub.chmod(0o755)
    code, out, err = spawn(str(stub), "do the thing", tools=["Read"], model="haiku",
                           timeout_s=10, cwd=str(tmp_path))
    assert code == 0
    assert "--model haiku" in out
    assert "--tools Read" in out
    assert "stub ran" in out


def test_spawn_reports_a_missing_binary_instead_of_raising(tmp_path):
    code, out, err = spawn(str(tmp_path / "nope"), "x", tools=["Read"], timeout_s=5)
    assert code != 0
    assert "not found" in err.lower()
