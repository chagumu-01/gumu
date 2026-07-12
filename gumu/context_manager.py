"""Prompt 组装与上下文预算控制（Gumu 智能上下文管理版）。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。

Gumu 特色：基于语义相关性的智能历史裁剪。当 prompt 超预算时，
不是简单地从尾部截断历史，而是根据当前用户请求的关键词，
对历史条目做相关性打分，保留最相关的条目，确保模型看到的上下文
对当前任务最有价值。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


DEFAULT_TOTAL_BUDGET = 12000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 3600,
    "memory": 1600,
    "relevant_memory": 1200,
    "history": 5200,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 1200,
    "memory": 400,
    "relevant_memory": 300,
    "history": 1500,
}
# 当 prompt 超预算时，会优先压缩这些 section。
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RELEVANT_MEMORY_LIMIT = 3

# 语义相关性：保留最近 N 条历史作为"近期窗口"，无论相关性如何都保留。
SEMANTIC_RECENT_WINDOW = 4
# 语义相关性：近期窗口之外的历史条目，按相关性打分后保留最多 N 条。
SEMANTIC_MAX_RELEVANT = 8
# 相关性加分：如果历史条目包含文件路径且与当前请求中的路径匹配，额外加分。
SEMANTIC_PATH_MATCH_BONUS = 3


def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


# ── Gumu 智能上下文管理：关键词提取与语义相关性打分 ──

# 中文停用词（高频但信息量低的词）
_CN_STOPWORDS = frozenset(
    "的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 "
    "自己 这 他 她 它 们 那 些 什么 怎么 为什么 如何 可以 这个 那个 以及 因为 所以 "
    "如果 但是 而且 或者 虽然 然后 现在 已经 可能 需要 应该 请 把 被 让 给 对 从 向 与 及 等 而 且 或 但"
    .split()
)

# 英文停用词
_EN_STOPWORDS = frozenset(
    "the a an is are was were be been being have has had do does did will would shall should "
    "may might can could must need to of in for on with at by from as into through during "
    "before after above below between out off over under again further then once here there "
    "when where why how all each every both few more most other some such no nor not only "
    "own same so than too very just because but and or if while although"
    .split()
)

# 技术关键词模式：文件名、函数名、类名、API 端点等
_TECH_PATTERNS = [
    re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[a-z]{2,5}"),  # file.ext
    re.compile(r"[A-Za-z_][A-Za-z0-9_]*\([^\)]*\)"),       # func(args)
    re.compile(r"/[A-Za-z0-9_./-]+"),                       # /path/to/file
    re.compile(r"[A-Z][a-z]+[A-Z][A-Za-z]*"),               # CamelCase
    re.compile(r"[a-z]+_[a-z_]+"),                           # snake_case
    re.compile(r"--?[a-z][a-z-]+"),                          # --cli-flags
    re.compile(r"[A-Z_]{3,}"),                               # CONSTANTS
]


def _extract_keywords(text):
    """从文本中提取有信息量的关键词。

    策略：
    1. 用技术模式匹配提取文件名、函数名、路径、类名等技术词汇
    2. 提取中英文单词，过滤停用词
    3. 去重后返回

    这样既保留了技术术语的完整性，又捕捉了自然语言中的关键概念。
    """
    keywords = set()
    text_lower = text.lower()

    # 1. 技术模式匹配
    for pattern in _TECH_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group().strip().rstrip("(").rstrip(")")
            if len(token) >= 2:
                keywords.add(token.lower())

    # 2. 英文单词提取（过滤停用词和短词）
    en_words = re.findall(r"[a-z]{3,}", text_lower)
    for word in en_words:
        if word not in _EN_STOPWORDS and len(word) >= 3:
            keywords.add(word)

    # 3. 中文词提取（简单按字符 n-gram + 过滤停用词）
    # 对于中文，提取 2-4 字的连续片段作为候选词
    cn_chars = re.findall(r"[\u4e00-\u9fff]+", text)
    for segment in cn_chars:
        if len(segment) >= 2:
            # 提取 2 字和 3 字片段
            for n in (2, 3):
                for i in range(len(segment) - n + 1):
                    chunk = segment[i : i + n]
                    if chunk not in _CN_STOPWORDS:
                        keywords.add(chunk)
            # 整个中文片段如果不太长也加入
            if len(segment) <= 6:
                keywords.add(segment)

    return keywords


def _score_history_relevance(history_item, query_keywords):
    """对单个历史条目计算与当前查询的语义相关性分数。

    打分规则：
    - 每个匹配的关键词 +1 分
    - 技术关键词（文件路径、函数名等）匹配 +2 分（权重更高）
    - 如果历史条目中的文件路径与查询中的路径匹配，额外 +3 分
    - 用户消息（role=user）比工具结果（role=tool）权重稍高

    返回 (score, matched_keywords) 便于调试。
    """
    if not query_keywords:
        return 0, set()

    item_text = ""
    if history_item.get("role") == "tool":
        item_text = f"{history_item.get('name', '')} {json.dumps(history_item.get('args', {}), sort_keys=True)} {history_item.get('content', '')}"
    else:
        item_text = str(history_item.get("content", ""))

    item_text_lower = item_text.lower()
    score = 0
    matched = set()

    for kw in query_keywords:
        if kw in item_text_lower:
            matched.add(kw)
            # 技术关键词（包含 . / _ 或大写字母的）权重更高
            if any(c in kw for c in "./_") or re.search(r"[A-Z]", kw):
                score += 2
            else:
                score += 1

    # 文件路径匹配额外加分
    query_paths = {kw for kw in query_keywords if "/" in kw or "." in kw}
    item_paths = set(re.findall(r"[A-Za-z_][A-Za-z0-9_./-]*\.[a-z]{2,5}", item_text))
    item_paths.update(re.findall(r"/[A-Za-z0-9_./-]+", item_text))
    if query_paths & item_paths:
        score += SEMANTIC_PATH_MATCH_BONUS

    # 用户消息稍高权重（因为是意图表达）
    if history_item.get("role") == "user":
        score = int(score * 1.2)

    return score, matched


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `Gumu.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            section_texts["prefix"] = section_texts["prefix"] + "\n\n" + checkpoint_text
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 如果 prompt 超预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先牺牲 relevant_memory，再牺牲 history，然后才动 memory 和 prefix。
        # 最新用户请求永远不裁剪，因为那是本轮最重要的输入。
        while len(prompt) > self.total_budget:
            overflow = len(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=section_texts,
        )
        return prompt, metadata

    def _render_sections_without_reduction(self, section_texts, selected_notes=None):
        selected_notes = selected_notes or []
        relevant_lines = ["Relevant memory:"]
        if selected_notes:
            relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [note["text"] for note in selected_notes],
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        floors = {
            section: max(20, int(budget) // 4)
            for section, budget in self.section_budgets.items()
        }
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_relevant_memory(self, selected_notes, budget):
        header = "Relevant memory:"
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join([header, "- none"])
        if not note_texts:
            rendered = raw
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            rendered_notes = [_tail_clip(text, per_note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if len(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = len(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def _render_history_section(self, budget):
        """渲染历史对话 section，Gumu 智能版。

        与原版的核心区别：
        原版只按时间顺序保留最近 N 条，超预算就从尾部截断。
        Gumu 版采用"近期窗口 + 语义相关性"混合策略：
        1. 始终保留最近 SEMANTIC_RECENT_WINDOW 条（近期上下文最重要）
        2. 对更早的历史条目，根据当前用户请求的关键词做相关性打分
        3. 保留相关性最高的 SEMANTIC_MAX_RELEVANT 条
        4. 在预算内优先渲染高相关性条目

        这样即使历史很长，模型也能看到与当前任务最相关的上下文，
        而不是简单地丢掉旧但可能很重要的信息。
        """
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)
        if not history:
            rendered = "Transcript:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                    "semantic_mode": True,
                    "relevant_entries_kept": 0,
                    "recent_entries_kept": 0,
                },
            )

        # 获取当前用户请求，用于关键词提取
        user_message = ""
        if hasattr(self.agent, "session") and self.agent.session:
            # 尝试从 session 中获取最近的用户消息
            for item in reversed(history):
                if item.get("role") == "user":
                    user_message = str(item.get("content", ""))
                    break

        query_keywords = _extract_keywords(user_message) if user_message else set()

        # 分近期窗口和远期历史
        recent_start = max(0, len(history) - SEMANTIC_RECENT_WINDOW)
        recent_entries = history[recent_start:]
        older_entries = history[:recent_start]

        # 对远期历史做相关性打分
        scored_older = []
        for idx, item in enumerate(older_entries):
            score, matched = _score_history_relevance(item, query_keywords)
            # 时间衰减：越近的旧条目基础分稍高
            time_bonus = (idx - len(older_entries)) * 0.1
            scored_older.append((idx, item, score + time_bonus, matched))

        # 按相关性排序，取 top N
        scored_older.sort(key=lambda x: x[2], reverse=True)
        top_relevant = scored_older[:SEMANTIC_MAX_RELEVANT]

        # 合并近期条目和相关性高的远期条目，保持原始时间顺序
        selected_indices = set()
        for idx, _, _, _ in top_relevant:
            selected_indices.add(idx)
        for idx in range(recent_start, len(history)):
            selected_indices.add(idx)

        # 按原始顺序构建待渲染条目
        ordered_entries = []
        for idx, item in enumerate(history):
            if idx in selected_indices:
                is_recent = idx >= recent_start
                ordered_entries.append({"index": idx, "item": item, "recent": is_recent})

        # 渲染条目
        history_entries = []
        seen_older_reads = set()
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
            "semantic_mode": True,
            "relevant_entries_kept": len(top_relevant),
            "recent_entries_kept": len(recent_entries),
            "query_keywords_sample": list(query_keywords)[:10],
        }

        for entry in ordered_entries:
            idx = entry["index"]
            item = entry["item"]
            recent = entry["recent"]

            if recent:
                line_limit = 900
                history_entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, line_limit),
                    }
                )
                continue

            # 远期条目的压缩逻辑（与原版相同）
            if item["role"] == "tool" and item["name"] == "read_file":
                path = str(item["args"].get("path", "")).strip()
                if path in seen_older_reads:
                    details["collapsed_duplicate_reads"] += 1
                    continue
                seen_older_reads.add(path)
                summary = self._reusable_file_summary(path)
                if summary:
                    history_entries.append({"recent": False, "lines": [f"{path} -> {summary}"]})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += 1
                    continue

            if item["role"] == "tool":
                summary_line = self._summarize_old_tool_item(item)
                history_entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue

            history_entries.append({"recent": False, "lines": self._render_history_item(item, 60)})

        # 按预算渲染，优先保留近期条目
        rendered_entries = []
        for entry in reversed(history_entries):
            recent = bool(entry.get("recent", False))
            candidate_lines = list(entry.get("lines", []))
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
            if len(candidate_rendered) <= budget:
                rendered_entries = candidate_entries
                continue
            if recent:
                available = budget - len("Transcript:")
                if rendered_entries:
                    available -= sum(len(line) + 1 for line in rendered_entries)
                available = max(20, available - 1)
                candidate_lines = [_tail_clip(line, available) for line in candidate_lines]
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
                if len(candidate_rendered) <= budget:
                    rendered_entries = candidate_entries
            else:
                smaller_lines = [_tail_clip(line, 20) for line in candidate_lines]
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["Transcript:", *smaller_entries])
                if len(smaller_rendered) <= budget:
                    rendered_entries = smaller_entries
        rendered = "\n".join(["Transcript:", *rendered_entries])

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "recent_window": SEMANTIC_RECENT_WINDOW,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
                **details,
            },
        )

    def _compressed_history_entries(self, history, recent_start):
        entries = []
        seen_older_reads = set()
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        for index, item in enumerate(history):
            recent = index >= recent_start
            if recent:
                line_limit = 900
                entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, line_limit),
                    }
                )
                continue

            if item["role"] == "tool" and item["name"] == "read_file":
                path = str(item["args"].get("path", "")).strip()
                if path in seen_older_reads:
                    details["collapsed_duplicate_reads"] += 1
                    continue
                seen_older_reads.add(path)
                summary = self._reusable_file_summary(path)
                if summary:
                    entries.append({"recent": False, "lines": [f"{path} -> {summary}"]})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += 1
                    continue

            if item["role"] == "tool":
                summary_line = self._summarize_old_tool_item(item)
                entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue

            entries.append({"recent": False, "lines": self._render_history_item(item, 60)})

        return entries, details

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        snapshot = memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), {})
        if not summary:
            return ""
        return str(summary.get("summary", "")).strip()

    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()]
            summary = " | ".join(lines[:3]) if lines else "(empty)"
            return f"{command} -> {summary}"
        return self._render_history_item(item, 60)[0]

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = _tail_clip(item["content"], max(20, line_limit))
            return [prefix, content]
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]

    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, user_message, section_texts):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }
