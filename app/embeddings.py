from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EmbeddingInputType = Literal["document", "query"]


class BailianSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BAILIAN_",
        extra="ignore",
        case_sensitive=False,
    )

    api_key: str | None = None
    api_host: str | None = None
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_dimension: int = 1024
    rerank_model: str = "qwen3-rerank"
    timeout_seconds: float = 60

    @field_validator("api_key", "api_host", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("embedding_dimension")
    @classmethod
    def supported_dimension(cls, value: int) -> int:
        if value not in {256, 512, 768, 1024, 1536, 2048, 2560}:
            raise ValueError("unsupported Bailian embedding dimension")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("BAILIAN_TIMEOUT_SECONDS must be positive")
        return value


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    total_tokens: int
    model: str
    request_id: str | None


def _api_root(api_host: str) -> str:
    value = api_host.strip().rstrip("/")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("BAILIAN_API_HOST must be an https URL")
    path = parsed.path.rstrip("/")
    for suffix in (
        "/compatible-mode/v1/embeddings",
        "/compatible-mode/v1",
        "/api/v1",
    ):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


class BailianEmbeddingClient:
    def __init__(
        self,
        settings: BailianSettings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings or BailianSettings()
        self.transport = transport

    @property
    def endpoint(self) -> str:
        if not self.settings.api_host:
            raise ValueError("BAILIAN_API_HOST is not configured")
        return (
            f"{_api_root(self.settings.api_host)}"
            "/api/v1/services/embeddings/text-embedding/text-embedding"
        )

    def embed(
        self,
        texts: list[str],
        *,
        input_type: EmbeddingInputType,
        instruct: str | None = None,
    ) -> EmbeddingBatch:
        if not self.settings.api_key:
            raise ValueError("BAILIAN_API_KEY is not configured")
        normalized = [text.strip() for text in texts]
        if not normalized or any(not text for text in normalized):
            raise ValueError("embedding texts must be non-empty")
        if len(normalized) > 20:
            raise ValueError("qwen3.7-text-embedding accepts at most 20 texts per request")

        parameters: dict[str, object] = {
            "dimension": self.settings.embedding_dimension,
            "output_type": "dense",
            "text_type": input_type,
        }
        if instruct:
            parameters["instruct"] = instruct.strip()
        payload = {
            "model": self.settings.embedding_model,
            "input": {"texts": normalized},
            "parameters": parameters,
        }
        with httpx.Client(
            timeout=self.settings.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if response.is_error:
            try:
                error_payload = response.json()
                detail = (
                    error_payload.get("message")
                    or error_payload.get("code")
                    or error_payload.get("error", {}).get("message")
                )
            except (TypeError, ValueError):
                detail = None
            raise RuntimeError(
                f"Bailian embedding request failed ({response.status_code})"
                + (f": {detail}" if detail else "")
            )

        result = response.json()
        output = result.get("output") or {}
        raw_items = output.get("embeddings")
        if not isinstance(raw_items, list):
            raw_items = result.get("data")
        if not isinstance(raw_items, list):
            raise RuntimeError("Bailian response does not contain embeddings")
        ordered = sorted(
            raw_items,
            key=lambda item: int(item.get("text_index", item.get("index", 0))),
        )
        vectors: list[tuple[float, ...]] = []
        for item in ordered:
            raw_vector = item.get("embedding")
            if not isinstance(raw_vector, list):
                raise RuntimeError("Bailian response contains an invalid embedding")
            vector = tuple(float(value) for value in raw_vector)
            if len(vector) != self.settings.embedding_dimension:
                raise RuntimeError(
                    "Bailian embedding dimension mismatch: "
                    f"expected {self.settings.embedding_dimension}, got {len(vector)}"
                )
            if not all(math.isfinite(value) for value in vector):
                raise RuntimeError("Bailian embedding contains non-finite values")
            vectors.append(vector)
        if len(vectors) != len(normalized):
            raise RuntimeError(
                f"Bailian returned {len(vectors)} vectors for {len(normalized)} texts"
            )

        usage = result.get("usage") or {}
        return EmbeddingBatch(
            vectors=tuple(vectors),
            total_tokens=int(usage.get("total_tokens") or 0),
            model=str(result.get("model") or self.settings.embedding_model),
            request_id=(
                str(result.get("request_id") or result.get("id"))
                if result.get("request_id") or result.get("id")
                else None
            ),
        )
