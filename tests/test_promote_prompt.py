from __future__ import annotations

from click.testing import CliRunner

from eval_shared.cli import promote_prompt
from eval_shared.common.ab_verdict import ABVerdict


class FakeLangfuseClient:
    def __init__(self, labels: list[str] | None = None):
        self.updated: tuple[str, int, list[str]] | None = None
        self._labels = labels if labels is not None else [
            "staging",
            "latest",
            ABVerdict.BETTER.value,
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get_prompt(self, name: str, label: str | None = None) -> dict:
        assert name == "intent-agent-prompt"
        assert label == "staging"
        return {"version": 7, "labels": list(self._labels)}

    def update_prompt_labels(self, name: str, version: int, labels: list[str]) -> dict:
        self.updated = (name, version, labels)
        return {"version": version, "labels": labels}


def _invoke(monkeypatch, fake_client: FakeLangfuseClient, *args: str):
    monkeypatch.setattr(promote_prompt, "init_env", lambda: None)
    monkeypatch.setattr(promote_prompt, "LangfuseClient", lambda: fake_client)
    return CliRunner().invoke(promote_prompt.main, ["--agent", "intent-agent", *args])


# ── 剥离规则 ──


def test_promote_strips_reserved_and_ab_state_labels(monkeypatch) -> None:
    """staging/latest 是流程标签，A/B 枚举是评估状态——production 版本上都不该出现。"""
    fake = FakeLangfuseClient(labels=["staging", "latest", ABVerdict.BETTER.value])
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert fake.updated == ("intent-agent-prompt", 7, ["production"])


def test_promote_strips_legacy_ab_label_format(monkeypatch) -> None:
    """历史 prompt 上可能存在旧格式 'A/B ✅ 70.0%→80.0%'，promote 时也要清理。"""
    fake = FakeLangfuseClient(labels=["staging", "latest", "A/B ✅ 70.0%→80.0%"])
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert fake.updated == ("intent-agent-prompt", 7, ["production"])


def test_promote_preserves_unrelated_labels(monkeypatch) -> None:
    """非保留 / 非 A/B 评估状态的 label（如自定义业务标签）应保留到 production。"""
    fake = FakeLangfuseClient(
        labels=["staging", "latest", ABVerdict.BETTER.value, "experimental-cohort-x"],
    )
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert fake.updated == ("intent-agent-prompt", 7, ["experimental-cohort-x", "production"])


# ── A/B 门禁 ──


def test_promote_blocks_on_new_enum_worse_label(monkeypatch) -> None:
    fake = FakeLangfuseClient(labels=["staging", "latest", ABVerdict.WORSE.value])
    result = _invoke(monkeypatch, fake)

    assert result.exit_code != 0
    assert fake.updated is None
    assert "A/B ❌" in result.output


def test_promote_blocks_on_legacy_worse_label_format(monkeypatch) -> None:
    """历史 prompt 上可能有 'A/B ❌ 67.7%→16.1% 回归17' 旧格式，门禁也要识别。"""
    fake = FakeLangfuseClient(
        labels=["staging", "latest", "A/B ❌ 67.7%→16.1% 回归17"],
    )
    result = _invoke(monkeypatch, fake)

    assert result.exit_code != 0
    assert fake.updated is None


def test_promote_force_bypasses_blocking_verdict(monkeypatch) -> None:
    fake = FakeLangfuseClient(labels=["staging", "latest", ABVerdict.WORSE.value])
    result = _invoke(monkeypatch, fake, "--force")

    assert result.exit_code == 0, result.output
    # --force 通过门禁后仍要剥离 A/B 标签（不让失败状态跟上生产）
    assert fake.updated == ("intent-agent-prompt", 7, ["production"])


def test_promote_warns_but_proceeds_on_same_verdict(monkeypatch) -> None:
    """🟰 状态：候选与基线相当，给 warning 但不阻塞。"""
    fake = FakeLangfuseClient(labels=["staging", "latest", ABVerdict.SAME.value])
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert "🟰" in result.output or "相当" in result.output
    assert fake.updated == ("intent-agent-prompt", 7, ["production"])


# ── Dry-run ──


def test_promote_dry_run_does_not_update_labels(monkeypatch) -> None:
    fake = FakeLangfuseClient()
    result = _invoke(monkeypatch, fake, "--dry-run")

    assert result.exit_code == 0, result.output
    assert fake.updated is None


def test_promote_dry_run_still_blocks_on_failed_ab(monkeypatch) -> None:
    """dry-run 也要执行门禁——避免「dry-run 看到能跑就盲推」"""
    fake = FakeLangfuseClient(labels=["staging", "latest", ABVerdict.WORSE.value])
    result = _invoke(monkeypatch, fake, "--dry-run")

    assert result.exit_code != 0
    assert fake.updated is None
