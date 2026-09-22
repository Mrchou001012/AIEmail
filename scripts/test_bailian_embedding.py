from __future__ import annotations

import json
import math

from app.embeddings import BailianEmbeddingClient, BailianSettings


def main() -> None:
    settings = BailianSettings()
    result = BailianEmbeddingClient(settings).embed(
        [
            (
                "Customer requests a quotation for 500 kg of a chemical product "
                "and asks for availability and lead time."
            )
        ],
        input_type="document",
    )
    vector = result.vectors[0]
    print(
        json.dumps(
            {
                "success": True,
                "model": result.model,
                "dimension": len(vector),
                "vectors": len(result.vectors),
                "input_tokens": result.total_tokens,
                "l2_norm": round(math.sqrt(sum(value * value for value in vector)), 6),
                "request_id_present": bool(result.request_id),
                "secret_printed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
