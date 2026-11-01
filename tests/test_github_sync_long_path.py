"""github_sync.import_from_github 必须能处理超过 Windows MAX_PATH(260) 的深层路径。

找茬会话发现的 bug：恢复备份时写文件用的是裸 open()/os.makedirs()，没有像
utils.atomic_write_text 那样加 `\\\\?\\` 长路径前缀。sanitize 后的深层 domain
路径叠加一个本来就很长的安装目录，在 Windows 上会真的超过 260 字符——这类
路径正是"恢复备份"这个场景最容易撞上的（本地导入是当前进程建的目录，通常
更短；而恢复的是别处导出、路径结构可能更深的备份）。
"""
import base64
import os
import shutil

import httpx
import pytest

from github_sync import GitHubSync
from utils import _win_long_path


class _FakeGitHubApi:
    """把 GitHubSync._request 会调用的四个只读端点录制成固定响应，不发真实网络请求。"""

    def __init__(self, rel_path: str, content: bytes):
        self.rel_path = rel_path
        self.content = content

    def response_for(self, method: str, url: str) -> httpx.Response:
        request = httpx.Request(method, url)
        if "/git/ref/heads/" in url:
            body = {"object": {"sha": "headsha"}}
        elif "/git/commits/headsha" in url:
            body = {"tree": {"sha": "treesha"}}
        elif "/git/trees/treesha" in url:
            body = {
                "tree": [
                    {"type": "blob", "path": f"ombre/{self.rel_path}", "sha": "blobsha"},
                ],
                "truncated": False,
            }
        elif "/git/blobs/blobsha" in url:
            body = {
                "encoding": "base64",
                "content": base64.b64encode(self.content).decode("ascii"),
            }
        else:
            raise AssertionError(f"unexpected GitHub API call: {method} {url}")
        return httpx.Response(200, json=body, request=request)


@pytest.fixture
def clean_deep_tree(tmp_path):
    """测完把深层目录删掉——pytest 自己删不动。

    这个用例故意造超过 MAX_PATH 的路径，而 pytest 的 `rm_rf` 不加长路径前缀，
    于是它清理临时目录时够不着这些路径：轮次目录只增不减，每跑一轮全量套件就在
    临时根里永久留下一份（实测攒到 12 个轮次目录 / 1.68 GB / 58106 个文件，
    其中 14 个目录的路径超过 255 字符、裸 Win32 API 根本访问不到）。

    造得出来就得收得回去，用同一套前缀 API 删。
    """
    yield
    shutil.rmtree(_win_long_path(os.path.abspath(str(tmp_path))), ignore_errors=True)


@pytest.mark.asyncio
async def test_import_from_github_restores_file_past_windows_max_path(
    tmp_path, monkeypatch, clean_deep_tree
):
    # 每层目录名拉满，六层嵌套：跟 tmp_path 拼起来必然超过 260 字符。
    segment = "深层目录名字段_" * 5  # ~35 字符/层
    rel_path = "/".join([segment] * 6) + "/记忆.md"
    combined_len = len(str(tmp_path)) + len(rel_path)
    assert combined_len > 260, f"测试前提不成立，路径不够长: {combined_len}"

    content = "---\nid: deep-restore-test\n---\n\n从很深的备份路径恢复的记忆".encode("utf-8")

    sync = GitHubSync(token="t", repo="someone/repo", branch="main", path_prefix="ombre")
    fake_api = _FakeGitHubApi(rel_path, content)

    async def fake_request(client, method, url, *, json=None, _max_retries=4):
        return fake_api.response_for(method, url)

    monkeypatch.setattr(sync, "_request", fake_request)

    result = await sync.import_from_github(str(tmp_path))

    assert result["ok"] is True
    assert result["imported"] == 1, result
    assert result["skipped"] == 0, result

    # 路径本身超过 260 字符时，不带 `\\?\` 前缀的普通 Win32 API（包括 pathlib
    # 默认行为）在这台机器上读不到它——这正是长路径前缀存在的意义，所以校验
    # 也必须走同一套前缀 API，而不是拿 Path.exists() 直接判。
    restored = os.path.abspath(str(tmp_path.joinpath(*rel_path.split("/"))))
    restored_long = _win_long_path(restored)
    assert os.path.exists(restored_long), f"深层路径文件没有被写出: {restored}"
    with open(restored_long, "rb") as f:
        assert f.read() == content
