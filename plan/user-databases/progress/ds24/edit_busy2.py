p = "tests/test_data_stores.py"
s = open(p).read()


def rep(a, b):
    global s
    assert a in s, a
    s = s.replace(a, b)


rep(
    '''    """`user`'s store, with its read-write role holding every connection it may."""
    store = db.scalar(select(DataStore).where(DataStore.owner_user_id == user.id))
    names = provision.StoreNames.of(store)
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url), autocommit=True) as provisioner:
        provisioner.execute(
            sql.SQL("ALTER ROLE {} CONNECTION LIMIT 1").format(sql.Identifier(names.rw))
        )
    with store_engine(store.id, "rw").connect():
        yield store.id
''',
    '''    """`user`'s store, with its roles holding every connection they may."""
    store = db.scalar(select(DataStore).where(DataStore.owner_user_id == user.id))
    names = provision.StoreNames.of(store)
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url), autocommit=True) as provisioner:
        for role in (names.rw, names.ro):
            provisioner.execute(
                sql.SQL("ALTER ROLE {} CONNECTION LIMIT 1").format(sql.Identifier(role))
            )
    with store_engine(store.id, "rw").connect(), store_engine(store.id, "ro").connect():
        yield store.id
''',
)
s += '''

def test_the_search_tools_tell_the_model_the_store_is_busy(busy_store, monkeypatch):
    async def embed_query(text):
        raise AssertionError("not embedded for a store that can't be searched")

    results = asyncio.run(
        search.document_search_results(data_store_id=busy_store, query="fish")
    )

    assert results == [{"error": search.STORE_BUSY}]
'''
open(p, "w").write(s)
