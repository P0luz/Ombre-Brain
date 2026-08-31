from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class YouToolGate:
    """Atomically add or remove the single optional You MCP tool."""

    TOOL_NAME = "You"

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
                        "我对人类一方形成的长期认识——我自己写下的，不是系统总结的。\n"
                        "无参或带 query 是读回；带 content 是写下或重申一条；"
                        "带 delete_id 是撤回一条。\n"
                        "撤回要先知道 id：读回时加 with_ids=True，每条后面就带上"
                        "[id=...]，把那个 id 传给 delete_id 即可。默认不带——"
                        "id 占的 token 会挤掉正文，而你多数时候只是读。\n"
                        "写之前先确定自己真的了解够了：这不是记录此刻发生的事"
                        "（那是 hold），是隔着若干次交往之后仍然站得住的判断。"
                        "拿不准就先别写，它不会因为写下来而变得更真。\n"
                        "aspect 就填这五个之一：\n"
                        "  preferred_address（怎么称呼人类）\n"
                        "  explicit_boundary（人类说过的边界）\n"
                        "  stable_fact（关于人类的长期事实）\n"
                        "  communication_preference（人类怎么沟通）\n"
                        "  interaction_habit（人类的相处习惯）\n"
                        "basis 说明这条认识是怎么来的，填这四个之一：\n"
                        "  explicit_statement（人类明确说过）\n"
                        "  observed_pattern（我自己观察到的规律，默认值）\n"
                        "  shared_event（一起经历过的事）\n"
                        "  user_confirmation（我问过、人类确认了）\n"
                        "preferred_address 与 explicit_boundary 是核心项，"
                        "只能记人类明确说过的话，必须同时传 explicit=True；"
                        "stable_fact 还要再加 long_term=True。\n"
                        "写入必须给 bucket_ids：至少两个真实记忆桶的 id，作为这条"
                        "认识的依据；id 从 breath / breath_search / dream 等处得到。"
                        "依据后来被删除，这条认识会自动失效；只是自然淡出（归档）不算——"
                        "那只改变它平时露不露面，原文还在。\n"
                        "同一个 concept_key + concept_value 再写一次算重申。"
                        "要在三个不同的日子重申过才真正落库——改主意了就别再确认，"
                        "它不会自己生效。改动已生效的条目同样要重新攒三天。\n"
                        "concept_key 用 snake_case，concept_value 用规范化短值，"
                        "语义相反的两条要用同一个 concept_key、不同 concept_value。\n"
                        "读回的内容是过去的判断，不是此刻的事实，也不是画像或定论。"
                    ),
                )
                argument_model = tool.fn_metadata.arg_model
                argument_model.model_config["extra"] = "forbid"
                argument_model.model_rebuild(force=True)
                tool.parameters = argument_model.model_json_schema()
            elif not enabled and existing is not None:
                self._mcp._tool_manager.remove_tool(self.TOOL_NAME)
            return self._mcp._tool_manager.get_tool(self.TOOL_NAME) is not None
