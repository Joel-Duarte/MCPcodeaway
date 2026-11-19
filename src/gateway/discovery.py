"""Pluggable tool discovery backends: Keyword, BM25, Embeddings, and Two-Stage System 1."""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from gateway.config import DiscoveryBackendMode, GatewaySettings
from gateway.upstream_clients import DiscoveredTool

_WORD_RE = re.compile(r"[a-z0-9]+")


class ScoredTool(BaseModel):
    """Tool ranked by a DiscoveryBackend along with relevance and stage-1 server scores."""

    tool: DiscoveredTool
    score: float
    stage1_server_score: float | None = None


class DiscoveryOutcome(BaseModel):
    """Structured discovery result including fallback telemetry and latency."""

    tools: list[ScoredTool] = Field(default_factory=list)
    backend_requested: str
    backend_used: str
    fallback_triggered: bool = False
    fallback_reason: str | None = None
    stage1_servers: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: float = 0.0


class DiscoveryBackend(Protocol):
    """Protocol for ranking RBAC-filtered tools against a natural-language or keyword query."""

    async def search(
        self,
        query: str,
        tools: list[DiscoveredTool],
        *,
        catalog_version: str = "",
        limit: int = 15,
    ) -> DiscoveryOutcome: ...


def _tokenize_text(text: str) -> list[str]:
    normalized = text.replace("_", " ").replace("-", " ").lower()
    return _WORD_RE.findall(normalized)


def _tool_tokens(tool: DiscoveredTool) -> list[str]:
    props = (tool.input_schema or {}).get("properties") or {}
    prop_parts: list[str] = []
    for pname, pval in props.items():
        prop_parts.append(str(pname))
        if isinstance(pval, dict) and pval.get("description"):
            prop_parts.append(str(pval["description"]))
    raw = f"{tool.server} {tool.name} {tool.name} {tool.description} {' '.join(prop_parts)}"
    return _tokenize_text(raw)


def _tool_card(tool: DiscoveredTool) -> str:
    props = ", ".join(((tool.input_schema or {}).get("properties") or {}).keys())
    return f"{tool.qualified_name}({props}): {tool.description}"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _extract_noul_probability(answer: Any) -> float | None:
    """Parse calibrated [0, 1] probability across System 1 wire schema variants."""
    if isinstance(answer, (int, float)):
        return max(0.0, min(1.0, float(answer)))
    if not isinstance(answer, dict):
        return None
    for key in ("noul", "probability", "score", "yes", "true", "p", "confidence"):
        val = answer.get(key)
        if isinstance(val, (int, float)):
            return max(0.0, min(1.0, float(val)))
    probs = answer.get("probabilities") or answer.get("distribution")
    if isinstance(probs, dict):
        for key in ("yes", "true", "Yes", "True", "1"):
            val = probs.get(key)
            if isinstance(val, (int, float)):
                return max(0.0, min(1.0, float(val)))
    if "value" in answer and isinstance(answer["value"], (int, float)):
        return max(0.0, min(1.0, float(answer["value"])))
    return None


class KeywordDiscoveryBackend:
    """Baseline lightweight substring scorer."""

    name = "keyword"

    async def search(
        self,
        query: str,
        tools: list[DiscoveredTool],
        *,
        catalog_version: str = "",
        limit: int = 15,
    ) -> DiscoveryOutcome:
        _ = catalog_version
        started = time.perf_counter()
        terms = [tok for tok in query.lower().split() if tok]
        if not terms:
            ordered = sorted(tools, key=lambda t: t.qualified_name)[:limit]
            scored = [ScoredTool(tool=t, score=1.0) for t in ordered]
        else:
            ranked: list[tuple[float, DiscoveredTool]] = []
            for tool in tools:
                haystack = (
                    f"{tool.qualified_name} {tool.server} {tool.name} {tool.description}".lower()
                )
                score = sum(
                    (3.0 if term in tool.name.lower() else 1.0)
                    for term in terms
                    if term in haystack
                )
                if score > 0:
                    ranked.append((score, tool))
            ranked.sort(key=lambda item: (-item[0], item[1].qualified_name))
            scored = [ScoredTool(tool=t, score=s) for s, t in ranked[:limit]]

        return DiscoveryOutcome(
            tools=scored,
            backend_requested=self.name,
            backend_used=self.name,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )


class BM25DiscoveryBackend:
    """Zero-dependency Okapi BM25 scorer over server, tool name, description, and schema."""

    name = "bm25"

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b

    async def search(
        self,
        query: str,
        tools: list[DiscoveredTool],
        *,
        catalog_version: str = "",
        limit: int = 15,
    ) -> DiscoveryOutcome:
        _ = catalog_version
        started = time.perf_counter()
        if not tools:
            return DiscoveryOutcome(
                tools=[],
                backend_requested=self.name,
                backend_used=self.name,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )

        q_tokens = _tokenize_text(query)
        if not q_tokens:
            ordered = sorted(tools, key=lambda t: t.qualified_name)[:limit]
            return DiscoveryOutcome(
                tools=[ScoredTool(tool=t, score=1.0) for t in ordered],
                backend_requested=self.name,
                backend_used=self.name,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )

        docs = [_tool_tokens(t) for t in tools]
        n_docs = len(docs)
        avgdl = sum(len(d) for d in docs) / max(n_docs, 1)

        df: dict[str, int] = defaultdict(int)
        tf_list: list[Counter[str]] = []
        for doc in docs:
            counts = Counter(doc)
            tf_list.append(counts)
            for term in counts:
                df[term] += 1

        scored: list[tuple[float, DiscoveredTool]] = []
        for tool, doc, tf in zip(tools, docs, tf_list, strict=True):
            dl = len(doc)
            score = 0.0
            for term in q_tokens:
                freq = tf.get(term, 0)
                if freq == 0:
                    continue
                doc_freq = df.get(term, 0)
                idf = math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
                denom = freq + self.k1 * (1.0 - self.b + self.b * (dl / max(avgdl, 1.0)))
                score += idf * ((freq * (self.k1 + 1.0)) / max(denom, 1e-9))
            if score > 0.0:
                scored.append((score, tool))

        scored.sort(key=lambda item: (-item[0], item[1].qualified_name))
        return DiscoveryOutcome(
            tools=[ScoredTool(tool=t, score=round(s, 4)) for s, t in scored[:limit]],
            backend_requested=self.name,
            backend_used=self.name,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )


class EmbeddingDiscoveryBackend:
    """OpenAI-compatible /v1/embeddings semantic discovery with per-catalog vector caching."""

    name = "embedding"

    def __init__(
        self,
        *,
        url: str | None,
        model: str,
        api_key: str = "",
        http_client: httpx.AsyncClient | None = None,
        fallback: BM25DiscoveryBackend | None = None,
    ) -> None:
        self.url = url
        self.model = model
        self.api_key = api_key
        self._http_client = http_client
        self._fallback = fallback or BM25DiscoveryBackend()
        self._vector_cache: dict[tuple[str, str], list[float]] = {}

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self.url:
            raise RuntimeError("embedding_url is not configured")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "input": texts}
        if self._http_client is not None:
            resp = await self._http_client.post(self.url, headers=headers, json=payload)
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(self.url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Embedding HTTP {resp.status_code}: {resp.text[:240]}")
        data = resp.json().get("data") or []
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        return [list(item["embedding"]) for item in ordered]

    async def search(
        self,
        query: str,
        tools: list[DiscoveredTool],
        *,
        catalog_version: str = "",
        limit: int = 15,
    ) -> DiscoveryOutcome:
        started = time.perf_counter()
        if not query.strip() or not tools:
            return await self._fallback.search(
                query, tools, catalog_version=catalog_version, limit=limit
            )
        try:
            missing = [
                t for t in tools if (catalog_version, t.qualified_name) not in self._vector_cache
            ]
            inputs_to_embed = [query, *(_tool_card(t) for t in missing)]
            vectors = await self._embed_texts(inputs_to_embed)
            query_vec = vectors[0]
            for tool, vec in zip(missing, vectors[1:], strict=True):
                self._vector_cache[(catalog_version, tool.qualified_name)] = vec

            scored: list[tuple[float, DiscoveredTool]] = []
            for tool in tools:
                tvec = self._vector_cache[(catalog_version, tool.qualified_name)]
                sim = _cosine_similarity(query_vec, tvec)
                scored.append((sim, tool))
            scored.sort(key=lambda item: (-item[0], item[1].qualified_name))
            return DiscoveryOutcome(
                tools=[ScoredTool(tool=t, score=round(s, 4)) for s, t in scored[:limit]],
                backend_requested=self.name,
                backend_used=self.name,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        except Exception as exc:
            fb = await self._fallback.search(
                query, tools, catalog_version=catalog_version, limit=limit
            )
            fb.backend_requested = self.name
            fb.fallback_triggered = True
            fb.fallback_reason = str(exc)
            fb.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            return fb


class System1DiscoveryBackend:
    """Two-stage hierarchical System 1 router via POST /v1/systemone (noul probabilities)."""

    def __init__(
        self,
        *,
        url: str | None,
        model: str,
        api_key: str = "",
        threshold: float = 0.35,
        hybrid_prefilter: bool = False,
        http_client: httpx.AsyncClient | None = None,
        fallback: BM25DiscoveryBackend | None = None,
    ) -> None:
        self.url = url
        self.model = model
        self.api_key = api_key
        self.threshold = threshold
        self.hybrid_prefilter = hybrid_prefilter
        self.name = "hybrid_system1" if hybrid_prefilter else "system1"
        self._http_client = http_client
        self._fallback = fallback or BM25DiscoveryBackend()

    async def _post_systemone_raw(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        if not self.url:
            raise RuntimeError("system1_url is not configured")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "state": state, "questions": questions}
        if self._http_client is not None:
            resp = await self._http_client.post(self.url, headers=headers, json=payload)
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(self.url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"System1 HTTP {resp.status_code}: {resp.text[:240]}")
        body = resp.json()
        raw_answers = body.get("answers") or body.get("results") or body
        if not isinstance(raw_answers, dict):
            raise RuntimeError(f"Unexpected System1 response payload: {type(raw_answers).__name__}")
        return raw_answers

    async def _post_systemone(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> dict[str, float]:
        raw_answers = await self._post_systemone_raw(state, questions)
        parsed: dict[str, float] = {}
        for qid in questions:
            prob = _extract_noul_probability(raw_answers.get(qid))
            if prob is None:
                raise RuntimeError(f"Missing calibrated probability for question '{qid}'")
            parsed[qid] = prob
        return parsed

    async def _score_tools_single_choice(
        self,
        state: str,
        candidates: list[DiscoveredTool],
        srv_score_map: dict[str, float],
    ) -> list[ScoredTool] | None:
        if not candidates:
            return []
        criteria = {
            tool.qualified_name: f"{tool.qualified_name}: {tool.description}" for tool in candidates
        }
        questions = {
            "tool_choice": {
                "type": "choice",
                "instructions": (
                    "Select the tool that best fulfills the user's request and return "
                    "calibrated probabilities across all candidate tools."
                ),
                "criteria": criteria,
            }
        }
        raw_answers = await self._post_systemone_raw(state, questions)
        ans = raw_answers.get("tool_choice")
        if not isinstance(ans, dict) or not isinstance(ans.get("probabilities"), dict):
            return None
        probs = ans["probabilities"]
        ranked: list[ScoredTool] = []
        for tool in candidates:
            t_score = max(0.0, min(1.0, float(probs.get(tool.qualified_name, 0.0))))
            ranked.append(
                ScoredTool(
                    tool=tool,
                    score=round(t_score, 4),
                    stage1_server_score=round(srv_score_map.get(tool.server, 1.0), 4),
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.tool.qualified_name))
        return ranked

    async def search(
        self,
        query: str,
        tools: list[DiscoveredTool],
        *,
        catalog_version: str = "",
        limit: int = 15,
    ) -> DiscoveryOutcome:
        started = time.perf_counter()
        if not query.strip() or not tools:
            return await self._fallback.search(
                query, tools, catalog_version=catalog_version, limit=limit
            )

        try:
            by_server: dict[str, list[DiscoveredTool]] = defaultdict(list)
            for tool in tools:
                by_server[tool.server].append(tool)

            # Stage 1: Server/cluster routing via one batched /v1/systemone call
            server_names = sorted(by_server)
            stage1_questions: dict[str, dict[str, Any]] = {}
            for srv in server_names:
                preview = ", ".join(t.name for t in by_server[srv][:6])
                stage1_questions[f"srv_{srv}"] = {
                    "type": "noul",
                    "instructions": (
                        f"Is MCP server '{srv}' (tools: {preview}) relevant for fulfilling "
                        f"the user's request?"
                    ),
                }

            srv_scores = await self._post_systemone(query, stage1_questions)
            stage1_ranked = sorted(
                ((srv, srv_scores[f"srv_{srv}"]) for srv in server_names),
                key=lambda item: (-item[1], item[0]),
            )
            stage1_records = [
                {"server": srv, "score": round(score, 4)} for srv, score in stage1_ranked
            ]

            surviving = [srv for srv, score in stage1_ranked if score >= self.threshold]
            if not surviving:
                surviving = [srv for srv, _ in stage1_ranked[:3]]
            srv_score_map = dict(stage1_ranked)

            # Stage 2: Fine tool scoring across surviving servers (hybrid prefilters >25 via BM25)
            candidates = [t for srv in surviving for t in by_server[srv]]
            if self.hybrid_prefilter and len(candidates) > 25:
                pref = await self._fallback.search(
                    query, candidates, catalog_version=catalog_version, limit=25
                )
                candidates = [st.tool for st in pref.tools] or candidates[:25]
            elif len(candidates) > 40:
                candidates = candidates[:40]

            choice_ranked = (
                await self._score_tools_single_choice(query, candidates, srv_score_map)
                if self._http_client is None
                else None
            )
            if choice_ranked is not None:
                ranked = choice_ranked
            else:
                stage2_questions: dict[str, dict[str, Any]] = {}
                for idx, tool in enumerate(candidates):
                    stage2_questions[f"tool_{idx}"] = {
                        "type": "noul",
                        "instructions": (
                            f"Should tool '{tool.qualified_name}' ({tool.description}) be used "
                            "to fulfill this request?"
                        ),
                    }

                tool_scores = await self._post_systemone(query, stage2_questions)
                ranked = []
                for idx, tool in enumerate(candidates):
                    t_score = tool_scores[f"tool_{idx}"]
                    ranked.append(
                        ScoredTool(
                            tool=tool,
                            score=round(t_score, 4),
                            stage1_server_score=round(srv_score_map[tool.server], 4),
                        )
                    )
                ranked.sort(key=lambda item: (-item.score, item.tool.qualified_name))

            return DiscoveryOutcome(
                tools=ranked[:limit],
                backend_requested=self.name,
                backend_used=self.name,
                stage1_servers=stage1_records,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        except Exception as exc:
            fb = await self._fallback.search(
                query, tools, catalog_version=catalog_version, limit=limit
            )
            fb.backend_requested = self.name
            fb.fallback_triggered = True
            fb.fallback_reason = str(exc)
            fb.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            return fb


class DiscoveryEngine:
    """Selects and invokes the requested DiscoveryBackend with safe BM25 fallback."""

    def __init__(
        self,
        settings: GatewaySettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.keyword = KeywordDiscoveryBackend()
        self.bm25 = BM25DiscoveryBackend()
        self.embedding = EmbeddingDiscoveryBackend(
            url=settings.embedding_url,
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            http_client=http_client,
            fallback=self.bm25,
        )
        self.system1 = System1DiscoveryBackend(
            url=settings.system1_url,
            model=settings.system1_model,
            api_key=settings.system1_api_key,
            threshold=settings.system1_threshold,
            hybrid_prefilter=False,
            http_client=http_client,
            fallback=self.bm25,
        )
        self.hybrid_system1 = System1DiscoveryBackend(
            url=settings.system1_url,
            model=settings.system1_model,
            api_key=settings.system1_api_key,
            threshold=settings.system1_threshold,
            hybrid_prefilter=True,
            http_client=http_client,
            fallback=self.bm25,
        )

    def _select(self, mode: DiscoveryBackendMode) -> DiscoveryBackend:
        mapping: dict[DiscoveryBackendMode, DiscoveryBackend] = {
            "keyword": self.keyword,
            "bm25": self.bm25,
            "embedding": self.embedding,
            "system1": self.system1,
            "hybrid_system1": self.hybrid_system1,
        }
        return mapping[mode]

    async def discover(
        self,
        query: str,
        tools: list[DiscoveredTool],
        *,
        catalog_version: str = "",
        backend: DiscoveryBackendMode | None = None,
        limit: int = 15,
    ) -> DiscoveryOutcome:
        mode = backend or self.settings.discovery_backend
        return await self._select(mode).search(
            query, tools, catalog_version=catalog_version, limit=limit
        )
