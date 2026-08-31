"""MediaStore 的两条硬边界：路径必须在临时目录内、条数必须先卡再动手。

与 test_media_store.py 的分工：那个验的是正常保存路径，这个验的是拒绝。

两条都来自真机复现：

- `media.path` 原来只查文件类型、符号链接和大小，没有任何路径白名单，
  `..` 和绝对路径畅通无阻——服务器上任何可读文件都能被复制进 vault 变成
  记忆附件。
- 10 万个媒体项会被逐个持久化完（实测 35.9 秒）才由 bucket_manager 的
  `_normalize_media` 截成 20 条。上限一直存在，只是执行在动手之后。
"""

import base64

import pytest

from ombrebrain.storage.media_store import MediaPersistenceError, MediaStore


def _建store(tmp_path):
    return MediaStore(str(tmp_path / "vault"), str(tmp_path / "vault" / "_media"))


def _一项(data="合成媒体内容"):
    原始 = data.encode("utf-8") if isinstance(data, str) else data
    return {"data_base64": base64.b64encode(原始).decode("ascii"),
            "filename": "合成.txt"}


@pytest.mark.asyncio
async def test_临时目录内的路径照常接受(tmp_path):
    """反面用例，必须先立在这里。

    path 这条路本来就是给「客户端刚上传、还躺在服务器临时目录里」的文件用的。
    白名单要是把它们也挡了，等于把功能删掉——pytest 的 tmp_path 正好在系统
    临时目录下，所以这条同时也是对白名单口径的锚定。
    """
    源 = tmp_path / "合成上传.txt"
    源.write_text("合成内容", encoding="utf-8")
    store = _建store(tmp_path)

    结果 = await store.persist("合成桶", [{"path": str(源)}])
    assert 结果[0]["stored"] is True


@pytest.mark.asyncio
async def test_临时目录之外的路径被拒(tmp_path, monkeypatch):
    """把允许根收窄成 vault 自己，再拿 tmp 下的文件去试——它就在允许根之外了。

    不直接拿 /etc/passwd 之类真实文件当靶子：那样测试本身就在读系统文件，
    而且不同机器上未必存在。
    """
    源 = tmp_path / "合成外部.txt"
    源.write_text("这个不该被读走", encoding="utf-8")
    store = _建store(tmp_path)
    monkeypatch.setattr(store, "allowed_roots", ((tmp_path / "vault").resolve(),))

    with pytest.raises(MediaPersistenceError, match="不在允许的临时目录内"):
        await store.persist("合成桶", [{"path": str(源)}])


@pytest.mark.asyncio
async def test_带parent的穿越路径同样按真实位置判定(tmp_path, monkeypatch):
    """`..` 不能变成绕过手段：判定用的是解析后的真实路径。"""
    源 = tmp_path / "合成外部.txt"
    源.write_text("这个不该被读走", encoding="utf-8")
    store = _建store(tmp_path)
    monkeypatch.setattr(store, "allowed_roots", ((tmp_path / "vault").resolve(),))
    穿越 = str(tmp_path / "vault" / ".." / "合成外部.txt")

    with pytest.raises(MediaPersistenceError, match="不在允许的临时目录内"):
        await store.persist("合成桶", [{"path": 穿越}])


@pytest.mark.asyncio
async def test_超过条数上限在写第一个字节之前就拒绝(tmp_path):
    store = _建store(tmp_path)
    媒体目录 = tmp_path / "vault" / "_media"

    with pytest.raises(MediaPersistenceError, match="一次最多"):
        await store.persist("合成桶", [_一项()] * (store.max_items + 1))

    落盘 = list(媒体目录.glob("**/*")) if 媒体目录.exists() else []
    assert not [p for p in 落盘 if p.is_file()], "被拒的调用不该留下任何媒体文件"


@pytest.mark.asyncio
async def test_正好等于上限仍然接受(tmp_path):
    """边界的另一侧：卡的是「超过」，不是「接近」。"""
    store = _建store(tmp_path)
    结果 = await store.persist(
        "合成桶", [_一项(f"第{i}项") for i in range(store.max_items)]
    )
    assert len(结果) == store.max_items


@pytest.mark.asyncio
async def test_precheck只看不写(tmp_path):
    """precheck 的存在是为了让 hold 在写原文之前就知道媒体能不能读。

    它必须真的不落盘，否则「先校验再提交」就成了「写两遍」。
    """
    store = _建store(tmp_path)
    媒体目录 = tmp_path / "vault" / "_media"

    await store.precheck([_一项()])
    落盘 = list(媒体目录.glob("**/*")) if 媒体目录.exists() else []
    assert not [p for p in 落盘 if p.is_file()]

    with pytest.raises(MediaPersistenceError, match="Base64"):
        await store.precheck([{"data_base64": "@@@不是base64@@@", "filename": "x"}])
