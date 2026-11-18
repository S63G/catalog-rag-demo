"""Structured catalogue built from the parsed documents: product rows, prices, filters, substitutes, customers.

Spec attributes come from the PDF table rows and prices from the price-list rows (both parsed at ingest time, not
from the generator). Customer agreed prices and credit data come from the ERP master (``data/master/customers.json``).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .documents import Chunk
from .text import compact, contains_token, normalize


@dataclass
class Product:
    sku: str
    name: str                 # 品名, e.g. 深沟球轴承
    spec: str                 # 规格型号, e.g. 6204-2RS
    unit: str                 # sales unit: 件 / 台 / 卷 / 箱
    pack: str                 # e.g. "100件/箱", "100m/卷", "按件"
    list_price: float         # 目录含税单价 per sales unit
    lead_time: str
    attributes: dict[str, str] = field(default_factory=dict)
    spec_chunk: str = ""      # chunk id of the spec-sheet table row
    price_chunk: str = ""     # chunk id of the price-list row
    doc: str = ""

    @property
    def pack_qty(self) -> int:
        match = re.match(r"(\d+)(\D+)/箱", self.pack)
        return int(match.group(1)) if match and self.unit == "箱" else 1

    @property
    def base_unit(self) -> str:
        match = re.match(r"\d+(\D+)/箱", self.pack)
        return match.group(1) if match and self.unit == "箱" else self.unit


@dataclass
class Customer:
    id: str
    name: str
    name_en: str
    contact: str
    credit_limit: float
    open_balance: float
    agreed_prices: dict[str, float]


@dataclass
class Filters:
    model: str | None = None
    material: str | None = None
    size: str | None = None
    price_min: float | None = None
    price_max: float | None = None

    @property
    def active(self) -> bool:
        return any(v not in (None, "") for v in asdict(self).values())

    @property
    def uses_price(self) -> bool:
        return self.price_min is not None or self.price_max is not None

    def describe(self) -> str:
        parts = []
        if self.model:
            parts.append(f"型号含“{self.model}”")
        if self.material:
            parts.append(f"材质含“{self.material}”")
        if self.size:
            parts.append(f"尺寸含“{self.size}”")
        if self.uses_price:
            low = f"{self.price_min:g}" if self.price_min is not None else "0"
            high = f"{self.price_max:g}" if self.price_max is not None else "不限"
            parts.append(f"目录含税单价 {low}~{high} 元")
        return "，".join(parts)


@dataclass(frozen=True)
class SubstitutionRule:
    same: tuple[str, ...]        # columns that must be equal
    upsize: str | None = None    # if set: the next larger value of this column
    note: str = ""


# Substitution logic per category, taken from section 6 "选型与替代说明" of each spec sheet.
SUBSTITUTION_RULES: dict[str, SubstitutionRule] = {
    "深沟球轴承": SubstitutionRule(("内径d(mm)", "外径D(mm)", "宽度B(mm)"), note="外形尺寸相同、密封形式不同，可互换"),
    "O型密封圈": SubstitutionRule(("内径(mm)", "线径(mm)"), note="尺寸相同、材质不同，注意耐温范围"),
    "不锈钢球阀": SubstitutionRule(("公称通径", "连接螺纹"), note="通径和螺纹相同、阀体材质不同"),
    "交流接触器": SubstitutionRule(("线圈电压",), upsize="额定电流Ie(A)", note="线圈电压相同、额定电流大一档"),
    "通用变频器": SubstitutionRule(("输入电压",), upsize="适配电机(kW)", note="功率大一档"),
    "导轨式接线端子": SubstitutionRule((), upsize="额定截面(mm2)", note="额定截面大一档，宽度增加"),
    "PU气管": SubstitutionRule(("外径(mm)", "内径(mm)"), note="尺寸相同、颜色不同"),
    "PVC电工胶带": SubstitutionRule(("宽度(mm)", "长度(m)"), note="规格相同、颜色不同；用作相色标识时不可替代"),
    "中间继电器": SubstitutionRule(("线圈电压",), note="线圈电压相同；MY2N 与 MY4N 引脚数不同，需同时更换插座"),
    "直线导轨": SubstitutionRule(("导轨宽度(mm)", "导轨高度(mm)"), upsize="长度(mm)", note="同规格更长的导轨，可截短使用"),
    "内六角圆柱头螺钉": SubstitutionRule(("螺纹规格",), upsize="长度L(mm)", note="同螺纹长一档，需确认旋合长度且不顶底"),
    "标准气缸": SubstitutionRule(("缸径(mm)",), upsize="行程(mm)", note="同缸径行程更长，需确认安装空间"),
}


@dataclass
class Alternative:
    product: Product
    note: str


def _number(value: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", value)
    return float(match.group()) if match else None


class Catalog:
    def __init__(self, products: list[Product], customers: list[Customer]):
        self.products = {p.sku: p for p in products}
        self.customers = {c.id: c for c in customers}

    # ------------------------------------------------------------------ build / persist

    @classmethod
    def from_chunks(cls, chunks: list[Chunk], customers: list[Customer]) -> "Catalog":
        specs = {c.skus[0]: c for c in chunks if c.kind == "table_row" and c.skus}
        products = []
        for c in chunks:
            if c.kind != "price_row" or not c.row:
                continue
            row = c.row
            spec = specs.get(row["物料编码"])
            products.append(Product(
                sku=row["物料编码"], name=row["品名"], spec=row["规格型号"], unit=row["销售单位"], pack=row["包装"],
                list_price=float(row["目录含税单价(元)"]), lead_time=row["标准交期(天)"],
                attributes={k: v for k, v in (spec.row or {}).items() if k not in ("型号", "物料编码")} if spec else {},
                spec_chunk=spec.id if spec else "", price_chunk=c.id, doc=spec.doc if spec else ""))
        return cls(products, customers)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps([asdict(p) for p in self.products.values()], ensure_ascii=False, indent=1),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path, customers_path: Path) -> "Catalog":
        products = [Product(**row) for row in json.loads(path.read_text(encoding="utf-8"))]
        return cls(products, load_customers(customers_path))

    # ------------------------------------------------------------------ matching

    def match(self, text: str) -> list[tuple[Product, float]]:
        """Products whose model code appears in ``text``; score = share of spec tokens present."""
        query = normalize(text)
        scored: list[tuple[Product, float]] = []
        for p in self.products.values():
            if contains_token(query, p.sku):
                scored.append((p, 1.0))
                continue
            tokens = normalize(p.spec).split()
            if not tokens or not contains_token(query, tokens[0]):
                continue
            scored.append((p, sum(contains_token(query, t) for t in tokens) / len(tokens)))
        return sorted(scored, key=lambda x: -x[1])

    def resolve(self, text: str) -> tuple[Product | None, list[Product]]:
        """The single best match, or None plus the tied candidates when ``text`` is ambiguous."""
        scored = self.match(text)
        if not scored:
            return None, []
        best = scored[0][1]
        top = [p for p, s in scored if s == best]
        return (top[0], top) if len(top) == 1 else (None, top)

    def match_customer(self, text: str) -> Customer | None:
        """Unique customer sharing the longest name fragment (>= 4 characters) with ``text``."""
        query = compact(text)
        best: list[tuple[int, Customer]] = []
        for c in self.customers.values():
            names = [compact(c.name), compact(c.name_en.replace("(fictional)", ""))]
            length = max(_longest_common(query, n) for n in names)
            if length >= 4:
                best.append((length, c))
        best.sort(key=lambda x: -x[0])
        if not best or (len(best) > 1 and best[0][0] == best[1][0]):
            return None
        return best[0][1]

    # ------------------------------------------------------------------ filters and substitutes

    def filter(self, f: Filters) -> list[Product]:
        out = []
        for p in self.products.values():
            haystack = normalize(" ".join([p.spec, *p.attributes.values()]))
            if f.model and compact(f.model) not in compact(p.spec) and f.model != p.sku:
                continue
            if f.material and compact(f.material) not in compact(p.attributes.get("材质", "")):
                continue
            if f.size and not contains_token(haystack, normalize(f.size)):
                continue
            if f.price_min is not None and p.list_price < f.price_min:
                continue
            if f.price_max is not None and p.list_price > f.price_max:
                continue
            out.append(p)
        return out

    def alternatives(self, sku: str) -> list[Alternative]:
        original = self.products[sku]
        rule = SUBSTITUTION_RULES.get(original.name)
        if rule is None:
            return []
        same = [p for p in self.products.values()
                if p.sku != sku and p.name == original.name
                and all(p.attributes.get(col) == original.attributes.get(col) for col in rule.same)]
        if rule.upsize:
            base = _number(original.attributes.get(rule.upsize, ""))
            bigger = [(v, p) for p in same if (v := _number(p.attributes.get(rule.upsize, ""))) is not None
                      and base is not None and v > base]
            if not bigger:
                return []
            step = min(v for v, _ in bigger)
            same = [p for v, p in bigger if v == step]
        return [Alternative(p, rule.note) for p in same]

    def agreed_price(self, customer: Customer | None, sku: str) -> tuple[float, str]:
        """Demo A's rule: the customer's agreed price if there is one, otherwise the list price."""
        if customer and sku in customer.agreed_prices:
            return customer.agreed_prices[sku], "协议价"
        return self.products[sku].list_price, "目录价"


def load_customers(path: Path) -> list[Customer]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [Customer(r["id"], r["name"], r["name_en"], r["contact"], float(r["credit_limit"]),
                     float(r["open_balance"]), {k: float(v) for k, v in r["agreed_prices"].items()}) for r in rows]


def _longest_common(a: str, b: str) -> int:
    best = 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, other in enumerate(b, 1):
            if ch == other:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best
