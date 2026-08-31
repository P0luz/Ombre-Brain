"""them 面板的前端契约。

守三件事，每一件都是真机上看出来才改的：

1. **名册按「怎么认识的」分组，一个人一页。** 一张平铺的名单里，
   听人类描述过的那个张三和模型自己遇到的那个张三长得一模一样，
   而这个入口存在的全部理由就是让人看出这两者的区别。
2. **图标不依赖外网。** 这一页讲的是「认得谁」，断网时正是最该看得见的东西。
3. **不用「它」称呼 AI，用配置里的名字。**
"""

import re
from pathlib import Path

import pytest


DASHBOARD = Path(__file__).parents[1] / "frontend" / "dashboard.html"


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _them_pane(html: str) -> str:
    """面板里 them 那一格。"""
    start = html.index('<div class="know-pane" id="know-pane-them"')
    end = html.index('<button class="self-fab"', start)
    return html[start:end]


def _them_js(html: str) -> str:
    """them 名册的渲染代码。"""
    start = html.index("var _ICO = {")
    end = html.index("async function saveSamplingSettings", start)
    return html[start:end]


def test_名册按来源分成两组():
    js = _them_js(_html())
    assert "themGroupHtml('heard'" in js
    assert "themGroupHtml('met'" in js
    # 分组的依据是 origin，不是别的什么顺手的字段。
    assert "origin === 'human'" in js


def test_一个人一页():
    html = _html()
    pane = _them_pane(html)
    js = _them_js(html)
    assert 'id="them-person-view"' in pane
    assert 'id="them-roster"' in pane
    assert "function openThemPerson(" in js
    assert "function backToThemRoster(" in js
    # 两个视图互斥，由一处切换决定，不能各自 display 各自的。
    assert "function renderThemView(" in js


def test_模型自己遇到的人不在前端露出正文():
    """rule.md 13.3：那一档的认识只属于模型，人类只看得到称呼。

    前端这一侧是第二道；第一道在 `list_people`，它根本不返回那些正文。
    """
    js = _them_js(_html())
    detail = js[js.index("function renderThemPersonView("):]
    正文段 = detail.index("know-claims")
    条件段 = detail.index("if (听说的)")
    assert 条件段 < 正文段, "正文渲染必须在「听你说的」这个分支里面"


def test_图标不依赖外网():
    """lucide 走 CDN。断网时这一页会退化成一排方框，而它恰恰是断网也该看的。"""
    js = _them_js(_html())
    icons = js[js.index("var _ICO = {"):js.index("function aiName(")]
    assert "data-lucide" not in icons
    assert icons.count("<svg") == 4
    assert "http" not in icons


def test_按钮有自己的样式():
    """`.btn-sm` 曾经全站在用却从来没定义过，拿到的是浏览器默认按钮——
    配上 padding 10px 的输入槽，就成了「字比按钮大」。"""
    html = _html()
    assert re.search(r"^\s*\.btn-sm\s*\{", html, re.M)


@pytest.mark.parametrize("片段", ["面板", "设置"])
def test_不用它称呼AI(片段):
    """人类介意被叫「它」。面板里一律用 config.yaml 的 ai_name。"""
    html = _html()
    if 片段 == "面板":
        文本 = _them_pane(html) + _them_js(html)
    else:
        start = html.index('<div class="config-section" id="sec-them"')
        文本 = html[start:html.index('<div class="config-section"', start + 1)]
    可见 = [
        行
        for 行 in 文本.split("\n")
        if "它" in 行 and not 行.strip().startswith(("//", "<!--", "*", "/*"))
    ]
    assert not 可见, f"这些行还在用「它」称呼 AI：{可见[:3]}"


def test_名字有地方来也有地方填():
    html = _html()
    assert "cfg.ai_name" in html
    assert "function applyAiName(" in html
    assert html.count('class="ai-name"') >= 3
