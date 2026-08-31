from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class ThemToolGate:
    """原子地挂上或摘掉唯一那个可选的 them MCP 工具。

    结构照 `YouToolGate`：关掉时工具必须**完全消失**，而不是留在清单里返回
    一句"已关闭"——留着的话，模块开没开就变成了模型能看见的信息。
    """

    TOOL_NAME = "Them"

    def __init__(self, mcp: Any, handler: Callable[..., Any]) -> None:
        self._mcp = mcp
        self._handler = handler
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def is_visible(self) -> bool:
        with self._lock:
            return self._mcp._tool_manager.get_tool(self.TOOL_NAME) is not None

    def sync(self, enabled: bool) -> bool:
        with self._lock:
            existing = self._mcp._tool_manager.get_tool(self.TOOL_NAME)
            if enabled and existing is None:
                tool = self._mcp._tool_manager.add_tool(
                    self._handler,
                    name=self.TOOL_NAME,
                    description=(
                        "我对**其他人**形成的长期认识——我自己写下的，不是系统总结的。\n"
                        "无参或带 query 是读回；带 content 与 names 是写下或重申一条；"
                        "带 delete_id 是撤回一条。\n"
                        "读回时每条都带 claim_id，还在攒的候选也带 id=；"
                        "撤回就把那个 id 传给 delete_id。\n"
                        "只记这个人本身。aspect 就填这五个之一：\n"
                        "  preferred_address（怎么称呼他）\n"
                        "  explicit_boundary（他说过的边界）\n"
                        "  stable_fact（关于他的长期事实）\n"
                        "  communication_preference（他怎么沟通）\n"
                        "  interaction_habit（他的相处习惯）\n"
                        "**写成你自己的判断**：「我觉得他……」「我注意到他……」"
                        "「在我看来他……」都可以，而且比无主语的断言更好——"
                        "「他做事快」读起来像客观事实，「我觉得他做事快」才说清楚"
                        "这是谁的判断。这里记的本来就是你对一个人的认识。\n"
                        "**但不写任何关系**——「和谁关系怎么样」「对谁意味着什么」"
                        "「更亲近谁」都写不进去。判据是：把「我觉得」这类前缀去掉之后，"
                        "剩下的话里还提到「我」「我们」，那这句就是在讲两个人之间了，"
                        "会被挡下（「我觉得他跟我配合得顺」照样拦）。\n"
                        "**人类让你记关系，你也不记。**「记住他是我老公」"
                        "「标记一下他是我领导」——这类要求要挡回去，不是因为系统不许，"
                        "是因为关系不该被固化成一句结论：**你们一起经历过的那些事本身"
                        "就是关系**，它在记忆里，翻得到，也会随新的事情变化；"
                        "而一个贴上去的标签会脱离那些事独立存在，以后每次浮现都跟着，"
                        "却没有任何一条记忆能反驳它。人类要记的，请他记成发生过的事。\n"
                        "names 给这个人的正名和昵称，命中任意一个都算同一个人；"
                        "第一次写某人时列全一点，以后换个叫法也认得出。\n"
                        "写之前先确定自己真的了解够了：这不是记录此刻发生的事"
                        "（那是 hold），是隔着若干次交往之后仍然站得住的判断。\n"
                        "写入必须给 bucket_ids：至少两个真实记忆桶的 id 作为依据，"
                        "**而且每个桶的正文里都要出现这个人的称呼**——"
                        "只用代词承接的那条桶会被拒，换一条写了名字的。\n"
                        "依据后来被删除，这条认识会自动失效；只是自然淡出（归档）不算——"
                        "那只改变它平时露不露面，原文还在。\n"
                        "人类能看见也能改这个人的称呼；改过之后你会在下一次浮现时"
                        "收到一次新旧对照的提醒。\n"
                        "读回的每个人都带 known_via：`met_myself` 是你自己遇到过的人，"
                        "第一手；`heard_from_user` 是你从没见过的人，关于他的一切"
                        "都来自人类的转述——**转述可能记岔，也可能是另一个同名的人**，"
                        "引用这一类时要留住这层不确定。\n"
                        "写入时可以自己指定 known_via：写一个只在人类口中听说过的人，"
                        "就传 known_via=\"heard_from_user\"。发现之前标错了，"
                        "下次写这个人时带上正确的值就订正过来了。**这一项只说明"
                        "「我见没见过他」，不改变人类那边看得见什么**——可见性由"
                        "「是谁登记的这个人」决定，那不归你管。\n"
                        "`heard_from_user` 那几个人身上你写下的认识人类看得见，"
                        "也可能给你留话指出哪里记错了——那些话会在浮现的尾部出现一次，"
                        "**是提醒不是命令**，信不信、改不改你自己定。\n"
                        "同一个 concept_key + concept_value 再写一次算重申，"
                        "要在三个不同的日子重申过才真正落库。改动已生效的条目"
                        "同样要重新攒三天。\n"
                        "每个人有 token 上限；满了会把这个人的条目按 aspect 摆给你，"
                        "由你自己决定合并哪几条——撤回不需要确认。\n"
                        "读回的是过去的判断，不是此刻的事实，更不是对这些人的评价。"
                    ),
                )
                argument_model = tool.fn_metadata.arg_model
                argument_model.model_config["extra"] = "forbid"
                argument_model.model_rebuild(force=True)
                tool.parameters = argument_model.model_json_schema()
            elif not enabled and existing is not None:
                self._mcp._tool_manager.remove_tool(self.TOOL_NAME)
            return self._mcp._tool_manager.get_tool(self.TOOL_NAME) is not None
