#!/usr/bin/env python3
"""发布侧：生成 update_manifest.json（热更新完整性清单）。

背景（安全加固 #1 / B1）：`/api/do-update` 已经内置「若仓库含 update_manifest.json
就逐文件核对 sha256/size，不符则整体中止、不落盘」的校验（见 web/meta._plan_update_files）。
但这只有当仓库里**真的带**这份清单时才生效。本脚本在发布时生成它，对用户完全零感知——
用户端热更新会自动多一道防篡改/防下坏的保险。

清单只覆盖热更新会覆盖的 src/ 与 frontend/。路径用 repo 相对（如 "src/server.py"），
与 _plan_update_files 的候选键一致。

用法（发布流程里跑一次，把产物一并提交/发布）：
    python deploy/gen_update_manifest.py               # 写到 <repo>/update_manifest.json
    python deploy/gen_update_manifest.py --check        # 只校验现有清单是否与当前代码一致（CI 用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRACKED_DIRS = ("src", "frontend")
_MANIFEST_PATH = os.path.join(_REPO_ROOT, "update_manifest.json")


def _iter_files(repo_root: str):
    for top in _TRACKED_DIRS:
        base = os.path.join(repo_root, top)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            # 跳过缓存/编译产物，别把 __pycache__ 写进清单
            if "__pycache__" in root.split(os.sep):
                continue
            for fn in files:
                if fn.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, repo_root).replace(os.sep, "/")
                yield rel, full


class ManifestSourceError(RuntimeError):
    """清单无法按仓库内容生成时抛出——宁可不出清单，也不出错的清单。"""


def _reject_unstaged_inputs(repo_root: str) -> None:
    """拒绝会让工作区与待发布 index 分叉的真实未暂存内容。"""

    proc = subprocess.run(
        [
            "git",
            "-C",
            repo_root,
            "diff-files",
            "--name-only",
            "-z",
            "--",
            "VERSION",
            *_TRACKED_DIRS,
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ManifestSourceError(f"无法检查 Git 工作区状态：{detail or 'git diff-files 失败'}")

    candidates = [os.fsdecode(path) for path in proc.stdout.split(b"\0") if path]
    changed = []
    for rel in candidates:
        worktree_path = os.path.join(repo_root, *rel.split("/"))
        if not os.path.isfile(worktree_path):
            changed.append(rel)
            continue

        index_proc = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--verify", f":{rel}"],
            capture_output=True,
        )
        worktree_proc = subprocess.run(
            [
                "git",
                "-C",
                repo_root,
                "hash-object",
                f"--path={rel}",
                "--",
                rel,
            ],
            capture_output=True,
        )
        if index_proc.returncode != 0 or worktree_proc.returncode != 0:
            changed.append(rel)
            continue

        index_oid = index_proc.stdout.strip().lower()
        prospective_oid = worktree_proc.stdout.strip().lower()
        if not index_oid or index_oid != prospective_oid:
            changed.append(rel)

    if changed:
        preview = "、".join(changed[:5])
        if len(changed) > 5:
            preview += f" 等 {len(changed)} 个文件"
        raise ManifestSourceError(
            f"存在未暂存的发布文件改动：{preview}。"
            "请先 git add VERSION src frontend，再生成更新清单。"
        )


def repo_bytes(repo_root: str, rel: str) -> bytes:
    """读这个文件在 Git 仓库里实际存储的字节（优先 index，回退 HEAD）。

    热更新校验的是 GitHub 源码归档里的成员字节，而归档携带的是仓库存储内容，
    不是某台机器的工作区内容。这两者在 Windows 上并不相同：core.autocrlf=true
    的检出里工作区是 CRLF，仓库里可能是 LF，逐字节读工作区会比归档每行多一个
    \\r。真实事故：清单首次入库后，206 个文件里有 170 个大小对不上，热更新整包
    中止在 frontend/onboarding.html（清单记 10819 字节，归档里是 10621）。

    也不能用「文本一律按 LF 归一」这种启发式来换算：本仓库 index 里有 9 个文件
    存的就是 CRLF、30 个存的是混合行尾，归一化会把这 39 个反过来算错。唯一可靠
    的口径是直接问 Git 要字节。

    因此发布顺序是固定的：先 git add（或提交）代码改动，再生成清单——清单描述的
    是仓库里的内容，不是磁盘上的内容。
    """
    for ref in (f":{rel}", f"HEAD:{rel}"):
        proc = subprocess.run(
            ["git", "-C", repo_root, "show", ref],
            capture_output=True,
        )
        if proc.returncode == 0:
            return proc.stdout
    raise ManifestSourceError(
        f"{rel} 不在 Git index 也不在 HEAD 中。清单必须描述仓库内容，"
        "请先 git add 这个文件再生成清单。"
    )


def build_manifest(repo_root: str = _REPO_ROOT) -> dict:
    _reject_unstaged_inputs(repo_root)
    try:
        version = repo_bytes(repo_root, "VERSION").decode("utf-8").strip() or "unknown"
    except UnicodeDecodeError as exc:
        raise ManifestSourceError("VERSION 不是有效的 UTF-8 文本") from exc
    entries = []
    for rel, _full in sorted(_iter_files(repo_root)):
        data = repo_bytes(repo_root, rel)
        entries.append({
            "path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return {
        "version": version,
        "rollout_strategy": "single-node",
        "files": entries,
    }


def _write(manifest: dict, path: str = _MANIFEST_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="生成 / 校验 update_manifest.json")
    ap.add_argument("--check", action="store_true", help="只校验现有清单是否与当前代码一致（不写文件）")
    args = ap.parse_args(argv[1:])

    fresh = build_manifest()
    if args.check:
        if not os.path.isfile(_MANIFEST_PATH):
            print("update_manifest.json 不存在，请先生成。", file=sys.stderr)
            return 1
        with open(_MANIFEST_PATH, "r", encoding="utf-8") as f:
            current = json.load(f)
        if current != fresh:
            print("update_manifest.json 与当前版本或 src/frontend 不一致，请重新生成。", file=sys.stderr)
            return 1
        print("update_manifest.json 与当前代码一致 ✓")
        return 0

    _write(fresh)
    print(f"已生成 {_MANIFEST_PATH}（{len(fresh['files'])} 个文件，version={fresh['version']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
