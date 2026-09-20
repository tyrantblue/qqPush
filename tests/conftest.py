"""测试公共设施：临时群映射、mock OneBot / mock 官方 API、后台起服务的 harness."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from qqpush.channels import OfficialAdapter, OneBotAdapter
from qqpush.config import Config, GroupTable
from qqpush.core import PushService
from qqpush.onebot import OneBotClient
from qqpush.qqbot_api import OfficialBotClient
from qqpush.qqbot_ws import GroupOpenIdRegistry
from qqpush.server import build_server


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MockOneBot:
    """假的 OneBot HTTP 服务，记录收到的 send_group_msg 调用."""

    def __init__(self, responder=None):
        self.requests: list[dict] = []
        self.responder = responder or self._ok
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A003
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = {"raw": body}
                action = self.path.lstrip("/")
                with mock._lock:
                    mock.requests.append(
                        {"action": action, "payload": payload, "headers": dict(self.headers)}
                    )
                    index = len(mock.requests) - 1
                status, response = mock.responder(action, payload, index)
                data = json.dumps(response).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.port = free_port()
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @staticmethod
    def _ok(action, payload, index):
        if action == "get_login_info":
            return 200, {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": 10001, "nickname": "测试机器人"},
            }
        return 200, {"status": "ok", "retcode": 0, "data": {"message_id": 1000 + index}}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def sent_messages(self) -> list[dict]:
        with self._lock:
            return [r["payload"] for r in self.requests if r["action"] == "send_group_msg"]

    def start(self) -> MockOneBot:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait_for_messages(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.sent_messages()) >= count:
                return True
            time.sleep(0.02)
        return False


class ServerHarness:
    """在后台线程里跑 asyncio 事件循环 + HTTP 服务，测试端用 urllib 调用.

    传入 mock（MockOneBot 或 MockQQBot）会自动按 cfg.channel 装配对应适配器。
    """

    def __init__(self, cfg: Config, groups_file: Path, mock, client=None):
        self.cfg = cfg
        self.groups_file = groups_file
        self.mock = mock
        self.port = free_port()
        self.cfg = replace(cfg, host="127.0.0.1", port=self.port, groups_file=str(groups_file))
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._main, daemon=True)
        self.service: PushService | None = None
        self.groups: GroupTable | None = None
        self.adapter = None
        self.error: BaseException | None = None
        self._client = client

    def _build_client(self):
        if self._client is not None:
            return self._client
        if self.cfg.is_official:
            return OfficialBotClient(
                self.cfg.qqbot_app_id,
                self.cfg.qqbot_app_secret,
                base_url=self.mock.url,
                timeout=self.cfg.onebot_timeout,
                retries=self.cfg.onebot_retries,
                rate_per_sec=1000,
                group_rate_per_minute=0,
                max_chars=self.cfg.qqbot_max_chars,
                strip_urls=self.cfg.qqbot_strip_urls,
            )
        return OneBotClient(
            self.mock.url,
            timeout=self.cfg.onebot_timeout,
            retries=self.cfg.onebot_retries,
            rate_per_sec=1000,
        )

    async def _run(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
            self.groups = GroupTable(path=self.groups_file)
            self.groups.load()
            client = self._build_client()
            if self.cfg.is_official:
                registry = GroupOpenIdRegistry(self.groups_file, on_change=self.groups.load)
                self.adapter = OfficialAdapter(client, registry, self.groups)
            else:
                self.adapter = OneBotAdapter(self.groups, client)
            self.service = PushService(self.cfg, self.groups, self.adapter)
            await self.service.start()
            server = build_server(self.cfg, self.service, self.groups, self._loop)
            self._stop = asyncio.Event()
            threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            ).start()
            self._ready.set()
            await self._stop.wait()
            server.shutdown()
            server.server_close()
            await self.service.stop(drain_timeout=3)
        except BaseException as exc:  # noqa: BLE001 - 传到测试线程
            self.error = exc
            self._ready.set()

    def _main(self) -> None:
        asyncio.run(self._run())

    def start(self) -> ServerHarness:
        self._thread.start()
        assert self._ready.wait(timeout=10), "服务启动超时"
        if self.error:
            raise self.error
        return self

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Push-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, (json.loads(raw) if raw else {})

    def post(self, path: str, body: dict | None = None, token: str | None = None):
        return self.request("POST", path, body, token)

    def get(self, path: str, token: str | None = None):
        return self.request("GET", path, None, token)


@pytest.fixture
def mock_onebot():
    server = MockOneBot().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def groups_file(tmp_path: Path) -> Path:
    path = tmp_path / "groups.json"
    path.write_text(
        json.dumps(
            {
                "groups": {"运维告警": 123456, "开发群": 654321},
                "default_targets": ["运维告警"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def base_cfg(tmp_path: Path) -> Config:
    return Config(
        host="127.0.0.1",
        port=0,
        push_token="",
        channel="onebot",
        onebot_base_url="http://127.0.0.1:1",
        onebot_timeout=2.0,
        onebot_retries=0,
        send_rate_limit_per_sec=1000,
        groups_file=str(tmp_path / "groups.json"),
        workers=2,
        queue_maxsize=100,
        dedup_ttl=60.0,
        failed_log=str(tmp_path / "failed.ndjson"),
        convert_markdown=True,
        wrap_code_block=False,
    )


class MockQQBot:
    """假的官方 API：/app/getAppAccessToken、/v2/groups/{openid}/messages、/users/@me."""

    def __init__(self, token_fail_times: int = 0, send_error_codes: list[int] | None = None):
        self.requests: list[dict] = []
        self.token_requests = 0
        self.token_fail_times = token_fail_times
        self.send_error_codes = list(send_error_codes or [])
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A003
                pass

            def _respond(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                with mock._lock:
                    mock.requests.append({"method": "GET", "path": self.path})
                if self.path == "/users/@me":
                    self._respond(200, {"id": "8909", "username": "Arl"})
                else:
                    self._respond(404, {"code": 404, "message": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = {"raw": body}
                with mock._lock:
                    mock.requests.append(
                        {
                            "method": "POST",
                            "path": self.path,
                            "payload": payload,
                            "authorization": self.headers.get("Authorization"),
                        }
                    )
                    index = len(mock.requests)
                if self.path == "/app/getAppAccessToken":
                    with mock._lock:
                        mock.token_requests += 1
                        attempt = mock.token_requests
                    if attempt <= mock.token_fail_times:
                        self._respond(200, {"code": 100001, "message": "Too many requests"})
                    else:
                        self._respond(200, {"access_token": f"tok-{attempt}", "expires_in": "7200"})
                    return
                if self.path.endswith("/messages"):
                    with mock._lock:
                        code = mock.send_error_codes.pop(0) if mock.send_error_codes else 0
                    if code:
                        self._respond(400, {"code": code, "message": f"mock error {code}"})
                    else:
                        self._respond(
                            200, {"id": f"msg-{index}", "timestamp": "2026-01-01T00:00:00+08:00"}
                        )
                    return
                self._respond(404, {"code": 404, "message": "not found"})

        self.port = free_port()
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> MockQQBot:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def sent_messages(self) -> list[dict]:
        with self._lock:
            return [
                r["payload"]
                for r in self.requests
                if r.get("method") == "POST" and str(r.get("path", "")).endswith("/messages")
            ]

    def sent_paths(self) -> list[str]:
        with self._lock:
            return [
                r["path"] for r in self.requests if str(r.get("path", "")).endswith("/messages")
            ]

    def wait_for_messages(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.sent_messages()) >= count:
                return True
            time.sleep(0.02)
        return False


@pytest.fixture
def mock_qqbot():
    server = MockQQBot().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def official_cfg(tmp_path: Path) -> Config:
    """官方通道配置（api_base 由测试覆盖为 mock 地址）."""
    return Config(
        host="127.0.0.1",
        port=0,
        push_token="",
        channel="official",
        qqbot_app_id="10000001",
        qqbot_app_secret="secret",
        qqbot_listen=False,
        qqbot_max_chars=1000,
        qqbot_group_rate_per_minute=0,  # 测试里不限速
        groups_file=str(tmp_path / "groups.json"),
        workers=2,
        queue_maxsize=100,
        dedup_ttl=60.0,
        failed_log=str(tmp_path / "failed.ndjson"),
        convert_markdown=False,
        wrap_code_block=False,
    )


@pytest.fixture
def official_groups_file(tmp_path: Path) -> Path:
    path = tmp_path / "groups_official.json"
    path.write_text(
        json.dumps(
            {
                "group_openids": {
                    "运维告警": {"openid": "OPENID_ALERT_0001", "group_number": "123456789"},
                    "开发群": {"openid": "OPENID_DEV_0002", "group_number": "1125756609"},
                },
                "default_targets": ["运维告警"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def harness(base_cfg: Config, groups_file: Path, mock_onebot: MockOneBot):
    server = ServerHarness(base_cfg, groups_file, mock_onebot).start()
    try:
        yield server
    finally:
        server.stop()
