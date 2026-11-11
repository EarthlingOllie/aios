from collections.abc import Sequence
from functools import lru_cache

from anyio import to_thread
from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings
from langchain_voyageai import VoyageAIEmbeddings

from aios import usage
from aios.config import get_settings
from aios.datastores.models import EMBEDDING_DIM
from aios.models import UsagePurpose
from aios.usage import Charge


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


async def embed_query(text: str, charge: Charge | None = None) -> list[float]:
    """The query's embedding, its usage charged to `charge` (else the current charge) as a
    search query (`aios.usage`)."""
    vector = list(await get_embedder().aembed_query(text))
    await to_thread.run_sync(usage.record_embedding, charge, [text], UsagePurpose.search_query)
    return vector


async def embed_documents(
    texts: Sequence[str],
    charge: Charge | None = None,
    purpose: UsagePurpose = UsagePurpose.index_embedding,
) -> list[list[float]]:
    """The texts' embeddings, their usage charged to `charge` (else the current charge, if any:
    none records nothing) as `purpose`, a row per batch the embedder sends
    (`embedding_batch_size`)."""
    texts = list(texts)
    vectors = [list(v) for v in await get_embedder().aembed_documents(texts)]
    size = max(1, get_settings().embedding_batch_size)
    for i in range(0, len(texts), size):
        await to_thread.run_sync(usage.record_embedding, charge, texts[i : i + size], purpose)
    return vectors
