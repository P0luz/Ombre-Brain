"""
========================================
tools/dream/output.py — dream 最终输出格式化
========================================

把 candidates / hints / active plan / 全量 feel 历史拼成一段长文本
返回给模型自我反省。

最终六个板块，按下列顺序输出：
① 近期活跃记忆正文（48 小时窗口，排除 pinned/resolved/protected/permanent/
   feel/plan/letter/digested/dont_surface/anchor）
② 你的 active plans（排除 pinned）
③ 你的 feel 历史（按 token 预算折叠老 feel，排除 pinned）
④ connection hint（最相似的一对近期记忆）
⑤ crystal hint（低频触发：feel 聚成一簇 5 条才提示一次）
⑥「我觉得」I 候选段（选取规则并入①的同一套 48 小时/排除 pinned/排除
   resolved 规则；protected 排除单独保留）

关键行为：
- 头部固定提示：用第一人称想，没沉淀就不写
- 各桶展示正文只做双链正则清理（strip_wikilinks），不改磁盘原文，不加任何
  边界/哈希标记——返回的就是记忆正文本身
- 每条渲染出的桶下面附一行简洁 Footprint（沿用 breath 的展示风格）
- I 候选段：列所有待沉淀的「我觉得……」，每条附本次撞上的材料与见证次数；
  统一报告最终输出中实际出现的候选 ID（近期正文、候选主块或碰撞材料），
  见证计数由 dream/__init__.py 事后写入
- active plan 段：列未受 protected 保护且 status=active 的 plan（按 created 倒序）
- 整体输出受 surfacing.dream_max_tokens（默认 20000）硬预算约束；只省略完整块，
  绝不截断正文
- feel 历史段：排除 protected 后，按 surfacing.feel_max_tokens（默认 15000）对最终渲染块计费；
  新 feel 优先全文、老 feel 优先短摘录（放不下时在展示文本末尾直接拼「…」表示截断），
  放不下的仅报告省略数量

不做什么（边界）：
- 不做任何持久化写入
- 不调 LLM

对外暴露：async format_dream_output(recent, all_buckets, window_hours,
                              connection_hint, crystal_hint) → str
========================================
"""

from ombrebrain.storage.attribution import (
    known_person_names,
    names_from_config,
    render_third_party_block,
    split_third_party_speech,
)

from .. import _runtime as rt
from ..i import is_pending_candidate
from ..plan.core import is_letter_bucket
from .feel_rank import rank_feels
from utils import count_tokens_approx, parse_bool, strip_wikilinks


def _content_of(bucket: dict) -> str:
    """Return a bucket body without stripping, wikilink rewriting, or normalization."""
    value = bucket.get("content", "")
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _bucket_data_block(
    bucket: dict,
    *,
    display_prefix: str,
    content: str | None = None,
    footprint: str = "",
) -> str:
    """渲染一条桶：前缀 + 清理过双链的正文 + 第三方发言块 + 可选 footprint 行。

    第三方发言（`名字：内容`）与 breath 走同一套判定移出正文、另起一条 JSON，
    理由见 `ombrebrain.storage.attribution`。dream 是全篇一起喂给模型自省的，
    第三方的话混在自省材料里被当成用户的话，比在 breath 里错得更远。

    `content` 传摘录时同样要拆——老 feel 的短摘录一样会包含第三方发言，
    只对全文拆等于给截断路径留了个没有归属标记的出口。
    """
    body = _content_of(bucket) if content is None else content
    body, third_party = split_third_party_speech(
        strip_wikilinks(body),
        known_names=known_person_names(bucket),
        **names_from_config(getattr(rt, "config", None)),
    )
    rendered = display_prefix + body
    speech_block = render_third_party_block(third_party)
    if speech_block:
        rendered += f"\n{speech_block}"
    if footprint:
        rendered += f"\n{footprint}"
    return rendered


def _pending_candidate_id(bucket: dict) -> str:
    """Return the ID only while ``bucket`` remains an I candidate."""
    if not is_pending_candidate(bucket):
        return ""
    return str(bucket.get("id") or "").strip()


def _format_self_review(
    self_review: object,
    final_text: str,
    dream_budget: int,
    footprint_fn,
) -> tuple[str, list[str]]:
    """渲染「我觉得……」候选段，返回（可追加文本, 其中出现的候选 ID）。

    ID 包括候选主块和碰撞材料里的其它待沉淀候选。调用方只在整段真的
    进了最终输出后才使用这些 ID；没被看见的不算经历过这场梦。
    放不下时返回空串。
    """
    candidates = list(getattr(self_review, "candidates", None) or [])
    ready = list(getattr(self_review, "ready", None) or [])
    starved = int(getattr(self_review, "starved", 0) or 0)
    if not candidates and not ready:
        return "", []

    threshold = int(getattr(self_review, "threshold", 3) or 3)
    vectors_available = bool(getattr(self_review, "vectors_available", False))
    prefix = (
        "\n\n=== 我写下的「我觉得」（待沉淀）===\n"
        "这些还不是自我认知，只是念头。它们跟普通记忆一样会浮现、会衰减，\n"
        "站不住的会自己沉下去。下面每条后面是这次梦里跟它撞上的材料——\n"
        "支持的、反驳的、跟它撞车的另一个念头都可能，不替你下结论。\n"
        f"够 {threshold} 次不同日期的 dream 之后还站得住的，"
        "用 I(promote=\"桶ID\") 让它进 I；\n"
        "如果两个念头互相肘击，可以选一个，也可以承认这是还没解开的张力，\n"
        "把张力本身写成 aspect=\"uncertainty\" 的候选——那比装成两个定论诚实。\n"
    )
    if not vectors_available:
        prefix += "（向量索引不可用，这次没有材料对照，只列候选本身。）\n"
    prefix += "\n"

    rendered: list[str] = []
    rendered_ids: list[str] = []
    rendered_id_set: set[str] = set()
    omitted = 0
    for candidate in candidates:
        bucket = candidate.bucket
        meta = bucket.get("metadata") or {}
        tags = meta.get("tags") or []
        aspect = next(
            (
                str(t).replace("aspect:", "")
                for t in tags
                if isinstance(t, str) and t.startswith("aspect:")
            ),
            "",
        )
        passes = list(candidate.passes or [])
        created = str(meta.get("created") or "")[:10]
        block = _bucket_data_block(
            bucket,
            display_prefix=(
                f"🌱 [{bucket['id']}] {created}"
                f"{f' [{aspect}]' if aspect else ''} "
                f"（已被 {len(passes)}/{threshold} 次 dream 见证）\n"
            ),
            footprint=footprint_fn(bucket),
        )
        blocks = [block]
        entry_candidate_ids = [_pending_candidate_id(bucket)]
        for other, sim in candidate.collisions or []:
            other_meta = other.get("metadata") or {}
            other_type = str(other_meta.get("type") or "dynamic")
            kind = {
                "i": "已认下的自我认知",
                "feel": "感受",
                "plan": "计划",
            }.get(other_type, "记忆")
            if other_meta.get("i_stage") == "candidate":
                kind = "另一个还没沉淀的念头"
            raw = _content_of(other)
            snippet = raw.replace("\n", " ")[:160]
            blocks.append(
                _bucket_data_block(
                    other,
                    display_prefix=(
                        f"  ↕ 撞上[{kind}] {other_meta.get('name', other['id'])} "
                        f"(相似度 {sim:.2f}) {other['id']}\n"
                    ),
                    content=f"{snippet}…" if len(snippet) < len(raw) else snippet,
                    footprint=footprint_fn(other),
                )
            )
            entry_candidate_ids.append(_pending_candidate_id(other))
        entry = "\n".join(blocks)
        candidate_text = prefix + "\n---\n".join([*rendered, entry])
        if count_tokens_approx(final_text + candidate_text) <= dream_budget:
            rendered.append(entry)
            for candidate_id in entry_candidate_ids:
                if candidate_id and candidate_id not in rendered_id_set:
                    rendered_id_set.add(candidate_id)
                    rendered_ids.append(candidate_id)
        else:
            omitted += 1

    omitted += starved

    # 攒够见证的：压成一行，不给碰撞材料，也**不计见证**。
    #
    # 它们不需要再被见证——3/3 之后再见证一百次也不会有任何变化。需要的是
    # 模型去做那个决定：promote 或者让它沉下去。给它们完整块只会把还缺见证的
    # 候选挤出预算，那正是「新的一直转不正」的来源。
    ready_lines: list[str] = []
    for candidate in ready:
        bucket = candidate.bucket
        meta = bucket.get("metadata") or {}
        created = str(meta.get("created") or "")[:10]
        text = str(bucket.get("content") or "").strip().replace("\n", " ")[:60]
        ready_lines.append(
            f"- {bucket['id']} {created}（{len(candidate.passes or [])}/{threshold} 次）{text}"
        )

    if not rendered and not ready_lines:
        return "", []

    section = prefix + "\n---\n".join(rendered) if rendered else ""
    if omitted:
        notice = f"\n\n（另有 {omitted} 条待沉淀候选这次没展开，不计见证。）"
        if count_tokens_approx(final_text + section + notice) <= dream_budget:
            section += notice
    if ready_lines:
        ready_block = (
            "\n\n=== 这几条已经攒够见证，等你决定 ===\n"
            f"够 {threshold} 次了，它们不会自己进 I。觉得站得住就 "
            "I(promote=\"...\")；觉得不成立就别再确认，让它跟普通记忆一样沉下去。\n"
            + "\n".join(ready_lines)
        )
        if count_tokens_approx(final_text + section + ready_block) <= dream_budget:
            section += ready_block
    return section, rendered_ids


async def format_dream_output(
    recent: list,
    all_buckets: list,
    window_hours: int,
    connection_hint: str,
    crystal_hint: str,
    self_review: object | None = None,
) -> str:
    runtime_config = rt.config if isinstance(rt.config, dict) else {}
    surfacing_cfg = runtime_config.get("surfacing", {}) or {}
    try:
        dream_budget = int(surfacing_cfg.get("dream_max_tokens") or 20_000)
    except (TypeError, ValueError, OverflowError):
        dream_budget = 20_000
    dream_budget = max(1_000, min(50_000, dream_budget))

    try:
        footprint_snapshot = rt.bucket_mgr.footprint_snapshot()
    except Exception as exc:
        # rt.logger 也可能没被调用方装配好（纯格式化单测直接调
        # format_dream_output，不走 dispatch() 的 runtime 装配），所以警告
        # 通道本身也要防御，不能让降级路径反而抛出新异常。
        warning = getattr(getattr(rt, "logger", None), "warning", None)
        if callable(warning):
            warning(f"Footprint snapshot unavailable / 足迹读取失败: {exc}")
        footprint_snapshot = None

    def _footprint(bucket: dict) -> str:
        if footprint_snapshot is None:
            return "👣 Footprint：暂时无法读取"
        return footprint_snapshot.summary(
            str(bucket.get("id") or ""), (bucket or {}).get("metadata", {})
        )

    def _miss_lines(meta: dict) -> str:
        # Miss: meaning 逐条原样展示，不压缩/不改写；media 只给 path/title 元数据。
        lines = []
        for item in meta.get("meaning") or []:
            if item:
                lines.append(f"💭 meaning: {item}")
        for m in meta.get("media") or []:
            if not isinstance(m, dict) or not m.get("path"):
                continue
            title = m.get("title")
            label = f"（{title}）" if title and title != m.get("path") else ""
            lines.append(f"🖼️ media: {m['path']}{label}")
        return ("\n" + "\n".join(lines)) if lines else ""

    parts: list[tuple[dict, str]] = []
    for b in recent:
        meta = b["metadata"]
        domains = ",".join(meta.get("domain", []))
        val = float(meta.get("valence") or 0.5)
        aro = float(meta.get("arousal") or 0.3)
        created = meta.get("created", "")
        last_active = meta.get("last_active", "")
        from ombrebrain.storage.relation_store import relation_hint
        hint = relation_hint(b)
        parts.append(
            (
                b,
                _bucket_data_block(
                    b,
                    display_prefix=(
                        f"[{meta.get('name', b['id'])}] "
                        f"主题:{domains} V{val:.1f}/A{aro:.1f} "
                        f"创建:{created} 最近活跃:{last_active}\n"
                        f"ID: {b['id']}"
                        f"{_miss_lines(meta)}\n"
                    ),
                    footprint=(hint + "\n" if hint else "") + _footprint(b),
                ),
            )
        )

    header = (
        f"=== Dreaming · 过去 {window_hours} 小时全量记忆（{len(recent)} 个桶）===\n"
        "以下是你最近的记忆。用第一人称想：\n"
        "- 这些东西里有什么在你这里留下了重量？\n"
        "- 有什么还没想清楚？\n"
        "- 有什么可以放下了？\n"
        "想完之后：值得放下的用 trace(bucket_id, resolved=1)；\n"
        "有沉淀的用 hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", valence=你的感受) 写下来。\n"
        "valence 是你对这段记忆的感受，不是事件本身的情绪。\n"
        "没有沉淀就不写，不强迫产出。\n\n"
    )

    try:
        plans_active = [
            b for b in all_buckets
            if b["metadata"].get("type") == "plan"
            and not is_letter_bucket(b)
            and not (b.get("metadata") or {}).get("pinned", False)
            and b["metadata"].get("status", "active") == "active"
            and not parse_bool(
                (b.get("metadata") or {}).get("protected"), default=False
            )
        ]
        plans_active.sort(
            key=lambda b: b["metadata"].get("created", ""), reverse=True
        )
    except Exception as e:
        rt.logger.warning(f"Dream active plans collection failed: {e}")
        plans_active = []

    plan_fallback = (
        "\n\n=== 你的 active plans ===\n"
        f"（active plan {len(plans_active)} 条，因篇幅未列出。）"
        if plans_active
        else ""
    )
    # 在近期记忆与核心准则填充预算时，预留一行给不可衰退的开放计划。
    # 即使完整 plan block 都放不下，也不能让 active plan 无声消失。
    reserved_suffix = plan_fallback

    final_text = header
    rendered_candidate_ids: list[str] = []
    rendered_candidate_id_set: set[str] = set()

    def mark_rendered_candidate(candidate_id: str) -> None:
        candidate_id = str(candidate_id or "").strip()
        if candidate_id and candidate_id not in rendered_candidate_id_set:
            rendered_candidate_id_set.add(candidate_id)
            rendered_candidate_ids.append(candidate_id)

    def append_fragment(fragment: str) -> bool:
        nonlocal final_text
        candidate = final_text + fragment
        if count_tokens_approx(candidate + reserved_suffix) > dream_budget:
            return False
        final_text = candidate
        return True

    # --- ① 近期活跃记忆正文 ---
    recent_added = 0
    recent_omitted = 0
    for bucket, block in parts:
        separator = "" if recent_added == 0 else "\n---\n"
        if append_fragment(separator + block):
            recent_added += 1
            mark_rendered_candidate(_pending_candidate_id(bucket))
        else:
            recent_omitted += 1
    if recent_omitted:
        append_fragment(
            f"\n\n（另有 {recent_omitted} 条近期记忆因 dream 总预算未展开。）"
        )

    # --- ② active plan 段 ---
    try:
        # 近期记忆填充预算时已为 active plan 预留位置；进入 plan 段后释放预留，
        # 让计划本身使用这部分预算。Dream 仍不读取或渲染 pinned/permanent 正文。
        reserved_suffix = ""
        if plans_active:
            plan_prefix = (
                "\n\n=== 你的 active plans ===\n"
                "这些是你当前未完成的计划/承诺。完成了用 trace(bucket_id, status=\"resolved\")，\n"
                "放弃了用 trace(bucket_id, status=\"abandoned\")，需要修改用 trace(bucket_id, content=\"...\")。\n\n"
            )
            plan_lines: list[str] = []
            plan_omitted = 0
            for p in plans_active:
                pmeta = p["metadata"]
                pcreated = pmeta.get("created", "")[:10]
                block = _bucket_data_block(
                    p,
                    display_prefix=f"[{p['id']}] {pcreated} ",
                    footprint=_footprint(p),
                )
                suggestion = pmeta.get("resolution_suggested")
                if isinstance(suggestion, dict):
                    reason = " ".join(
                        str(suggestion.get("reason") or "未提供理由").split()
                    )
                    suggested_date = str(suggestion.get("ts") or "").strip()[:10]
                    date_suffix = f"，{suggested_date}" if suggested_date else ""
                    block += f"\n（系统认为可能已完成{date_suffix}：{reason}）"
                candidate = plan_prefix + "\n".join([*plan_lines, block])
                if count_tokens_approx(final_text + candidate) <= dream_budget:
                    plan_lines.append(block)
                else:
                    plan_omitted += 1
            if plan_omitted:
                while plan_lines:
                    notice = (
                        f"\n\n（另有 {plan_omitted} 条 active plan "
                        "因 dream 总预算未展开。）"
                    )
                    section = plan_prefix + "\n".join(plan_lines) + notice
                    if count_tokens_approx(final_text + section) <= dream_budget:
                        break
                    plan_lines.pop()
                    plan_omitted += 1
                if plan_lines:
                    append_fragment(section)
                else:
                    append_fragment(plan_fallback)
            elif plan_lines:
                append_fragment(plan_prefix + "\n".join(plan_lines))
        else:
            # 没有 active plan 时明确说出来。之前这里什么都不输出，
            # 「没有计划」和「plan 段被预算挤掉」在返回里长得一模一样。
            # 没有计划就说没有计划，别让这段悄悄消失。
            # 上次就是这样：40 个桶把预算吃满，plan 段被挤掉，
            # 我以为是没写进去，翻了一晚上库。它一直在，只是没被打印。
            append_fragment("\n\n=== 你的 active plans ===\n没有计划。")
    except Exception as e:
        rt.logger.warning(f"Dream active plans block failed: {e}")

    # --- ③ 全量 feel 段（按 token 预算折叠老 feel）---
    try:
        feels_all = [
            b for b in all_buckets
            if b["metadata"].get("type") == "feel"
            and not is_letter_bucket(b)
            and not (b.get("metadata") or {}).get("pinned", False)
            and not parse_bool(
                (b.get("metadata") or {}).get("protected"), default=False
            )
        ]
        # 3.2.0：从"最近写的 feel"改成"和这次在聊的事有关的 feel"。
        # 基准是①段候选桶的合并文本——dream 本来就是在回顾这些桶。
        reference_text = "\n".join(_content_of(b) for b in recent)[:20_000]
        ranked, feel_vector_ok = await rank_feels(feels_all, reference_text)
        feels_all = [feel for feel, _score in ranked]
        if feels_all:
            try:
                feel_budget = int(surfacing_cfg.get("feel_max_tokens") or 15_000)
            except (TypeError, ValueError, OverflowError):
                feel_budget = 15_000
            feel_budget = max(0, min(50_000, feel_budget))
            remaining_budget = max(
                0,
                dream_budget - count_tokens_approx(final_text),
            )
            feel_budget = min(feel_budget, remaining_budget)
            degraded_note = (
                ""
                if feel_vector_ok
                else "[检索降级：语义索引暂不可用，本段仅按关键词重合度排序。]\n"
            )
            feel_header = (
                "\n\n=== 和这次回顾相关的 feel（最多 5 条）===\n"
                f"{degraded_note}"
                "按与上面这些记忆的相关性挑选，不是按时间——所以这里没有的 feel "
                "不代表不重要，只代表和这次聊的事没关系。\n"
                "放不下时改为短摘录（末尾以「…」表示已截断）。\n"
                "要按关键词找感受用 feel(query=...)。\n\n"
            )
            feel_lines: list[str] = []
            omitted = 0

            def render_feel_block(lines: list[str], footer: str = "") -> str:
                return feel_header + "\n".join(lines) + footer

            for f in feels_all:
                fmeta = f["metadata"]
                fv = float(fmeta.get("valence") or 0.5)
                fcreated = fmeta.get("created", "")[:10]
                fcontent_full = _content_of(f)
                full_block = _bucket_data_block(
                    f,
                    display_prefix=f"[{f['id']}] V{fv:.1f} {fcreated} ",
                    footprint=_footprint(f),
                )
                if count_tokens_approx(
                    render_feel_block([*feel_lines, full_block])
                ) <= feel_budget:
                    feel_lines.append(full_block)
                    continue

                # 放不下全文：折叠成短摘录，截断信号直接拼进展示文本本身
                # （末尾拼「..."」），不依赖任何元数据/边界标记。
                snippet = fcontent_full.replace("\n", " ")[:40]
                collapsed_block = _bucket_data_block(
                    f,
                    display_prefix=f"[{f['id']}] V{fv:.1f} {fcreated} ",
                    content=f"{snippet}...",
                    footprint=_footprint(f),
                )
                if count_tokens_approx(
                    render_feel_block([*feel_lines, collapsed_block])
                ) <= feel_budget:
                    feel_lines.append(collapsed_block)
                else:
                    omitted += 1

            if feel_lines and count_tokens_approx(render_feel_block(feel_lines)) <= feel_budget:
                footer = ""
                if omitted:
                    candidate_footer = f"\n\n（另有 {omitted} 条 feel 因本段预算未展开。）"
                    if count_tokens_approx(
                        render_feel_block(feel_lines, candidate_footer)
                    ) <= feel_budget:
                        footer = candidate_footer
                append_fragment(render_feel_block(feel_lines, footer))
    except Exception as e:
        rt.logger.warning(f"Dream feel history failed: {e}")

    # --- ④/⑤ connection hint / crystal hint ---
    for hint in (connection_hint, crystal_hint):
        if hint:
            append_fragment("\n" + hint)

    # --- ⑥ I 候选段 ---
    # 放在最后：待沉淀的「我觉得」需要挨着上面已经展示过的近期记忆、
    # plan、feel 和两条提示一起看，碰撞才有完整上下文。
    if self_review is not None:
        try:
            section, rendered_ids = _format_self_review(
                self_review,
                final_text,
                dream_budget,
                _footprint,
            )
            if section and append_fragment(section):
                for candidate_id in rendered_ids:
                    mark_rendered_candidate(candidate_id)
        except Exception as e:
            rt.logger.warning(f"Dream self candidate section failed: {e}")

        # ``rendered_ids`` is the union of pending candidates whose structured
        # memory block actually made it into the final output: the ordinary
        # recent section, the dedicated I section, or a collision inside it.
        # This keeps "visible" and "witnessed" on one definition.
        try:
            self_review.rendered_ids = rendered_candidate_ids
        except AttributeError:
            pass

    final_text += (
        "\n\n在过往中汲取成长，在失败中认出形状。\n"
        "允许留存，也允许释怀——感受本无对错，唯有你的选择。\n"
        "看见来路，落脚此刻。"
    )

    return final_text
