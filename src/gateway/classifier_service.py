"""Standalone CPU System-1 workflow classifier service (ONNX + calibrated fallback)."""

from __future__ import annotations

import hmac
import importlib
import math
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from gateway.workflow_detector import SyntheticWorkflowClassifier, System1JudgeVerdict, _tokenize


class WorkflowCandidateSummary(BaseModel):
    workflow_id: str
    name: str
    description: str
    participating_tools: list[str] = Field(default_factory=list)
    intent_examples: list[str] = Field(default_factory=list)


class MatchIntentRequest(BaseModel):
    redacted_intent: str = ""
    observed_tools: list[str] = Field(default_factory=list)
    candidates: list[WorkflowCandidateSummary] = Field(default_factory=list)


class JudgeEpisodeRequest(BaseModel):
    redacted_intent: str | None = None
    matched_workflow_id: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


def _parse_secrets(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _authorize_request(
    authorization: str | None,
    x_classifier_secret: str | None,
    expected_secret: str,
    *,
    allow_unauthenticated: bool,
) -> None:
    keys = _parse_secrets(expected_secret)
    if not keys:
        if allow_unauthenticated:
            return
        raise HTTPException(status_code=401, detail="Classifier secret is required.")
    token = (
        authorization[7:].strip()
        if isinstance(authorization, str) and authorization.lower().startswith("bearer ")
        else ""
    )
    presented = token or (x_classifier_secret or "")
    if not presented or not any(hmac.compare_digest(presented, key) for key in keys):
        raise HTTPException(status_code=401, detail="Invalid classifier credentials.")


class OnnxOrHeuristicEngine:
    """Loads pre-baked ONNX models when available, falling back cleanly to calibrated scoring."""

    def __init__(self, model_dir: str | None = None) -> None:
        self.model_dir = Path(model_dir) if model_dir else None
        self.embed_session: Any = None
        self.embed_tokenizer: Any = None
        self.nli_session: Any = None
        self.nli_tokenizer: Any = None
        self.heuristic = SyntheticWorkflowClassifier()
        self.onnx_loaded = False
        self._try_load_onnx()

    def _try_load_onnx(self) -> None:
        if self.model_dir is None or not self.model_dir.is_dir():
            return
        try:
            ort = importlib.import_module("onnxruntime")
            tokenizers_mod = importlib.import_module("tokenizers")
            Tokenizer = tokenizers_mod.Tokenizer
        except ImportError:
            return
        embed_path = self.model_dir / "embedder" / "model.onnx"
        embed_tok = self.model_dir / "embedder" / "tokenizer.json"
        if embed_path.is_file() and embed_tok.is_file():
            self.embed_session = ort.InferenceSession(
                str(embed_path), providers=["CPUExecutionProvider"]
            )
            self.embed_tokenizer = Tokenizer.from_file(str(embed_tok))
            self.embed_tokenizer.enable_truncation(max_length=256)
            self.embed_tokenizer.enable_padding(length=256)
        nli_path = self.model_dir / "nli" / "model.onnx"
        nli_tok = self.model_dir / "nli" / "tokenizer.json"
        if nli_path.is_file() and nli_tok.is_file():
            self.nli_session = ort.InferenceSession(
                str(nli_path), providers=["CPUExecutionProvider"]
            )
            self.nli_tokenizer = Tokenizer.from_file(str(nli_tok))
            self.nli_tokenizer.enable_truncation(max_length=384)
            self.nli_tokenizer.enable_padding(length=384)
        self.onnx_loaded = bool(self.embed_session is not None or self.nli_session is not None)

    def _embed_text(self, text: str) -> list[float] | None:
        if self.embed_session is None or self.embed_tokenizer is None:
            return None
        np = importlib.import_module("numpy")

        encoded = self.embed_tokenizer.encode(text)
        input_ids = np.array([encoded.ids], dtype=np.int64)
        attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
        feed = {"input_ids": input_ids, "attention_mask": attention_mask}
        input_names = {inp.name for inp in self.embed_session.get_inputs()}
        if "token_type_ids" in input_names:
            feed["token_type_ids"] = np.zeros_like(input_ids)
        outputs = self.embed_session.run(None, feed)
        vec = outputs[0][0].mean(axis=0) if outputs[0].ndim == 3 else outputs[0][0]
        norm = float(np.linalg.norm(vec)) or 1.0
        return (vec / norm).tolist()

    def _nli_entailment(self, premise: str, hypothesis: str) -> float | None:
        """Return entailment probability (index 2 on 3-class MNLI models, else last logit)."""
        if self.nli_session is None or self.nli_tokenizer is None:
            return None
        np = importlib.import_module("numpy")

        encoded = self.nli_tokenizer.encode(premise, hypothesis)
        input_ids = np.array([encoded.ids], dtype=np.int64)
        attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
        feed = {"input_ids": input_ids, "attention_mask": attention_mask}
        input_names = {inp.name for inp in self.nli_session.get_inputs()}
        if "token_type_ids" in input_names:
            feed["token_type_ids"] = np.array([encoded.type_ids], dtype=np.int64)
        logits = self.nli_session.run(None, feed)[0][0]
        exps = [math.exp(float(x) - float(max(logits))) for x in logits]
        probs = [val / sum(exps) for val in exps]
        if len(probs) == 3:
            return float(probs[2])
        return float(probs[-1])

    def match_intent(self, req: MatchIntentRequest) -> dict[str, Any]:
        started = time.perf_counter()
        intent_vec = self._embed_text(req.redacted_intent) if req.redacted_intent else None
        intent_tokens = _tokenize(req.redacted_intent)
        observed = set(req.observed_tools)
        ranked: list[dict[str, Any]] = []
        for candidate in req.candidates:
            parts = set(candidate.participating_tools)
            tool_score = len(observed & parts) / max(len(parts), 1) if parts else 0.0
            wf_text = (
                f"{candidate.name}. {candidate.description} {' '.join(candidate.intent_examples)}"
            )
            if intent_vec is not None:
                cand_vec = self._embed_text(wf_text) or []
                sem_score = max(0.0, sum(a * b for a, b in zip(intent_vec, cand_vec, strict=False)))
            else:
                cand_tokens = _tokenize(wf_text)
                sem_score = (
                    len(intent_tokens & cand_tokens) / max(len(intent_tokens), 1)
                    if intent_tokens and cand_tokens
                    else 0.0
                )
            score = round(0.6 * tool_score + 0.4 * sem_score, 4)
            ranked.append({"workflow_id": candidate.workflow_id, "score": score})
        ranked.sort(key=lambda item: -item["score"])
        return {
            "matches": ranked,
            "backend": "onnx_biencoder" if intent_vec is not None else "synthetic_heuristic",
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    def judge_episode(self, req: JudgeEpisodeRequest) -> System1JudgeVerdict:
        started = time.perf_counter()
        base = self.heuristic.judge_episode(
            redacted_intent=req.redacted_intent,
            steps=req.steps,
            matched_workflow_id=req.matched_workflow_id,
        )
        step_summary = "; ".join(
            f"{s.get('tool_qualified_name')}({s.get('effect')}:{s.get('status')})"
            for s in req.steps
        )
        premise = f"Intent: {req.redacted_intent or 'unspecified'}. Steps: {step_summary}."
        entail = self._nli_entailment(
            premise,
            "The user's requested workflow completed successfully without missing steps.",
        )
        if entail is not None:
            blended = round(max(0.01, min(0.99, 0.55 * base.confidence + 0.45 * entail)), 4)
            base.confidence = blended
            base.completion_pct = int(round(blended * 100))
            base.backend = "onnx_nli"
            if entail < 0.35 and base.completed:
                base.completed = False
                base.verdict = "partial_or_incomplete"
                base.missing_goals.append("nli_entailment_below_threshold")
        base.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        return base


def create_classifier_app(
    *,
    classifier_secret: str | None = None,
    allow_unauthenticated: bool | None = None,
    model_dir: str | None = None,
) -> FastAPI:
    """Create the standalone System-1 workflow classifier microservice."""

    resolved_secret = (
        classifier_secret
        if classifier_secret is not None
        else os.getenv("GATEWAY_WORKFLOW_CLASSIFIER_SECRET", "")
    )
    app_env = os.getenv("GATEWAY_APP_ENV", "sandbox").lower()
    unauthenticated_dev = (
        app_env in {"sandbox", "dev"} if allow_unauthenticated is None else allow_unauthenticated
    )
    if not _parse_secrets(resolved_secret) and not unauthenticated_dev:
        raise RuntimeError("GATEWAY_WORKFLOW_CLASSIFIER_SECRET must be set outside dev/sandbox.")

    resolved_model_dir = (
        model_dir if model_dir is not None else os.getenv("CLASSIFIER_MODEL_DIR", "/app/models")
    )
    engine = OnnxOrHeuristicEngine(model_dir=resolved_model_dir)
    app = FastAPI(title="MCPcodeaway Workflow Classifier", version="0.1.0")
    app.state.engine = engine

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "onnx_loaded": engine.onnx_loaded,
            "backend": "onnx" if engine.onnx_loaded else "synthetic_heuristic",
        }

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        return {"ready": True, "onnx_loaded": engine.onnx_loaded}

    @app.post("/v1/match-intent")
    async def match_intent(
        body: MatchIntentRequest,
        authorization: str | None = Header(default=None),
        x_classifier_secret: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorize_request(
            authorization,
            x_classifier_secret,
            resolved_secret,
            allow_unauthenticated=unauthenticated_dev,
        )
        return engine.match_intent(body)

    @app.post("/v1/judge-episode")
    async def judge_episode(
        body: JudgeEpisodeRequest,
        authorization: str | None = Header(default=None),
        x_classifier_secret: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorize_request(
            authorization,
            x_classifier_secret,
            resolved_secret,
            allow_unauthenticated=unauthenticated_dev,
        )
        return engine.judge_episode(body).model_dump(mode="json")

    return app


app = create_classifier_app()
