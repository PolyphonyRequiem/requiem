"""Tests for ADR-0030 §2 role→model routing.

The resolver returns a ModelSpec for a given role, consulting the
``models:`` block on a ProcessConfig. Precedence is:

  1. ``models.<role>``  — explicit per-role binding
  2. ``models.default`` — catch-all
  3. empty ModelSpec    — caller falls through to v0 defaults

Behaviour pinned by these tests:

  * ``role=None`` short-circuits to empty (backward-compat bypass).
  * ``process_config=None`` returns empty (no policy configured).
  * Unknown role with a ``default`` entry falls through to default.
  * Unknown role without a ``default`` entry returns empty (NOT an error
    — forward-compat: new workflows invent new roles).
  * Malformed entry raises ValueError with actionable text naming the
    role + offending field.
"""
from __future__ import annotations

import pytest

from requiem.model_routing import ModelSpec, resolve_model_for_role
from requiem.process_config import ProcessConfig


def _config(models: dict) -> ProcessConfig:
    """Build a ProcessConfig with only a ``models`` block populated."""
    return ProcessConfig(
        root_parent_types=frozenset({"Scenario"}),
        type_aliases={},
        decomposable_types=frozenset(),
        implementable_types=frozenset(),
        types={},
        roles={},
        models=models,
        source=None,
        sha256=None,
    )


# ---- precedence ---------------------------------------------------------


def test_per_role_binding_wins_over_default() -> None:
    """``models.<role>`` takes precedence over ``models.default``."""
    cfg = _config({
        "planner":  {"provider": "anthropic", "model": "claude-opus-4.7"},
        "default":  {"provider": "copilot",   "model": "gpt-4.1"},
    })
    spec = resolve_model_for_role("planner", cfg)
    assert spec == ModelSpec(provider="anthropic", model="claude-opus-4.7", max_tokens=None)


def test_default_used_when_role_not_listed() -> None:
    """Unknown role falls through to ``models.default``."""
    cfg = _config({
        "default": {"provider": "openai", "model": "gpt-4o-mini", "max_tokens": 1024},
    })
    spec = resolve_model_for_role("reviewer", cfg)
    assert spec == ModelSpec(provider="openai", model="gpt-4o-mini", max_tokens=1024)


def test_empty_when_no_models_block() -> None:
    """A ProcessConfig without a ``models:`` block returns empty."""
    cfg = _config({})  # no models entries
    spec = resolve_model_for_role("planner", cfg)
    assert spec.is_empty()


def test_unknown_role_without_default_returns_empty() -> None:
    """Unknown role + no default entry → empty (NOT an error).

    Forward-compat: new workflows invent new roles; the resolver must
    not crash a workflow because it doesn't yet have a policy entry.
    Caller falls through to the engine's default provider per the
    documented precedence.
    """
    cfg = _config({"planner": {"provider": "anthropic", "model": "claude-opus-4.7"}})
    spec = resolve_model_for_role("unknown_role", cfg)
    assert spec.is_empty()


# ---- short-circuits -----------------------------------------------------


def test_role_none_short_circuits() -> None:
    """``role=None`` bypasses the resolver entirely (backward-compat)."""
    cfg = _config({
        "planner": {"provider": "anthropic", "model": "claude-opus-4.7"},
        "default": {"provider": "copilot",   "model": "gpt-4.1"},
    })
    spec = resolve_model_for_role(None, cfg)
    assert spec.is_empty()


def test_process_config_none_returns_empty() -> None:
    """``process_config=None`` is the same as no policy configured."""
    spec = resolve_model_for_role("planner", None)
    assert spec.is_empty()


# ---- fail-closed --------------------------------------------------------


def test_malformed_entry_not_a_mapping_raises_with_role_name() -> None:
    """Entry must be a mapping; non-dict raises ValueError naming the role."""
    cfg = _config({"planner": "not-a-dict"})
    with pytest.raises(ValueError, match="models.planner"):
        resolve_model_for_role("planner", cfg)


def test_malformed_entry_missing_provider_raises() -> None:
    cfg = _config({"planner": {"model": "claude-opus-4.7"}})
    with pytest.raises(ValueError, match="provider"):
        resolve_model_for_role("planner", cfg)


def test_malformed_entry_missing_model_raises() -> None:
    cfg = _config({"planner": {"provider": "anthropic"}})
    with pytest.raises(ValueError, match="model"):
        resolve_model_for_role("planner", cfg)


def test_malformed_entry_empty_string_provider_raises() -> None:
    cfg = _config({"planner": {"provider": "  ", "model": "claude-opus-4.7"}})
    with pytest.raises(ValueError, match="non-empty"):
        resolve_model_for_role("planner", cfg)


def test_malformed_max_tokens_non_int_raises() -> None:
    cfg = _config({"planner": {
        "provider": "anthropic", "model": "claude-opus-4.7",
        "max_tokens": "lots",
    }})
    with pytest.raises(ValueError, match="max_tokens"):
        resolve_model_for_role("planner", cfg)


def test_malformed_max_tokens_zero_raises() -> None:
    cfg = _config({"planner": {
        "provider": "anthropic", "model": "claude-opus-4.7",
        "max_tokens": 0,
    }})
    with pytest.raises(ValueError, match="positive integer"):
        resolve_model_for_role("planner", cfg)


def test_malformed_max_tokens_bool_raises() -> None:
    """``True``/``False`` are int subclasses; the validator must reject them."""
    cfg = _config({"planner": {
        "provider": "anthropic", "model": "claude-opus-4.7",
        "max_tokens": True,
    }})
    with pytest.raises(ValueError, match="max_tokens"):
        resolve_model_for_role("planner", cfg)


# ---- ModelSpec ergonomics ----------------------------------------------


def test_modelspec_is_empty_when_all_fields_none() -> None:
    assert ModelSpec().is_empty()
    assert not ModelSpec(provider="x").is_empty()
    assert not ModelSpec(model="y").is_empty()
    assert not ModelSpec(max_tokens=1).is_empty()


# ---- reasoning_effort / reasoning_summary / context_tier (run-#28 follow-up) ----


def test_reasoning_effort_carried_on_model_spec() -> None:
    """``models.<role>.reasoning_effort`` round-trips through the
    resolver and lands on :class:`ModelSpec`.

    Run #28 against AB#62759077 showed that even with the routing
    plumbing fixed (operator yaml `models.implementer` → coder
    agent actually runs on sonnet-4.6), the 600s session ceiling
    still dominated failures. claude-sonnet-4.6 supports
    `reasoning_effort` ('low'/'medium'/'high'/'max') — pinning to
    'low' should force faster turnaround. The kernel threads this
    into `AgentCall.model_options`; the CopilotProvider reads it.
    """
    from requiem.model_routing import resolve_model_for_role
    cfg = _config({
        "implementer": {
            "provider": "copilot",
            "model": "claude-sonnet-4.6",
            "reasoning_effort": "low",
        },
    })
    resolved = resolve_model_for_role("implementer", cfg)
    assert resolved.provider == "copilot"
    assert resolved.model == "claude-sonnet-4.6"
    assert resolved.reasoning_effort == "low"
    assert resolved.reasoning_summary is None
    assert resolved.context_tier is None


def test_all_three_reasoning_knobs_carried() -> None:
    """All three knobs round-trip; absent fields stay None."""
    from requiem.model_routing import resolve_model_for_role
    cfg = _config({
        "implementer": {
            "provider": "copilot",
            "model": "claude-sonnet-4.6",
            "reasoning_effort": "max",
            "reasoning_summary": "concise",
            "context_tier": "extended",
        },
    })
    resolved = resolve_model_for_role("implementer", cfg)
    assert resolved.reasoning_effort == "max"
    assert resolved.reasoning_summary == "concise"
    assert resolved.context_tier == "extended"


def test_to_model_options_returns_only_set_keys() -> None:
    """``ModelSpec.to_model_options()`` omits None-valued keys so the
    provider sees its own constructor defaults rather than redundant
    None entries — keeps the wire shape clean and test stub
    recorded-kwargs assertions unambiguous."""
    from requiem.model_routing import ModelSpec
    # Empty spec → empty dict.
    assert ModelSpec().to_model_options() == {}
    # Only reasoning_effort set.
    only_effort = ModelSpec(
        provider="copilot", model="x", reasoning_effort="low",
    )
    assert only_effort.to_model_options() == {"reasoning_effort": "low"}
    # All three.
    full = ModelSpec(
        provider="copilot", model="x",
        reasoning_effort="high",
        reasoning_summary="detailed",
        context_tier="standard",
    )
    assert full.to_model_options() == {
        "reasoning_effort": "high",
        "reasoning_summary": "detailed",
        "context_tier": "standard",
    }


def test_reasoning_effort_invalid_shape_fails_closed() -> None:
    """A non-string `reasoning_effort` (e.g. integer, list) raises
    ValueError at resolve time so a typo in process.yaml is
    one-line debuggable.

    Note: we DO NOT validate the value's vocabulary
    ('low'/'medium'/'high'/'max') here — that's the provider's job.
    A new model with a 'turbo' tier should just work without
    coordinated changes here."""
    from requiem.model_routing import resolve_model_for_role
    import pytest
    cfg = _config({
        "implementer": {
            "provider": "copilot",
            "model": "claude-sonnet-4.6",
            "reasoning_effort": 42,
        },
    })
    with pytest.raises(ValueError, match="reasoning_effort.*non-empty string"):
        resolve_model_for_role("implementer", cfg)


def test_is_empty_accounts_for_reasoning_knobs() -> None:
    """A ModelSpec with only a reasoning knob set is NOT empty —
    `is_empty()` must return False so the kernel knows to thread the
    knob into model_options (and emit the agent_call_started with
    the resolved provider)."""
    from requiem.model_routing import ModelSpec
    assert ModelSpec().is_empty() is True
    # Only reasoning_effort set: still a routing override.
    assert ModelSpec(reasoning_effort="low").is_empty() is False
    assert ModelSpec(reasoning_summary="x").is_empty() is False
    assert ModelSpec(context_tier="x").is_empty() is False
