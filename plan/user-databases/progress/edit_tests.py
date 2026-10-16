import os

os.chdir("/home/user/aios/.claude/worktrees/agent-adf32d3f8ad761786/backend")


def edit(p, pairs):
    s = open(p).read()
    for a, b in pairs:
        assert s.count(a) == 1, (p, a)
        s = s.replace(a, b)
    open(p, "w").write(s)


FAKE1 = '    app.dependency_overrides[current_user] = lambda: User(id=1, github_id=1, login="t")\n'
FAKE7 = '    app.dependency_overrides[current_user] = lambda: User(id=7, github_id=7, login="t")\n'
REAL = "    app.dependency_overrides[current_user] = lambda: user\n"

edit(
    "tests/test_chat.py",
    [
        (
            "def test_chat_streams_ai_sdk_protocol(client):\n" + FAKE1,
            "def test_chat_streams_ai_sdk_protocol(client, user):\n"
            "    # A user in the database: the turn resolves their data store.\n" + REAL,
        ),
        (
            "def test_chat_searches_the_users_documents(client, monkeypatch):",
            "def test_chat_searches_the_users_documents(client, user, monkeypatch):",
        ),
        (FAKE7, REAL),
        (
            '    assert calls == [{"owner_id": 7, "query": "a"}]',
            '    assert calls == [{"data_store_id": data_store_for(user.id), "query": "a"}]',
        ),
        (
            "def test_chat_replays_the_clients_conversation(client):",
            "def test_chat_replays_the_clients_conversation(client, user):",
        ),
        (
            "        ],\n    }\n" + FAKE1 + "    try:\n        with use_model(model(script)):",
            "        ],\n    }\n" + REAL + "    try:\n        with use_model(model(script)):",
        ),
        (
            "from aios.auth import current_user\n",
            "from aios.auth import current_user\nfrom aios.datastores import data_store_for\n",
        ),
    ],
)
edit(
    "tests/test_agents.py",
    [
        (
            "    async def search(owner_id, query, limit):",
            "    async def search(data_store_id, query, limit):",
        )
    ],
)
edit(
    "tests/test_search.py",
    [
        (
            '    assert asyncio.run(hybrid_search(owner_id=1, query="  ")) == []',
            '    assert asyncio.run(hybrid_search(data_store_id=uuid.uuid4(), query="  ")) == []',
        ),
        ("import asyncio\nimport math\n", "import asyncio\nimport math\nimport uuid\n"),
    ],
)
