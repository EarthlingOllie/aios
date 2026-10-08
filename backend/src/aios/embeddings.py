from collections.abc import Sequence
from functools import lru_cache

from pydantic_ai import Embedder
from pydantic_ai.embeddings import EmbeddingSettings

from aios.config import get_settings


@lru_cache
def get_embedder() -> Embedder:
    settings = get_settings()
    return Embedder(
        settings.embedding_model,
        settings=EmbeddingSettings(dimensions=settings.embedding_dimensions),
    )


async def embed_query(text: str) -> list[float]:
    result = await get_embedder().embed_query(text)
    return list(result.embeddings[0])


async def embed_documents(texts: Sequence[str]) -> list[list[float]]:
    batch_size = get_settings().embedding_batch_size
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        result = await get_embedder().embed_documents(texts[start : start + batch_size])
        vectors.extend(list(v) for v in result.embeddings)
    return vectors
