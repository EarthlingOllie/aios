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
