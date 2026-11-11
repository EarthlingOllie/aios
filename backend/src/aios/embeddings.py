from collections.abc import Sequence
from functools import lru_cache

from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings
from langchain_voyageai import VoyageAIEmbeddings

from aios.config import get_settings
from aios.datastores.models import EMBEDDING_DIM


@lru_cache
def get_embedder() -> Embeddings:
    settings = get_settings()
    if settings.scripted_models_enabled:
        # No provider: the same text always embeds to the same vector (`aios.testing`).
        return DeterministicFakeEmbedding(size=EMBEDDING_DIM)
    provider, _, model = settings.embedding_model.partition(":")
    if provider != "voyageai" or not model:
        raise ValueError(
            f"Unsupported EMBEDDING_MODEL {settings.embedding_model!r}; use voyageai:<model>"
        )
    # Reads VOYAGE_API_KEY from the environment.
    return VoyageAIEmbeddings(
        model=model, output_dimension=EMBEDDING_DIM, batch_size=settings.embedding_batch_size
    )


async def embed_query(text: str) -> list[float]:
    return list(await get_embedder().aembed_query(text))


async def embed_documents(texts: Sequence[str]) -> list[list[float]]:
    # Batched by the embedder (`embedding_batch_size`).
    return [list(v) for v in await get_embedder().aembed_documents(list(texts))]
