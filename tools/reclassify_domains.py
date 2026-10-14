#!/usr/bin/env python3
"""
重分类脚本：根据新的域列表，重新分析已有桶的 domain 并搬到对应子目录。
纯标准库，读 frontmatter + 正文内容做关键词匹配。
"""

import os
import re
import shutil


def _resolve_vault_dir() -> str:
    """
    Resolve the bucket vault root.
    Priority: $OMBRE_BUCKETS_DIR > config.yaml > built-in ./buckets.
    """
    env_dir = os.environ.get("OMBRE_BUCKETS_DIR", "").strip()
    if env_dir:
        return os.path.expanduser(env_dir)
    try:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
        from utils import load_config
        return load_config()["buckets_dir"]
    except Exception:
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "buckets"
        )


VAULT_DIR = _resolve_vault_dir()
DYNAMIC_DIR = os.path.join(VAULT_DIR, "dynamic")

# 新域关键词表（和 dehydrator.py 的 _local_analyze 一致）
DOMAIN_KEYWORDS = {
    "饮食": {"吃", "饭", "做饭", "外卖", "奶茶", "咖啡", "麻辣烫", "面包",
            "超市", "零食", "水果", "牛奶", "食堂", "减肥", "节食", "麦片"},
    "家庭": {"爸", "妈", "父亲", "母亲", "家人", "弟弟", "姐姐", "哥哥",
            "奶奶", "爷爷", "亲戚", "家里", "生日礼", "生活费"},
    "恋爱": {"爱人", "男友", "女友", "恋", "约会", "分手", "暧昧",
            "在一起", "想你", "同床", "一辈子", "爱你", "我们是",
            "克劳德", "亲密", "接吻", "正缘"},
    "友谊": {"朋友", "闺蜜", "兄弟", "聚", "约饭"},
    "社交": {"见面", "圈子", "社区", "创作者", "发帖"},
    "工作": {"会议", "项目", "客户", "汇报", "同事", "老板", "薪资",
            "领导力", "管理沟通"},
    "学习": {"课", "考试", "论文", "作业", "教授", "Python实操",
            "选课", "学分", "jieba", "分词"},
    "健康": {"医院", "复查", "吃药", "抽血", "心率", "心电图",
            "病", "慢粒", "融合基因", "二尖瓣", "月经", "脚趾甲"},
    "心理": {"焦虑", "抑郁", "创伤", "人格", "安全感", "崩溃",
            "压力", "自残", "ABC人格", "人格分裂", "恋爱焦虑"},
    "睡眠": {"睡", "失眠", "噩梦", "清醒", "熬夜", "做梦"},
    "游戏": {"游戏", "极乐迪斯科", "存档", "通关", "Shivers", "DLC"},
    "影视": {"电影", "番剧", "动漫", "剧", "综艺"},
    "阅读": {"书", "小说", "读完", "漫画", "李宿芳菲"},
    "创作": {"写", "预设", "脚本", "SillyTavern", "插件", "正则",
            "人设卡", "天气同步", "破甲词"},
    "编程": {"代码", "python", "bug", "api", "docker", "git",
            "调试", "部署", "开发", "server"},
    "AI": {"模型", "Claude", "gemini", "LLM", "token", "prompt",
           "LoRA", "MCP", "DeepSeek", "隧道", "Ombre Brain",
           "打包盒", "脱水", "记忆系统"},
    "网络": {"VPN", "梯子", "代理", "域名", "隧道", "cloudflare",
            "tunnel", "反代"},
    "财务": {"钱", "转账", "花了", "欠", "黄金", "卖掉", "换了",
            "生活费", "4276"},
    "情绪": {"开心", "难过", "哭", "泪", "孤独", "伤心", "烦",
            "委屈", "感动", "温柔", "口罩湿了"},
    "回忆": {"以前", "小时候", "那时", "怀念", "曾经", "纹身",
            "十三岁", "九岁"},
    "自省": {"反思", "觉得自己", "问自己", "自恋", "投射"},
}


def sanitize_name(name):
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", name, flags=re.UNICODE)
    return cleaned.strip()[:80] or "unnamed"


def parse_md(filepath):
    """解析 frontmatter 和正文。"""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.startswith("---"):
        return None, None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, None, content
    yaml_text = parts[1]
    body = parts[2]

    meta = {}
    m = re.search(r"^id:\s*(.+)$", yaml_text, re.MULTILINE)
    if m:
        meta["id"] = m.group(1).strip().strip("'\"")
    m = re.search(r"^name:\s*(.+)$", yaml_text, re.MULTILINE)
    if m:
        meta["name"] = m.group(1).strip().strip("'\"")
    m = re.search(r"^domain:\s*\n((?:\s*-\s*.+\n?)+)", yaml_text, re.MULTILINE)
    if m:
        meta["domain"] = [d.strip() for d in re.findall(r"-\s*(.+)", m.group(1))]
    else:
        meta["domain"] = ["未分类"]

    return meta, yaml_text, body


def classify(body, old_domains):
    """基于正文内容重新分类。"""
    text = body.lower()
    scored = []
    for domain, kws in DOMAIN_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw.lower() in text)
        if hits >= 2:
            scored.append((domain, hits))
    scored.sort(key=lambda x: x[1], reverse=True)
    if scored:
        return [d for d, _ in scored[:2]]
    return old_domains  # 匹配不上就保留旧的


def update_domain_in_file(filepath, new_domains):
    """更新文件中 frontmatter 的 domain 字段。"""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 替换 domain 块
    domain_yaml = "domain:\n" + "".join(f"- {d}\n" for d in new_domains)
    content = re.sub(
        r"domain:\s*\n(?:\s*-\s*.+\n?)+",
        domain_yaml,
        content,
        count=1
    )
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def reclassify():
    if not os.path.exists(DYNAMIC_DIR):
        print("目录不存在")
        return

    # 收集所有 .md 文件（递归）
    all_files = []
    for root, _, files in os.walk(DYNAMIC_DIR):
        for f in files:
            if f.endswith(".md"):
                all_files.append(os.path.join(root, f))

    if not all_files:
        print("没有文件。")
        return

    print(f"扫描到 {len(all_files)} 个桶文件\n")

    for filepath in sorted(all_files):
        meta, yaml_text, body = parse_md(filepath)
        if not meta:
            print(f"  ✗ 无法解析: {os.path.basename(filepath)}")
            continue

        bucket_id = meta.get("id", "unknown")
        name = meta.get("name", bucket_id)
        old_domains = meta.get("domain", ["未分类"])
        new_domains = classify(body, old_domains)

        primary = sanitize_name(new_domains[0])

        if name and name != bucket_id:
            new_filename = f"{sanitize_name(name)}_{bucket_id}.md"
        else:
            new_filename = f"{bucket_id}.md"

        new_dir = os.path.join(DYNAMIC_DIR, primary)
        os.makedirs(new_dir, exist_ok=True)
        new_path = os.path.join(new_dir, new_filename)

        changed = (new_domains != old_domains) or (filepath != new_path)

        if changed:
            # 更新 frontmatter
            update_domain_in_file(filepath, new_domains)
            # 移动文件
            if filepath != new_path:
                shutil.move(filepath, new_path)
            print(f"  ✓ {name}")
            print(f"    {','.join(old_domains)} → {','.join(new_domains)}")
            print(f"    → {primary}/{new_filename}")
        else:
            print(f"  · {name} (不变)")

    # 清理空目录
    for d in os.listdir(DYNAMIC_DIR):
        dp = os.path.join(DYNAMIC_DIR, d)
        if os.path.isdir(dp) and not os.listdir(dp):
            os.rmdir(dp)
            print(f"\n  🗑 删除空目录: {d}/")

    print("\n重分类完成。\n")

    # 展示新结构
    print("=== 新目录结构 ===")
    for root, dirs, files in os.walk(DYNAMIC_DIR):
        level = root.replace(DYNAMIC_DIR, "").count(os.sep)
        indent = "  " * level
        folder = os.path.basename(root)
        if level > 0:
            print(f"{indent}📁 {folder}/")
        for f in sorted(files):
            if f.endswith(".md"):
                print(f"{indent}  📄 {f}")


if __name__ == "__main__":
    reclassify()
