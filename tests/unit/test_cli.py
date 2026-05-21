"""Smoke tests for the combinator CLI.

These tests exercise the command-line layer using a FakeLLM injected
in-process; they do not invoke the binary as a subprocess.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml
from orchestral.context.message import Message
from orchestral.llm.base.llm import LLM
from orchestral.llm.base.response import Response
from rich.console import Console

from combinator.cli import (
    _handle_command,
    _print_tree,
    _run_one_shot,
)
from combinator.config import load_config_from_mapping
from combinator.engines.orchestral import make_orchestral_engine_factory
from combinator.record import AgentSpec
from combinator.runtime import Runtime


class _FakeLLM(LLM):
    def __init__(self) -> None:
        super().__init__(tools=None)
        self._calls = 0
        self.tools: list = []

    def set_tools(self, tools):
        self.tools = list(tools)

    def get_response(self, context, **kwargs):
        self._calls += 1
        return Response(
            model="fake",
            message=Message(role="assistant", text="ok", tool_calls=None),
        )

    def call_api(self, formatted_input, **kwargs):
        raise NotImplementedError

    def call_streaming_api(self, formatted_input, **kwargs):
        raise NotImplementedError

    def extract_text_from_chunk(self, chunk) -> str:
        return ""

    def process_api_input(self, context):
        return None

    def process_api_response(self, api_response):
        raise NotImplementedError

    def process_streaming_response(self, accumulated_chunks, accumulated_text, final_chunk):
        raise NotImplementedError

    def _convert_tools_to_provider_format(self):
        return []


def _captured_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)


def _runtime_with_fake_llm():
    factory = make_orchestral_engine_factory(llms={"default": _FakeLLM()})
    return Runtime(engine_factory=factory)


def test_repl_tree_command_emits_tree():
    rt = _runtime_with_fake_llm()
    root = rt.root(AgentSpec(role_prompt="r", tools=["primitive"], label="iota"))
    console = _captured_console()
    _print_tree(rt, root, console=console)
    out = console.file.getvalue()
    assert "iota" in out
    assert root.id in out
    rt.shutdown()


def test_repl_help_command_does_not_signal_exit():
    rt = _runtime_with_fake_llm()
    root = rt.root(AgentSpec(role_prompt="r", tools=["primitive"]))
    console = _captured_console()
    should_exit = _handle_command(":help", console=console, runtime=rt, root=root)
    assert should_exit is False
    assert "commands" in console.file.getvalue().lower()
    rt.shutdown()


def test_repl_quit_command_signals_exit():
    rt = _runtime_with_fake_llm()
    root = rt.root(AgentSpec(role_prompt="r", tools=["primitive"]))
    console = _captured_console()
    assert _handle_command(":quit", console=console, runtime=rt, root=root) is True
    rt.shutdown()


def test_repl_inbox_unknown_addr():
    rt = _runtime_with_fake_llm()
    root = rt.root(AgentSpec(role_prompt="r", tools=["primitive"]))
    console = _captured_console()
    _handle_command(":inbox ag-bogus", console=console, runtime=rt, root=root)
    assert "no agent" in console.file.getvalue()
    rt.shutdown()


def test_repl_send_json_body_parses():
    rt = _runtime_with_fake_llm()
    root = rt.root(AgentSpec(role_prompt="r", tools=["primitive"]))
    console = _captured_console()
    _handle_command(
        f':send {root.id} {{"task": "hi"}}',
        console=console, runtime=rt, root=root,
    )
    inbox = rt.read_inbox(root)
    assert any(env.body == {"task": "hi"} for env in inbox)
    rt.shutdown()


def test_repl_send_falls_back_to_raw_string():
    rt = _runtime_with_fake_llm()
    root = rt.root(AgentSpec(role_prompt="r", tools=["primitive"]))
    console = _captured_console()
    _handle_command(
        f":send {root.id} plain text",
        console=console, runtime=rt, root=root,
    )
    inbox = rt.read_inbox(root)
    assert any(env.body == "plain text" for env in inbox)
    rt.shutdown()


def test_one_shot_mode_runs_and_exits():
    rt = _runtime_with_fake_llm()
    root = rt.root(AgentSpec(role_prompt="r", tools=["primitive"], label="iota"))
    console = _captured_console()
    rc = _run_one_shot(console, rt, root, task="do work")
    assert rc == 0


def test_config_loader_path_round_trip(tmp_path: Path):
    config_data = {
        "llms": {"default": {"provider": "anthropic"}},
        "root": {"role_prompt": "you are iota"},
        "mode": "repl",
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    cfg = load_config_from_mapping(config_data)
    assert cfg.root.label == "root"
