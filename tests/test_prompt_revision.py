import pytest

from rca_lab.mcp.answer import SUBMIT_TOOL, submit_tool
from rca_lab.mcp.prompts import SYSTEM_PROMPT, system_prompt
from rca_lab.mcp.student import StudentConfig


def test_revision_one_is_the_original_text():
    assert system_prompt() is SYSTEM_PROMPT and system_prompt(1) is SYSTEM_PROMPT
    assert submit_tool() is SUBMIT_TOOL
    assert StudentConfig(endpoint="http://x", model="m").prompt_revision == 1


def test_revision_two_separates_origins_from_exposing_conditions():
    r2 = system_prompt(2)
    assert "계측 밖 원인(외부 API, 브로커, 네트워크)은 external_causes 로 적고" not in r2
    assert "발원지만 적는다" in r2 and "계기·조건" in r2 and "mechanism 에 쓰고" in r2
    # everything outside the external-cause rule is unchanged
    head, tail = SYSTEM_PROMPT.split("- 계측 밖 원인", 1)
    assert r2.startswith(head) and r2.endswith(tail.split("\n", 1)[1])
    tool = submit_tool(2)
    assert "계기·조건" in tool["description"] and tool["name"] == SUBMIT_TOOL["name"]
    assert tool["inputSchema"]["required"] == SUBMIT_TOOL["inputSchema"]["required"]
    assert "계기" not in SUBMIT_TOOL["description"]                     # the shared revision-1 tool is not mutated


def test_unknown_revision_is_rejected():
    with pytest.raises(ValueError):
        system_prompt(4)
    with pytest.raises(ValueError):
        submit_tool(0)
