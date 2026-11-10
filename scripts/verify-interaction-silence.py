#!/usr/bin/env python3
"""Exercise interaction suppression without a model call or an interactive question.

Usage: python3 scripts/verify-interaction-silence.py /absolute/path/to/AskHuman [--codex /path/to/codex]
All configuration and session markers live in a temporary home. The first assertion uses
invalid ask arguments, so an unfixed binary cannot accidentally open a popup.
"""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time


DISABLED = "AskHuman interaction is disabled"


def queued_lines(stream):
    lines = queue.Queue()

    def read():
        for line in stream:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    return lines


class Mcp:
    def __init__(self, binary, env):
        self.process = subprocess.Popen(
            [binary, "mcp"], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        self.lines = queued_lines(self.process.stdout)
        self.counter = 0
        try:
            self.request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "interaction-regression", "version": "0"},
            })
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.counter += 1
        target = self.counter
        self.send({"jsonrpc": "2.0", "id": target, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if not line:
                raise RuntimeError("MCP process exited")
            value = json.loads(line)
            if value.get("id") == target:
                return value
        raise TimeoutError(f"MCP {method} timed out")

    def call(self, tool, args, meta=None):
        params = {"name": tool, "arguments": args}
        if meta is not None:
            params["_meta"] = meta
        return self.request("tools/call", params)

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def assert_disabled(response):
    result = response.get("result", {})
    assert result.get("isError") is True and DISABLED in json.dumps(result), response


def verify_native_hook(codex_binary, binary, root, home, env):
    """Run the host's real Codex with only a closed local inference endpoint configured."""
    codex_home = root / ".codex"
    command = f"{shlex.quote(binary)} __context-recovery-hook codex session-start"
    handler = {"type": "command", "command": command, "timeout": 5, "async": False, "additionalContextLimit": 0}
    identity = {"event_name": "session_start", "hooks": [handler]}
    trust = "sha256:" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    hooks = codex_home / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [handler]}]}}))
    (codex_home / "config.toml").write_text(
        'model = "offline-probe"\nmodel_provider = "offline_probe"\n'
        '[model_providers.offline_probe]\nname = "Offline probe"\n'
        'base_url = "http://127.0.0.1:9"\nwire_api = "responses"\n'
        'requires_openai_auth = false\nrequest_max_retries = 0\nstream_max_retries = 0\n'
        '[mcp_servers.askhuman]\ncommand = ' + json.dumps(binary) + '\nargs = ["mcp"]\n'
        '[mcp_servers.askhuman.env]\nASKHUMAN_HOME = ' + json.dumps(str(home)) + '\n'
        '[hooks.state.' + json.dumps(str(hooks) + ':session_start:0:0') + ']\ntrusted_hash = ' + json.dumps(trust) + '\n'
    )
    native_env = dict(env, LODY_SESSION_ID="native-child", LODY_OPENED_BY_SESSION_ID="native-parent")
    with (root / "native-stderr.log").open("w") as log:
        process = subprocess.Popen([codex_binary, "app-server"], cwd=root, env=native_env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
        lines = queued_lines(process.stdout)

        def send(value):
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        def response(target):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if not line:
                    raise RuntimeError("native Codex exited")
                value = json.loads(line)
                if value.get("id") == target:
                    return value
            raise TimeoutError("native Codex response timed out")

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "acp-extension-codex", "version": "interaction-regression"},
                "capabilities": {"experimentalApi": True}}})
            assert "result" in response(1)
            send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": {
                "cwd": str(root), "model": "offline-probe", "approvalPolicy": "never", "sandbox": "read-only"}})
            started = response(2)
            session = started["result"]["thread"]["id"]
            marker = home / "state" / "codex-delegated" / f"{session}.json"
            send({"jsonrpc": "2.0", "id": 3, "method": "turn/start", "params": {
                "threadId": session, "input": [{"type": "text", "text": "offline hook regression"}]}})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not marker.exists():
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not line:
                    break
            assert marker.exists(), (
                "real native SessionStart did not persist the suppression verdict\n"
                + (root / "native-stderr.log").read_text()[-4000:]
            )
            assert json.loads(marker.read_text())["signal"] == "external_interaction_policy"
            for rollout in (codex_home / "sessions").rglob("*.jsonl"):
                for line in rollout.read_text().splitlines():
                    item = json.loads(line)
                    payload = item.get("payload", {})
                    if item.get("type") == "response_item" and payload.get("role") == "developer":
                        assert "askhuman-protocol:" not in json.dumps(payload), "silent thread received the interaction protocol"
            filtered = Mcp(binary, env)
            try:
                assert_disabled(filtered.call("ask", {}, {"threadId": session}))
            finally:
                filtered.close()
            print("PASS: real Lody Codex app-server → trusted SessionStart → native session marker → filtered MCP")
            print("Native inference endpoint: http://127.0.0.1:9 only; no model service or credentials")
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    binary = str(Path(sys.argv[1]).resolve())
    # Keep macOS's Unix socket pathname below sockaddr_un.sun_path's 104-byte limit.
    with tempfile.TemporaryDirectory(prefix="ah-int-") as temporary, ExitStack() as cleanup:
        root = Path(temporary).resolve()
        home = root / "askhuman"
        home.mkdir()
        codex = root / ".codex"
        codex.mkdir()
        # Presence of an MCP entry is enough to identify integration mode; no installation needed.
        (codex / "config.toml").write_text('[mcp_servers.askhuman]\ncommand = "AskHuman"\n')
        (home / "config.json").write_text(json.dumps({
            "interaction": {"suppressWhen": [{"env": "LODY_OPENED_BY_SESSION_ID"}]},
            "general": {"menuBarIcon": "off", "popupPrewarm": False},
            "channels": {"popup": {"enabled": False}},
        }))
        # External suppression must win even when ordinary delegated runs are allowed.
        (home / "delegated-preferences.json").write_text('{"codex":true}')
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "TZ"}}
        env.update(HOME=str(root), ASKHUMAN_HOME=str(home), CODEX_HOME=str(codex))

        def run(args, current_env, data=""):
            return subprocess.run([binary, *args], env=current_env, input=data,
                                  capture_output=True, text=True, timeout=10)

        # Always stop this fixture before TemporaryDirectory removes its socket and config.
        cleanup.callback(lambda: run(["daemon", "stop"], env))
        child_env = dict(env, LODY_SESSION_ID="child", LODY_OPENED_BY_SESSION_ID="parent")
        child = Mcp(binary, child_env)
        try:
            assert_disabled(child.call("ask", {}))
            print("PASS: Lody child is rejected before question validation")
            for tool, arguments in [("whats_next", {}), ("todo_add", {"text": " "}),
                                    ("todo_update", {"todoId": "missing"})]:
                assert_disabled(child.call(tool, arguments))
            for tool in ["show_last", "todo_list"]:
                assert DISABLED not in json.dumps(child.call(tool, {})), tool
            listed = child.request("tools/list", {})
            assert {tool["name"] for tool in listed["result"]["tools"]} >= {"show_last", "todo_list"}, listed
        finally:
            child.close()

        session = "interaction-regression-child"
        payload = {"session_id": session, "source": "startup", "transcript_path": "/missing/rollout.jsonl", "cwd": temporary}
        hook = run(["__context-recovery-hook", "codex", "session-start"], child_env, json.dumps(payload))
        assert hook.returncode == 0 and not hook.stdout.strip(), hook
        marker = home / "state" / "codex-delegated" / f"{session}.json"
        assert marker.exists(), "SessionStart must persist the verdict for environment-filtered MCP"
        filtered = Mcp(binary, env)
        try:
            # The same already-running server observes marker changes on every call.
            for metadata in [{"threadId": session}, {"x-codex-turn-metadata": {"session_id": session}}]:
                assert_disabled(filtered.call("ask", {}, metadata))
            assert DISABLED not in json.dumps(filtered.call("show_last", {}, {"threadId": session}))
            native_env = dict(env, CODEX_THREAD_ID=session)
            direct = run(["--stdin"], native_env, "{}")
            assert direct.returncode == 1 and DISABLED in direct.stderr, direct
            for command in [["--whats-next"], ["todo", "clear", "--yes"]]:
                direct = run(command, native_env)
                assert direct.returncode == 1 and DISABLED in direct.stderr, direct
            added = run(["todo", "add", "--auto", "private regression task"], env)
            assert added.returncode == 0, added
            todo_file = home / "state" / "todos.json"
            before = todo_file.read_bytes()
            direct = run(["--whats-next"], native_env)
            assert direct.returncode == 1 and DISABLED in direct.stderr, direct
            assert todo_file.read_bytes() == before, "disabled whats_next must not dequeue automatic todos"
            for command, hook_payload in [
                (["__stop-hook", "codex", "track", "confirm"], dict(payload, hook_event_name="Stop", last_assistant_message="private regression")),
                (["__permission-hook", "codex"], dict(payload, hook_event_name="PermissionRequest", permission_mode="default",
                                                       tool_name="exec_command", tool_input={"cmd": "echo private-regression"})),
            ]:
                result = run(command, child_env, json.dumps(hook_payload))
                assert result.returncode == 0 and result.stdout.strip() in ("", "{}"), result
            audit = (home / "daemon.log").read_text()
            assert "stop_confirmation" in audit and "permission_hook" in audit, "valid hooks must reach their suppression guards"
            for source in ["compact", "resume"]:
                payload["source"] = source
                assert not run(["__context-recovery-hook", "codex", "session-start"], child_env, json.dumps(payload)).stdout.strip()
                resumed = run(["__context-recovery-hook", "codex", "session-start"], env, json.dumps(payload))
                assert "additionalContext" in resumed.stdout and not marker.exists(), resumed
                passed = filtered.call("ask", {}, {"threadId": session})
                assert passed.get("error", {}).get("code") == -32602, passed
            print("PASS: hook → session marker → filtered MCP/CLI; native hooks fall through; takeover clears the marker")
        finally:
            filtered.close()

        main_session = Mcp(binary, dict(env, LODY_SESSION_ID="main"))
        try:
            assert main_session.call("ask", {}).get("error", {}).get("code") == -32602
        finally:
            main_session.close()
        explicit = Mcp(binary, dict(env, ASKHUMAN_INTERACTION="off"))
        try:
            assert_disabled(explicit.call("ask", {}))
            listed = explicit.request("tools/list", {})
            assert {tool["name"] for tool in listed["result"]["tools"]} >= {"show_last", "todo_list"}, listed
        finally:
            explicit.close()
        for command in [["--stdin"], ["--whats-next"], ["todo", "add", "blocked"]]:
            direct = run(command, dict(env, ASKHUMAN_INTERACTION="off"))
            assert direct.returncode == 1 and DISABLED in direct.stderr, direct
        for rules, variables, expected in [
            ([{"env": "ROLE", "equals": "worker"}], {"ROLE": "worker"}, True),
            ([{"env": "ROLE", "equals": "worker"}], {"ROLE": "Worker"}, False),
            ([{"env": "ROLE"}], {"ROLE": ""}, False),
        ]:
            configured = run(["config", "set", "interaction.suppressWhen", json.dumps(rules)], env)
            assert configured.returncode == 0, configured
            server = Mcp(binary, dict(env, **variables))
            try:
                response = server.call("ask", {})
                if expected:
                    assert_disabled(response)
                else:
                    assert response.get("error", {}).get("code") == -32602, response
            finally:
                server.close()
        invalid = run(["config", "set", "interaction.suppressWhen", '[{"env":""}]'], env)
        assert invalid.returncode != 0, invalid
        if len(sys.argv) == 4 and sys.argv[2] == "--codex":
            configured = run(["config", "set", "interaction.suppressWhen", '[{"env":"LODY_OPENED_BY_SESSION_ID"}]'], env)
            assert configured.returncode == 0, configured
            verify_native_hook(str(Path(sys.argv[3]).resolve()), binary, root, home, env)
        print("PASS: main Lody sessions remain interactive; explicit off works without Codex metadata")
        print("PASS: config CLI round-trip, exact equality, empty-value rules, and invalid-rule rejection")


if __name__ == "__main__":
    main()
