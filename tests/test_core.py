"""推送核心（去重/队列/投递/失败落盘）与 OneBot 客户端重试测试."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qqpush.channels import OneBotAdapter
from qqpush.config import Config, GroupTable
from qqpush.core import Deduper, PushService
from qqpush.onebot import OneBotClient, OneBotError

from .conftest import MockOneBot


class FakeClient:
    """记录调用的假 OneBot 客户端."""

    def __init__(self, fail_groups: set[int] | None = None):
        self.calls: list[tuple[int, list[dict]]] = []
        self.fail_groups = fail_groups or set()

    def send_group_msg(self, group_id: int, segments: list[dict]):
        self.calls.append((group_id, segments))
        if group_id in self.fail_groups:
            raise OneBotError("模拟发送失败", retryable=False)
        return 12345

    def login_info(self):
        return {"user_id": 1, "nickname": "bot"}


def make_service(
    tmp_path: Path, cfg: Config, groups: dict[str, int], client
) -> tuple[PushService, GroupTable]:
    path = tmp_path / "groups.json"
    path.write_text(
        json.dumps({"groups": groups, "default_targets": list(groups)[:1]}), encoding="utf-8"
    )
    table = GroupTable(path=path)
    table.load()
    cfg = Config(
        **{**cfg.__dict__, "groups_file": str(path), "failed_log": str(tmp_path / "failed.ndjson")}
    )
    return PushService(cfg, table, OneBotAdapter(table, client)), table


class TestDeduper:
    def test_first_time_is_new(self):
        deduper = Deduper(ttl=10)
        assert deduper.check_and_add("a") is True
        assert deduper.check_and_add("a") is False

    def test_expires_after_ttl(self, monkeypatch):
        deduper = Deduper(ttl=0.05)
        assert deduper.check_and_add("a") is True
        import time

        time.sleep(0.06)
        assert deduper.check_and_add("a") is True

    def test_empty_key_never_dedups(self):
        deduper = Deduper(ttl=10)
        assert deduper.check_and_add("") is True
        assert deduper.check_and_add("") is True

    def test_disabled_when_ttl_zero(self):
        deduper = Deduper(ttl=0)
        assert deduper.check_and_add("a") is True
        assert deduper.check_and_add("a") is True


class TestPushService:
    async def test_send_to_named_group(self, base_cfg, tmp_path):
        client = FakeClient()
        service, _ = make_service(tmp_path, base_cfg, {"运维": 111}, client)
        await service.start()
        result = await service.submit(
            "hello", segments=[{"type": "text", "data": {"text": "hello"}}]
        )
        assert result["accepted"] is True
        await service.stop(drain_timeout=3)
        assert client.calls == [(111, [{"type": "text", "data": {"text": "hello"}}])]
        assert service.stats["sent"] == 1

    async def test_unknown_group_rejected(self, base_cfg, tmp_path):
        service, _ = make_service(tmp_path, base_cfg, {"运维": 111}, FakeClient())
        await service.start()
        result = await service.submit("hi", targets=["不存在的群"])
        await service.stop(drain_timeout=1)
        assert result["accepted"] is False
        assert "未知的群" in result["error"]
        assert result["known"] == ["运维"]

    async def test_no_default_target_rejected(self, base_cfg, tmp_path):
        path = tmp_path / "groups.json"
        path.write_text(json.dumps({"groups": {}}))
        table = GroupTable(path=path)
        table.load()
        service = PushService(base_cfg, table, FakeClient())
        await service.start()
        result = await service.submit("hi")
        await service.stop(drain_timeout=1)
        assert result["accepted"] is False
        assert "没有可用的推送目标" in result["error"]

    async def test_duplicate_id_ignored(self, base_cfg, tmp_path):
        service, _ = make_service(tmp_path, base_cfg, {"运维": 111}, FakeClient())
        await service.start()
        first = await service.submit("hi", dedup_key="evt-1")
        second = await service.submit("hi", dedup_key="evt-1")
        await service.stop(drain_timeout=3)
        assert first["accepted"] is True
        assert second.get("duplicate") is True
        assert service.stats["duplicate"] == 1

    async def test_failure_recorded_to_disk(self, base_cfg, tmp_path):
        service, _ = make_service(tmp_path, base_cfg, {"运维": 111}, FakeClient(fail_groups={111}))
        await service.start()
        await service.submit("boom", segments=[{"type": "text", "data": {"text": "boom"}}])
        await service.stop(drain_timeout=3)
        records = [
            json.loads(line) for line in (tmp_path / "failed.ndjson").read_text().splitlines()
        ]
        assert len(records) == 1
        assert records[0]["target"] == 111
        assert records[0]["channel"] == "onebot"
        assert "模拟发送失败" in records[0]["error"]
        assert service.stats["failed"] == 1

    async def test_partial_failure_keeps_other_groups(self, base_cfg, tmp_path):
        client = FakeClient(fail_groups={222})
        service, _ = make_service(tmp_path, base_cfg, {"A": 111, "B": 222}, client)
        await service.start()
        result = await service.submit("hi", targets=["A", "B"])
        await service.stop(drain_timeout=3)
        assert result["accepted"] is True
        assert sorted(g for g, _ in client.calls) == [111, 222]
        assert service.stats["sent"] == 1 and service.stats["failed"] == 1

    async def test_job_status_tracked(self, base_cfg, tmp_path):
        service, _ = make_service(tmp_path, base_cfg, {"运维": 111}, FakeClient())
        await service.start()
        result = await service.submit("hi")
        await service.stop(drain_timeout=3)
        job = service.jobs.get(result["job_id"])
        assert job is not None
        assert job["state"] == "sent"
        assert job["results"][0]["message_id"] == 12345

    async def test_queue_full_rejects(self, base_cfg, tmp_path):
        client = FakeClient()
        service, _ = make_service(tmp_path, base_cfg, {"运维": 111}, client)
        # 不启动 worker，直接塞满队列
        service._queue = asyncio.Queue(maxsize=1)
        first = await service.submit("1")
        second = await service.submit("2")
        assert first["accepted"] is True
        assert second["accepted"] is False
        assert "繁忙" in second["error"]

    async def test_probe_reports_error_when_onebot_down(self, base_cfg, tmp_path):
        class BrokenClient(FakeClient):
            def login_info(self):
                raise OneBotError("连不上", retryable=False)

        service, _ = make_service(tmp_path, base_cfg, {"运维": 111}, BrokenClient())
        health = await service.probe()
        assert health["onebot"] == "error"


class TestOneBotClient:
    def test_send_group_msg_success(self, mock_onebot: MockOneBot):
        client = OneBotClient(mock_onebot.url, timeout=3, retries=0, rate_per_sec=0)
        assert client.send_group_msg(555, [{"type": "text", "data": {"text": "hi"}}]) == 1000
        assert mock_onebot.sent_messages()[0]["group_id"] == 555

    def test_token_is_sent(self, mock_onebot: MockOneBot):
        client = OneBotClient(mock_onebot.url, token="secret", timeout=3, retries=0, rate_per_sec=0)
        client.send_group_msg(1, [{"type": "text", "data": {"text": "x"}}])
        auth = mock_onebot.requests[0]["headers"].get("Authorization")
        assert auth == "Bearer secret"

    def test_no_retry_on_4xx(self):
        calls = {"n": 0}

        def responder(action, payload, index):
            calls["n"] += 1
            return 403, {"status": "failed", "retcode": 1403, "message": "token 错误"}

        mock = MockOneBot(responder=responder).start()
        try:
            client = OneBotClient(mock.url, timeout=3, retries=3, rate_per_sec=0)
            with pytest.raises(OneBotError):
                client.send_group_msg(1, [{"type": "text", "data": {"text": "x"}}])
            assert calls["n"] == 1
        finally:
            mock.stop()

    def test_retries_5xx_then_succeeds(self):
        state = {"n": 0}

        def responder(action, payload, index):
            state["n"] += 1
            if state["n"] == 1:
                return 500, {"status": "failed", "retcode": -1, "message": "内部错误"}
            return 200, {"status": "ok", "retcode": 0, "data": {"message_id": 7}}

        mock = MockOneBot(responder=responder).start()
        try:
            client = OneBotClient(mock.url, timeout=3, retries=2, rate_per_sec=0)
            assert client.send_group_msg(1, [{"type": "text", "data": {"text": "x"}}]) == 7
            assert state["n"] == 2
        finally:
            mock.stop()

    def test_retry_exhausted_raises(self):
        def responder(action, payload, index):
            return 500, {"status": "failed", "retcode": -1, "message": "一直失败"}

        mock = MockOneBot(responder=responder).start()
        try:
            client = OneBotClient(mock.url, timeout=3, retries=1, rate_per_sec=0)
            with pytest.raises(OneBotError) as exc:
                client.send_group_msg(1, [{"type": "text", "data": {"text": "x"}}])
            assert "一直失败" in str(exc.value)
            assert len(mock.requests) == 2
        finally:
            mock.stop()

    def test_connection_error_is_wrapped(self):
        client = OneBotClient("http://127.0.0.1:1", timeout=1, retries=0, rate_per_sec=0)
        with pytest.raises(OneBotError) as exc:
            client.send_group_msg(1, [{"type": "text", "data": {"text": "x"}}])
        assert "连接失败" in str(exc.value)
        assert exc.value.retryable is True
