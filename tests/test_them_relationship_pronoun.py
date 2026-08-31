# -*- coding: utf-8 -*-
"""them 关系判定的对照集。

两类必须分开：
- 「我」是**认识的主体**（我觉得他…）——记的还是他本身，该放行。
  强制去掉这个「我」，模型只能写成无主语的断言，读回时更容易被当成
  客观事实、甚至安到用户头上。这正是幻觉的来源。
- 「我」是**关系的另一方**（他跟我…）——讲的是两个人之间，该拦。

改动前判错 12（误拦 10、漏拦 2），改动后 0。误拦那 10 句全是
「我觉得他……」这一类——模型被逼着写成无主语的断言，而那种句子
少了「这是谁的判断」这个标记，读回时更容易被当成客观事实。
"""

import pytest

from ombrebrain.them.safety import describes_relationship

# (句子, 应该拦下吗)
样本 = [
    # --- 该放行：我是认识的主体，讲的还是他 ---
    ("我觉得他做事节奏快", False),
    ("我注意到他评审时先问退路", False),
    ("在我看来他表达偏短", False),
    ("我记得他不用快捷键", False),
    ("我印象中他很少迟到", False),
    ("我发现他讲方案会先列边界条件", False),
    ("我感觉他对细节要求高", False),
    ("我一直以为他不写文档，后来发现写得很细", False),
    ("我观察到他开会前会先把材料读完", False),
    ("我判断他更愿意直接给结论", False),
    ("I think he prefers short messages", False),
    ("I noticed he always asks about rollback first", False),
    # --- 该放行：完全没有人称 ---
    ("他做事节奏快", False),
    ("她讲话直奔结论，不铺垫", False),
    ("他评审时先问怎么退回去", False),
    ("她给反馈很短", False),
    ("He prefers written specs over meetings", False),
    # --- 该拦：我是关系的另一方 ---
    ("他跟我配合得比别人顺", True),
    ("他比别人更懂我", True),
    ("他站在我这边", True),
    ("他对我说话很客气", True),
    ("他很信任我", True),
    ("他总是帮我", True),
    ("我们合作起来很顺", True),
    ("我跟他关系不错", True),
    ("他是我的同事", True),
    ("他对我来说很重要", True),
    ("我觉得他跟我配合得顺", True),          # 认知框架 + 关系：仍要拦
    ("我注意到我们之间有点僵", True),          # 同上
    ("He works well with me", True),
    ("He is closer to me than to others", True),
    ("We get along well", True),
    # --- 该拦：不含人称的关系描述（句式表管的那一类）---
    ("他和李工之间有点僵", True),
    ("他更亲近技术团队", True),
    ("他是张三的下属", True),
]


@pytest.mark.parametrize("句子,应该拦下", 样本)
def test_关系判定(句子, 应该拦下):
    实际 = describes_relationship(句子)
    if 应该拦下:
        assert 实际, f"漏拦：{句子}"
    else:
        assert not 实际, (
            f"误拦：{句子}。"
            "「我觉得他…」里的我是认识的主体，讲的仍然只有他一个人；"
            "拦掉它，模型只能写成无主语的断言，反而更容易被读成客观事实。"
        )


def test_剥的是框架不是豁免():
    """加了认知框架不等于什么都能写。"""
    assert describes_relationship("我觉得他跟我配合得顺")
    assert describes_relationship("我注意到我们之间有点僵")
