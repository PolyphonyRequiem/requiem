"""Role→model routing — ADR-0030 §2.

Mirrors the ``roles:`` block from ADR-0017 §1 (delivered by
``process_config.py``) with a ``models:`` block keyed by role name. A
workflow author tags an :class:`~requiem.agent.AgentSpec` with a ``role``
(``planner``, ``reviewer``, ``implementer``, ``closer``, ``judge``, …);
this module resolves that role against the loaded
:class:`~requiem.process_config.ProcessConfig` and returns the
(provider, model, max_tokens) that the engine should use for that one
call.

Resolution precedence (deterministic):

1. ``models.<role>``           — explicit per-role binding.
2. ``models.default``          — catch-all when no per-role entry exists.
3. :class:`ModelSpec` of ``(None, None, None)`` — caller falls through
   to ``default_provider()`` / ``AgentSpec`` defaults (today's behaviour;
   backward-compatible by default).

A workflow whose ``AgentSpec.role`` is ``None`` is bypassed entirely —
it keeps the v0 behaviour of using whatever provider/model was wired into
``Engine``. This keeps every existing call site working without edits
(ADR-0030 §Decision: "Backward-compatible by default").

Fail-closed posture:

* Malformed entries (non-string ``provider``/``model``, negative
  ``max_tokens``, missing ``provider``/``model``) raise
  :class:`ValueError` with actionable text. The engine surfaces this as
  a workflow build failure, not a silent fall-through to defaults —
  silent guessing on routing is exactly what INV-NO-CORRUPT-FORWARD
  forbids.
* Unknown role names are intentionally NOT rejected (the catalogue of
  valid roles is open-ended; new workflows invent new roles). The
  resolver falls back to ``models.default`` and, if that is absent, to
  ``ModelSpec(None, None, None)``.

This module is pure; no I/O. Construct a :class:`ModelSpec`, plumb it
into the call boundary in ``kernel.py``, record the resolved tuple in
the ``agent_call_started`` event envelope payload so resume reads it
back (ADR-0030 §Idempotency: "the recorded value wins").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from requiem.process_config import ProcessConfig


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Per-call routing decision.

    ``provider`` and ``model`` are both ``None`` when no policy is
    configured — callers fall through to the ``default_provider()``-
    resolved provider and the :class:`AgentSpec` model default
    (typically the sentinel ``"fake"`` which providers reinterpret as
    "use my own default model"; see ``AnthropicProvider.invoke``).

    ``max_tokens`` is independently optional: a policy may pin
    ``provider`` + ``model`` without pinning ``max_tokens``, in which
    case the provider's own default applies.

    ``reasoning_effort``, ``reasoning_summary``, ``context_tier`` are
    provider-specific knobs surfaced by the GitHub Copilot SDK (run #28
    follow-up: discovered claude-sonnet-4.6 has a tunable reasoning
    loop while claude-sonnet-4.5 does not — ``reasoning_effort='low'``
    forces faster turnaround at the cost of less deliberate reasoning).
    The kernel threads non-None values through to the provider via
    ``AgentCall.model_options``; providers that don't understand a key
    silently ignore it. Operator yaml shape::

        models:
          implementer:
            provider: copilot
            model: claude-sonnet-4.6
            reasoning_effort: low      # 'low' | 'medium' | 'high' | 'max'
            reasoning_summary: none    # provider-specific
            context_tier: standard     # provider-specific
    """

    provider: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    reasoning_summary: str | None = None
    context_tier: str | None = None

    def is_empty(self) -> bool:
        """True when no override was resolved — caller may skip plumbing."""
        return (
            self.provider is None
            and self.model is None
            and self.max_tokens is None
            and self.reasoning_effort is None
            and self.reasoning_summary is None
            and self.context_tier is None
        )

    def to_model_options(self) -> dict[str, Any]:
        """Project the provider-specific knobs into the dict that
        :class:`requiem.agent.AgentCall.model_options` expects.

        Returns only the keys that are explicitly set — ``None`` values
        are omitted so the receiving provider sees its own
        constructor-time defaults rather than a redundant ``None``.
        Keeps the wire shape clean and the test stub recorded-kwargs
        comparison straightforward."""
        opts: dict[str, Any] = {}
        if self.reasoning_effort is not None:
            opts["reasoning_effort"] = self.reasoning_effort
        if self.reasoning_summary is not None:
            opts["reasoning_summary"] = self.reasoning_summary
        if self.context_tier is not None:
            opts["context_tier"] = self.context_tier
        return opts


def _validate_entry(role: str, entry: Any) -> ModelSpec:
    """Coerce a raw ``models.<role>`` mapping into a :class:`ModelSpec`.

    Fail-closed on every shape we don't recognise. The fail-closed
    messages name the offending role + field so a typo in
    ``process.yaml`` is one-line debuggable.
    """
    if not isinstance(entry, dict):
        raise ValueError(
            f"models.{role}: entry must be a mapping with 'provider' + 'model', "
            f"got {type(entry).__name__!s}"
        )
    provider = entry.get("provider")
    model = entry.get("model")
    max_tokens = entry.get("max_tokens")
    reasoning_effort = entry.get("reasoning_effort")
    reasoning_summary = entry.get("reasoning_summary")
    context_tier = entry.get("context_tier")

    if provider is None or not isinstance(provider, str) or not provider.strip():
        raise ValueError(
            f"models.{role}.provider must be a non-empty string; got {provider!r}"
        )
    if model is None or not isinstance(model, str) or not model.strip():
        raise ValueError(
            f"models.{role}.model must be a non-empty string; got {model!r}"
        )
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise ValueError(
                f"models.{role}.max_tokens must be a positive integer when set; "
                f"got {max_tokens!r}"
            )
        if max_tokens <= 0:
            raise ValueError(
                f"models.{role}.max_tokens must be a positive integer when set; "
                f"got {max_tokens}"
            )
    # The three reasoning knobs are all "non-empty string when set" — the
    # provider validates the actual values against its own enum
    # (e.g. Copilot accepts 'low'/'medium'/'high'/'max' for
    # reasoning_effort; we don't hard-code that vocabulary here so new
    # providers can extend it without coordinated changes here).
    for fname, fval in (
        ("reasoning_effort", reasoning_effort),
        ("reasoning_summary", reasoning_summary),
        ("context_tier", context_tier),
    ):
        if fval is not None and (not isinstance(fval, str) or not fval.strip()):
            raise ValueError(
                f"models.{role}.{fname} must be a non-empty string when set; "
                f"got {fval!r}"
            )
    return ModelSpec(
        provider=provider,
        model=model,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        reasoning_summary=reasoning_summary,
        context_tier=context_tier,
    )


def resolve_model_for_role(
    role: str | None, process_config: ProcessConfig | None
) -> ModelSpec:
    """Return the :class:`ModelSpec` for ``role`` from ``process_config``.

    See module docstring for precedence. ``role=None`` short-circuits to
    an empty :class:`ModelSpec` so the caller bypasses the resolver
    entirely (backward-compat: workflows that don't opt into role
    tagging keep v0 behaviour).

    ``process_config=None`` is treated as "no policy configured" — the
    same as a present config with no ``models:`` block. Returns an empty
    :class:`ModelSpec`.

    Raises :class:`ValueError` only when a present-but-malformed entry
    is encountered (fail-closed; never silently guess).
    """
    if role is None:
        return ModelSpec()
    if process_config is None:
        return ModelSpec()
    models = getattr(process_config, "models", None)
    if not models:
        return ModelSpec()
    # Explicit per-role entry wins.
    if role in models:
        return _validate_entry(role, models[role])
    # Fall through to the default entry.
    if "default" in models:
        return _validate_entry("default", models["default"])
    # No policy applies — caller uses today's defaults.
    return ModelSpec()


__all__ = ["ModelSpec", "resolve_model_for_role"]
