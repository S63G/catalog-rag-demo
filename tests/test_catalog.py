"""Text normalisation, product matching, filters, substitutes and the agreed-price rule."""
from __future__ import annotations

import unittest

from catalog_rag.catalog import Filters
from catalog_rag.text import compact, contains_token, normalize, tokenize
from tests.support import catalog


class TextTest(unittest.TestCase):
    def test_normalize_maps_domain_spellings(self):
        self.assertEqual(normalize("2.5平方 BV线"), "2.5mm2 bv线")
        self.assertEqual(normalize("YE3 3kW 4极"), "ye3 3kw 4p")
        self.assertEqual(normalize("Ｍ８×２０"), "m8x20")
        self.assertEqual(compact("25 × 3  FKM"), "25x3fkm")

    def test_tokenize_keeps_model_codes_and_their_parts(self):
        tokens = tokenize("6204-2RS 深沟球轴承")
        for t in ("6204-2rs", "6204", "2rs", "深沟", "轴承", "球"):
            self.assertIn(t, tokens)

    def test_contains_token_respects_alphanumeric_edges(self):
        self.assertTrue(contains_token("m6 1.0 10", "m6"))
        self.assertFalse(contains_token("m16x1.5", "m6"))
        self.assertFalse(contains_token("ye3 5.5kw", "5kw"))
        self.assertTrue(contains_token("cjx2-1810 线圈ac220v", "线圈ac220v"))


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = catalog()

    def spec(self, spec: str):
        return next(p for p in self.cat.products.values() if p.spec == spec)

    def test_products_combine_spec_rows_and_price_rows(self):
        p = self.spec("6204-2RS")
        self.assertEqual(len(self.cat.products), 300)
        self.assertEqual((p.sku, p.list_price, p.attributes["外径D(mm)"]), ("1.01.0009", 11.4, "47"))
        self.assertTrue(p.spec_chunk.startswith("RF-01:p2:") and p.price_chunk.startswith("瑞丰价目表"))
        screw = self.spec("M8×20 12.9级")
        self.assertEqual((screw.unit, screw.pack_qty, screw.base_unit), ("箱", 100, "件"))

    def test_resolve_unique_and_ambiguous(self):
        self.assertEqual(self.cat.resolve("6204-2RS 200个")[0].sku, "1.01.0009")
        self.assertEqual(self.cat.resolve("YE3 3kW 4极电机 2台")[0].sku, "5.01.0010")
        self.assertEqual(self.cat.resolve("M8×20内六角螺钉 5箱")[0].sku, "3.01.0021")
        product, candidates = self.cat.resolve("2.5平方的BV线")
        self.assertIsNone(product)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(self.cat.resolve("SKF 轴承"), (None, []))

    def test_filters(self):
        valves = self.cat.filter(Filters(material="316", price_max=150))
        self.assertEqual(sorted(p.spec for p in valves), ["DN15 316", "DN20 316", "DN25 316", "DN32 316"])
        self.assertEqual(len(self.cat.filter(Filters(size="M6"))), 6)
        self.assertEqual(len(self.cat.filter(Filters(material="FKM"))), 8)
        self.assertEqual(len(self.cat.filter(Filters(model="LJ18"))), 4)
        self.assertFalse(Filters().active)
        self.assertIn("目录含税单价 0~150 元", Filters(price_max=150).describe())

    def test_alternatives_follow_the_spec_sheet_rules(self):
        def alts(spec: str) -> list[str]:
            return [a.product.spec for a in self.cat.alternatives(self.spec(spec).sku)]

        self.assertEqual(alts("6205-2RS"), ["6205-ZZ"])
        self.assertEqual(alts("CJX2-1810 线圈AC220V"), ["CJX2-2510 线圈AC220V"])
        self.assertEqual(alts("25×3 NBR"), ["25×3 FKM"])
        self.assertEqual(alts("DN32 304"), ["DN32 316"])
        self.assertEqual(alts("CJX2-6511 线圈AC380V"), [])     # nothing larger
        self.assertEqual(alts("DZ47-63 2P C16"), [])           # sheet says: no substitution

    def test_agreed_price_then_list_price(self):
        c001 = self.cat.customers["C001"]
        self.assertEqual(self.cat.agreed_price(c001, "1.01.0009"), (10.8, "协议价"))
        self.assertEqual(self.cat.agreed_price(c001, "9.01.0006"), (126.4, "目录价"))
        self.assertEqual(self.cat.agreed_price(None, "1.01.0009"), (11.4, "目录价"))

    def test_match_customer(self):
        self.assertEqual(self.cat.match_customer("沪东衡砺").id, "C001")
        self.assertEqual(self.cat.match_customer("Pacific Rimstone").id, "C003")
        self.assertIsNone(self.cat.match_customer("有限公司"))
        self.assertIsNone(self.cat.match_customer("某某公司"))


if __name__ == "__main__":
    unittest.main()
