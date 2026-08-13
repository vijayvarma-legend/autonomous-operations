"""Provider-agnostic LLM call used by any agent that needs to reason
(Planner now; Decision Agent later). Dispatches on `settings.llm_provider`
at call time so the actual provider can be decided/changed via .env without
touching agent code.
"""

from __future__ import annotations

from app.config import settings

_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
    # OpenRouter has no sane default — model slugs are OpenRouter-specific
    # (e.g. "nvidia/..."), so LLM_MODEL must be set explicitly.
}

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Planner/Decision responses are short JSON — capping avoids a model's own
# default (some reasoning models default to 32k+) running up cost or
# exceeding OpenRouter credit limits.
_MAX_TOKENS = 1024


def complete(system: str, user: str) -> str:
    """Send a single system+user turn to the configured provider and return
    the raw text response. Raises ValueError if no provider is configured —
    a config boundary, not something callers should silently work around.
    """
    provider = settings.llm_provider
    model = settings.llm_model or _DEFAULT_MODELS.get(provider, "")

    if provider == "anthropic":
        return _complete_anthropic(system, user, model)
    if provider == "openai":
        return _complete_openai(system, user, model)
    if provider == "openrouter":
        if not model:
            raise ValueError("LLM_MODEL must be set when LLM_PROVIDER=openrouter.")
        return _complete_openai_compatible(
            system, user, model, api_key=settings.openrouter_api_key, base_url=_OPENROUTER_BASE_URL
        )
    raise ValueError(
        f"LLM_PROVIDER={provider!r} is not configured. Set LLM_PROVIDER to one of "
        "anthropic, openai, openrouter (plus the matching API key) in .env."
    )


def _complete_anthropic(system: str, user: str, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text


def _complete_openai(system: str, user: str, model: str) -> str:
    return _complete_openai_compatible(system, user, model, api_key=settings.openai_api_key)


def _complete_openai_compatible(
    system: str, user: str, model: str, *, api_key: str, base_url: str | None = None
) -> str:
    """Shared by openai and openrouter — OpenRouter speaks the OpenAI chat
    completions wire format, so the SDK just needs a different base_url.
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content
