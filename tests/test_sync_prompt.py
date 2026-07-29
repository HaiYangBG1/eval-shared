"""sync_prompt pull 的写入行为测试（#42③：纯时间戳脏 diff 根治）。"""

from __future__ import annotations

from pathlib import Path

from eval_shared.cli.sync_prompt import _pull


class FakeClient:
    def __init__(self, prompt_payload: dict) -> None:
        self.prompt_payload = prompt_payload

    def get_prompt(self, _name: str, label: str | None = None) -> dict:
        return self.prompt_payload


_PAYLOAD = {
    "type": "chat",
    "version": 3,
    "labels": ["production", "latest"],
    "prompt": [
        {"role": "system", "content": "你是意图识别专家"},
        {"role": "user", "content": "{{query}}"},
    ],
}


def _prompt_path(tmp_path) -> Path:
    return tmp_path / "agents" / "intention" / "prompt.yaml"


def test_pull_writes_file_first_time(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _pull(FakeClient(_PAYLOAD), "intention", None)
    assert _prompt_path(tmp_path).exists()
    assert "✅ v3" in capsys.readouterr().out


def test_pull_unchanged_content_skips_rewrite(monkeypatch, tmp_path, capsys) -> None:
    """内容/版本/标签均无变化 → 跳过写入，时间戳不刷新（无脏 diff）。"""
    monkeypatch.chdir(tmp_path)
    _pull(FakeClient(_PAYLOAD), "intention", None)
    before = _prompt_path(tmp_path).read_text(encoding="utf-8")

    _pull(FakeClient(_PAYLOAD), "intention", None)

    after = _prompt_path(tmp_path).read_text(encoding="utf-8")
    assert after == before  # 逐字节一致，含时间戳行
    assert "跳过写入" in capsys.readouterr().out
    assert not _prompt_path(tmp_path).with_suffix(".yaml.tmp").exists()


def test_pull_content_change_rewrites(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    _pull(FakeClient(_PAYLOAD), "intention", None)

    changed = {**_PAYLOAD, "version": 4, "prompt": [
        {"role": "system", "content": "你是意图识别专家 v4"},
        {"role": "user", "content": "{{query}}"},
    ]}
    _pull(FakeClient(changed), "intention", None)

    text = _prompt_path(tmp_path).read_text(encoding="utf-8")
    assert "v4" in text and "意图识别专家 v4" in text


def test_pull_label_only_change_rewrites(monkeypatch, tmp_path) -> None:
    """标签变化（如 staging 挪动）也要落盘——只有时间戳变化才跳过。"""
    monkeypatch.chdir(tmp_path)
    _pull(FakeClient(_PAYLOAD), "intention", None)

    relabeled = {**_PAYLOAD, "labels": ["production", "staging", "latest"]}
    _pull(FakeClient(relabeled), "intention", None)

    assert "staging" in _prompt_path(tmp_path).read_text(encoding="utf-8")
