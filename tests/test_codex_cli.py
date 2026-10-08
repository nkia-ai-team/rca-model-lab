import subprocess

import pytest

from rca_lab.mcp import codex_cli


class FakeProcess:
    def __init__(self, *, returncode=0, stdout="", stderr="", timeouts=0):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timeouts = timeouts
        self.pid = 12345
        self.received = None
        self.communicate_calls = 0

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        if self.communicate_calls <= self.timeouts:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)
        self.received = input
        return self.stdout, self.stderr


class NamedFile:
    def __init__(self, name):
        self.name = str(name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def close(self):
        pass


def patch_temp_files(monkeypatch, tmp_path, final_file):
    monkeypatch.setattr(codex_cli.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "empty-cwd"))
    monkeypatch.setattr(
        codex_cli.tempfile,
        "NamedTemporaryFile",
        lambda prefix, delete: NamedFile(final_file),
    )
    monkeypatch.setattr(codex_cli.shutil, "rmtree", lambda *args, **kwargs: None)


def test_success_returns_final_file_only(monkeypatch, tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("REAL FINAL\n", encoding="utf-8")
    process = FakeProcess(stdout='{"type":"item.completed","item":{"text":"log text"}}\n')
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        command[command.index("-o") + 1] = str(final_file)
        return process

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)

    text, diagnostic = codex_cli.run_codex(
        system_prompt="system", prompt="prompt", model="gpt-6-astra", timeout_seconds=5
    )

    assert (text, diagnostic) == ("REAL FINAL", "")
    command = captured["command"]
    assert command[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert command[command.index("-s") + 1] == "read-only"
    assert command[command.index("-m") + 1] == "gpt-6-astra"
    assert command[-1] == "-"
    assert 'shell_environment_policy.inherit="none"' in command
    assert "project_doc_max_bytes=0" in command
    assert 'web_search="disabled"' in command
    assert "features.shell_tool=false" in command
    assert "features.unified_exec=false" in command
    assert "features.multi_agent=false" in command
    assert "features.apps=false" in command
    assert "features.plugins=false" in command
    assert captured["kwargs"]["start_new_session"] is True
    assert process.received == "system\n\nprompt"


def test_command_env_drops_omx_but_keeps_auth(monkeypatch):
    monkeypatch.setattr(
        codex_cli.os,
        "environ",
        {
            "PATH": "/bin",
            "HOME": "/home/u",
            "CODEX_HOME": "/home/u/.codex",
            "OPENAI_API_KEY": "secret",
            "OMX_HOOK": "drop",
            "CLAUDE_CONFIG_DIR": "drop",
        },
    )

    env = codex_cli._codex_env()

    assert env["PATH"] == "/bin"
    assert env["HOME"] == "/home/u"
    assert env["CODEX_HOME"] == "/home/u/.codex"
    assert env["OPENAI_API_KEY"] == "secret"
    assert "OMX_HOOK" not in env
    assert "CLAUDE_CONFIG_DIR" not in env


@pytest.mark.parametrize(
    "stdout,stderr",
    [
        ("You've hit your weekly limit · resets Oct 10, 3pm (UTC)", ""),
        ('{"type":"error","message":"Rate limit exceeded"}', ""),
        ("", "You've hit your usage limit · resets 5:20am (UTC)"),
    ],
)
def test_quota_notice_raises(monkeypatch, tmp_path, stdout, stderr):
    final_file = tmp_path / "final.txt"
    final_file.write_text("", encoding="utf-8")

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(stdout=stdout, stderr=stderr)

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)

    with pytest.raises(codex_cli.SessionLimitError):
        codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5)


def test_nonzero_returns_diagnostic(monkeypatch, tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("", encoding="utf-8")

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(returncode=2, stdout='{"type":"error","message":"bad"}\n', stderr="stderr")

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)

    text, diagnostic = codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5)

    assert text is None
    assert "exit 2" in diagnostic
    assert "bad" in diagnostic


def test_json_error_event_fails_even_with_zero_returncode(monkeypatch, tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("do not use this", encoding="utf-8")

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(stdout='{"type":"error","message":"backend unavailable"}\n')

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)

    text, diagnostic = codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5)

    assert text is None
    assert "backend unavailable" in diagnostic


def test_nested_turn_failed_error_is_reported(monkeypatch, tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("do not use this", encoding="utf-8")

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(stdout='{"type":"turn.failed","error":{"message":"nested failure"}}\n')

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)

    text, diagnostic = codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5)

    assert text is None
    assert "nested failure" in diagnostic


def test_empty_success_is_failed_call(monkeypatch, tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("   ", encoding="utf-8")

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)

    text, diagnostic = codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5)

    assert text is None
    assert "empty final output" in diagnostic


def test_tool_event_fails_closed(monkeypatch, tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("do not use this", encoding="utf-8")

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(stdout='{"type":"item.completed","item":{"type":"tool_call"}}\n')

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)

    text, diagnostic = codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5)

    assert text is None
    assert "unexpected codex tool event" in diagnostic


@pytest.mark.parametrize("item_type", ["command_execution", "file_change", "web_search"])
def test_non_tool_named_side_effect_events_fail_closed(monkeypatch, tmp_path, item_type):
    final_file = tmp_path / "final.txt"
    final_file.write_text("do not use this", encoding="utf-8")

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(stdout=f'{{"type":"item.completed","item":{{"type":"{item_type}"}}}}\n')

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)

    text, diagnostic = codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5)

    assert text is None
    assert "unexpected codex tool event" in diagnostic


def test_quota_words_in_success_item_text_do_not_raise(monkeypatch, tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("REAL FINAL", encoding="utf-8")

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(
            stdout='{"type":"item.completed","item":{"type":"agent_message","text":"Rate limit exceeded caused the incident"}}\n'
        )

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)

    assert codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5) == (
        "REAL FINAL",
        "",
    )


def test_timeout_kills_process_group(monkeypatch, tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("", encoding="utf-8")
    killed = {}

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(timeouts=1)

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)
    monkeypatch.setattr(codex_cli.os, "killpg", lambda pid, sig: killed.update({"pid": pid, "sig": sig}))

    text, diagnostic = codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5)

    assert text is None
    assert "timeout after 5s" in diagnostic
    assert killed == {"pid": 12345, "sig": codex_cli.signal.SIGTERM}


def test_timeout_escalates_to_sigkill_when_reap_hangs(monkeypatch, tmp_path):
    final_file = tmp_path / "final.txt"
    final_file.write_text("", encoding="utf-8")
    killed = []

    def fake_popen(command, **kwargs):
        command[command.index("-o") + 1] = str(final_file)
        return FakeProcess(timeouts=3)

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)
    patch_temp_files(monkeypatch, tmp_path, final_file)
    monkeypatch.setattr(codex_cli.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    text, diagnostic = codex_cli.run_codex(system_prompt="s", prompt="p", model="m", timeout_seconds=5)

    assert text is None
    assert "timeout after 5s" in diagnostic
    assert killed == [(12345, codex_cli.signal.SIGTERM), (12345, codex_cli.signal.SIGKILL)]
