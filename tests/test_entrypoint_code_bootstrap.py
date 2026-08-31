import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None,
    reason="entrypoint behavior test requires a POSIX shell",
)


def _prepare_image(root: Path, source: str = "IMAGE = 'one'\n") -> None:
    shutil.copytree(ROOT / "src", root / "src")
    (root / "src" / "server.py").write_text(source, encoding="utf-8")
    (root / "frontend").mkdir(parents=True)
    (root / "frontend" / "dashboard.html").write_text("<h1>image</h1>\n", encoding="utf-8")
    (root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    (root / "requirements.txt").write_text("package>=1\n", encoding="utf-8")
    (root / "requirements.lock.txt").write_text("package==1\n", encoding="utf-8")
    (root / "config.default.yaml").write_text("buckets_dir: ./buckets\n", encoding="utf-8")


def _run(
    image_root: Path,
    code_dir: Path,
    data_dir: Path,
    **extra_env: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "OMBRE_IMAGE_ROOT": str(image_root),
        "OMBRE_CODE_DIR": str(code_dir),
        "OMBRE_BUCKETS_DIR": str(data_dir),
        "OMBRE_CONFIG_PATH": str(data_dir / "config.yaml"),
        "OMBRE_BOOTSTRAP_ONLY": "1",
        **extra_env,
    })
    return subprocess.run(
        ["sh", str(ROOT / "entrypoint.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_same_version_changed_image_reseeds_but_unchanged_image_preserves_hot_update(tmp_path):
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image)

    first = _run(image, code, data)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (code / "src" / "server.py").read_text(encoding="utf-8") == "IMAGE = 'one'\n"
    assert (code / ".seeded_image_fingerprint").is_file()
    assert (code / "requirements.txt").read_text(encoding="utf-8") == "package>=1\n"
    assert (code / "requirements.lock.txt").read_text(encoding="utf-8") == "package==1\n"

    # Dashboard hot update: image did not change, so the persisted runtime must win.
    (code / "src" / "server.py").write_text("HOT_UPDATE = True\n", encoding="utf-8")
    (code / "requirements.txt").write_text("hot-package>=1\n", encoding="utf-8")
    (code / "requirements.lock.txt").write_text("hot-package==1\n", encoding="utf-8")
    unchanged = _run(image, code, data)
    assert unchanged.returncode == 0, unchanged.stdout + unchanged.stderr
    assert (code / "src" / "server.py").read_text(encoding="utf-8") == "HOT_UPDATE = True\n"
    assert (code / "requirements.lock.txt").read_text(encoding="utf-8") == "hot-package==1\n"

    # A locally rebuilt image with the same VERSION must still replace the old baseline.
    (image / "src" / "server.py").write_text("IMAGE = 'two'\n", encoding="utf-8")
    (image / "requirements.txt").write_text("package>=2\n", encoding="utf-8")
    (image / "requirements.lock.txt").write_text("package==2\n", encoding="utf-8")
    rebuilt = _run(image, code, data)
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    assert (code / "src" / "server.py").read_text(encoding="utf-8") == "IMAGE = 'two'\n"
    assert (code / "requirements.txt").read_text(encoding="utf-8") == "package>=2\n"
    assert (code / "requirements.lock.txt").read_text(encoding="utf-8") == "package==2\n"
    assert (code / "_prev" / "requirements.txt").read_text(encoding="utf-8") == "hot-package>=1\n"
    assert (code / "_prev" / "requirements.lock.txt").read_text(encoding="utf-8") == "hot-package==1\n"
    assert "代码指纹" in rebuilt.stdout


def test_non_active_legacy_data_app_is_reported_without_deletion(tmp_path):
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image)
    legacy = data / "_app"
    (legacy / "src").mkdir(parents=True)
    (legacy / "src" / "server.py").write_text("LEGACY = True\n", encoding="utf-8")
    (legacy / "VERSION").write_text("2.4.6\n", encoding="utf-8")

    result = _run(image, code, data)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "旧布局代码遗留" in result.stdout
    assert "未被当前进程使用" in result.stdout
    assert (legacy / "src" / "server.py").is_file()


def test_directory_config_path_fails_closed_without_deleting_contents(tmp_path):
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    mistaken_config = data / "mistaken-config"
    remembered = mistaken_config / "permanent" / "must-survive.md"
    _prepare_image(image)
    remembered.parent.mkdir(parents=True)
    remembered.write_text("irreplaceable memory\n", encoding="utf-8")

    result = _run(
        image,
        code,
        data,
        OMBRE_CONFIG_PATH=str(mistaken_config),
    )

    assert result.returncode == 1
    assert "refusing to delete" in result.stdout
    assert remembered.read_text(encoding="utf-8") == "irreplaceable memory\n"


def test_image_seed_failure_keeps_existing_runtime_tree(tmp_path):
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image)
    assert _run(image, code, data).returncode == 0
    before = (code / "src" / "server.py").read_bytes()

    shutil.rmtree(image / "frontend")
    failed = _run(image, code, data, OMBRE_FORCE_CODE_RESEED="1")

    assert "持久卷代码不可用" in failed.stdout
    assert (code / "src" / "server.py").read_bytes() == before


def test_crash_rollback_is_not_immediately_overwritten_by_same_image(tmp_path):
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image, "IMAGE = 'known-good'\n")
    assert _run(image, code, data).returncode == 0

    # Same VERSION, new image content. Seeding keeps the healthy prior runtime in _prev.
    (image / "src" / "server.py").write_text("IMAGE = 'crashing'\n", encoding="utf-8")
    (image / "requirements.txt").write_text("crashing-package>=2\n", encoding="utf-8")
    (image / "requirements.lock.txt").write_text("crashing-package==2\n", encoding="utf-8")
    assert _run(image, code, data).returncode == 0
    assert (code / "_prev" / "src" / "server.py").read_text(encoding="utf-8") == (
        "IMAGE = 'known-good'\n"
    )
    assert (code / "_prev" / "requirements.txt").read_text(encoding="utf-8") == "package>=1\n"
    assert (code / "_prev" / "requirements.lock.txt").read_text(encoding="utf-8") == "package==1\n"

    # Simulate two failed service starts. The next entrypoint pass must restore _prev
    # and treat it as a persisted override instead of reseeding the same bad image.
    (code / ".boot_fails").write_text("2\n", encoding="utf-8")
    rolled_back = _run(image, code, data)

    assert rolled_back.returncode == 0, rolled_back.stdout + rolled_back.stderr
    assert "回滚到上一版代码" in rolled_back.stdout
    assert "reason=image-fingerprint-changed" not in rolled_back.stdout
    assert (code / "src" / "server.py").read_text(encoding="utf-8") == (
        "IMAGE = 'known-good'\n"
    )
    assert (code / "requirements.txt").read_text(encoding="utf-8") == "package>=1\n"
    assert (code / "requirements.lock.txt").read_text(encoding="utf-8") == "package==1\n"


# ============================================================
# 可选目录（docs/ tools/）的播种
#
# adr_requirements 与 preflight_cli_diagnostics 两项系统诊断在运行时目录
# <vault>/_app 下分别读 docs/adr/ 与 tools/vnext_preflight.py。此前 seed 只
# 搬 src/ frontend/ 与三个 root 文件，这两项诊断在任何 Docker 部署上恒红。
# 老镜像没有这两个目录，因此必须「有就搬、没有也能正常启动」。
# ============================================================

def _add_optional_dirs(root: Path, adr_body: str = "ADR one\n", cli_body: str = "CLI one\n") -> None:
    (root / "docs" / "adr").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "adr" / "ADR-0001.md").write_text(adr_body, encoding="utf-8")
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "vnext_preflight.py").write_text(cli_body, encoding="utf-8")


def test_optional_dirs_are_seeded_into_runtime_tree(tmp_path):
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image)
    _add_optional_dirs(image)

    result = _run(image, code, data)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (code / "docs" / "adr" / "ADR-0001.md").read_text(encoding="utf-8") == "ADR one\n"
    assert (code / "tools" / "vnext_preflight.py").read_text(encoding="utf-8") == "CLI one\n"


def test_image_without_optional_dirs_still_boots(tmp_path):
    """老镜像没有 docs/ tools/：不能因为缺失就启动失败。"""
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image)

    result = _run(image, code, data)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (code / "src" / "server.py").is_file()
    assert not (code / "docs").exists()
    assert not (code / "tools").exists()


def test_optional_dirs_refresh_on_reseed(tmp_path):
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image)
    _add_optional_dirs(image)
    assert _run(image, code, data).returncode == 0

    # 同版本、新内容 → 换代播种必须把可选目录也刷新
    (image / "src" / "server.py").write_text("IMAGE = 'two'\n", encoding="utf-8")
    _add_optional_dirs(image, adr_body="ADR two\n", cli_body="CLI two\n")
    result = _run(image, code, data)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (code / "docs" / "adr" / "ADR-0001.md").read_text(encoding="utf-8") == "ADR two\n"
    assert (code / "tools" / "vnext_preflight.py").read_text(encoding="utf-8") == "CLI two\n"


def test_rollback_restores_optional_dirs_from_prev(tmp_path):
    """崩溃回滚后运行时树必须与 _prev 完全一致，可选目录不能留着新版本。"""
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image, "IMAGE = 'known-good'\n")
    _add_optional_dirs(image)
    assert _run(image, code, data).returncode == 0

    (image / "src" / "server.py").write_text("IMAGE = 'crashing'\n", encoding="utf-8")
    _add_optional_dirs(image, adr_body="ADR crashing\n", cli_body="CLI crashing\n")
    assert _run(image, code, data).returncode == 0
    assert (code / "docs" / "adr" / "ADR-0001.md").read_text(encoding="utf-8") == "ADR crashing\n"

    (code / ".boot_fails").write_text("2\n", encoding="utf-8")
    rolled_back = _run(image, code, data)

    assert rolled_back.returncode == 0, rolled_back.stdout + rolled_back.stderr
    assert "回滚到上一版代码" in rolled_back.stdout
    assert (code / "docs" / "adr" / "ADR-0001.md").read_text(encoding="utf-8") == "ADR one\n"
    assert (code / "tools" / "vnext_preflight.py").read_text(encoding="utf-8") == "CLI one\n"


def test_rollback_drops_optional_dirs_absent_from_prev(tmp_path):
    """_prev 是升级前的老运行时（没有 docs/tools）：回滚必须把它们删干净。"""
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image, "IMAGE = 'old-good'\n")
    assert _run(image, code, data).returncode == 0
    assert not (code / "docs").exists()

    # 升级到带可选目录的新镜像；老运行时成为 _prev
    (image / "src" / "server.py").write_text("IMAGE = 'new-crashing'\n", encoding="utf-8")
    _add_optional_dirs(image)
    assert _run(image, code, data).returncode == 0
    assert (code / "docs" / "adr" / "ADR-0001.md").is_file()

    (code / ".boot_fails").write_text("2\n", encoding="utf-8")
    rolled_back = _run(image, code, data)

    assert rolled_back.returncode == 0, rolled_back.stdout + rolled_back.stderr
    assert (code / "src" / "server.py").read_text(encoding="utf-8") == "IMAGE = 'old-good'\n"
    assert not (code / "docs").exists(), "回滚后不该残留新镜像带来的 docs/"
    assert not (code / "tools").exists(), "回滚后不该残留新镜像带来的 tools/"


def test_seed_log_keeps_code_dir_path_and_valid_utf8(tmp_path):
    """播种日志里的 CODE_DIR 路径不能被 shell 变量展开吃掉。

    `$CODE_DIR：` 中的全角冒号是多字节字符，shell 会把它的首字节当作变量名的
    一部分 → 展开成空、并吐出半个字符。运维因此看不到代码播种到哪，输出还是
    非法 UTF-8（`subprocess(text=True)` 直接抛 UnicodeDecodeError）。
    """
    image = tmp_path / "image"
    code = tmp_path / "code" / "_app"
    data = tmp_path / "data"
    _prepare_image(image)

    raw = subprocess.run(
        ["sh", str(ROOT / "entrypoint.sh")],
        env={
            **os.environ,
            "OMBRE_IMAGE_ROOT": str(image),
            "OMBRE_CODE_DIR": str(code),
            "OMBRE_BUCKETS_DIR": str(data),
            "OMBRE_CONFIG_PATH": str(data / "config.yaml"),
            "OMBRE_BOOTSTRAP_ONLY": "1",
        },
        capture_output=True,
        check=False,
    )

    assert raw.returncode == 0
    raw.stdout.decode("utf-8")  # 非法字节会在这里抛 UnicodeDecodeError
    text = raw.stdout.decode("utf-8")
    assert "播种代码到持久卷" in text
    assert str(code) in text, "日志必须带上真实的 CODE_DIR 路径"
