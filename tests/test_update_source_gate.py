"""热更新来源白名单 + 自动 pip 安装闸门（安全加固 #2）。

do-update 会把远端 zip 覆盖到 src/ 并（旧行为）自动 pip install，等于把「谁能改
config.update」放大成 RCE。默认只信官方仓、自动 pip 默认关闭。
"""
import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import web.meta as meta
import web._shared as sh


@pytest.fixture(autouse=True)
def _restore():
    old = sh.config
    yield
    sh.config = old


def test_official_repo_allowed(monkeypatch):
    monkeypatch.delenv("OMBRE_ALLOW_CUSTOM_UPDATE_REPO", raising=False)
    assert meta._update_repo_allowed("P0luz/Ombre-Brain")
    assert meta._update_repo_allowed("p0luz/ombre-brain")   # 大小写不敏感
    assert meta._update_repo_allowed("/P0luz/Ombre-Brain/")  # 容忍多余斜杠


def test_foreign_repo_rejected_by_default(monkeypatch):
    monkeypatch.delenv("OMBRE_ALLOW_CUSTOM_UPDATE_REPO", raising=False)
    assert not meta._update_repo_allowed("attacker/evil")
    assert not meta._update_repo_allowed("p0luz/ombre-brain-evil")


def test_foreign_repo_allowed_via_optin(monkeypatch):
    monkeypatch.setenv("OMBRE_ALLOW_CUSTOM_UPDATE_REPO", "1")
    assert meta._update_repo_allowed("myfork/ombre-brain")


def test_pip_install_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OMBRE_UPDATE_ALLOW_PIP", raising=False)
    monkeypatch.setattr(sh, "config", {})
    assert meta._pip_install_allowed() is False


def test_pip_install_enabled_via_config(monkeypatch):
    monkeypatch.delenv("OMBRE_UPDATE_ALLOW_PIP", raising=False)
    monkeypatch.setattr(sh, "config", {"update": {"allow_pip_install": True}})
    assert meta._pip_install_allowed() is True


def test_pip_install_enabled_via_env(monkeypatch):
    monkeypatch.setattr(sh, "config", {})
    monkeypatch.setenv("OMBRE_UPDATE_ALLOW_PIP", "1")
    assert meta._pip_install_allowed() is True


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        ("2.6.1", "v2.4.6", True),
        ("2.6.1", "2.6.1", False),
        ("2.6.1", "2.7.0", False),
        ("2.6", "2.6.0", False),
        ("dev", "2.4.6", False),
    ],
)
def test_hot_update_downgrade_guard(current, target, expected):
    assert meta._is_version_downgrade(current, target) is expected


def test_hot_update_defaults_to_same_main_branch_as_version_check():
    source = open(meta.__file__, encoding="utf-8").read()
    assert '_ucfg.get("channel") or "branch"' in source


def test_ci_lock_verification_freezes_package_index_snapshot():
    repo_root = Path(meta.__file__).resolve().parents[2]
    workflow = (repo_root / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )
    _, remainder = workflow.split("      - name: Verify dependency lockfiles", 1)
    step = remainder.split("\n      - name:", 1)[0]

    match = re.search(
        r"(?m)^\s+UV_EXCLUDE_NEWER:\s*['\"]([^'\"]+)['\"]\s*$",
        step,
    )
    assert match, "lock 校验必须固定包索引时间，避免无输入变更时发生解析漂移"
    cutoff_text = match.group(1)
    assert cutoff_text.endswith("Z"), "lock 索引时间必须使用明确的 UTC 时间"
    cutoff = datetime.fromisoformat(cutoff_text[:-1] + "+00:00")
    assert cutoff.tzinfo == timezone.utc
    assert cutoff <= datetime.now(timezone.utc)

    assert step.count("uv pip compile ") == 2
    assert "--upgrade" not in step
    assert "--exclude-newer" not in step, (
        "cutoff 必须通过环境变量传入，避免 uv 把参数写进 lock 头部造成纯文本漂移"
    )
    reset_command = "rm -f requirements.lock.txt requirements-dev.lock.txt"
    assert reset_command in step, "lock 校验必须从空输出重建，不能依赖已有 pin 偏好"
    assert step.index(reset_command) < step.index("uv pip compile ")


def test_release_archive_omits_loose_requirements_but_keeps_lock():
    repo_root = Path(meta.__file__).resolve().parents[2]
    attributes = (repo_root / ".gitattributes").read_text(encoding="utf-8")
    active_rules = {
        line.strip()
        for line in attributes.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "/requirements.txt export-ignore" in active_rules
    assert "/requirements.lock.txt export-ignore" not in active_rules
    assert "COPY requirements.lock.txt ./" in (repo_root / "Dockerfile").read_text(
        encoding="utf-8"
    )


def test_legacy_archive_compatibility_requires_284_release_lock():
    """requirements.lock.txt 内容钉死的绊线：任何改动都必须是一次清醒决定，不能顺手改掉。

    2026-08-09：为修复 cryptography==49.0.0 的 PYSEC-2026-3552（无 fix 版本可选，
    只能升级到 50.0.0），推进了 CI 的 UV_EXCLUDE_NEWER 快照并重新生成了两份锁，
    连带升级了 mcp/openai/uvicorn 等包。这是一次明确接受的破坏性变更：仍在跑
    v2.8.4 之前旧更新器逻辑、且尚未升级过一次的部署实例，这次热更新后可能无法
    再走旧的 legacy 依赖回退路径，需要用户手动升级一次。基线哈希已推进到新内容；
    下一次改锁文件时，请再一次有意识地评估这条兼容性问题，而不是让测试直接绿。

    2026-08-25（3.6.1）：新增 `tzdata`。评估结论是这次远轻于 2026-08-09 那次——

    - lock diff 是**纯增量的 4 行**，只多一个 `tzdata==2026.3`，既有包的版本
      一个都没动，解析树没有重新收敛。上次是升 cryptography 连带把
      mcp/openai/uvicorn 一起带上去，那是明确接受的破坏性变更。
    - `tzdata` 是无依赖的纯数据包，不参与任何解析约束。
    - 旧更新器仍会把「依赖变了」判为真并触发一次依赖安装，这条路径本身没变，
      变的只是要多装一个叶子包。

    为什么值得跨这条线：`zoneinfo` 只查宿主机的 IANA 库，`python:*-slim` 与
    Windows 都没有。缺了它 `utils.get_tzinfo()` 会静默兜底成固定 +08:00——
    配 America/New_York 的用户拿到东八区的解锁时间，且没有任何提示。
    详见 3.6.1 的 CHANGELOG。
    """
    repo_root = Path(meta.__file__).resolve().parents[2]
    lock_bytes = (repo_root / "requirements.lock.txt").read_bytes()
    normalized = lock_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    assert hashlib.sha256(normalized).hexdigest() == (
        "ae0255bd41e5ceb45694244017a9b0b2dc1e4c445ab027cec1f7183ad969700a"
    ), (
        "requirements.lock.txt 已变化：确认这次变化是否会影响还没升级过的旧版"
        "热更新器（尤其 v2.8.4 之前、缺少 lock 感知回退逻辑的实例），评估后再把"
        "这里的基线哈希推进到新内容"
    )


def test_dependency_check_uses_release_lock_and_normalizes_line_endings(tmp_path):
    old_source = b"mcp>=1.0.0\r\n"
    new_source = b"mcp>=1.27,<2\n"
    old_lock = b"mcp==1.28.1 \\\r\n    --hash=sha256:abc\r\n"
    new_lock = b"mcp==1.28.1 \\\n    --hash=sha256:abc\n"
    (tmp_path / "requirements.txt").write_bytes(old_source)
    (tmp_path / "requirements.lock.txt").write_bytes(old_lock)

    assert meta._requirements_changed(
        str(tmp_path), new_source, new_lock
    ) is False
    assert meta._requirements_changed(
        str(tmp_path), old_source, b"mcp==1.28.2\n"
    ) is True


def test_dependency_check_falls_back_to_source_for_legacy_archive(tmp_path):
    (tmp_path / "requirements.txt").write_bytes(b"mcp>=1.0.0\r\n")

    assert meta._requirements_changed(
        str(tmp_path), b"mcp>=1.0.0\n", None
    ) is False
    assert meta._requirements_changed(
        str(tmp_path), b"mcp>=1.27,<2\n", None
    ) is True


def test_dependency_check_falls_back_to_configured_image_root(
    monkeypatch, tmp_path
):
    runtime_root = tmp_path / "runtime"
    image_root = tmp_path / "image"
    runtime_root.mkdir()
    image_root.mkdir()
    (image_root / "requirements.lock.txt").write_bytes(b"mcp==1.28.1\r\n")
    monkeypatch.setenv("OMBRE_IMAGE_ROOT", str(image_root))

    assert meta._requirements_changed(
        str(runtime_root),
        b"mcp>=1.27,<2\n",
        b"mcp==1.28.1\n",
    ) is False


def test_dependency_check_without_any_baseline_remains_fail_closed(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("OMBRE_IMAGE_ROOT", raising=False)
    monkeypatch.setattr(sh, "in_docker", lambda: False)
    monkeypatch.setattr(meta, "_runtime_satisfies_locked_versions", lambda _data: False)

    assert meta._requirements_changed(
        str(tmp_path), b"new-package==1\n", b"new-package==1 --hash=sha256:abc\n"
    ) is True
    assert meta._requirements_changed(
        str(tmp_path), None, b"new-package==1 --hash=sha256:abc\n"
    ) is True


def test_new_lock_never_falls_back_to_matching_source_without_lock_baseline(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("OMBRE_IMAGE_ROOT", raising=False)
    monkeypatch.setattr(sh, "in_docker", lambda: False)
    monkeypatch.setattr(meta, "_runtime_satisfies_locked_versions", lambda _data: False)
    source = b"package>=1\n"
    (tmp_path / "requirements.txt").write_bytes(source)

    assert meta._requirements_changed(
        str(tmp_path), source, b"package==1 --hash=sha256:abc\n"
    ) is True


def test_missing_lock_baseline_accepts_exactly_satisfied_runtime(monkeypatch, tmp_path):
    monkeypatch.delenv("OMBRE_IMAGE_ROOT", raising=False)
    monkeypatch.setattr(sh, "in_docker", lambda: False)
    checked = []

    def satisfied(data):
        checked.append(data)
        return True

    monkeypatch.setattr(meta, "_runtime_satisfies_locked_versions", satisfied)
    lock = b"package==1 \\\n    --hash=sha256:" + b"a" * 64 + b"\n"

    assert meta._requirements_changed(str(tmp_path), None, lock) is False
    assert checked == [meta._normalize_dependency_manifest(lock)]


def test_changed_lock_remains_changed_even_if_runtime_probe_would_pass(
    monkeypatch, tmp_path
):
    (tmp_path / "requirements.lock.txt").write_bytes(b"package==1\n")

    def unexpected_probe(_data):
        raise AssertionError("已有 lock 不同属于真实迁移，不应走缺基线兼容探测")

    monkeypatch.setattr(meta, "_runtime_satisfies_locked_versions", unexpected_probe)

    assert meta._requirements_changed(
        str(tmp_path), None, b"package==2 --hash=sha256:" + b"b" * 64 + b"\n"
    ) is True


def test_runtime_lock_probe_is_local_read_only_and_cleans_temp_file(
    monkeypatch, tmp_path
):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        requirement_path = Path(command[-1])
        captured["path"] = requirement_path
        captured["content"] = requirement_path.read_bytes()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    lock = b"package==1 \\\n    --hash=sha256:" + b"c" * 64 + b"\n"

    assert meta._runtime_satisfies_locked_versions(lock) is True
    command = captured["command"]
    assert "--dry-run" in command
    assert "--no-index" in command
    assert "--no-deps" in command
    assert "--require-hashes" in command
    assert "--no-cache-dir" in command
    assert captured["kwargs"]["timeout"] == 60
    assert captured["content"] == lock
    assert not captured["path"].exists()


def test_runtime_lock_probe_accepts_repository_release_lock_syntax(monkeypatch):
    repo_root = Path(meta.__file__).resolve().parents[2]
    captured = {}

    def fake_run(command, **_kwargs):
        captured["content"] = Path(command[-1]).read_bytes()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    lock = (repo_root / "requirements.lock.txt").read_bytes()

    assert meta._runtime_satisfies_locked_versions(lock) is True
    assert b"mcp==1.29.0" in captured["content"]


def test_runtime_lock_probe_nonzero_result_fails_closed_and_cleans_temp(
    monkeypatch,
):
    captured = {}

    def fake_run(command, **_kwargs):
        captured["path"] = Path(command[-1])
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    lock = b"package==1 \\\n    --hash=sha256:" + b"e" * 64 + b"\n"

    assert meta._runtime_satisfies_locked_versions(lock) is False
    assert not captured["path"].exists()


def test_runtime_lock_probe_timeout_and_invalid_utf8_fail_closed(monkeypatch):
    captured = {}

    def timeout(command, **_kwargs):
        captured["path"] = Path(command[-1])
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr(subprocess, "run", timeout)
    lock = b"package==1 \\\n    --hash=sha256:" + b"f" * 64 + b"\n"

    assert meta._runtime_satisfies_locked_versions(lock) is False
    assert not captured["path"].exists()
    assert meta._runtime_satisfies_locked_versions(b"package==1\n\xff") is False


@pytest.mark.parametrize(
    "lock",
    [
        b"--index-url=https://attacker.invalid/simple\npackage==1\n",
        b"package @ https://attacker.invalid/package.whl#sha256=abc\n",
        b"-e ../local-package\n",
        b"package==1\n",
        b"package>=1 --hash=sha256:" + b"d" * 64 + b"\n",
    ],
)
def test_runtime_lock_probe_rejects_unsafe_or_unhashed_syntax(monkeypatch, lock):
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("不安全 lock 不能交给 pip 解析")

    monkeypatch.setattr(subprocess, "run", unexpected_run)
    assert meta._runtime_satisfies_locked_versions(lock) is False


def test_lock_install_enforces_hashes(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "requirements.lock.txt"
    data = b"package==1 --hash=sha256:abc\n"

    result = meta._install_update_requirements(
        str(target), data, require_hashes=True
    )

    assert result.returncode == 0
    assert target.read_bytes() == data
    assert "--require-hashes" in captured["command"]
    assert captured["command"][-2:] == ["-r", str(target)]
