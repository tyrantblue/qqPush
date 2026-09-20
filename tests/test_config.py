"""配置解析与群映射表测试."""

from __future__ import annotations

import json
from pathlib import Path

from qqpush.config import Config, GroupTable


class TestConfig:
    def test_from_env_defaults(self, monkeypatch):
        for key in list(Config.__dataclass_fields__):
            monkeypatch.delenv(key, raising=False)
        cfg = Config.from_env()
        assert cfg.port == 8088
        assert cfg.onebot_base_url == "http://127.0.0.1:3000"
        assert cfg.workers >= 1

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("QQPUSH_PORT", "9999")
        monkeypatch.setenv("ONEBOT_BASE_URL", "http://napcat:3000/")
        monkeypatch.setenv("QQPUSH_WORKERS", "128")
        monkeypatch.setenv("QQPUSH_MARKDOWN", "false")
        cfg = Config.from_env()
        assert cfg.port == 9999
        assert cfg.onebot_base_url == "http://napcat:3000"  # 去掉结尾斜杠
        assert cfg.workers == 64  # 上限收敛
        assert cfg.convert_markdown is False

    def test_invalid_int_falls_back(self, monkeypatch):
        monkeypatch.setenv("QQPUSH_PORT", "not-a-number")
        assert Config.from_env().port == 8088


class TestGroupTable:
    def test_load_and_resolve_by_name(self, tmp_path: Path):
        path = tmp_path / "groups.json"
        path.write_text(
            json.dumps({"groups": {"运维": 111, "开发": 222}, "default_targets": ["运维"]})
        )
        table = GroupTable(path=path)
        assert table.load() == 2
        assert table.resolve("运维") == 111
        assert table.default_targets == ["运维"]
        assert table.names == ["开发", "运维"]

    def test_resolve_accepts_raw_id(self, tmp_path: Path):
        table = GroupTable(path=tmp_path / "missing.json")
        table.load()
        assert table.resolve("87654321") == 87654321
        assert table.resolve("not-exist") is None

    def test_missing_file_is_not_fatal(self, tmp_path: Path):
        table = GroupTable(path=tmp_path / "nope.json")
        assert table.load() == 0
        assert table.names == []

    def test_bad_group_id_skipped(self, tmp_path: Path):
        path = tmp_path / "groups.json"
        path.write_text(json.dumps({"groups": {"好群": 123, "坏群": "abc"}}, ensure_ascii=False))
        table = GroupTable(path=path)
        assert table.load() == 1
        assert table.resolve("坏群") is None

    def test_reload_picks_up_changes(self, tmp_path: Path):
        path = tmp_path / "groups.json"
        path.write_text(json.dumps({"groups": {"a": 1}}))
        table = GroupTable(path=path)
        table.load()
        path.write_text(json.dumps({"groups": {"a": 1, "b": 2}}))
        assert table.load() == 2
        assert table.resolve("b") == 2
