"""The assistant: role checks, intent, retrieval, cited answers or refusals, substitutes, quote drafts, logging."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import TypeVar

from pydantic import BaseModel, Field, ValidationError

from .catalog import Alternative, Filters
from .config import DISTRIBUTOR, ROLES, Settings
from .documents import Chunk
from .index import Index
from .providers import ChatClient, ProviderError
from .querylog import QueryLog
from .quote import QuoteDraft, QuoteRequest, build_quote, render_quote, save_quote_xlsx
from .retrieval import Hit, Retriever
from .text import normalize
from .tracing import Stage, Trace

M = TypeVar("M", bound=BaseModel)

QUOTE_WORDS = re.compile(r"报价|报个价|出个价|报价单|quote", re.I)
PRICE_WORDS = re.compile(r"多少钱|价格|价钱|单价|目录价|协议价|差价|几块钱|多少元|price", re.I)
SUBSTITUTE_WORDS = re.compile(r"替代|代替|替换|平替|换哪个|换什么|换成|没货|缺货|断货")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_MARKER = re.compile(r"\[(\d+)\]")

TOP_K = 5
TOP_K_FILTERED = 10

ROLE_RULES = {
    "销售": "可以回答价格；价目表中的价格是含税目录价（协议客户以协议价为准）。",
    "客服": "客服看不到价格，回答中不要出现任何价格或金额。",
}
ANSWER_SYSTEM = (
    f"你是{DISTRIBUTOR}的产品目录助手，回答公司销售和客服同事的问题。"
    "只能依据用户消息里 <资料> 中的内容回答，不要使用资料以外的知识，也不要做资料里没有的推算。\n"
    "规则：\n"
    "1. 资料能回答问题时：answerable=true；回答简洁，直接给出型号、参数和单位；每句事实后用 [编号] 标注来源，"
    "编号必须是资料里给出的编号。\n"
    "2. 资料不能回答问题时（问到的型号、规格、参数、品牌、认证等在资料中不存在）：answerable=false，answer 用一句话说明"
    "目录资料中没有这方面信息（可以顺带说明资料里有哪些相近内容），citations 为空。不要猜测。\n"
    "3. 表格数据原样引用，不要换算单位。\n"
    "4. 给出“系统匹配的替代候选”时，只推荐这些候选并说明替代条件；没有给出候选时不要自行推荐替代型号。\n"
    "5. 当前提问者角色：{role}。{role_rule}\n"
    '只输出一个 JSON 对象：{{"answerable": true 或 false, "answer": "回答正文", "citations": [引用的编号]}}'
)
QUOTE_SYSTEM = (
    "从销售同事的消息里提取报价请求，只输出一个 JSON 对象。只提取消息里写明的内容：customer 为客户名称原文（没写则 null）；"
    "items 每行包括 product（型号/规格原文，保留原写法，如“6204-2RS”“M8×20”“DN25 316 球阀”）、quantity（数字）、"
    "unit（原文单位，如 个、箱、台；没写则 null）、target_price（客户要求的单价；没写则 null）。不要换算单位，不要补全型号。\n"
    "JSON Schema:\n" + json.dumps(QuoteRequest.model_json_schema(), ensure_ascii=False)
)


class ModelAnswer(BaseModel):
    answerable: bool
    answer: str = Field(..., min_length=1, max_length=2000)
    citations: list[int] = Field(default_factory=list)


@dataclass
class Citation:
    n: int
    chunk_id: str
    doc: str
    locator: str
    label: str


@dataclass
class Answer:
    question: str
    role: str
    intent: str                   # qa | substitute | quote | price
    status: str                   # answered | refused | denied | quote | clarify | error
    text: str
    citations: list[Citation] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    alternatives: list[Alternative] = field(default_factory=list)
    quote: QuoteDraft | None = None
    warnings: list[str] = field(default_factory=list)
    trace: Trace = field(default_factory=Trace)
    filters: Filters = field(default_factory=Filters)
    log_id: int | None = None

    def render(self) -> str:
        """Plain text for the terminal and Feishu: answer, sources, trace line."""
        out = [self.text]
        if self.citations:
            out.append("来源：")
            out += [f"[{c.n}] {c.label}" for c in self.citations]
        out += [f"提示：{w}" for w in self.warnings]
        out.append(self.trace.summary())
        return "\n".join(out)

    def to_dict(self) -> dict:
        return {
            "question": self.question, "role": self.role, "intent": self.intent, "status": self.status,
            "text": self.text, "citations": [asdict(c) for c in self.citations],
            "retrieved": [{"chunk_id": h.chunk.id, "label": h.chunk.cite(), "score": round(h.score, 4),
                           "ranks": h.ranks} for h in self.hits],
            "alternatives": [{"sku": a.product.sku, "spec": a.product.spec, "name": a.product.name, "note": a.note}
                             for a in self.alternatives],
            "quote": _quote_dict(self.quote) if self.quote else None,
            "warnings": self.warnings, "trace": self.trace.to_dict(), "filters": asdict(self.filters),
            "log_id": self.log_id,
        }


def _quote_dict(q: QuoteDraft) -> dict:
    return {"number": q.number, "customer": q.customer.name if q.customer else q.customer_text,
            "lines": [{"line_no": ln.line_no, "sku": ln.product.sku, "spec": ln.product.spec, "name": ln.product.name,
                       "quantity": ln.quantity, "unit": ln.product.unit, "unit_price": ln.unit_price,
                       "price_basis": ln.price_basis, "amount": ln.amount} for ln in q.lines],
            "unresolved": [{"text": u.request_text, "reason": u.reason, "candidates": [p.spec for p in u.candidates]}
                           for u in q.unresolved],
            "total": q.total, "total_ex_tax": q.total_ex_tax, "flags": q.flags,
            "file": q.path.name if q.path else None}


class Assistant:
    def __init__(self, settings: Settings, index: Index, retriever: Retriever, chat: ChatClient, log: QueryLog):
        self.settings, self.index, self.retriever, self.chat, self.log = settings, index, retriever, chat, log

    # ------------------------------------------------------------------ entry point

    def ask(self, question: str, role: str, filters: Filters | None = None, *, channel: str = "cli",
            user: str = "local") -> Answer:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        filters = filters or Filters()
        answer = self._answer(question.strip(), role, filters)
        answer.log_id = self.log.write(
            channel=channel, user=user, role=role, question=question, filters=asdict(filters),
            intent=answer.intent, pipeline=self.settings.pipeline, status=answer.status, answer=answer.text,
            citations=[asdict(c) for c in answer.citations],
            retrieved=[{"chunk_id": h.chunk.id, "score": round(h.score, 4), "ranks": h.ranks} for h in answer.hits],
            warnings=answer.warnings, trace=answer.trace.to_dict())
        return answer

    def _answer(self, question: str, role: str, filters: Filters) -> Answer:
        if QUOTE_WORDS.search(question):
            if role != "销售":
                return Answer(question, role, "quote", "denied", "当前角色（客服）无权生成报价，请联系对接销售。",
                              filters=filters)
            return self._quote(question, role, filters)
        if role != "销售" and (PRICE_WORDS.search(question) or filters.uses_price):
            return Answer(question, role, "price", "denied", "当前角色（客服）无权查看价格信息，请联系对接销售。",
                          filters=filters)
        intent = "substitute" if SUBSTITUTE_WORDS.search(question) else "qa"
        try:
            return self._rag(question, role, filters, intent)
        except ProviderError as exc:
            return Answer(question, role, intent, "error", f"模型或检索服务调用失败：{exc}", filters=filters)

    # ------------------------------------------------------------------ RAG answer

    def _allowed(self, role: str, filters: Filters) -> set[str]:
        visible = [c for c in self.index.chunks if role == "销售" or not c.sales_only]
        if not filters.active:
            return {c.id for c in visible}
        skus = {p.sku for p in self.index.catalog.filter(filters)}
        return {c.id for c in visible if skus.intersection(c.skus)}

    def _rag(self, question: str, role: str, filters: Filters, intent: str) -> Answer:
        trace = Trace()
        answer = Answer(question, role, intent, "refused", "", trace=trace, filters=filters)
        allowed = self._allowed(role, filters)
        if not allowed:
            answer.text = f"目录中没有符合筛选条件（{filters.describe()}）的产品。"
            return answer
        k = TOP_K_FILTERED if filters.active else TOP_K
        answer.hits, answer.warnings = self.retriever.search(question, allowed, k, trace)
        context = [h.chunk for h in answer.hits]
        notes: list[str] = []
        if intent == "substitute":
            context, notes = self._substitutes(question, role, answer, context)
        if not context:
            answer.text = "目录资料中没有找到与这个问题相关的内容。"
            return answer
        model_answer = self._generate(question, role, filters, context, notes, trace)
        self._finish(answer, model_answer, context)
        return answer

    def _substitutes(self, question: str, role: str, answer: Answer,
                     context: list[Chunk]) -> tuple[list[Chunk], list[str]]:
        product, _ = self.index.catalog.resolve(question)
        if product is None:
            return context, []
        answer.alternatives = self.index.catalog.alternatives(product.sku)
        extra_ids = [product.spec_chunk] + [a.product.spec_chunk for a in answer.alternatives]
        selection = self.index.section_chunk(product.doc, "替代")
        if selection:
            extra_ids.append(selection.id)
        extra = [self.index.by_id[i] for i in extra_ids if i in self.index.by_id]
        merged = list({c.id: c for c in extra + context}.values())   # keep order, drop duplicates
        visible = [c for c in merged if role == "销售" or not c.sales_only]
        if answer.alternatives:
            items = "；".join(f"{a.product.spec}（物料编码 {a.product.sku}）：{a.note}" for a in answer.alternatives)
            notes = [f"系统按规格书第 6 节替代规则为 {product.spec} 匹配的替代候选：{items}"]
        else:
            notes = [f"系统没有为 {product.spec} 匹配到符合规格书替代规则的候选，不要自行推荐替代型号"]
        return visible, notes

    def _generate(self, question: str, role: str, filters: Filters, context: list[Chunk], notes: list[str],
                  trace: Trace) -> ModelAnswer:
        blocks = []
        for n, chunk in enumerate(context, 1):
            body = chunk.text.split("\n", 1)[1] if "\n" in chunk.text else chunk.text
            blocks.append(f"[{n}] 来源：{chunk.cite()}｜{chunk.title}\n{body}")
        extra = ""
        if filters.active:
            extra += f"\n筛选条件：{filters.describe()}（资料已按该条件筛选，只列出符合条件的产品）"
        for note in notes:
            extra += f"\n{note}"
        messages = [
            {"role": "system", "content": ANSWER_SYSTEM.format(role=role, role_rule=ROLE_RULES[role])},
            {"role": "user", "content": "<资料>\n" + "\n\n".join(blocks) + "\n</资料>" + extra + f"\n\n问题：{question}"},
        ]
        return self._structured(messages, ModelAnswer, trace, "generate")

    def _finish(self, answer: Answer, model: ModelAnswer, context: list[Chunk]) -> None:
        if not model.answerable:
            answer.status = "refused"
            answer.text = model.answer
            return
        answer.status = "answered"
        markers = {int(m) for m in _MARKER.findall(model.answer)}
        used = sorted(markers | set(model.citations))
        valid = [n for n in used if 1 <= n <= len(context)]
        invalid = [n for n in used if n not in valid]
        text = model.answer
        for n in invalid:
            text = text.replace(f"[{n}]", "")
            answer.warnings.append(f"模型引用了不存在的资料编号 [{n}]，已移除")
        answer.citations = [Citation(n, context[n - 1].id, context[n - 1].doc, context[n - 1].locator,
                                     context[n - 1].cite()) for n in valid]
        if not answer.citations:
            answer.warnings.append("回答没有标注来源，请人工核对")
        prompt_text = " ".join(c.text for c in context) + " " + answer.question + " " + answer.filters.describe()
        grounded = set(_NUMBER.findall(normalize(prompt_text)))
        stray = [n for n in dict.fromkeys(_NUMBER.findall(normalize(_MARKER.sub("", text))))
                 if n not in grounded and (len(n) > 1 or "." in n)]
        if stray and answer.role == "客服":
            for n in stray:
                text = re.sub(rf"([¥￥]\s*){re.escape(n)}|{re.escape(n)}(\s*元)", "***", text)
        if stray:
            answer.warnings.append("回答中的数字 " + "、".join(stray) + " 没有出现在引用资料里，请人工核对")
        answer.text = text

    # ------------------------------------------------------------------ quote draft

    def _quote(self, question: str, role: str, filters: Filters) -> Answer:
        trace = Trace()
        answer = Answer(question, role, "quote", "clarify", "", trace=trace, filters=filters)
        try:
            request = self._structured([{"role": "system", "content": QUOTE_SYSTEM},
                                        {"role": "user", "content": question}], QuoteRequest, trace, "quote_parse")
        except ProviderError as exc:
            answer.status, answer.text = "error", f"无法解析报价请求：{exc}"
            return answer
        start = time.perf_counter()
        draft = build_quote(request, self.index.catalog, self.settings.price_tolerance,
                            dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date())
        if draft.complete:
            save_quote_xlsx(draft, self.settings.quotes_dir)
            answer.status = "quote"
        trace.add(Stage("quote_build", round((time.perf_counter() - start) * 1000, 1)))
        answer.quote = draft
        answer.text = render_quote(draft)
        for line in draft.lines:
            chunk = self.index.by_id.get(line.product.price_chunk)
            if line.price_basis == "目录价" and chunk:
                answer.citations.append(Citation(len(answer.citations) + 1, chunk.id, chunk.doc, chunk.locator,
                                                 chunk.cite()))
        return answer

    # ------------------------------------------------------------------ structured output with one retry

    def _structured(self, messages: list[dict[str, str]], schema: type[M], trace: Trace, stage: str) -> M:
        content, st = self.chat.complete(messages, stage_name=stage)
        trace.add(st)
        try:
            return schema.model_validate_json(_strip_fences(content))
        except ValidationError as exc:
            error = exc.json(include_url=False)[:1500]
        retry = messages + [{"role": "assistant", "content": content},
                            {"role": "user", "content": "上面的 JSON 未通过校验，请修正后只输出完整 JSON。校验错误：" + error}]
        content, st = self.chat.complete(retry, stage_name=stage + "_retry")
        trace.add(st)
        try:
            return schema.model_validate_json(_strip_fences(content))
        except ValidationError as exc:
            raise ProviderError(f"模型输出两次未通过校验：{exc.json(include_url=False)[:200]}") from exc


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()
