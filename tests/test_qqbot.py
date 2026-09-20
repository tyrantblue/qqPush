"""官方 QQ 机器人通道测试：凭据刷新、发送、错误码、事件取 openid、端到端."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from qqpush.app import auto_record_group
from qqpush.channels import OfficialAdapter
from qqpush.config import Config, GroupTable
from qqpush.qqbot_api import BotApiError, OfficialBotClient, RateLimiter, describe_code
from qqpush.qqbot_auth import BotAuthError, TokenManager
from qqpush.qqbot_ws import (
    EVENT_GROUP_ADD_ROBOT,
    EVENT_GROUP_AT_MESSAGE,
    BotEventClient,
    GroupOpenIdRegistry,
    describe_event,
)

from .conftest import MockQQBot, ServerHarness

OPENID = "OPENID_ALERT_0001"


class TestTokenManager:
    def test_fetch_and_cache(self, mock_qqbot: MockQQBot):
        manager = TokenManager(
            "app", "secret", timeout=3, token_url=f"{mock_qqbot.url}/app/getAppAccessToken"
        )
        assert manager.get_token() == "tok-1"
        assert manager.get_token() == "tok-1"  # 命中缓存
        assert mock_qqbot.token_requests == 1

    def test_business_error_raises(self):
        mock = MockQQBot(token_fail_times=99).start()
        try:
            manager = TokenManager(
                "app", "secret", timeout=3, token_url=f"{mock.url}/app/getAppAccessToken"
            )
            with pytest.raises(BotAuthError) as exc:
                manager.get_token()
            assert "100001" in str(exc.value)
            assert exc.value.retryable is True  # 频控可重试
        finally:
            mock.stop()

    def test_invalidate_forces_refresh(self, mock_qqbot: MockQQBot):
        manager = TokenManager(
            "app", "secret", timeout=3, token_url=f"{mock_qqbot.url}/app/getAppAccessToken"
        )
        manager.get_token()
        manager.invalidate()
        manager.get_token()
        assert mock_qqbot.token_requests == 2

    def test_missing_secret_is_fatal(self):
        mock = MockQQBot(token_fail_times=99).start()
        try:
            manager = TokenManager(
                "app", "secret", timeout=3, token_url=f"{mock.url}/app/getAppAccessToken"
            )
            with pytest.raises(BotAuthError):
                manager.get_token()
        finally:
            mock.stop()

    def test_auth_header_format(self, mock_qqbot: MockQQBot):
        manager = TokenManager(
            "app", "secret", timeout=3, token_url=f"{mock_qqbot.url}/app/getAppAccessToken"
        )
        assert manager.auth_header() == {"Authorization": "QQBot tok-1"}


class TestOfficialClient:
    def make_client(self, mock: MockQQBot, **kwargs) -> OfficialBotClient:
        defaults = {
            "base_url": mock.url,
            "timeout": 3,
            "retries": 1,
            "rate_per_sec": 1000,
            "group_rate_per_minute": 0,
        }
        defaults.update(kwargs)
        return OfficialBotClient("app", "secret", **defaults)

    def test_send_group_msg(self, mock_qqbot: MockQQBot):
        client = self.make_client(mock_qqbot)
        message_id = client.send_group_msg(OPENID, [{"type": "text", "data": {"text": "磁盘 95%"}}])
        assert str(message_id).startswith("msg-")
        sent = mock_qqbot.sent_messages()[0]
        assert sent["msg_type"] == 0
        assert sent["content"] == "磁盘 95%"
        assert isinstance(sent["msg_seq"], int)
        assert mock_qqbot.sent_paths()[0] == f"/v2/groups/{OPENID}/messages"

    def test_auth_header_sent(self, mock_qqbot: MockQQBot):
        client = self.make_client(mock_qqbot)
        client.send_group_msg(OPENID, [{"type": "text", "data": {"text": "x"}}])
        auth = [
            r["authorization"]
            for r in mock_qqbot.requests
            if str(r.get("path", "")).endswith("/messages")
        ][0]
        assert auth == "QQBot tok-1"

    def test_msg_seq_increments(self, mock_qqbot: MockQQBot):
        client = self.make_client(mock_qqbot)
        client.send_group_msg(OPENID, [{"type": "text", "data": {"text": "a"}}])
        client.send_group_msg(OPENID, [{"type": "text", "data": {"text": "b"}}])
        seqs = [m["msg_seq"] for m in mock_qqbot.sent_messages()]
        assert seqs[1] > seqs[0], "相同消息重复发送会被官方判重，msg_seq 必须递增"

    def test_segments_flattened_to_text(self, mock_qqbot: MockQQBot):
        client = self.make_client(mock_qqbot)
        client.send_group_msg(
            OPENID,
            [
                {"type": "text", "data": {"text": "看图 "}},
                {"type": "image", "data": {"file": "https://x/a.png"}},
            ],
        )
        content = mock_qqbot.sent_messages()[0]["content"]
        assert "看图" in content and "[图片]" in content

    def test_long_content_truncated(self, mock_qqbot: MockQQBot):
        client = self.make_client(mock_qqbot, max_chars=20)
        client.send_group_msg(OPENID, [{"type": "text", "data": {"text": "x" * 100}}])
        content = mock_qqbot.sent_messages()[0]["content"]
        assert len(content) == 20 and content.endswith("…")

    def test_strip_urls(self, mock_qqbot: MockQQBot):
        client = self.make_client(mock_qqbot, strip_urls=True)
        client.send_group_msg(
            OPENID, [{"type": "text", "data": {"text": "详见 https://ci/build/1"}}]
        )
        content = mock_qqbot.sent_messages()[0]["content"]
        assert "http" not in content
        assert "链接已省略" in content

    def test_fatal_error_not_retried(self):
        mock = MockQQBot(send_error_codes=[40034101]).start()
        try:
            client = self.make_client(mock, retries=3)
            with pytest.raises(BotApiError) as exc:
                client.send_group_msg(OPENID, [{"type": "text", "data": {"text": "x"}}])
            assert exc.value.code == 40034101
            assert exc.value.retryable is False
            assert "机器人非群成员" in str(exc.value)
            assert len(mock.sent_messages()) == 1, "致命错误不应重试"
        finally:
            mock.stop()

    def test_rate_limit_error_is_retried(self):
        mock = MockQQBot(send_error_codes=[40034100] * 10).start()
        try:
            client = self.make_client(mock, retries=2)
            with pytest.raises(BotApiError) as exc:
                client.send_group_msg(OPENID, [{"type": "text", "data": {"text": "x"}}])
            assert exc.value.code == 40034100
            assert len(mock.sent_messages()) == 3, "频控错误应重试到耗尽"
        finally:
            mock.stop()

    def test_recover_after_transient_error(self):
        mock = MockQQBot(send_error_codes=[50055001]).start()
        try:
            client = self.make_client(mock, retries=1)
            assert client.send_group_msg(OPENID, [{"type": "text", "data": {"text": "x"}}])
            assert len(mock.sent_messages()) == 2
        finally:
            mock.stop()

    def test_passive_reply_carries_msg_id(self, mock_qqbot: MockQQBot):
        client = self.make_client(mock_qqbot)
        mid = client.send_passive_reply(OPENID, "ROBOT1.0_MSGID", "被动回复")
        assert str(mid).startswith("msg-")
        sent = mock_qqbot.sent_messages()[0]
        assert sent["msg_id"] == "ROBOT1.0_MSGID"
        assert sent["msg_seq"] == 1
        assert sent["content"] == "被动回复"

    def test_passive_reply_rejects_empty(self, mock_qqbot: MockQQBot):
        with pytest.raises(BotApiError):
            self.make_client(mock_qqbot).send_passive_reply(OPENID, "mid", "  ")

    def test_empty_content_rejected(self, mock_qqbot: MockQQBot):
        client = self.make_client(mock_qqbot)
        with pytest.raises(BotApiError):
            client.send_group_msg(OPENID, [{"type": "text", "data": {"text": "   "}}])

    def test_login_info(self, mock_qqbot: MockQQBot):
        assert self.make_client(mock_qqbot).login_info()["nickname"] == "Arl"

    def test_describe_code_has_chinese_hint(self):
        assert "群成员" in describe_code(40034101)
        assert "频控" in describe_code(40034100)
        assert "URL" in describe_code(40054010)
        assert "主动消息" in describe_code(40034105)
        assert describe_code(99999999) == "未知错误"


class TestRateLimiter:
    def test_global_interval(self):
        limiter = RateLimiter(per_second=50, per_group_per_minute=0)
        import time

        start = time.monotonic()
        for _ in range(3):
            limiter.wait("g")
        assert time.monotonic() - start >= 0.04  # 2 个间隔

    def test_disabled_limits_are_noop(self):
        limiter = RateLimiter(per_second=0, per_group_per_minute=0)
        limiter.wait("g")
        limiter.wait("g")


class TestRegistry:
    def test_load_and_resolve(self, official_groups_file: Path):
        registry = GroupOpenIdRegistry(official_groups_file)
        assert registry.get("运维告警") == OPENID
        assert registry.by_group_number("123456789") == ("运维告警", OPENID)

    def test_simple_string_form(self, tmp_path: Path):
        path = tmp_path / "g.json"
        path.write_text(json.dumps({"group_openids": {"群A": "OPENID_X"}}))
        registry = GroupOpenIdRegistry(path)
        assert registry.get("群A") == "OPENID_X"

    def test_record_writes_back_preserving_groups(self, official_groups_file: Path):
        registry = GroupOpenIdRegistry(official_groups_file)
        registry.record("新群", "OPENID_NEW", "999")
        raw = json.loads(official_groups_file.read_text(encoding="utf-8"))
        assert raw["group_openids"]["新群"]["openid"] == "OPENID_NEW"
        assert "default_targets" in raw  # 原有字段保留
        assert GroupOpenIdRegistry(official_groups_file).get("新群") == "OPENID_NEW"

    def test_record_is_idempotent(self, official_groups_file: Path):
        registry = GroupOpenIdRegistry(official_groups_file)
        assert registry.record("X", "OPENID_1", "1") is True
        assert registry.record("X", "OPENID_1", "1") is False


class TestEventHandling:
    """官方事件里的 group_id 就是 group_openid（不是真实群号），绑定靠排除法."""

    OPENID_REAL = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"  # 实测事件里的形状

    def _table(self, tmp_path: Path, groups: dict, openids: dict | None = None) -> GroupTable:
        path = tmp_path / "g.json"
        path.write_text(
            json.dumps({"groups": groups, "group_openids": openids or {}}, ensure_ascii=False),
            encoding="utf-8",
        )
        table = GroupTable(path=path)
        table.load()
        return table

    def test_bound_by_elimination_when_single_pending_group(self, tmp_path: Path):
        """只剩一个待绑定群时，事件里的 openid 就是它（实测走的就是这条路径）."""
        groups = self._table(tmp_path, {"运维告警": 123456789})
        registry = GroupOpenIdRegistry(tmp_path / "g.json")
        recorded = auto_record_group(
            registry,
            groups,
            EVENT_GROUP_AT_MESSAGE,
            {"group_openid": self.OPENID_REAL, "group_id": self.OPENID_REAL, "content": " 11"},
        )
        assert recorded is True
        assert registry.get("运维告警") == self.OPENID_REAL
        raw = json.loads((tmp_path / "g.json").read_text(encoding="utf-8"))
        assert raw["group_openids"]["运维告警"]["openid"] == self.OPENID_REAL

    def test_binds_by_real_group_number_when_event_provides_one(self, tmp_path: Path):
        groups = self._table(tmp_path, {"运维告警": 123456789, "开发群": 987654321})
        registry = GroupOpenIdRegistry(tmp_path / "g.json")
        recorded = auto_record_group(
            registry,
            groups,
            EVENT_GROUP_AT_MESSAGE,
            {"group_openid": OPENID, "group_id": "987654321"},
        )
        assert recorded is True
        assert registry.get("开发群") == OPENID
        assert registry.get("运维告警") is None

    def test_ambiguous_candidates_are_not_guessed(self, tmp_path: Path):
        """两个群都待绑定时不猜，避免把消息推到错误的群."""
        groups = self._table(tmp_path, {"运维告警": 123456789, "开发群": 987654321})
        registry = GroupOpenIdRegistry(tmp_path / "g.json")
        recorded = auto_record_group(
            registry,
            groups,
            EVENT_GROUP_AT_MESSAGE,
            {"group_openid": self.OPENID_REAL, "group_id": self.OPENID_REAL},
        )
        assert recorded is False
        assert registry.get("运维告警") is None and registry.get("开发群") is None

    def test_multiple_pending_groups_need_manual_binding(self, tmp_path: Path):
        """两个群都没绑定时不猜；先手工绑一个，剩下的那个就能自动认领."""
        groups = self._table(tmp_path, {"运维告警": 111, "开发群": 222})
        registry = GroupOpenIdRegistry(tmp_path / "g.json")
        event = {"group_openid": "OPENID_A", "group_id": "OPENID_A"}
        assert auto_record_group(registry, groups, EVENT_GROUP_AT_MESSAGE, event) is False
        registry.record("运维告警", "OPENID_A", "")
        assert registry.pending_names(groups.names) == ["开发群"]
        assert (
            auto_record_group(
                registry,
                groups,
                EVENT_GROUP_AT_MESSAGE,
                {"group_openid": "OPENID_B", "group_id": "OPENID_B"},
            )
            is True
        )
        assert registry.get("开发群") == "OPENID_B"
        assert sorted(registry.bound_openids()) == ["OPENID_A", "OPENID_B"]

    def test_already_bound_openid_is_ignored(self, tmp_path: Path):
        groups = self._table(tmp_path, {"运维告警": 123456789})
        registry = GroupOpenIdRegistry(tmp_path / "g.json")
        auto_record_group(
            registry, groups, EVENT_GROUP_AT_MESSAGE, {"group_openid": OPENID, "group_id": OPENID}
        )
        assert (
            auto_record_group(
                registry,
                groups,
                EVENT_GROUP_AT_MESSAGE,
                {"group_openid": OPENID, "group_id": OPENID},
            )
            is False
        )

    def test_no_configured_groups_warns_and_skips(self, tmp_path: Path):
        groups = self._table(tmp_path, {})
        registry = GroupOpenIdRegistry(tmp_path / "g.json")
        assert (
            auto_record_group(
                registry,
                groups,
                EVENT_GROUP_AT_MESSAGE,
                {"group_openid": OPENID, "group_id": OPENID},
            )
            is False
        )

    def test_auto_record_on_bot_added(self, tmp_path: Path):
        groups = self._table(tmp_path, {"开发群": 123456})
        registry = GroupOpenIdRegistry(tmp_path / "g.json")
        assert auto_record_group(
            registry,
            groups,
            EVENT_GROUP_ADD_ROBOT,
            {"group_openid": OPENID, "group_id": OPENID},
        )
        assert registry.get("开发群") == OPENID

    def test_pending_names_and_bound_openids(self, tmp_path: Path):
        groups = self._table(tmp_path, {"A": 1, "B": 2})
        registry = GroupOpenIdRegistry(tmp_path / "g.json")
        assert registry.pending_names(groups.names) == ["A", "B"]
        registry.record("A", "OPENID_A", "")
        assert registry.pending_names(groups.names) == ["B"]
        assert registry.bound_openids() == {"OPENID_A"}

    def test_describe_event_mentions_openid(self):
        text = describe_event(EVENT_GROUP_AT_MESSAGE, {"group_openid": OPENID, "content": "hi"})
        assert OPENID in text and "hi" in text

    async def test_event_client_dispatch(self):
        client = BotEventClient(TokenManager("a", "b"))
        seen: list[tuple[str, dict]] = []
        client.on_event = lambda event, data: seen.append((event, data))
        await client._handle(
            {
                "op": 0,
                "s": 1,
                "t": "READY",
                "d": {"session_id": "sess-1", "user": {"username": "bot"}},
            }
        )
        assert client.session_id == "sess-1" and client.connected
        await client._handle(
            {"op": 0, "s": 2, "t": EVENT_GROUP_AT_MESSAGE, "d": {"group_openid": OPENID}}
        )
        assert seen == [(EVENT_GROUP_AT_MESSAGE, {"group_openid": OPENID})]
        assert client.last_seq == 2


class TestOfficialAdapter:
    def test_resolve_by_name(self, official_groups_file: Path):
        registry = GroupOpenIdRegistry(official_groups_file)
        groups = GroupTable(path=official_groups_file)
        groups.load()
        adapter = OfficialAdapter(None, registry, groups)
        assert adapter.resolve("运维告警") == OPENID

    def test_resolve_raw_openid(self, official_groups_file: Path):
        registry = GroupOpenIdRegistry(official_groups_file)
        adapter = OfficialAdapter(None, registry, GroupTable(path=official_groups_file))
        assert adapter.resolve("OPENID_RAW_1234567890") == "OPENID_RAW_1234567890"

    def test_resolve_by_group_number(self, official_groups_file: Path):
        registry = GroupOpenIdRegistry(official_groups_file)
        adapter = OfficialAdapter(None, registry, GroupTable(path=official_groups_file))
        assert adapter.resolve("123456789") == OPENID

    def test_resolve_unknown(self, official_groups_file: Path):
        adapter = OfficialAdapter(
            None, GroupOpenIdRegistry(official_groups_file), GroupTable(path=official_groups_file)
        )
        assert adapter.resolve("不存在的群") is None


OFFICIAL_GROUPS = {
    "group_openids": {"运维告警": {"openid": OPENID, "group_number": "123456789"}},
    "default_targets": ["运维告警"],
}


@pytest.fixture
def official_harness(official_cfg: Config, tmp_path: Path, mock_qqbot: MockQQBot):
    path = tmp_path / "groups.json"
    path.write_text(json.dumps(OFFICIAL_GROUPS, ensure_ascii=False), encoding="utf-8")
    server = ServerHarness(official_cfg, path, mock_qqbot).start()
    try:
        yield server
    finally:
        server.stop()


class TestOfficialEndToEnd:
    def test_push_to_group_name(self, official_harness: ServerHarness, mock_qqbot: MockQQBot):
        status, body = official_harness.post("/push", {"message": "磁盘 95%"})
        assert status == 202, body
        assert body["targets"] == [OPENID]
        assert mock_qqbot.wait_for_messages(1)
        assert mock_qqbot.sent_messages()[0]["content"] == "磁盘 95%"

    def test_targets_are_openids_in_status(
        self, official_harness: ServerHarness, mock_qqbot: MockQQBot
    ):
        _, body = official_harness.post("/push", {"message": "hi"})
        assert mock_qqbot.wait_for_messages(1)
        import time

        deadline = time.time() + 3
        job = None
        while time.time() < deadline:
            _, job = official_harness.get(f"/status/{body['job_id']}")
            if job["state"] != "pending":
                break
            time.sleep(0.05)
        assert job["state"] == "sent"
        assert job["results"][0]["target"] == OPENID

    def test_unknown_group_404_lists_known(self, official_harness: ServerHarness):
        status, body = official_harness.post("/push", {"message": "x", "groups": ["没有这个群"]})
        assert status == 404
        assert "运维告警" in body["known"]

    def test_configured_group_without_openid_returns_409(
        self, tmp_path: Path, official_cfg, mock_qqbot
    ):
        """群里配了群号但还没拿到 openid 时，要给出可执行的下一步，而不是「未知的群」."""
        path = tmp_path / "groups_pending.json"
        path.write_text(
            json.dumps(
                {
                    "groups": {"运维告警": 123456789},
                    "group_openids": {},
                    "default_targets": ["运维告警"],
                }
            ),
            encoding="utf-8",
        )
        server = ServerHarness(official_cfg, path, mock_qqbot).start()
        try:
            status, body = server.post("/push", {"message": "x"})
            assert status == 409, body
            assert "group_openid" in body["error"]
            assert body["pending_openid"] == ["运维告警"]
        finally:
            server.stop()

    def test_openids_endpoint_lists_group_openid(self, official_harness: ServerHarness):
        status, body = official_harness.get("/openids")
        assert status == 200
        assert body["channel"] == "official"
        assert body["openids"] == [{"name": "运维告警", "target": OPENID}]
        assert body["target_hint"]

    def test_groups_endpoint_is_alias_of_openids(self, official_harness: ServerHarness):
        _, a = official_harness.get("/openids")
        _, b = official_harness.get("/groups")
        assert a["openids"] == b["openids"]

    def test_push_by_openid_field(self, official_harness: ServerHarness, mock_qqbot: MockQQBot):
        """传参也支持直接给 group_openid."""
        status, body = official_harness.post("/push", {"message": "hi", "openid": OPENID})
        assert status == 202, body
        assert body["targets"] == [OPENID]
        assert body["group_openid"] == OPENID
        assert mock_qqbot.wait_for_messages(1)

    def test_push_response_maps_back_to_group_name(
        self, official_harness: ServerHarness, mock_qqbot
    ):
        _, body = official_harness.post("/push", {"message": "hi", "groups": ["运维告警"]})
        assert body["names"] == ["运维告警"]
        assert body["group_openid"] == OPENID

    def test_health_reports_channel(self, official_harness: ServerHarness):
        status, body = official_harness.get("/healthz")
        assert status == 200 and body["ok"] is True
        assert body["channel"] == "official"

    def test_group_added_later_is_pickable(
        self, official_harness: ServerHarness, mock_qqbot: MockQQBot
    ):
        """新群通过事件写入后，无需重启即可推送（/reload 热加载）."""
        official_harness.groups_file.write_text(
            json.dumps(
                {
                    "group_openids": {
                        "运维告警": {"openid": OPENID, "group_number": "123456789"},
                        "新群": {"openid": "OPENID_NEW_0003", "group_number": "999"},
                    },
                    "default_targets": ["运维告警"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        official_harness.post("/reload")
        status, body = official_harness.post("/push", {"message": "hi", "groups": ["新群"]})
        assert status == 202 and body["targets"] == ["OPENID_NEW_0003"]
        assert mock_qqbot.wait_for_messages(1)


class TestConfigChannel:
    def test_default_channel_is_official(self, monkeypatch):
        monkeypatch.delenv("QQPUSH_CHANNEL", raising=False)
        assert Config.from_env().channel == "official"
        assert Config.from_env().is_official is True

    def test_unknown_channel_falls_back(self, monkeypatch):
        monkeypatch.setenv("QQPUSH_CHANNEL", "napcat")
        assert Config.from_env().channel == "official"

    def test_onebot_channel(self, monkeypatch):
        monkeypatch.setenv("QQPUSH_CHANNEL", "onebot")
        cfg = Config.from_env()
        assert cfg.channel == "onebot" and cfg.is_official is False

    def test_qqbot_env(self, monkeypatch):
        monkeypatch.setenv("QQBOT_APPID", "1234567890")
        monkeypatch.setenv("QQBOT_SECRET", "s3cret")
        monkeypatch.setenv("QQBOT_STRIP_URLS", "true")
        cfg = Config.from_env()
        assert cfg.qqbot_app_id == "1234567890"
        assert cfg.qqbot_strip_urls is True


class TestOfficialProbe:
    def test_probe_reports_ok(self, official_harness: ServerHarness):
        health = official_harness.get("/healthz")[1]
        assert "stats" in health
        result = official_harness.request("GET", "/healthz")[1]
        assert result["ok"] is True

    async def test_probe_via_service(
        self, official_cfg: Config, official_groups_file: Path, mock_qqbot
    ):
        from qqpush.channels import OfficialAdapter
        from qqpush.core import PushService
        from qqpush.qqbot_api import OfficialBotClient
        from qqpush.qqbot_ws import GroupOpenIdRegistry

        cfg = replace(official_cfg, qqbot_api_base=mock_qqbot.url)
        groups = GroupTable(path=official_groups_file)
        groups.load()
        client = OfficialBotClient(
            "app", "secret", base_url=mock_qqbot.url, timeout=3, retries=0, rate_per_sec=1000
        )
        adapter = OfficialAdapter(client, GroupOpenIdRegistry(official_groups_file), groups)
        service = PushService(cfg, groups, adapter)
        probe = await service.probe()
        assert probe["qqbot"] == "ok"
        assert probe["channel"] == "official"
