"""第三方发言分块：拆什么、不拆什么、拆出来长什么样。

这一层的失败模式不对称，测试也照这个不对称来写：
- 漏拆 = 维持现状，正文里那句话还在，只是没被标出来
- 错拆 = 系统把用户自己说的话标成了别人说的，凭空造出一个错误归属

所以「不该拆的没被拆」的用例比「该拆的拆了」更多，也更该守。
"""

import json

import pytest

from ombrebrain.storage.attribution import (
    is_third_party_speaker,
    known_person_names,
    names_from_config,
    render_third_party_block,
    split_third_party_speech,
)
from ombrebrain.storage.quote_store import render_quotes

KNOWN = ["Zoey", "阿哲"]


class TestSplit:
    def test_拆出第三方发言并移出正文(self):
        content = "今天聊到方案。\nZoey：我觉得这个方案撑不住。\n后来就散了。"
        body, statements = split_third_party_speech(content, known_names=KNOWN)
        assert "Zoey" not in body
        assert "撑不住" not in body
        assert body == "今天聊到方案。\n后来就散了。"
        assert statements == [
            {
                "order": 1,
                "speaker": "Zoey",
                "speaker_role": "third_party",
                "text": "我觉得这个方案撑不住。",
            }
        ]

    def test_半角冒号同样命中(self):
        _, statements = split_third_party_speech(
            "Zoey: it will not hold", known_names=KNOWN
        )
        assert len(statements) == 1
        assert statements[0]["text"] == "it will not hold"

    def test_多条按原顺序编号(self):
        content = "Zoey：先做 A。\n中间说了点别的。\n阿哲：我不同意。"
        _, statements = split_third_party_speech(content, known_names=KNOWN)
        assert [s["order"] for s in statements] == [1, 2]
        assert [s["speaker"] for s in statements] == ["Zoey", "阿哲"]

    def test_没有第三方时正文一个字不动(self):
        content = "她验收时要求逐条核对。\n注意：不接受囫囵结论。"
        body, statements = split_third_party_speech(content, known_names=KNOWN)
        assert body == content
        assert statements == []

    def test_显式标记不需要预先认得这个名字(self):
        content = "@林工：这个接口下周才冻结。"
        body, statements = split_third_party_speech(content)
        assert statements[0]["speaker"] == "林工"
        assert "@" not in statements[0]["speaker"]
        assert body == ""


class TestRecognitionRequired:
    """第一版只看行首形状，被真实用例抓出错拆。现在必须先认得这个名字。"""

    def test_没认出来的名字不拆(self):
        content = "Zoey：我觉得这个方案撑不住。"
        body, statements = split_third_party_speech(content)
        assert statements == []
        assert body == content

    def test_短语加冒号不再被当成说话人(self):
        # 这条来自 tests/test_breath_query_catalog_regression.py 的真实正文，
        # 第一版在这里错拆出了一个叫「精准命中正文」的说话人。
        content = "精准命中正文：两个都是你，怎么还让我选。"
        body, statements = split_third_party_speech(content, known_names=KNOWN)
        assert statements == []
        assert body == content

    def test_引语署名让这个名字被认出来(self):
        bucket = {
            "content": "Zoey：我觉得这个方案撑不住。",
            "metadata": {"quotes": [{"text": "撑不住", "speaker": "Zoey"}]},
        }
        assert "zoey" in known_person_names(bucket)
        _, statements = split_third_party_speech(
            bucket["content"], known_names=known_person_names(bucket)
        )
        assert len(statements) == 1

    def test_双链让这个名字被认出来(self):
        bucket = {"content": "今天和 [[Zoey]] 聊了很久。\nZoey：这个方案撑不住。"}
        assert "zoey" in known_person_names(bucket)
        _, statements = split_third_party_speech(
            bucket["content"], known_names=known_person_names(bucket)
        )
        assert len(statements) == 1

    def test_没有任何署名的桶认不出人名(self):
        assert known_person_names({"content": "结论：先不做。"}) == set()
        assert known_person_names(None) == set()


class TestNeverSplit:
    """错拆的代价比漏拆高，这些用例是主要防线。"""

    @pytest.mark.parametrize(
        "line",
        [
            "@注意：这段不能删",
            "@结论：方案可行",
            "@原因：上游改了字段名",
            "@TODO: fix the encoding",
            "@Note: this is not a person",
            "@背景：这是三月的事",
            "@时间：2026-08-20",
        ],
    )
    def test_结构词不当成人名(self, line):
        """结构词表挡在显式标记之后：`@结论：` 也不算有人在说话。"""
        body, statements = split_third_party_speech(line)
        assert statements == []
        assert body == line

    @pytest.mark.parametrize(
        "line",
        ["@我：今天很累", "@你：要不要先歇会儿", "@用户：帮我记一下", "@Claude: I remember"],
    )
    def test_我和用户的发言不算第三方(self, line):
        """连显式 `@` 都不能把我或用户变成第三方——归属表比标记更硬。"""
        _, statements = split_third_party_speech(line)
        assert statements == []

    def test_配置里补的称呼被认成用户(self):
        line = "@poluz的猫：喵"
        _, before = split_third_party_speech(line)
        assert len(before) == 1  # 没登记时按第三方处理
        _, after = split_third_party_speech(line, user_names=["poluz的猫"])
        assert after == []

    def test_url_不被冒号切开(self):
        line = "参考 https://example.com/a"
        body, statements = split_third_party_speech(line, known_names=["https"])
        assert statements == []
        assert body == line

    def test_行首URL也不被拆(self):
        line = "https://example.com/a"
        _, statements = split_third_party_speech(line, known_names=["https"])
        assert statements == []

    def test_列表编号不当成人名(self):
        line = "@1：先跑单测"
        _, statements = split_third_party_speech(line)
        assert statements == []

    @pytest.mark.parametrize(
        "line",
        [
            "@这一段其实是我自己在复盘：所以不该算别人说的",
            "@当时脑子里闪过的第一个念头：先别急着下结论",
            "@we talked for a while and then agreed: ship it",
        ],
    )
    def test_一句话不当成人名(self, line):
        """名字长度是最后一道：整句话再怎么标记也不是说话人。"""
        _, statements = split_third_party_speech(line)
        assert statements == []

    def test_带空格的英文名仍然认得出(self):
        _, statements = split_third_party_speech(
            "Zoey Chen：it will not hold", known_names=["Zoey Chen"]
        )
        assert len(statements) == 1
        assert statements[0]["speaker"] == "Zoey Chen"

    def test_行中间的冒号不触发(self):
        line = "我记得她说过 Zoey：这句其实在句中"
        _, statements = split_third_party_speech(line, known_names=KNOWN)
        assert statements == []


class TestRender:
    def test_一条JSON而不是每人一条(self):
        block = render_third_party_block(
            [
                {"order": 1, "speaker": "Zoey", "speaker_role": "third_party", "text": "a"},
                {"order": 2, "speaker": "阿哲", "speaker_role": "third_party", "text": "b"},
            ]
        )
        assert block.count("```json") == 1
        payload = json.loads(block.split("```json")[1].split("```")[0])
        assert len(payload["non_user_speech"]) == 2

    def test_块头写明不是用户说的(self):
        block = render_third_party_block(
            [{"order": 1, "speaker": "Zoey", "speaker_role": "third_party", "text": "a"}]
        )
        assert "不是用户说的" in block

    def test_没有第三方就没有块(self):
        assert render_third_party_block([]) == ""


class TestQuotes:
    def test_署名第三方的引语走JSON(self):
        rendered = render_quotes([{"text": "这个方案撑不住", "speaker": "Zoey"}])
        assert "🗣️" not in rendered
        assert "non_user_speech" in rendered

    def test_没署名的引语留在文本行(self):
        rendered = render_quotes([{"text": "我不会走的"}])
        assert rendered.startswith("🗣️")
        assert "non_user_speech" not in rendered

    def test_用户署名的引语留在文本行(self):
        rendered = render_quotes(
            [{"text": "验收要逐条对", "speaker": "她"}], user_names=["她"]
        )
        assert rendered.startswith("🗣️")
        assert "non_user_speech" not in rendered


class TestRenderOnly:
    """拆分只发生在渲染层：不动磁盘，也不动召回。

    这是这次改动最该守住的边界。被拆走的那句话仍然完整躺在 `bucket["content"]`
    里，BM25 与向量索引读的都是它——所以一条普通记忆的召回分数不会因为
    第三方分块而变化。

    真机侧已经验过同一件事：只出现在第三方发言里的词
    （「先看结论」「过程回头再说」）照样能被 breath_search 检索到。
    这里锁住单测这一半，免得哪天有人把拆分挪进写入路径。
    """

    def test_桶本身一个字都不改(self):
        from tools.breath._verbatim import render_stored_bucket

        bucket = {
            "id": "b1",
            "content": "今天开会。\nZoey：先看结论，过程回头再说。",
            "metadata": {"type": "dynamic", "quotes": [{"text": "x", "speaker": "Zoey"}]},
        }
        原文 = bucket["content"]
        rendered, _ = render_stored_bucket(bucket, "[bucket_id:b1]")
        # 展示正文里第三方那句已经移走
        assert "先看结论" not in rendered.split("```json")[0]
        # 但桶本身没被动过——索引读的是这一份
        assert bucket["content"] == 原文
        assert "先看结论" in bucket["content"]


class TestConfig:
    def test_没有配置段时返回空表(self):
        assert names_from_config(None) == {"self_names": [], "user_names": []}
        assert names_from_config({}) == {"self_names": [], "user_names": []}

    def test_读出配置里的称呼(self):
        names = names_from_config({"attribution": {"user_names": ["她", "Poluz"]}})
        assert names["user_names"] == ["poluz", "她"]

    def test_内置表始终生效(self):
        assert not is_third_party_speaker("用户")
        assert is_third_party_speaker("Zoey")
