"""Quote drafts: agreed prices, unit conversion, tolerance / credit / freight flags, unresolved items, Excel."""
from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from catalog_rag.quote import QuoteItemRequest, QuoteRequest, build_quote, render_quote, save_quote_xlsx
from tests.support import catalog

TODAY = dt.date(2026, 11, 18)


def request(customer: str | None, *items: tuple) -> QuoteRequest:
    return QuoteRequest(customer=customer, items=[QuoteItemRequest(product=i[0], quantity=i[1], unit=i[2],
                                                                   target_price=i[3] if len(i) > 3 else None)
                                                  for i in items])


class QuoteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = catalog()

    def quote(self, customer: str | None, *items: tuple):
        return build_quote(request(customer, *items), self.cat, 0.02, TODAY)

    def test_agreed_prices_and_total(self):
        q = self.quote("沪东衡砺", ("6204-2RS", 200, "个"), ("M8×20", 5, "箱"))
        self.assertEqual(q.customer.id, "C001")
        self.assertEqual([(ln.product.sku, ln.quantity, ln.unit_price, ln.price_basis) for ln in q.lines],
                         [("1.01.0009", 200, 10.8, "协议价"), ("3.01.0021", 5, 106.0, "协议价")])
        self.assertEqual(q.total, 2690.0)
        self.assertEqual(q.total_ex_tax, round(2690 / 1.13, 2))
        self.assertTrue(q.complete)
        self.assertTrue(any("运费" in f for f in q.flags))   # below the 3000 free-freight threshold

    def test_list_price_when_no_agreement(self):
        q = self.quote("苏州晨岚汇", ("YE3 3kW 4极", 2, "台"), ("DN25 316 球阀", 10, "个"))
        self.assertEqual([(ln.unit_price, ln.price_basis) for ln in q.lines], [(1523.8, "目录价"), (126.4, "目录价")])
        self.assertEqual(q.total, 4311.6)
        self.assertFalse(any("运费" in f for f in q.flags))

    def test_inner_units_convert_to_boxes_and_round_up(self):
        q = self.quote("甬港澄远", ("PC6-01", 30, "个"))
        self.assertEqual((q.lines[0].quantity, q.lines[0].unit_price), (3, 34.3))
        q = self.quote("甬港澄远", ("PC6-01", 25, "个"))
        self.assertEqual(q.lines[0].quantity, 3)
        self.assertTrue(any("不是整箱" in f for f in q.flags))

    def test_target_price_outside_tolerance_needs_approval(self):
        self.assertTrue(any("需销售经理审批" in f for f in self.quote("沪东衡砺", ("6204-2RS", 10, "个", 9.9)).flags))
        self.assertFalse(any("审批" in f for f in self.quote("沪东衡砺", ("6204-2RS", 10, "个", 10.7)).flags))

    def test_credit_limit(self):
        q = self.quote("佛山顺达屿", ("YE3 3kW 4极", 1, "台"))
        self.assertTrue(any("信用额度" in f for f in q.flags))

    def test_unresolved_items_block_the_draft(self):
        q = self.quote("沪东衡砺", ("SKF 6204", 10, "个"), ("2.5平方 BV", 2, "卷"), ("6204-2RS", 3, "米"))
        self.assertFalse(q.complete)
        self.assertEqual([u.reason for u in q.unresolved][:2], ["目录中找不到这个型号", "型号不唯一，请指明"])
        self.assertEqual(len(q.unresolved[1].candidates), 3)
        self.assertIn("无法识别", q.unresolved[2].reason)

    def test_unknown_customer_uses_list_prices(self):
        q = self.quote("某某公司", ("6204-2RS", 10, None))
        self.assertIsNone(q.customer)
        self.assertEqual(q.lines[0].unit_price, 11.4)
        self.assertTrue(any("未识别客户" in f for f in q.flags))

    def test_excel_and_text_rendering(self):
        q = self.quote("沪东衡砺", ("6204-2RS", 200, "个"))
        folder = Path(tempfile.mkdtemp(prefix="quotes_"))
        self.addCleanup(shutil.rmtree, folder, True)
        path = save_quote_xlsx(q, folder)
        values = [v for row in load_workbook(path).active.iter_rows(values_only=True) for v in row if v is not None]
        self.assertIn(2160.0, values)
        self.assertIn("草稿", render_quote(q))
        self.assertIn("¥2,160.00", render_quote(q))


if __name__ == "__main__":
    unittest.main()
