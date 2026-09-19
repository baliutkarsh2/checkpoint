"""Real, unmodified HTTP clients reach the real GitHub twin through the proxy.

Each client runs as a subprocess configured ONLY by ``proxy.client_env()`` —
exactly how an agent under test is launched — and calls
``https://api.github.com`` with a token the twin does not know. The routes come
from checkpoint.proxy.routes, so the credential stamping is the production one.
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import uuid

import httpx
import pytest

from checkpoint.proxy.routes import proxy_routes
from checkpoint.proxy.server import EgressPolicy

from .support import recorded

pytestmark = pytest.mark.integration

_PYTHON_CLIENTS = {
    "httpx": """
import httpx, json, sys
name = sys.argv[1]
auth = {"Authorization": "token not-the-twin-token"}
repo = httpx.post("https://api.github.com/user/repos", json={"name": name}, headers=auth)
repo.raise_for_status()
full = repo.json()["full_name"]
issue = httpx.post(f"https://api.github.com/repos/{full}/issues", json={"title": "via httpx"})
print(json.dumps({"repo": full, "issue": issue.status_code}))
""",
    "requests": """
import json, requests, sys
name = sys.argv[1]
repo = requests.post("https://api.github.com/user/repos", json={"name": name}, timeout=30)
repo.raise_for_status()
full = repo.json()["full_name"]
issue = requests.post(f"https://api.github.com/repos/{full}/issues", json={"title": "via requests"})
print(json.dumps({"repo": full, "issue": issue.status_code}))
""",
    "urllib": """
import json, sys, urllib.request
def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.status, json.load(response)
_, repo = post("https://api.github.com/user/repos", {"name": sys.argv[1]})
status, _ = post(f"https://api.github.com/repos/{repo['full_name']}/issues", {"title": "via urllib"})
print(json.dumps({"repo": repo["full_name"], "issue": status}))
""",
}

# Node >= 24.5 / 22.21 honour HTTPS_PROXY via NODE_USE_ENV_PROXY; older Nodes
# have no built-in proxy support, so the script tunnels by hand — which still
# exercises the proxy and NODE_EXTRA_CA_CERTS trust.
_NODE_CLIENT = r"""
const name = process.argv[1];
const [major, minor] = process.versions.node.split('.').map(Number);
const envProxy = major > 24 || (major === 24 && minor >= 5) || (major === 22 && minor >= 21);
const body = JSON.stringify({ name });
async function viaFetch() {
  const r = await fetch('https://api.github.com/user/repos', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body });
  return [r.status, await r.json()];
}
function viaConnect() {
  const http = require('http'), https = require('https'), tls = require('tls');
  const proxy = new URL(process.env.HTTPS_PROXY);
  return new Promise((resolve, reject) => {
    const req = http.request({ host: proxy.hostname, port: proxy.port, method: 'CONNECT',
                               path: 'api.github.com:443', headers: { host: 'api.github.com:443' } });
    req.on('connect', (res, socket) => {
      if (res.statusCode !== 200) return reject(new Error('CONNECT ' + res.statusCode));
      const post = https.request({
        host: 'api.github.com', path: '/user/repos', method: 'POST',
        headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) },
        createConnection: () => tls.connect({ socket, servername: 'api.github.com' }),
      }, (resp) => {
        let data = '';
        resp.on('data', (c) => { data += c; });
        resp.on('end', () => resolve([resp.statusCode, JSON.parse(data)]));
      });
      post.on('error', reject);
      post.end(body);
    });
    req.on('error', reject);
    req.end();
  });
}
(envProxy ? viaFetch() : viaConnect()).then(
  ([status, json]) => console.log(JSON.stringify({ status, repo: json.full_name })),
  (err) => { console.error(err); process.exit(1); });
"""


@pytest.fixture
def github_proxy(make_proxy, github_twin):
    return make_proxy(proxy_routes({"api.github.com": github_twin}), EgressPolicy.allowlist([]))


def _run(proxy, argv: list[str]) -> dict:
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("all_proxy", "ssl_cert_dir")}
    env.update(proxy.client_env())
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"{argv[0]} failed:\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def _repo_name(client: str) -> str:
    return f"via-{client}-{uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize("client", sorted(_PYTHON_CLIENTS))
def test_python_clients_reach_the_twin(github_proxy, client):
    if client == "requests":
        pytest.importorskip("requests")
    name = _repo_name(client)
    out = _run(github_proxy, [sys.executable, "-c", _PYTHON_CLIENTS[client], name])
    assert out == {"repo": f"default-user/{name}", "issue": 201}
    assert recorded(github_proxy, path="/user/repos", status=201, routed=True)
    assert recorded(github_proxy, path=f"/repos/default-user/{name}/issues", status=201)


def test_curl_reaches_the_twin(github_proxy):
    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("curl is not installed")
    argv = [curl, "-sS", "--fail", "-X", "POST", "https://api.github.com/user/repos",
            "-H", "Content-Type: application/json"]
    version = subprocess.run([curl, "--version"], capture_output=True, text=True).stdout
    if "Schannel" in version:
        # Windows' Schannel builds of curl (Git's and System32's) ignore
        # CURL_CA_BUNDLE and demand revocation data a local CA cannot publish,
        # so they need both spelled out. OpenSSL builds use the env var as-is.
        argv += ["--cacert", github_proxy.client_env()["CURL_CA_BUNDLE"], "--ssl-no-revoke"]
    name = _repo_name("curl")
    out = _run(github_proxy, [*argv, "-d", json.dumps({"name": name})])
    assert out["full_name"] == f"default-user/{name}"


def test_node_reaches_the_twin(github_proxy):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    name = _repo_name("node")
    out = _run(github_proxy, [node, "-e", _NODE_CLIENT, name])
    assert out == {"status": 201, "repo": f"default-user/{name}"}


def test_twin_204_and_state_through_the_proxy(github_proxy, github_twin, ca):
    """A real 204 from the twin keeps the connection usable, and the state change is real."""
    name = _repo_name("state")
    trust = ssl.create_default_context(cafile=str(ca.cert_path))
    with httpx.Client(proxy=github_proxy.client_env()["HTTPS_PROXY"], verify=trust,
                      trust_env=False, timeout=30) as client:
        client.post("https://api.github.com/user/repos", json={"name": name}).raise_for_status()
        refs = f"https://api.github.com/repos/default-user/{name}/git/refs"
        client.post(refs, json={"ref": "refs/heads/topic"}).raise_for_status()
        deleted = client.delete(f"{refs}/heads/topic")
        assert deleted.status_code == 204 and deleted.content == b""
        branches = client.get(f"https://api.github.com/repos/default-user/{name}/branches")
    assert [b["name"] for b in branches.json()] == ["main"]
    state = httpx.get(f"{github_twin}/_state").json()
    assert f"default-user/{name}" in state["repos"]
    recorded(github_proxy, method="DELETE", status=204)
