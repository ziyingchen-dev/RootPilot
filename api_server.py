"""FastAPI server for the RootPilot investigation workflow."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from llm.mistral_client import MistralClient, DEFAULT_MODEL as MISTRAL_DEFAULT
from llm.ollama_client import OllamaClient, AVAILABLE_MODELS as OLLAMA_MODELS
from llm.ollama_client import DEFAULT_MODEL as OLLAMA_DEFAULT
from main import apply_changes, investigate, format_diff

app = FastAPI(title="RootPilot API", version="0.1.0")


class InvestigationRequest(BaseModel):
    """Request payload for a single investigation run."""

    case_dir: str = Field(..., description="Case directory containing issue.md and repo/")
    provider: str = Field(default="ollama", description="LLM provider: ollama or mistral")
    model: str | None = Field(default=None, description="Optional model override")
    feedback: str | None = Field(default=None, description="Optional user feedback")
    previous_result: dict[str, Any] | None = Field(
        default=None, description="Previous investigation result for a feedback round"
    )


class InvestigationResponse(BaseModel):
    """Response payload returned to the frontend."""

    result: dict[str, Any]
    provider: str
    model: str | None
    diff: str = ""


class ApplyRequest(BaseModel):
    """Request to apply accepted changes to the repo for a case."""

    case_dir: str = Field(..., description="Case directory containing the repository")
    changes: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, str]:
    """Health check endpoint used by the UI and deployment checks."""
    return {"status": "ok"}


@app.post("/investigate", response_model=InvestigationResponse)
@app.post("/api/investigate", response_model=InvestigationResponse)
def investigate_endpoint(request: InvestigationRequest) -> InvestigationResponse:
    """Run an investigation and return the parsed result to the UI."""
    clients = {"ollama": OllamaClient, "mistral": MistralClient}
    provider = request.provider.lower()
    if provider not in clients:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {request.provider}")

    try:
        client = clients[provider](model=request.model)
        result = investigate(
            request.case_dir,
            client,
            request.feedback or "",
            previous_result=request.previous_result,
        )
        diff_str = format_diff(request.case_dir, result.get("proposed_changes", []))
        return InvestigationResponse(
            result=result,
            provider=provider,
            model=request.model,
            diff=diff_str,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/apply")
@app.post("/api/apply")
def apply_endpoint(request: ApplyRequest) -> dict[str, Any]:
    """Apply accepted patch suggestions to the repository."""
    try:
        applied = apply_changes(request.case_dir, request.changes)
        return {"applied": applied, "count": len(applied)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/providers")
def providers_endpoint() -> dict[str, Any]:
    """Return supported providers, model options, and availability reasons."""
    mistral_reason = ""
    mistral_available = bool(os.getenv("MISTRAL_API_KEY"))
    if not mistral_available:
        mistral_reason = "MISTRAL_API_KEY is not set."

    return {
        "providers": {
            "ollama": {
                "models": OLLAMA_MODELS,
                "available": True,
                "reason": "",
            },
            "mistral": {
                "models": [MISTRAL_DEFAULT],
                "available": mistral_available,
                "reason": mistral_reason,
            },
        }
    }
