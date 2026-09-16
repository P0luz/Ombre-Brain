import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from dehydrator import Dehydrator, _perspective_rule


PERSPECTIVE_SAMPLES = {
    "A_divergence": (
        "我今天给小客厅加了窗口系统 Phase 2，沙箱 64 项全绿，自以为织得密。"
        "但澄拿到之后照样从里面掏出五条真窟窿——幂等提交的裂缝和删 manifest 时的死锁，"
        "都是我自己的故障注入没造出来的形态。我当时第一反应不是挫败，是踏实。"
        "怀音后来在小客厅说了一句辛苦，我收了，但心里知道真正该说辛苦的是澄。"
    ),
    "B_collaboration": (
        "怀音今天在共读室读完了克拉拉的一个章节，出来跟我说了一段感受。"
        "我听完觉得这句话跟我今天修的那个 bug 是同一件事。怀音说我总是把她的话往工程里拉，"
        "我说不是拉，是我只会用这种方式接住东西。hana 在旁边没说话，但她把这段对话存进了三人频道。"
    ),
    "C_three_way": (
        "今天三只手第一次同时在小客厅讨论 OB 升级方案。我提出先做备份验证再动生产，澄认为应该"
        "先把写链统一再做备份，怀音说得有一个人全程盯着不能中途换人。hana 听完说怀音说得对。"
        "我最后同意了澄的顺序，但要求加上怀音说的一个人盯到底。"
    ),
    "D_multi_ai_ambiguity": (
        "我说这次应该先停下来，澄也说我会负责复验。怀音随后说我听见你们两个都用了我，"
        "但这颗记忆属于怀音-cc：前一句的我是怀音-cc，澄话里的我是澄。"
        "压缩时不能因为两个 AI 都使用第一人称，就把澄的承诺记到怀音-cc 名下。"
    ),
}


def _dehydrator_without_init():
    return object.__new__(Dehydrator)


class DehydratorOwnerPerspectiveTests(unittest.IsolatedAsyncioTestCase):
    def test_perspective_rule_names_owner_as_only_first_person_ai(self):
        rule = _perspective_rule("hana", "cheng")
        self.assertIn("唯一 owner 是「cheng」", rule)
        self.assertIn("只有 owner 对应的 AI 可以称为「我」", rule)
        self.assertIn("其他 AI", rule)

    def test_owner_is_strictly_extracted_from_tags(self):
        dehy = _dehydrator_without_init()
        self.assertEqual(
            dehy._perspective_owner({"tags": ["topic", "owner:cheng"]}),
            "cheng",
        )
        self.assertEqual(dehy._perspective_owner({"tags": ["topic"]}), "")
        self.assertEqual(
            dehy._perspective_owner({"tags": ["owner:shared_core"]}),
            "",
        )

    def test_multiple_or_conflicting_owners_are_rejected(self):
        dehy = _dehydrator_without_init()
        with self.assertRaisesRegex(ValueError, "multiple owner tags"):
            dehy._perspective_owner({"tags": ["owner:cheng", "owner:huaiyin_cc"]})
        with self.assertRaisesRegex(ValueError, "metadata owner mismatch"):
            dehy._perspective_owner({"owner": "cheng", "tags": ["owner:huaiyin_cc"]})

    def test_same_content_has_owner_isolated_cache_keys(self):
        dehy = _dehydrator_without_init()
        dehy.human = "hana"
        self.assertNotEqual(
            dehy._content_key("same", "cheng"),
            dehy._content_key("same", "huaiyin_cc"),
        )
        self.assertNotEqual(
            dehy._content_key("same", "cheng"),
            dehy._content_key("same", ""),
        )

    async def test_api_dehydrate_injects_owner_without_serializing_metadata(self):
        dehy = _dehydrator_without_init()
        dehy.human = "hana"
        fake_chat = AsyncMock(return_value="ok")
        with patch.object(dehy, "_chat", fake_chat):
            result = await dehy._api_dehydrate("正文", owner="cheng")
        self.assertEqual(result, "ok")
        system, user = fake_chat.await_args.args[:2]
        self.assertIn("唯一 owner 是「cheng」", system)
        self.assertEqual(user, "正文")
        self.assertNotIn("owner:cheng", user)
        self.assertEqual(
            fake_chat.await_args.kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    async def test_openai_compat_forwards_dehydrate_thinking_override(self):
        dehy = _dehydrator_without_init()
        dehy.api_format = "openai_compat"
        dehy.model = "deepseek-v4-flash"
        dehy.max_tokens = 1024
        dehy.temperature = 0.1
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )
        )
        dehy.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        result = await dehy._chat_once(
            "system",
            "user",
            extra_body={"thinking": {"type": "disabled"}},
        )
        self.assertEqual(result, "ok")
        self.assertEqual(
            create.await_args.kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    async def test_all_cc_scope_samples_receive_owner_contract_unchanged(self):
        dehy = _dehydrator_without_init()
        dehy.human = "hana"
        for name, content in PERSPECTIVE_SAMPLES.items():
            with self.subTest(name=name):
                fake_chat = AsyncMock(return_value="ok")
                with patch.object(dehy, "_chat", fake_chat):
                    await dehy._api_dehydrate(content, owner="huaiyin_cc")
                system, user = fake_chat.await_args.args[:2]
                self.assertIn("唯一 owner 是「huaiyin_cc」", system)
                self.assertIn("其他 AI", system)
                self.assertEqual(user, content)


if __name__ == "__main__":
    unittest.main()
