"""Prompt builders used by the investigation workflow."""

from .devil_review import build_devil_review_prompt
from .investigation import build_investigation_prompt

__all__ = ["build_devil_review_prompt", "build_investigation_prompt"]
