from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


_CODE_DIRS = ("src", "frontend")
# 与 entrypoint.sh 的 SEED_DIRS_OPTIONAL 保持一致：这些目录会被播种到运行时树，
# 因此必须计入指纹——否则改了它们指纹不变，entrypoint 判定 image-match 而跳过
# 播种，运行时目录永远停留在第一次播种时的版本（诊断读的正是运行时目录）。
# 与 _CODE_DIRS 的区别是「缺失不算错」：老镜像和老运行时都没有这几个目录。
_OPTIONAL_CODE_DIRS = ("docs", "tools", "kernel")
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _code_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for name in _CODE_DIRS + _OPTIONAL_CODE_DIRS:
        code_root = root / name
        if not code_root.is_dir():
            if name in _OPTIONAL_CODE_DIRS:
                continue
            raise FileNotFoundError(f"missing code directory: {code_root}")
        for path in code_root.rglob("*"):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix in _IGNORED_SUFFIXES:
                continue
            if path.is_file():
                files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def fingerprint_code_tree(root: str | Path) -> str:
    """Return a stable SHA-256 for the runtime source and frontend trees.

    Relative paths are part of the digest so renames are detected. Runtime bytecode
    is excluded because importing a module must not make a deployed tree appear to
    be a different image build.
    """
    root_path = Path(root).resolve()
    digest = hashlib.sha256()
    for path in _code_files(root_path):
        relative = path.relative_to(root_path).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fingerprint Ombre Brain runtime code")
    parser.add_argument("root", help="image or persisted runtime root")
    args = parser.parse_args(argv)
    try:
        print(fingerprint_code_tree(args.root))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"code fingerprint failed: {exc}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by entrypoint integration
    raise SystemExit(main())
