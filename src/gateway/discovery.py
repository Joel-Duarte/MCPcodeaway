"""Zero-dependency tool discovery backends: Okapi BM25 (default) and Keyword."""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from typing import Any, Protocol

from pydantic import BaseModel, Field

from gateway.config import DiscoveryBackendMode, GatewaySettings
from gateway.upstream_clients import DiscoveredTool

_WORD_RE = re.compile(r"[a-z0-9]+")


class ScoredTool(BaseModel):
    """Tool ranked by a DiscoveryBackend along with relevance score."""

    tool: DiscoveredTool
    score: float
    stage1_server_score: float | None = None


class DiscoveryOutcome(BaseModel):
    """Structured discovery result including latency."""

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


class DiscoveryEngine:
    """Zero-dependency in-process tool discovery engine (BM25 default + Keyword)."""

    def __init__(
        self,
        settings: GatewaySettings,
        *,
        http_client: Any = None,
    ) -> None:
        _ = http_client
        self.settings = settings
        self.keyword = KeywordDiscoveryBackend()
        self.bm25 = BM25DiscoveryBackend()

    def _select(self, mode: DiscoveryBackendMode) -> DiscoveryBackend:
        if mode == "keyword":
            return self.keyword
        return self.bm25

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
