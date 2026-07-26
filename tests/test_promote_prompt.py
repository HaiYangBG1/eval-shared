from __future__ import annotations

from click.testing import CliRunner

from eval_shared.cli import promote_prompt
from eval_shared.common.ab_verdict import ABVerdict


class FakeLangfuseClient:
    """模拟 Langfuse 真实 label 语义：label 全局唯一，newLabels 只增/移动、不删除。

    2026-07-24 踩坑定案：PATCH …/versions/{v} 无法从版本上摘标签，
    只能把标签"移动"到别的版本。Fake 必须还原这个语义，
    否则测试会像旧版一样对"剥离生效"给出假阳性。
    """

    def __init__(self, version_labels: dict[int, list[str]], staging_version: int = 7):
        # {version: [labels]}
        self.state: dict[int, list[str]] = {
            v: list(lbs) for v, lbs in version_labels.items()
        }
        self.staging_version = staging_version
        self.update_calls: list[tuple[str, int, list[str]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def _version_with_label(self, label: str) -> int | None:
        for v, lbs in self.state.items():
            if label in lbs:
                return v
        return None

    def get_prompt(self, name: str, label: str | None = None) -> dict:
        v = self._version_with_label(label) if label else max(self.state)
        if v is None:
            import httpx

            req = httpx.Request("GET", "http://fake")
            raise httpx.HTTPStatusError(
                "not found", request=req, response=httpx.Response(404, request=req)
            )
        return {"version": v, "labels": list(self.state[v])}

    def list_prompt_meta(self, name: str) -> dict:
        return {"name": name, "versions": sorted(self.state)}

    def update_prompt_labels(self, name: str, version: int, labels: list[str]) -> dict:
        self.update_calls.append((name, version, list(labels)))
        # 真实语义：把 label 从其它版本移走，加到本版本；不会删除本版本已有标签
        for lb in labels:
            src = self._version_with_label(lb)
            if src is not None and src != version:
                self.state[src].remove(lb)
            if lb not in self.state[version]:
                self.state[version].append(lb)
        return {"version": version, "labels": list(self.state[version])}


def _invoke(monkeypatch, fake_client: FakeLangfuseClient, *args: str):
    monkeypatch.setattr(promote_prompt, "init_env", lambda: None)
    monkeypatch.setattr(promote_prompt, "LangfuseClient", lambda: fake_client)
    return CliRunner().invoke(promote_prompt.main, ["--agent", "intent-agent", *args])


# ── promote + graveyard 剥离 ──


def test_promote_moves_production_and_graveyards_ab_labels(monkeypatch) -> None:
    """production 移到 staging 版本；A/B 状态标签移到最老的非本版本（graveyard）。"""
    fake = FakeLangfuseClient(
        version_labels={
            1: ["production"],
            7: ["staging", "latest", ABVerdict.BETTER.value],
        },
    )
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert "production" in fake.state[7]
    assert ABVerdict.BETTER.value not in fake.state[7]  # 剥离生效
    assert ABVerdict.BETTER.value in fake.state[1]  # 落在 graveyard
    assert "🪦" in result.output
    assert "回读校验通过" in result.output


def test_promote_strips_legacy_ab_label_format(monkeypatch) -> None:
    """旧格式 'A/B ✅ 70.0%→80.0%' 也要被 graveyard 移走。"""
    legacy = "A/B ✅ 70.0%→80.0%"
    fake = FakeLangfuseClient(
        version_labels={1: ["production"], 7: ["staging", legacy]},
    )
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert legacy not in fake.state[7]
    assert legacy in fake.state[1]


def test_promote_preserves_unrelated_labels(monkeypatch) -> None:
    """非 A/B 状态的自定义标签留在 production 版本上，不被移走。"""
    fake = FakeLangfuseClient(
        version_labels={
            1: ["production"],
            7: ["staging", ABVerdict.BETTER.value, "experimental-cohort-x"],
        },
    )
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert "experimental-cohort-x" in fake.state[7]


def test_promote_single_version_warns_without_graveyard(monkeypatch) -> None:
    """只有一个版本时无 graveyard 可用：明确告警而非静默假成功（#21）。"""
    fake = FakeLangfuseClient(
        version_labels={7: ["staging", ABVerdict.BETTER.value]},
    )
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert "graveyard" in result.output
    assert "⚠️" in result.output


def test_promote_reminds_dify_sync_contract(monkeypatch) -> None:
    """PROTOCOL §2.3：production 标签=Dify 实际运行版，promote 后必须提示同步。"""
    fake = FakeLangfuseClient(version_labels={1: [], 7: ["staging"]})
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert "Dify" in result.output
    assert "sync:prompts:pull" in result.output


# ── A/B 门禁（行为不变） ──


def test_promote_blocks_on_new_enum_worse_label(monkeypatch) -> None:
    fake = FakeLangfuseClient(
        version_labels={7: ["staging", "latest", ABVerdict.WORSE.value]},
    )
    result = _invoke(monkeypatch, fake)

    assert result.exit_code != 0
    assert fake.update_calls == []
    assert "A/B ❌" in result.output


def test_promote_blocks_on_legacy_worse_label_format(monkeypatch) -> None:
    fake = FakeLangfuseClient(
        version_labels={7: ["staging", "latest", "A/B ❌ 67.7%→16.1% 回归17"]},
    )
    result = _invoke(monkeypatch, fake)

    assert result.exit_code != 0
    assert fake.update_calls == []


def test_promote_force_bypasses_blocking_verdict(monkeypatch) -> None:
    fake = FakeLangfuseClient(
        version_labels={1: [], 7: ["staging", "latest", ABVerdict.WORSE.value]},
    )
    result = _invoke(monkeypatch, fake, "--force")

    assert result.exit_code == 0, result.output
    # --force 通过门禁后仍要剥离 A/B 标签（失败状态不跟上生产版本）
    assert ABVerdict.WORSE.value not in fake.state[7]
    assert "production" in fake.state[7]


def test_promote_warns_but_proceeds_on_same_verdict(monkeypatch) -> None:
    fake = FakeLangfuseClient(
        version_labels={1: [], 7: ["staging", "latest", ABVerdict.SAME.value]},
    )
    result = _invoke(monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert "🟰" in result.output or "相当" in result.output
    assert "production" in fake.state[7]


# ── Dry-run ──


def test_promote_dry_run_does_not_update_labels(monkeypatch) -> None:
    fake = FakeLangfuseClient(version_labels={7: ["staging", "latest"]})
    result = _invoke(monkeypatch, fake, "--dry-run")

    assert result.exit_code == 0, result.output
    assert fake.update_calls == []


def test_promote_dry_run_still_blocks_on_failed_ab(monkeypatch) -> None:
    """dry-run 也要执行门禁——避免「dry-run 看到能跑就盲推」"""
    fake = FakeLangfuseClient(
        version_labels={7: ["staging", "latest", ABVerdict.WORSE.value]},
    )
    result = _invoke(monkeypatch, fake, "--dry-run")

    assert result.exit_code != 0
    assert fake.update_calls == []
