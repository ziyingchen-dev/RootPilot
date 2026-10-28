"""FastAPI server for the RootPilot investigation workflow."""

from __future__ import annotations

import os
import secrets
from copy import deepcopy
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from llm.mistral_client import MistralClient, DEFAULT_MODEL as MISTRAL_DEFAULT
from llm.ollama_client import OllamaClient, AVAILABLE_MODELS as OLLAMA_MODELS
from llm.ollama_client import DEFAULT_MODEL as OLLAMA_DEFAULT
from main import (
    _add_generation_metadata,
    _enforce_data_policy,
    apply_changes,
    devil_review,
    enforce_patch_policy,
    format_diff,
    investigate,
    read_case,
)

app = FastAPI(title="RootPilot API", version="0.1.0")
_investigation_results: dict[str, dict[str, Any]] = {}
_MAX_STORED_INVESTIGATION_RESULTS = 100


class InvestigationRequest(BaseModel):
    """Request payload for a single investigation run."""

    case_dir: str = Field(..., description="Case directory containing issue.md and repo/")
    provider: str = Field(default="ollama", description="LLM provider: ollama or mistral")
    model: str | None = Field(default=None, description="Optional model override")
    data_policy: Literal["standard", "confidential"] = Field(
        default="standard", description="Data handling policy"
    )
    feedback: str | None = Field(default=None, description="Optional user feedback")
    previous_result: dict[str, Any] | None = Field(
        default=None, description="Previous investigation result for a feedback round"
    )


class InvestigationResponse(BaseModel):
    """Response payload returned to the frontend."""

    result: dict[str, Any]
    result_id: str | None
    provider: str
    model: str | None
    data_policy: str
    generation_metadata: dict[str, Any]
    diff: str = ""


class DevilReviewRequest(BaseModel):
    """Request payload for an A/B Devil Review run."""

    case_dir: str = Field(..., description="Case directory containing issue.md and repo/")
    provider: str = Field(default="ollama", description="LLM provider: ollama or mistral")
    model: str | None = Field(default=None, description="Optional model override")
    data_policy: Literal["standard", "confidential"] = Field(
        default="standard", description="Data handling policy"
    )
    report_a: dict[str, Any]
    report_b: dict[str, Any]


class DevilReviewResponse(BaseModel):
    """Response payload for a C Devil Review report."""

    result: dict[str, Any]
    provider: str
    model: str | None
    data_policy: str
    generation_metadata: dict[str, Any]


class ApplyRequest(BaseModel):
    """Request to apply a server-owned investigation result."""

    model_config = ConfigDict(extra="forbid")
    case_dir: str = Field(..., description="Case directory containing the repository")
    result_id: str = Field(..., min_length=1)


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
        _enforce_data_policy(provider, request.data_policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    try:
        client = clients[provider](model=request.model)
        result = enforce_patch_policy(investigate(
            request.case_dir,
            client,
            request.feedback or "",
            previous_result=request.previous_result,
        ))
        result = _add_generation_metadata(
            result, client, provider, request.model, request.data_policy
        )
        result_id = None
        diff_str = ""
        proposed_changes = result.get("proposed_changes")
        if (
            result.get("patch_decision", {}).get("status") == "allowed"
            and isinstance(proposed_changes, list)
            and proposed_changes
        ):
            diff_str = format_diff(request.case_dir, proposed_changes)
        if diff_str:
            result_id = secrets.token_urlsafe(32)
            _investigation_results[result_id] = {
                "case_dir": request.case_dir,
                "result": deepcopy(result),
            }
            if len(_investigation_results) > _MAX_STORED_INVESTIGATION_RESULTS:
                oldest_result_id = next(iter(_investigation_results))
                del _investigation_results[oldest_result_id]
        metadata = result["generation_metadata"]
        return InvestigationResponse(
            result=result,
            result_id=result_id,
            provider=provider,
            model=metadata["model"],
            data_policy=request.data_policy,
            generation_metadata=metadata,
            diff=diff_str,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/devil-review", response_model=DevilReviewResponse)
@app.post("/api/devil-review", response_model=DevilReviewResponse)
def devil_review_endpoint(request: DevilReviewRequest) -> DevilReviewResponse:
    """Review A and B against the original case evidence."""
    clients = {"ollama": OllamaClient, "mistral": MistralClient}
    provider = request.provider.lower()
    if provider not in clients:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {request.provider}")

    try:
        _enforce_data_policy(provider, request.data_policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    try:
        case = read_case(request.case_dir)
        client = clients[provider](model=request.model)
        result = devil_review(
            case["issue"],
            case["logs"],
            case["repository"],
            case["git_history"],
            request.report_a,
            request.report_b,
            client,
        )
        result = _add_generation_metadata(
            result, client, provider, request.model, request.data_policy
        )
        metadata = result["generation_metadata"]
        return DevilReviewResponse(
            result=result,
            provider=provider,
            model=metadata["model"],
            data_policy=request.data_policy,
            generation_metadata=metadata,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/apply")
@app.post("/api/apply")
def apply_endpoint(request: ApplyRequest) -> dict[str, Any]:
    """Apply accepted changes from a server-owned investigation result."""
    try:
        stored_entry = _investigation_results.get(request.result_id)
        if stored_entry is None:
            raise HTTPException(
                status_code=404, detail="Investigation result was not found."
            )
        if stored_entry["case_dir"] != request.case_dir:
            raise HTTPException(
                status_code=400,
                detail="Investigation result does not belong to this case directory.",
            )

        result = enforce_patch_policy(deepcopy(stored_entry["result"]))
        if result.get("patch_decision", {}).get("status") != "allowed":
            _investigation_results.pop(request.result_id, None)
            reason = result.get("patch_decision", {}).get("reason", "Patch is blocked.")
            raise HTTPException(status_code=400, detail=reason)
        applied = apply_changes(request.case_dir, result.get("proposed_changes", []))
        _investigation_results.pop(request.result_id, None)
        return {"applied": applied, "count": len(applied)}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
