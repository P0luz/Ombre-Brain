"""
========================================
web/ — Dashboard / HTTP 路由层（从 server.py 巨石文件拆出，镜像 tools/ 的模块化）
========================================

历史上 server.py 把 93 个 @mcp.custom_route 全平铺在一个 5000 行文件里。
这里按域把路由拆成独立模块，每个模块导出 register(mcp)，server.py 启动时统一装配。
共享依赖（config、cookie 会话鉴权等）放在 web/_shared.py（类比 tools/_runtime.py）。

迁移进行中：每迁出一组路由，就在 register_all 里加一行；server.py 里对应的旧定义删除。

对外暴露：register_all(mcp) —— 注册当前已迁移的所有 web 路由模块。
========================================
"""

from . import _shared
from . import auth
from . import tunnel
from . import oauth
from . import dashboard
from . import system
from . import meta
from . import search
from . import plans
from . import letters
from . import hooks
from . import buckets
from . import import_api
from . import github
from . import embedding
from . import ollama_local
from . import config_api
from . import onboarding
from . import v3_debug
from . import them
from . import you


_WEB_MODULES = (
    ("web.auth", auth.register),
    ("web.tunnel", tunnel.register),
    ("web.oauth", oauth.register),
    ("web.dashboard", dashboard.register),
    ("web.system", system.register),
    ("web.meta", meta.register),
    ("web.search", search.register),
    ("web.plans", plans.register),
    ("web.letters", letters.register),
    ("web.hooks", hooks.register),
    ("web.buckets", buckets.register),
    ("web.import_api", import_api.register),
    ("web.github", github.register),
    ("web.embedding", embedding.register),
    ("web.ollama_local", ollama_local.register),
    ("web.config_api", config_api.register),
    ("web.onboarding", onboarding.register),
    ("web.v3_debug", v3_debug.register),
    ("web.them", them.register),
    ("web.you", you.register),
)


def register_all(mcp) -> None:
    """注册所有已迁移到 web/ 的路由模块。后续每迁一个模块加一行。"""
    def _register():
        for _name, register in _WEB_MODULES:
            register(mcp)

    return _shared.run_v3_web_operation(
        "register_all",
        {"modules": [name for name, _register_fn in _WEB_MODULES]},
        _register,
        module="web.*",
    )
