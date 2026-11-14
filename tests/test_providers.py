from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import unquote

import httpx

from cascade.nodes import FileBrowserConnector, S3Connector, WebDAVConnector


def _row(kind: str, config: dict) -> dict:
    return {
        "id": 1,
        "name": "Test provider",
        "kind": kind,
        "config": json.dumps(config),
        "enabled": 1,
    }


def test_new_providers_appear_in_the_nodes_dialog(panel):
    response = panel.get("/admin/nodes")
    assert response.status_code == 200
    for label in ("S3-compatible", "WebDAV", "Microsoft OneDrive"):
        assert label in response.text
    assert "Adding a node" in response.text
    assert "Required fields are marked" in response.text


def test_s3_requires_bucket_and_credentials(panel):
    response = panel.post(
        "/api/nodes",
        json={"name": "R2", "kind": "s3", "config": {"bucket": "media"}},
    )
    assert response.status_code == 400
    assert "Access key ID is required" in response.text


def test_webdav_requires_password(panel):
    response = panel.post(
        "/api/nodes",
        json={
            "name": "Nextcloud",
            "kind": "webdav",
            "config": {
                "base_url": "https://cloud.example.com/remote.php/dav/files/me",
                "username": "me",
            },
        },
    )
    assert response.status_code == 400
    assert "Password is required" in response.text


def test_onedrive_requires_token(panel):
    response = panel.post(
        "/api/nodes",
        json={"name": "OneDrive", "kind": "onedrive", "config": {}},
    )
    assert response.status_code == 400
    assert "Graph access token is required" in response.text


def test_s3_secrets_are_hidden_after_save(panel):
    response = panel.post(
        "/api/nodes",
        json={
            "name": "R2",
            "kind": "s3",
            "config": {
                "endpoint": "https://account.r2.cloudflarestorage.com",
                "bucket": "media",
                "access_key": "key-id",
                "secret_key": "super-secret",
            },
        },
    )
    assert response.status_code == 200, response.text
    node = response.json()["node"]
    assert node["secrets"]["secret_key"] is True
    assert "secret_key" not in node["config"]
    assert node["capabilities"] == ["direct", "list", "read", "write"]


def test_s3_presigns_path_style_object_urls():
    connector = S3Connector(
        _row(
            "s3",
            {
                "bucket": "media",
                "prefix": "library",
                "access_key": "key-id",
                "secret_key": "secret",
            },
        )
    )
    url = connector._presign("GET", connector._object_key("/folder/movie name.mp4"))
    assert url.startswith(
        "https://s3.us-east-1.amazonaws.com/media/library/folder/movie%20name.mp4?"
    )
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "X-Amz-Signature=" in url
    assert connector._object_key("/other/file.bin") == "library/other/file.bin"


def test_webdav_builds_encoded_urls_and_basic_auth():
    connector = WebDAVConnector(
        _row(
            "webdav",
            {
                "base_url": "https://cloud.example.com/remote.php/dav/files/me",
                "username": "me",
                "password": "secret",
            },
        )
    )
    assert connector._url("/folder/my file.txt") == (
        "https://cloud.example.com/remote.php/dav/files/me/folder/my%20file.txt"
    )
    expected = "Basic " + base64.b64encode(b"me:secret").decode("ascii")
    assert connector._auth_headers()["Authorization"] == expected


def test_filebrowser_quantum_uses_v2_auth_resources_and_source(monkeypatch):
    from cascade import nodes as nodes_module

    base = "https://quantum.test"
    for cache in (
        nodes_module._FB_TOKENS,
        nodes_module._FB_DOWNLOAD_STYLE,
        nodes_module._FB_API_STYLE,
        nodes_module._FB_SOURCES,
    ):
        cache.pop(base, None)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/login":
            return httpx.Response(404)
        if path == "/api/auth/login":
            assert request.url.params["username"] == "admin"
            assert unquote(request.headers["x-password"]) == "p@ss"
            return httpx.Response(200, text="quantum-token")
        assert request.headers["authorization"] == "Bearer quantum-token"
        if path == "/api/settings/sources":
            return httpx.Response(200, json={"media": {"used": 1}})
        if path == "/api/resources":
            if request.method == "GET":
                assert request.url.params["path"] == "/"
                assert request.url.params["source"] == "media"
                return httpx.Response(
                    200,
                    json={
                        "name": "",
                        "path": "/",
                        "type": "directory",
                        "files": [
                            {"name": "Movies", "type": "directory", "size": 0},
                            {
                                "name": "clip.mp4",
                                "type": "blob",
                                "size": 5,
                                "modified": "2026-01-01T00:00:00Z",
                            },
                        ],
                    },
                )
            if request.method == "POST" and request.url.params.get("isDir") == "true":
                assert request.url.params["path"] == "/new"
                assert request.url.params["source"] == "media"
                return httpx.Response(200)
            if request.method == "POST":
                assert request.url.params["path"] == "/hello.txt"
                assert request.url.params["override"] == "true"
                assert request.content == b"hello"
                return httpx.Response(200)
            if request.method == "PATCH":
                payload = json.loads(request.content)
                assert payload["action"] == "rename"
                assert payload["items"] == [
                    {
                        "fromPath": "/hello.txt",
                        "toPath": "/renamed.txt",
                        "fromSource": "media",
                        "toSource": "media",
                    }
                ]
                return httpx.Response(200, json={"succeeded": [], "failed": []})
            if request.method == "DELETE":
                assert request.url.params["path"] == "/renamed.txt"
                assert request.url.params["source"] == "media"
                return httpx.Response(200)
        if path == "/api/resources/download":
            assert request.url.params["file"] == "/clip.mp4"
            assert request.url.params["source"] == "media"
            return httpx.Response(
                200,
                stream=QuantumDownload(),
                headers={"Content-Type": "text/plain", "Content-Length": "7"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    class QuantumDownload(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"quantum"

    connector = FileBrowserConnector(
        _row(
            "filebrowser",
            {
                "base_url": base,
                "username": "admin",
                "password": "p@ss",
            },
        )
    )

    def fake_client(timeout=30.0):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=base,
            timeout=httpx.Timeout(timeout, connect=15.0, read=None),
            follow_redirects=True,
        )

    monkeypatch.setattr(connector, "_client", fake_client)

    async def scenario():
        items = await connector.list("/")
        assert [(item.name, item.is_dir, item.size) for item in items] == [
            ("Movies", True, 0),
            ("clip.mp4", False, 5),
        ]
        assert items[1].path == "/clip.mp4"

        fetched = await connector.fetch("/clip.mp4")
        assert fetched.filename == "clip.mp4"
        assert fetched.size == 7
        assert b"".join([chunk async for chunk in fetched.body]) == b"quantum"

        async def chunks():
            yield b"hello"

        assert await connector.upload("/hello.txt", chunks(), size=5, mime="text/plain") == 5
        await connector.mkdir("/new")
        await connector.rename("/hello.txt", "/renamed.txt")
        await connector.delete("/renamed.txt")

    try:
        asyncio.run(scenario())
    finally:
        for cache in (
            nodes_module._FB_TOKENS,
            nodes_module._FB_DOWNLOAD_STYLE,
            nodes_module._FB_API_STYLE,
            nodes_module._FB_SOURCES,
        ):
            cache.pop(base, None)


def test_filebrowser_legacy_path_api_still_works(monkeypatch):
    from cascade import nodes as nodes_module

    base = "https://legacy-files.test"
    for cache in (
        nodes_module._FB_TOKENS,
        nodes_module._FB_DOWNLOAD_STYLE,
        nodes_module._FB_API_STYLE,
        nodes_module._FB_SOURCES,
    ):
        cache.pop(base, None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"token": "legacy-token"})
        if request.url.path == "/api/resources/":
            assert request.headers["x-auth"] == "legacy-token"
            return httpx.Response(
                200,
                json={"items": [{"name": "old.txt", "path": "/old.txt", "size": 3}]},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    connector = FileBrowserConnector(
        _row(
            "filebrowser",
            {
                "base_url": base,
                "username": "admin",
                "password": "secret",
            },
        )
    )

    def fake_client(timeout=30.0):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=base,
            timeout=httpx.Timeout(timeout, connect=15.0, read=None),
        )

    monkeypatch.setattr(connector, "_client", fake_client)
    items = asyncio.run(connector.list("/"))
    assert [(item.name, item.path, item.size) for item in items] == [
        ("old.txt", "/old.txt", 3)
    ]
    assert nodes_module._FB_API_STYLE[base] == "legacy"
