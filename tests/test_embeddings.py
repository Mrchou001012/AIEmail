from __future__ import annotations

import httpx

from app.embeddings import BailianEmbeddingClient, BailianSettings


def test_bailian_embedding_request_and_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/api/v1/services/embeddings/text-embedding/text-embedding"
        )
        assert request.headers["Authorization"] == "Bearer secret-test-key"
        payload = __import__("json").loads(request.content)
        assert payload["model"] == "qwen3.7-text-embedding"
        assert payload["parameters"] == {
            "dimension": 1024,
            "output_type": "dense",
            "text_type": "document",
        }
        return httpx.Response(
            200,
            json={
                "output": {
                    "embeddings": [
                        {
                            "embedding": [0.0] * 1023 + [1.0],
                            "text_index": 0,
                        }
                    ]
                },
                "usage": {"total_tokens": 12},
                "request_id": "request-1",
            },
        )

    settings = BailianSettings(
        api_key="secret-test-key",
        api_host="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    result = BailianEmbeddingClient(
        settings,
        transport=httpx.MockTransport(handler),
    ).embed(["Test document"], input_type="document")

    assert len(result.vectors) == 1
    assert len(result.vectors[0]) == 1024
    assert result.total_tokens == 12
    assert result.request_id == "request-1"


def test_bailian_accepts_api_host_without_scheme() -> None:
    settings = BailianSettings(
        api_key="secret-test-key",
        api_host="workspace.cn-beijing.maas.aliyuncs.com",
    )

    assert BailianEmbeddingClient(settings).endpoint == (
        "https://workspace.cn-beijing.maas.aliyuncs.com"
        "/api/v1/services/embeddings/text-embedding/text-embedding"
    )
