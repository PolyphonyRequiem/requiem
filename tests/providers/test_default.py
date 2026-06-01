"""Tests for `requiem.providers.default_provider`."""
from __future__ import annotations

import pytest

from requiem.providers import (
    AnthropicProvider,
    OpenAIProvider,
    default_provider,
)


def test_default_provider_prefers_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    p = default_provider()
    assert isinstance(p, AnthropicProvider)


def test_default_provider_picks_openai_when_only_openai_set(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    p = default_provider()
    assert isinstance(p, OpenAIProvider)


def test_default_provider_raises_when_neither_set(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        default_provider()


def test_provider_constructors_raise_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        OpenAIProvider()
