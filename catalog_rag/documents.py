"""Parse catalogue documents into citable chunks.

- Spec-sheet PDFs: page by page (page numbers kept). Running headers/footers are dropped. Prose is chunked per section
  per page; every table row becomes its own chunk that carries the table title and header, so a row on a continuation
  page ("表1 …（续）") is still self-describing.
- Price-list Excel: one chunk per row (header attached); the whole workbook is marked sales-only.
- FAQ markdown: one chunk per question.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from openpyxl import load_workbook
from pypdf import PdfReader

_PAGE_FOOTER = re.compile(r"第\s*\d+\s*页\s*/\s*共\s*\d+\s*页")
_SECTION = re.compile(r"^\d+\s+\S")
_TABLE_TITLE = re.compile(r"^表\d+\s")
_CELL_SPLIT = re.compile(r"\s{2,}")
_SKU = re.compile(r"^\d\.\d{2}\.\d{4}$")


@dataclass
class Chunk:
    id: str
    doc: str              # file name
    locator: str          # "p2" for PDFs, "价目表!r13" for Excel, "Q4" for the FAQ
    kind: str             # "text" | "table_row" | "price_row" | "policy" | "faq"
    title: str            # document title › section / table
    text: str             # what is indexed, embedded and shown to the model
    skus: list[str] = field(default_factory=list)
    row: dict[str, str] | None = None
    sales_only: bool = False

    @property
    def page(self) -> int | None:
        return int(self.locator[1:]) if self.locator.startswith("p") and self.locator[1:].isdigit() else None

    @property
    def source_key(self) -> str:
        return f"{self.doc}|{self.locator}"

    def cite(self) -> str:
        """Human-readable citation: document + page / row / question."""
        if self.page is not None:
            return f"{self.doc} 第{self.page}页"
        if "!r" in self.locator:
            sheet, row = self.locator.split("!r")
            return f"{self.doc} {sheet} 第{row}行"
        return f"{self.doc} {self.locator}"

    def to_dict(self) -> dict:
        return asdict(self)


def _next_id(chunks: list[Chunk], doc: str, locator: str) -> str:
    """Stable id: short document stem, locator, running number on that page/row."""
    stem = doc.split("_")[0] if doc.startswith("RF-") else Path(doc).stem
    n = sum(1 for c in chunks if c.doc == doc and c.locator == locator) + 1
    return f"{stem}:{locator}:{n}"


# --------------------------------------------------------------------------- PDF


def _page_lines(reader: PdfReader) -> list[list[str]]:
    pages = [[line.rstrip() for line in (page.extract_text(extraction_mode="layout") or "").splitlines()]
             for page in reader.pages]
    # Running header: a non-empty line repeated on (almost) every page.
    counts = Counter(line.strip() for lines in pages for line in set(lines) if line.strip())
    boilerplate = {line for line, n in counts.items() if len(pages) > 1 and n >= max(2, int(0.8 * len(pages)))}
    return [[line for line in lines if line.strip() not in boilerplate and not _PAGE_FOOTER.search(line)]
            for lines in pages]


def _join(lines: list[str]) -> str:
    """Join wrapped lines; bullets ("· ") start new lines."""
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("·") or not out:
            out.append(line)
        else:
            out[-1] += line
    return "\n".join(out)


def parse_pdf(path: Path) -> list[Chunk]:
    reader = PdfReader(path)
    pages = _page_lines(reader)
    doc = path.name
    first = [line.strip() for line in pages[0] if line.strip()]
    doc_title = first[0] if first else path.stem
    chunks: list[Chunk] = []
    section = ""
    for page_no, lines in enumerate(pages, 1):
        locator = f"p{page_no}"
        prose: list[str] = []

        def flush() -> None:
            body = _join(prose)
            prose.clear()
            if not body or body == doc_title:
                return
            title = f"{doc_title} › {section}" if section else doc_title
            chunks.append(Chunk(_next_id(chunks, doc, locator), doc, locator, "text", title, f"{title}\n{body}"))

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if _TABLE_TITLE.match(line):
                flush()
                i = _parse_table(lines, i, doc, doc_title, locator, chunks)
                continue
            if _SECTION.match(line) and len(line) < 30:
                flush()
                section = line
            elif line and line != doc_title and not line.startswith("文档编号"):
                prose.append(line)
            i += 1
        flush()
    return chunks


def _parse_table(lines: list[str], start: int, doc: str, doc_title: str, locator: str, chunks: list[Chunk]) -> int:
    """Rows after the header line that split into the same number of cells; returns the next unread line index."""
    table_title = lines[start].strip()
    i = start + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return i
    header = _CELL_SPLIT.split(lines[i].strip())
    i += 1
    title = f"{doc_title} › {table_title.replace('（续）', '')}"
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        cells = _CELL_SPLIT.split(line)
        if len(cells) != len(header):
            break
        row = dict(zip(header, cells))
        body = "；".join(f"{k} {v}" for k, v in row.items())
        chunks.append(Chunk(_next_id(chunks, doc, locator), doc, locator, "table_row", title, f"{title}\n{body}",
                            skus=[v for v in cells if _SKU.match(v)], row=row))
        i += 1
    return i


# --------------------------------------------------------------------------- Excel price list


def parse_price_list(path: Path) -> list[Chunk]:
    wb = load_workbook(path, data_only=True, read_only=True)
    doc = path.name
    chunks: list[Chunk] = []
    for ws in wb.worksheets:
        rows = [[("" if v is None else str(v).strip()) for v in r] for r in ws.iter_rows(values_only=True)]
        header_idx = next((i for i, r in enumerate(rows) if "物料编码" in r), None)
        sheet_title = next((r[0] for r in rows if r and r[0]), ws.title)
        if header_idx is None:  # free-text sheet (price policy): one chunk per line
            title = f"{doc} › {sheet_title}"
            for r_no, r in enumerate(rows, 1):
                if r and r[0] and r_no > 1:
                    loc = f"{ws.title}!r{r_no}"
                    chunks.append(Chunk(_next_id(chunks, doc, loc), doc, loc, "policy", title, f"{title}\n{r[0]}",
                                        sales_only=True))
            continue
        header = rows[header_idx]
        title = f"{doc} › {ws.title}"
        for r_no, r in enumerate(rows[header_idx + 1:], header_idx + 2):
            if not any(r):
                continue
            row = {h: v for h, v in zip(header, r) if h}
            loc = f"{ws.title}!r{r_no}"
            body = "；".join(f"{k} {v}" for k, v in row.items())
            chunks.append(Chunk(_next_id(chunks, doc, loc), doc, loc, "price_row", title, f"{title}\n{body}",
                                skus=[row["物料编码"]], row=row, sales_only=True))
    wb.close()
    return chunks


# --------------------------------------------------------------------------- FAQ markdown


def parse_faq(path: Path) -> list[Chunk]:
    doc = path.name
    text = path.read_text(encoding="utf-8")
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
    chunks: list[Chunk] = []
    for match in re.finditer(r"^## (Q\d+)\.\s*(.+?)\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S):
        q_id, question, answer = match.group(1), match.group(2).strip(), match.group(3).strip()
        chunks.append(Chunk(_next_id(chunks, doc, q_id), doc, q_id, "faq", f"{title} › {q_id}",
                            f"{title} › {q_id}\n问：{question}\n答：{answer}"))
    return chunks


def parse_folder(folder: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(folder.iterdir()):
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            chunks += parse_pdf(path)
        elif suffix == ".xlsx":
            chunks += parse_price_list(path)
        elif suffix == ".md":
            chunks += parse_faq(path)
    return chunks
