from types import SimpleNamespace

import pytest

from gpt import GPTClient, GPTServiceError


def _raise_runtime_error(*args, **kwargs):
    raise RuntimeError("boom")


def test_chat_completion_wraps_errors(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # Ensure optional values do not bleed in from developer machines
    monkeypatch.delenv("OPENAI_ORG", raising=False)

    client = GPTClient()
    client._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=_raise_runtime_error)
        )
    )

    with pytest.raises(GPTServiceError):
        client._chat_completion(messages=[], temperature=0.0, max_tokens=1)
