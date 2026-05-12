from mitmproxy.test import tflow

from checkpoint.proxy.addon import RouteMode
from checkpoint.proxy.routes import register


def _make_flow(host: str, auth: str | None = "Bearer original-secret"):
    f = tflow.tflow()
    f.request.host = host
    f.request.port = 443
    f.request.scheme = "https"
    f.request.headers["Host"] = host
    if auth is None:
        if "Authorization" in f.request.headers:
            del f.request.headers["Authorization"]
    else:
        f.request.headers["Authorization"] = auth
    return f


def setup_module(_):
    register("api.github.com", "http://127.0.0.1:54123")


def test_known_host_is_rewritten_and_authorization_swapped():
    f = _make_flow("api.github.com")
    RouteMode().request(f)
    assert f.request.host == "127.0.0.1"
    assert f.request.port == 54123
    assert f.request.scheme == "http"
    assert f.request.headers["Host"] == "api.github.com"
    assert f.request.headers["Authorization"].startswith("token ghp_")


def test_unknown_host_passes_through():
    f = _make_flow("api.unknown.example")
    before = (f.request.host, f.request.port, f.request.scheme,
              f.request.headers.get("Authorization"))
    RouteMode().request(f)
    after = (f.request.host, f.request.port, f.request.scheme,
             f.request.headers.get("Authorization"))
    assert before == after


def test_missing_authorization_is_added():
    f = _make_flow("api.github.com", auth=None)
    RouteMode().request(f)
    assert f.request.headers["Authorization"].startswith("token ghp_")
