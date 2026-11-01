# ============================================================
# Shared test fixtures — isolated temp environment for all tests
# 共享测试 fixtures —— 为所有测试提供隔离的临时环境
#
# IMPORTANT: All tests run against a temp directory.
# Your real /data or local buckets are NEVER touched.
# 重要：所有测试在临时目录运行，绝不触碰真实记忆数据。
# ============================================================

import os
import sys
import tempfile
import pytest
from pathlib import Path

# ------------------------------------------------------------
# iter 1.8: 必须在任何 src/* 导入之前设置 OMBRE_BUCKETS_DIR
# iter 1.9 F: 统一推荐 OMBRE_VAULT_DIR；测试也优先用新名
# Must set OMBRE_VAULT_DIR / OMBRE_BUCKETS_DIR BEFORE any test
# imports src/server.py, because server.py runs load_config() at
# import time which mkdirs /data.
# ------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_VAULT = tempfile.TemporaryDirectory(prefix="ombrebrain-pytest-")
_TEST_BUCKETS = Path(_TEST_VAULT.name)
# conftest 就是 pytest 边界：即使调用者 shell 里配了真实 vault，也不允许
# 测试进程继承后读写它。两个变量始终指向本进程的 OS 临时目录，
# 避免仓库内 test_buckets/ 跨轮次残留并污染测试。
os.environ["OMBRE_VAULT_DIR"] = str(_TEST_BUCKETS)
os.environ["OMBRE_BUCKETS_DIR"] = str(_TEST_BUCKETS)

# F-09: embedding.enabled=true 时无 key 会拒绝启动。测试环境注入 dummy key，
# 避免 `import server`（模块级导入）触发 SystemExit。
# 真实 API 调用在测试中均被 mock，dummy key 不会发起网络请求。
if not os.environ.get("OMBRE_EMBED_API_KEY"):
    os.environ["OMBRE_EMBED_API_KEY"] = "__test_dummy__"

# 子进程崩溃不能把整场测试拖下水。
#
# Windows 上子进程的 traceback 按控制台代码页写 stderr（这台机器是 GBK）。
# 用户名或路径里有中文，就会产生 UTF-8 解不开的字节；pytest 的捕获层按 UTF-8
# 解，那个 IncrementalDecoder 一旦卡在这段字节上就再也解不出来，而它会被继续
# 复用——**此后每个测试的 setup 和 teardown 各报一次 error**，整场剩下的测试
# 全灭。实测抓到过一次：`1 failed, 332 passed, 97 skipped, 4911 errors`，
# 4911 = 之后每个测试两条。
#
# 让子进程也说 UTF-8：崩溃就只是一条失败，而不是一场灾难。
# 用 setdefault，外面显式配了就听外面的。
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# Ensure src/ is importable
sys.path.insert(0, str(_REPO_ROOT / "src"))

_MISSING = object()

# 别让迁移工作区的清扫线程在测试进程里真的跑起来。
#
# 它是进程级 daemon，起来之后跑满整个 pytest 进程：每 60 秒醒一次，按当时读到的
# `_PARSED_WORKSPACE_TTL_SECONDS` 去删所有已注册 engine 的未应用工作区。而
# `test_migrate_job_state.py` 会把那个模块级常量 monkeypatch 成 10 秒——两件事
# 撞上就是一次谁也复现不了的删除：线程按自己的节拍醒，和测试顺序无关，
# 所以表现成低频、换种子也不稳定的偶发失败。
#
# 没有测试需要它真的在后台跑：要测过期的那条用例是直接调
# `engine._expire_parsed_workspace(...)` 的。而且 migrate_engine 本身就为
# 「起不来 daemon」留了惰性回退（status/reservation 调用里也会做同样的过期检查），
# 所以关掉它不改变任何被测行为。
try:
    import migrate_engine as _migrate_engine

    _migrate_engine._MIGRATE_SWEEPER_STARTED = True
except Exception:  # pragma: no cover - 模块结构变了不该让整个套件起不来
    pass


@pytest.fixture(autouse=True)
def _restore_tool_runtime():
    """每个测试跑完，把 tools/_runtime 的全局装配还原。

    二十多个测试文件直接写 `rt.embedding_engine = ...` 而不是走 monkeypatch，
    于是它留给下一个测试。后果是**静默的**：dream 的 feel 段按融合分挑选，
    向量可用时是 `0.7*向量 + 0.3*关键词`，不可用时关键词独自承担。继承到一个
    「enabled 但查不出东西」的引擎，向量那路恒为 0，门槛就变成事实上的 1.67 倍，
    整段 feel 无声消失——测试不报错，只是断言的东西不见了。

    这不是假想：`test_feel_search_channel.py` 之后跟 `test_dream_prompt_boundary.py`，
    两个文件两秒就能复现两条失败；随机序下命中率约五分之一。

    在这里还原而不是去改那二十多个文件：装配是全局的，边界就该在 conftest，
    而不是指望每个新测试都记得自己收拾。
    """
    from tools import _runtime as rt

    # 全量快照，不挑名字：装配槽以后会增减，漏跟一个就是同一个静默 bug 再来一次。
    # 从没被改过的（含 typing 那些 import）身份比较相等，还原是空操作。
    before = dict(vars(rt))
    yield
    for name, value in before.items():
        if getattr(rt, name, _MISSING) is not value:
            setattr(rt, name, value)
    for name in set(vars(rt)) - set(before):
        delattr(rt, name)


@pytest.fixture
def test_config(tmp_path):
    """
    Minimal config pointing to a temp directory.
    Uses spec-correct scoring weights (after B-05, B-06, B-07 fixes).
    """
    buckets_dir = str(tmp_path / "buckets")
    os.makedirs(os.path.join(buckets_dir, "permanent"), exist_ok=True)
    os.makedirs(os.path.join(buckets_dir, "dynamic"), exist_ok=True)
    os.makedirs(os.path.join(buckets_dir, "archive"), exist_ok=True)
    os.makedirs(os.path.join(buckets_dir, "feel"), exist_ok=True)

    return {
        "buckets_dir": buckets_dir,
        "merge_threshold": 75,
        "matching": {"fuzzy_threshold": 50, "max_results": 10},
        "wikilink": {"enabled": False},
        # Spec-correct weights (post B-05/B-06/B-07 fix)
        "scoring_weights": {
            "topic_relevance": 4.0,
            "emotion_resonance": 2.0,
            "time_proximity": 1.5,  # spec: 1.5 (was 2.5 in buggy code)
            "importance": 1.0,
            "content_weight": 1.0,  # spec: 1.0 (was 3.0 in buggy code)
        },
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {"base": 1.0, "arousal_boost": 0.8},
        },
        "dehydration": {
            "api_key": os.environ.get("OMBRE_COMPRESS_API_KEY", "test-key"),
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-2.5-flash-lite",
        },
        "embedding": {
            "api_key": os.environ.get("OMBRE_EMBED_API_KEY", ""),
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-embedding-001",
            "enabled": False,
        },
    }


class FakeEmbeddingEngine:
    """最小化可用的 embedding 引擎替身。

    Markdown 是写入真源，embedding 是可重建的派生索引。大多数测试要验证
    评分/衰减/检索等逻辑，所以默认 bucket_mgr fixture 配一个永远成功的
    fake；离线写入与后台重试契约在 test_embedding_outbox.py 单独覆盖。
    """

    enabled = True

    def __init__(self):
        self._store: dict[str, list[float]] = {}

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        self._store[bucket_id] = [0.1, 0.2, 0.3]
        return True

    def delete_embedding(self, bucket_id: str) -> None:
        self._store.pop(bucket_id, None)

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        return self._store.get(bucket_id)

    async def search_similar(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        return []


@pytest.fixture
def fake_embedding_engine():
    return FakeEmbeddingEngine()


@pytest.fixture
def bucket_mgr(test_config, fake_embedding_engine):
    from bucket_manager import BucketManager

    return BucketManager(test_config, embedding_engine=fake_embedding_engine)


@pytest.fixture
def decay_eng(test_config, bucket_mgr):
    from decay_engine import DecayEngine

    return DecayEngine(test_config, bucket_mgr)
