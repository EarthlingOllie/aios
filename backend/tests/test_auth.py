import asyncio
from urllib.parse import parse_qs, urlparse


def test_me_requires_session(client):
    assert client.get("/api/me").status_code == 401


def test_login_redirects_to_github_with_state(client):
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 302
    url = urlparse(resp.headers["location"])
    assert url.netloc == "github.com"
    params = parse_qs(url.query)
    assert params["redirect_uri"] == ["http://localhost:5173/auth/callback"]
    assert params["state"]
    # Authlib stashed the state in the signed session cookie.
    assert "aios_session" in resp.cookies


def test_callback_rejects_missing_state(client):
    resp = client.get("/auth/callback?code=x&state=forged", follow_redirects=False)
    assert resp.status_code == 400


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_signing_in_gives_the_user_a_data_store(client, db, monkeypatch):
    from aios import auth
    from aios.models import DataStore, User

    class Response:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    async def authorize_access_token(request):
        return {"access_token": "t"}

    async def get(path, token):
        return Response({"id": -42, "login": "new-user", "email": "new@example.com"})

    on_loop = []

    def data_store_for(user_id, *args):
        # Blocking: must run in the threadpool, not on the event loop.
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real_data_store_for(user_id, *args)

    real_data_store_for = auth.data_store_for
    monkeypatch.setattr(auth.oauth.github, "authorize_access_token", authorize_access_token)
    monkeypatch.setattr(auth.oauth.github, "get", get)
    monkeypatch.setattr(auth, "data_store_for", data_store_for)
    try:
        resp = client.get("/auth/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 303
        assert on_loop == [False]
        user = db.query(User).filter_by(github_id=-42).one()
        assert db.query(DataStore).filter_by(owner_user_id=user.id).count() == 1
    finally:
        from conftest import delete_user

        user = db.query(User).filter_by(github_id=-42).one_or_none()
        if user is not None:
            delete_user(db, user)
