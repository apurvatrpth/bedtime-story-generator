"""Thin wrapper around the Anthropic SDK. Every model call in the app goes through here."""

import os
from typing import TypeVar

import anthropic
from pydantic import BaseModel

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 16000  # thinking counts toward this cap, so leave plenty of room

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


class StoryError(RuntimeError):
    """A problem we can explain to the user in plain words."""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise StoryError(
                "ANTHROPIC_API_KEY is not set. Set it as an environment variable "
                "(see README.md) and open a new terminal."
            )
        _client = anthropic.Anthropic()  # reads the key from the environment
    return _client


def _check_stop_reason(response) -> None:
    if response.stop_reason == "refusal":
        raise StoryError("The model declined this request. Try a different story idea.")
    if response.stop_reason == "max_tokens":
        raise StoryError("The response was cut off before it finished. Please try again.")


def call_model(prompt: str, system: str | None = None, effort: str = "medium") -> str:
    """Send one prompt and return the model's text reply.

    Haiku 5.5 does not accept temperature, so style and consistency are steered
    through the prompts and `effort` (how much the model thinks before answering).
    """
    kwargs = {"system": system} if system else {}
    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    _check_stop_reason(response)
    # The reply can start with thinking blocks, so read text blocks by type.
    return "".join(block.text for block in response.content if block.type == "text").strip()


def call_structured(prompt: str, schema: type[T], system: str | None = None, effort: str = "medium") -> T:
    """Send one prompt and get back a validated Pydantic object (structured outputs)."""
    kwargs = {"system": system} if system else {}
    response = _get_client().messages.parse(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"effort": effort},
        output_format=schema,
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    _check_stop_reason(response)
    return response.parsed_output
