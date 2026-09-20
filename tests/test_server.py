"""HTTP 接口端到端测试：真实起服务 + mock OneBot."""

from __future__ import annotations

from dataclasses import replace

import pytest

from qqpush.config import Config

from .conftest import ServerHarness


class TestPublicEndpoints:
    def test_healthz(self, harness: ServerHarness):
        status, body = harness.get("/healthz")
        assert status == 200
        assert body["ok"] is True
        assert body["channel"] == "onebot"
        assert body["targets"] == 2
        assert "queue_depth" in body["stats"]

    def test_help_lists_endpoints(self, harness: ServerHarness):
        status, body = harness.get("/")
        assert status == 200
        assert any("POST /push" in key for key in body["endpoints"])

    def test_groups_requires_no_token_when_unset(self, harness: ServerHarness):
        status, body = harness.get("/groups")
        assert status == 200
        assert body["targets"] == ["开发群", "运维告警"]
        assert body["channel"] == "onebot"
        assert body["default_targets"] == ["运维告警"]

    def test_unknown_route_404(self, harness: ServerHarness):
        status, _ = harness.get("/nope")
        assert status == 404


class TestPush:
    def test_push_to_default_group(self, harness: ServerHarness):
        status, body = harness.post("/push", {"message": "磁盘使用率 95%"})
        assert status == 202, body
        assert body["ok"] is True and body["targets"] == [123456]
        assert harness.mock.wait_for_messages(1)
        sent = harness.mock.sent_messages()[0]
        assert sent["group_id"] == 123456
        assert "磁盘使用率 95%" in sent["message"][0]["data"]["text"]

    def test_push_to_multiple_groups(self, harness: ServerHarness):
        status, body = harness.post("/push", {"message": "hi", "groups": ["运维告警", "开发群"]})
        assert status == 202
        assert sorted(body["targets"]) == [123456, 654321]
        assert harness.mock.wait_for_messages(2)
        assert sorted(m["group_id"] for m in harness.mock.sent_messages()) == [123456, 654321]

    def test_push_accepts_raw_group_id(self, harness: ServerHarness):
        status, body = harness.post("/push", {"message": "hi", "groups": ["999888"]})
        assert status == 202
        assert body["targets"] == [999888]
        assert harness.mock.wait_for_messages(1)

    def test_push_batch_messages(self, harness: ServerHarness):
        status, body = harness.post("/push", {"messages": ["第一条", "第二条"]})
        assert status == 202
        assert body["chunks"] == 2
        assert harness.mock.wait_for_messages(2)
        texts = [m["message"][0]["data"]["text"] for m in harness.mock.sent_messages()]
        assert any("第一条" in t for t in texts) and any("第二条" in t for t in texts)

    def test_push_with_title_and_prefix(self, harness: ServerHarness):
        harness.post("/push", {"message": "正文", "title": "告警", "prefix": "[PROD] "})
        assert harness.mock.wait_for_messages(1)
        text = "".join(s["data"]["text"] for s in harness.mock.sent_messages()[0]["message"])
        assert text.startswith("&#91;PROD&#93; 【告警】")
        assert "正文" in text

    def test_push_image_segment(self, harness: ServerHarness):
        status, _ = harness.post(
            "/push",
            {"message": [{"type": "image", "data": {"file": "https://example.com/a.png"}}]},
        )
        assert status == 202
        assert harness.mock.wait_for_messages(1)
        assert harness.mock.sent_messages()[0]["message"][0]["type"] == "image"

    def test_long_message_is_split(self, base_cfg: Config, groups_file, mock_onebot):
        cfg = replace(base_cfg, group_msg_max_chars=20)
        server = ServerHarness(cfg, groups_file, mock_onebot).start()
        try:
            status, body = server.post("/push", {"message": "x" * 95})
            assert status == 202
            assert body["chunks"] >= 5
            assert mock_onebot.wait_for_messages(body["chunks"])
            for message in mock_onebot.sent_messages():
                assert sum(len(s["data"].get("text", "")) for s in message["message"]) <= 20
        finally:
            server.stop()

    def test_idempotent_by_id(self, harness: ServerHarness):
        first = harness.post("/push", {"message": "hi", "id": "evt-1"})
        second = harness.post("/push", {"message": "hi", "id": "evt-1"})
        assert first[0] == 202
        assert second[0] == 200 and second[1]["duplicate"] is True
        assert harness.mock.wait_for_messages(1)
        assert len(harness.mock.sent_messages()) == 1

    def test_unknown_group_404(self, harness: ServerHarness):
        status, body = harness.post("/push", {"message": "hi", "groups": ["不存在"]})
        assert status == 404
        assert "未知的群" in body["error"]
        assert body["known"] == ["开发群", "运维告警"]

    def test_missing_message_400(self, harness: ServerHarness):
        status, body = harness.post("/push", {})
        assert status == 400
        assert "message" in body["error"]

    def test_invalid_json_400(self, harness: ServerHarness):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f"{harness.base}/push", data=b"{not json", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 400

    def test_bad_segment_type_400(self, harness: ServerHarness):
        status, body = harness.post("/push", {"message": [{"type": "eval", "data": {}}]})
        assert status == 400
        assert "不支持的消息段类型" in body["error"]

    def test_groups_field_must_be_list(self, harness: ServerHarness):
        status, _ = harness.post("/push", {"message": "hi", "groups": {"a": 1}})
        assert status == 400


class TestAuth:
    @pytest.fixture
    def secured(self, base_cfg: Config, groups_file, mock_onebot):
        cfg = replace(base_cfg, push_token="s3cret")
        server = ServerHarness(cfg, groups_file, mock_onebot).start()
        try:
            yield server
        finally:
            server.stop()

    def test_rejects_without_token(self, secured: ServerHarness):
        status, body = secured.post("/push", {"message": "hi"})
        assert status == 401
        assert "鉴权失败" in body["error"]

    def test_rejects_wrong_token(self, secured: ServerHarness):
        status, _ = secured.post("/push", {"message": "hi"}, token="wrong")
        assert status == 401

    def test_accepts_header_token(self, secured: ServerHarness):
        status, _ = secured.post("/push", {"message": "hi"}, token="s3cret")
        assert status == 202
        assert secured.mock.wait_for_messages(1)

    def test_accepts_bearer_token(self, secured: ServerHarness):
        import json
        import urllib.request

        req = urllib.request.Request(
            f"{secured.base}/push",
            data=json.dumps({"message": "hi"}).encode(),
            method="POST",
            headers={"Authorization": "Bearer s3cret"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 202
        assert secured.mock.wait_for_messages(1)

    def test_healthz_stays_open(self, secured: ServerHarness):
        assert secured.get("/healthz")[0] == 200


class TestFailureHandling:
    def test_failed_send_recorded_and_status_failed(
        self, base_cfg: Config, groups_file, mock_onebot
    ):
        from .conftest import MockOneBot

        mock_onebot.stop()
        failing = MockOneBot(
            responder=lambda a, p, i: (
                500,
                {"status": "failed", "retcode": -1, "message": "群不存在"},
            )
        ).start()
        try:
            cfg = replace(base_cfg, onebot_retries=0)
            server = ServerHarness(cfg, groups_file, failing).start()
            try:
                status, body = server.post("/push", {"message": "hi"})
                assert status == 202
                job_id = body["job_id"]
                import time

                deadline = time.time() + 5
                job = None
                while time.time() < deadline:
                    _, job = server.get(f"/status/{job_id}")
                    if job["state"] != "pending":
                        break
                    time.sleep(0.05)
                assert job is not None and job["state"] == "failed", job
                assert "群不存在" in job["results"][0]["error"]
                with open(cfg.failed_log, encoding="utf-8") as handle:
                    failed_log = handle.read().strip().splitlines()
                assert len(failed_log) == 1
            finally:
                server.stop()
        finally:
            failing.stop()

    def test_status_unknown_job_404(self, harness: ServerHarness):
        assert harness.get("/status/deadbeef")[0] == 404


class TestReloadGroups:
    def test_reload_picks_up_new_group(self, harness: ServerHarness, groups_file):
        import json

        groups_file.write_text(
            json.dumps(
                {"groups": {"新群": 777777}, "default_targets": ["新群"]}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        status, body = harness.post("/reload")
        assert status == 200 and body["targets"] == 1
        status, body = harness.post("/push", {"message": "hi"})
        assert status == 202 and body["targets"] == [777777]
        assert harness.mock.wait_for_messages(1)
