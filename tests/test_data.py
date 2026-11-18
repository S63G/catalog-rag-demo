"""The generator is deterministic and the parser recovers exactly what the generator wrote."""
from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from catalog_rag.config import PROJECT_ROOT
from tests.support import DATA, chunks, truth


def _generator():
    spec = importlib.util.spec_from_file_location("make_data", PROJECT_ROOT / "tools" / "make_data.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GeneratorTest(unittest.TestCase):
    def test_regenerating_reproduces_the_committed_documents(self):
        out = Path(tempfile.mkdtemp(prefix="catalog_gen_"))
        self.addCleanup(shutil.rmtree, out, True)
        shutil.copytree(DATA / "master", out / "master")
        _generator().generate(out)
        self.assertEqual(json.loads((out / "catalog_truth.json").read_text(encoding="utf-8")), truth())
        for pdf in (DATA / "docs").glob("*.pdf"):
            self.assertEqual((out / "docs" / pdf.name).read_bytes(), pdf.read_bytes(), pdf.name)
        self.assertEqual((out / "docs" / "常见问题FAQ.md").read_text(encoding="utf-8"),
                         (DATA / "docs" / "常见问题FAQ.md").read_text(encoding="utf-8"))
        fresh = load_workbook(out / "docs" / "瑞丰价目表_2026Q4.xlsx")
        committed = load_workbook(DATA / "docs" / "瑞丰价目表_2026Q4.xlsx")
        for name in committed.sheetnames:
            self.assertEqual([r for r in fresh[name].iter_rows(values_only=True)],
                             [r for r in committed[name].iter_rows(values_only=True)])

    def test_truth_covers_every_product(self):
        t = truth()
        self.assertEqual(len(t["skus"]), 300)
        self.assertEqual(len(t["documents"]), 20)
        for sku, v in t["skus"].items():
            self.assertTrue(v["doc"].endswith(".pdf") and v["page"] >= 2 and v["price_row"] >= 5, sku)


class ParserTest(unittest.TestCase):
    def test_every_table_row_is_parsed_with_its_page(self):
        rows = {c.skus[0]: c for c in chunks() if c.kind == "table_row"}
        self.assertEqual(len(rows), 300)
        for sku, expected in truth()["skus"].items():
            chunk = rows[sku]
            self.assertEqual((chunk.doc, chunk.page, chunk.row), (expected["doc"], expected["page"], expected["row"]))

    def test_continuation_page_rows_keep_the_table_title(self):
        m12 = [c for c in chunks() if c.kind == "table_row" and c.row and c.row["型号"].startswith("M12×")]
        self.assertEqual({c.page for c in m12}, {3})
        self.assertTrue(all("表1 内六角圆柱头螺钉规格参数" in c.title and "续" not in c.title for c in m12))

    def test_running_header_and_page_footer_are_dropped(self):
        for c in chunks():
            self.assertNotIn("共 4 页", c.text)
            self.assertNotIn("产品资料  RF-SS", c.text)

    def test_price_list_rows_are_sales_only_and_located_by_row(self):
        prices = [c for c in chunks() if c.kind == "price_row"]
        self.assertEqual(len(prices), 300)
        self.assertTrue(all(c.sales_only for c in prices))
        t = truth()["skus"]
        self.assertTrue(all(c.locator == f"价目表!r{t[c.skus[0]]['price_row']}" for c in prices))
        policy = [c for c in chunks() if c.kind == "policy"]
        self.assertTrue(policy and all(c.sales_only for c in policy))

    def test_faq_is_one_chunk_per_question_and_visible_to_everyone(self):
        faq = [c for c in chunks() if c.kind == "faq"]
        self.assertEqual([c.locator for c in faq], [f"Q{i}" for i in range(1, 21)])
        self.assertFalse(any(c.sales_only for c in faq))

    def test_citation_labels(self):
        by_kind = {c.kind: c for c in chunks()}
        self.assertRegex(by_kind["table_row"].cite(), r"\.pdf 第\d+页$")
        self.assertRegex(by_kind["price_row"].cite(), r"\.xlsx 价目表 第\d+行$")
        self.assertRegex(by_kind["faq"].cite(), r"FAQ\.md Q\d+$")
        self.assertEqual(len({c.id for c in chunks()}), len(chunks()))


if __name__ == "__main__":
    unittest.main()
