from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    """Raised when the underlying LLM call fails (timeout, bad exit, bad JSON)."""


@dataclass
class LLMCall:
    prompt: str
    web: bool = False


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, prompt: str, *, web: bool = False, timeout: int = 120) -> str:
        ...


# Web tools the installed claude CLI is allowed to use when web=True. Kept in one
# place so it is easy to adjust as the CLI's tool names change.
_WEB_TOOLS = ["WebSearch", "WebFetch"]


class ClaudeCliClient:
    """Headless LLM client that shells out to `claude -p` and parses the JSON
    result. Reuses the local Claude Code auth, so it runs unattended."""

    def __init__(self, binary: str = "claude"):
        self.binary = binary

    def complete(self, prompt: str, *, web: bool = False, timeout: int = 120) -> str:
        cmd = [self.binary, "-p", prompt, "--output-format", "json"]
        if web:
            cmd += ["--allowedTools", ",".join(_WEB_TOOLS)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude -p timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise LLMError(f"claude -p exited {proc.returncode}: {proc.stderr.strip()}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude -p returned non-JSON output: {exc}") from exc
        result = payload.get("result")
        if result is None:
            raise LLMError("claude -p JSON had no 'result' field")
        return result


class FakeLLMClient:
    """Deterministic test double. Pass a list to return responses in order, or
    a dict to match the first key found as a substring of the prompt. Records
    every call for assertions."""

    def __init__(self, responses: dict | list):
        self.responses = responses
        self.calls: list[LLMCall] = []

    def complete(self, prompt: str, *, web: bool = False, timeout: int = 120) -> str:
        self.calls.append(LLMCall(prompt=prompt, web=web))
        if isinstance(self.responses, list):
            if not self.responses:
                raise LLMError("FakeLLMClient ran out of queued responses")
            return self.responses.pop(0)
        for key, value in self.responses.items():
            if key in prompt:
                return value
        raise LLMError(f"FakeLLMClient had no response matching prompt: {prompt!r}")
