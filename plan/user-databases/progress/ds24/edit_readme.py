p = "README.md"
s = open(p).read()


def rep(a, b):
    global s
    assert a in s, a
    s = s.replace(a, b)


rep(
    """                    provision.py (creating and migrating each store's database), and
                    migrations/ (the store Alembic tree)""",
    """                    models.py (the tables every store has: documents, chunks),
                    provision.py (creating and migrating each store's database), and
                    migrations/ (the store Alembic tree)""",
)
rep(
    """Documents and chunks are still in the platform database: `documents_session` opens a platform
session for them, until they move into the stores. `store_session(id, role)` already opens a
session on the store's own database, as `_rw` or `_ro`.
""",
    """Documents and chunks live in each store, in its `aios` schema (`aios.datastores.models`, made
by the store tree); the platform database has none. `store_session(id, role)` opens a session
on the store's own database: uploads, listing, deletion and ingest as `_rw`, search (the API's
and the document search tools') as `_ro`. Searches have no owner filter: a store holds only its
owner's documents, and its roles can't connect to any other database.

Each role may hold `DATA_STORE_CONNECTION_LIMIT` connections (default 20), and each process
pools up to 3 per store and role, so the limit must be at least 3 x the processes that use
store roles (about 6 on AWS: see `config.py`). A connection past the limit is refused: the API
answers 503 to try again, the ingest task retries, and the search tools tell the model.
""",
)
rep(
    """`vector(1024)` (`EMBEDDING_DIM` in `models.py`), and embeddings are requested at that size, so
`EMBEDDING_MODEL` must support 1024-dimension output. To use a different size, change
`EMBEDDING_DIM`, add a migration that changes the column size, then re-ingest documents.""",
    """`vector(1024)` (`EMBEDDING_DIM` in `datastores/models.py`), and embeddings are requested at
that size, so `EMBEDDING_MODEL` must support 1024-dimension output. To use a different size,
change `EMBEDDING_DIM`, add a store tree migration that changes the column size, then re-ingest
documents.""",
)
rep(
    """- **Uploads are stored in Postgres** (`documents.data`), so the API and worker don't need a
  shared bucket. Move them to S3 if files get large.""",
    """- **Uploads are stored in Postgres** (`documents.data`, in the user's data store), so the API
  and worker don't need a shared bucket. Move them to S3 if files get large.""",
)
rep(
    """  the `data_store_id`, not the user. For now every store is in the platform database and its
  rows are told apart by `owner_id`; giving each store a database of its own changes only
  what's behind the seam.""",
    """  the `data_store_id`, not the user. Each store is a database of its own, so isolating users'
  documents is the database's job, not a filter in every query.""",
)
open(p, "w").write(s)
