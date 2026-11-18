"""“生成报价草稿”: the model only parses the request; pricing is deterministic and follows the order-entry demo.

Price rule: the customer's agreed price if the ERP master has one, otherwise the list price. Quantities given in the
inner unit of a boxed item are converted to boxes (rounded up to whole boxes, per price policy item 5, and flagged).
A target price outside the ±tolerance band, a credit-limit breach and an order below the free-freight threshold are
flagged. The draft is saved as an Excel file and is never sent anywhere.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from pydantic import BaseModel, Field

from .catalog import Catalog, Customer, Product
from .config import DISTRIBUTOR

FREE_FREIGHT = 3000.0
FREIGHT_FEE = 15.0
TAX_RATE = 0.13

# Units as people write them -> canonical unit (same table as the order-entry demo).
UNIT_ALIASES: dict[str, str] = {
    "件": "件", "个": "件", "只": "件", "支": "件", "pcs": "件", "pc": "件", "piece": "件", "pieces": "件",
    "ea": "件", "each": "件", "条": "件", "根": "件",
    "台": "台", "set": "台", "sets": "台", "unit": "台", "units": "台",
    "卷": "卷", "roll": "卷", "rolls": "卷",
    "箱": "箱", "box": "箱", "boxes": "箱", "ctn": "箱", "carton": "箱", "cartons": "箱",
    "包": "包", "bag": "包", "bags": "包", "pack": "包", "packs": "包",
}
COUNT_UNITS = {"件", "台"}


class QuoteItemRequest(BaseModel):
    product: str = Field(..., min_length=1, description="型号/规格原文，如 6204-2RS、M8×20、DN25 316 球阀")
    quantity: float = Field(..., gt=0)
    unit: str | None = Field(None, description="原文单位，如 个、箱、台；没有写则 null")
    target_price: float | None = Field(None, gt=0, description="客户要求的单价；没有写则 null")


class QuoteRequest(BaseModel):
    customer: str | None = Field(None, description="客户名称原文；没有写则 null")
    items: list[QuoteItemRequest] = Field(..., min_length=1)


@dataclass
class QuoteLine:
    line_no: int
    product: Product
    request_text: str
    quantity: float               # in the product's sales unit
    unit_price: float             # 含税, per sales unit
    price_basis: str              # 协议价 / 目录价
    source: str                   # where the price comes from

    @property
    def amount(self) -> float:
        return round(self.quantity * self.unit_price, 2)


@dataclass
class Unresolved:
    request_text: str
    reason: str
    candidates: list[Product] = field(default_factory=list)


@dataclass
class QuoteDraft:
    number: str
    customer: Customer | None
    customer_text: str | None
    lines: list[QuoteLine]
    unresolved: list[Unresolved]
    flags: list[str]
    created: dt.date
    path: Path | None = None

    @property
    def total(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)

    @property
    def total_ex_tax(self) -> float:
        return round(self.total / (1 + TAX_RATE), 2)

    @property
    def complete(self) -> bool:
        return bool(self.lines) and not self.unresolved


def _to_sales_unit(product: Product, item: QuoteItemRequest) -> tuple[float | None, str | None]:
    """(quantity in sales units, flag) or (None, reason) when the unit cannot be converted."""
    unit = UNIT_ALIASES.get((item.unit or "").strip().lower()) if item.unit else product.unit
    if unit is None:
        return None, f"单位“{item.unit}”无法识别"
    if unit == product.unit or (unit in COUNT_UNITS and product.unit in COUNT_UNITS):
        return item.quantity, None
    if product.pack_qty > 1 and unit == product.base_unit:
        boxes = item.quantity / product.pack_qty
        if abs(boxes - round(boxes)) < 1e-9:
            return float(round(boxes)), None
        up = math.ceil(boxes)
        return float(up), (f"{product.spec} 按箱销售（{product.pack}），{item.quantity:g}{item.unit} 不是整箱，"
                           f"草稿按 {up} 箱报价，需与客户确认")
    return None, f"单位“{item.unit}”无法换算为该品的销售单位“{product.unit}”"


def build_quote(request: QuoteRequest, catalog: Catalog, tolerance: float, today: dt.date) -> QuoteDraft:
    customer = catalog.match_customer(request.customer) if request.customer else None
    flags: list[str] = []
    if customer is None:
        flags.append(f"未识别客户“{request.customer or '（未写客户）'}”，全部按目录价报价")
    lines: list[QuoteLine] = []
    unresolved: list[Unresolved] = []
    for item in request.items:
        product, candidates = catalog.resolve(item.product)
        if product is None:
            reason = "型号不唯一，请指明" if candidates else "目录中找不到这个型号"
            unresolved.append(Unresolved(item.product, reason, candidates[:6]))
            continue
        qty, note = _to_sales_unit(product, item)
        if qty is None:
            unresolved.append(Unresolved(item.product, note or "单位无法换算"))
            continue
        if note:
            flags.append(note)
        price, basis = catalog.agreed_price(customer, product.sku)
        source = "客户协议价（ERP 主数据）" if basis == "协议价" else f"价目表 {product.price_chunk}"
        line = QuoteLine(len(lines) + 1, product, item.product, qty, price, basis, source)
        lines.append(line)
        if item.target_price is not None:
            per_inner_unit = product.pack_qty > 1 and UNIT_ALIASES.get((item.unit or "").lower()) == product.base_unit
            target = item.target_price * (product.pack_qty if per_inner_unit else 1)
            deviation = (target - price) / price
            if abs(deviation) > tolerance + 1e-9:
                flags.append(f"第{line.line_no}行目标价 ¥{target:,.2f} 与{basis} ¥{price:,.2f} 偏差 {deviation:+.1%}，"
                             f"超出 ±{tolerance:.0%}，需销售经理审批")
    total = round(sum(line.amount for line in lines), 2)
    if customer and customer.open_balance + total > customer.credit_limit:
        flags.append(f"应收余额 ¥{customer.open_balance:,.2f} + 本报价 ¥{total:,.2f} 超出信用额度 "
                     f"¥{customer.credit_limit:,.2f}，下单前需财务审批")
    if lines and total < FREE_FREIGHT:
        flags.append(f"含税合计不满 {FREE_FREIGHT:,.0f} 元，华东地区另收运费 {FREIGHT_FEE:.0f} 元（价格政策第 4 条）")
    digest = hashlib.sha256(repr((customer.id if customer else request.customer,
                                  [(line.product.sku, line.quantity) for line in lines])).encode()).hexdigest()[:6]
    number = f"Q-{today:%Y%m%d}-{digest.upper()}"
    return QuoteDraft(number, customer, request.customer, lines, unresolved, flags, today)


def save_quote_xlsx(draft: QuoteDraft, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "报价草稿"
    ws.append([f"{DISTRIBUTOR} 报价草稿 {draft.number}（演示数据，均为虚构；需销售确认后才能发给客户）"])
    ws["A1"].font = Font(bold=True)
    ws.append([f"客户：{draft.customer.name if draft.customer else draft.customer_text or '未识别'}",
               f"日期：{draft.created.isoformat()}", "有效期：15 天"])
    ws.append([])
    ws.append(["行", "物料编码", "品名", "规格型号", "数量", "单位", "含税单价", "价格依据", "金额"])
    for c in ws[4]:
        c.font = Font(bold=True)
    for line in draft.lines:
        p = line.product
        ws.append([line.line_no, p.sku, p.name, p.spec, line.quantity, p.unit, line.unit_price, line.price_basis,
                   line.amount])
    ws.append([])
    ws.append(["", "", "", "", "", "", "含税合计", "", draft.total])
    ws.append(["", "", "", "", "", "", "不含税金额", "", draft.total_ex_tax])
    for flag in draft.flags:
        ws.append([f"注意：{flag}"])
    for col, width in zip("ABCDEFGHI", (5, 12, 16, 24, 8, 6, 12, 10, 12)):
        ws.column_dimensions[col].width = width
    path = folder / f"{draft.number}.xlsx"
    wb.save(path)
    draft.path = path
    return path


def render_quote(draft: QuoteDraft) -> str:
    who = draft.customer.name if draft.customer else (draft.customer_text or "未识别客户")
    out = [f"报价草稿 {draft.number}（客户：{who}）"]
    for line in draft.lines:
        p = line.product
        out.append(f"{line.line_no}. {p.sku} {p.name} {p.spec} × {line.quantity:g}{p.unit} "
                   f"@ ¥{line.unit_price:,.2f}（{line.price_basis}）= ¥{line.amount:,.2f}")
    if draft.lines:
        out.append(f"含税合计 ¥{draft.total:,.2f}（不含税 ¥{draft.total_ex_tax:,.2f}，税率 13%）")
    for u in draft.unresolved:
        options = "、".join(p.spec for p in u.candidates)
        out.append(f"未报价：“{u.request_text}” {u.reason}" + (f"（候选：{options}）" if options else ""))
    out += [f"注意：{flag}" for flag in draft.flags]
    out.append("这是草稿，需销售确认后再发给客户；价格依据为客户协议价（ERP 主数据）或 2026Q4 目录价。")
    return "\n".join(out)
