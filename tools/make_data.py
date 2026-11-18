"""Deterministic generator for the fictional product catalogue.

Every company, document number, parameter and price below is invented for the demo (演示数据均为虚构).
The product master and customer agreed prices are copied from the order-entry demo (data/master/).

Writes (under --out, default ``data/``):
  docs/RF-01_…规格书.pdf … RF-20_…   20 spec sheets: Chinese text-layer PDFs, multi-page, tables span pages
  docs/瑞丰价目表_2026Q4.xlsx          list prices, MOQ, lead time + a price-policy sheet
  docs/常见问题FAQ.md                  customer-service FAQ
  catalog_truth.json                   per SKU: document, page and table row values; per document: page titles

Run:  python tools/make_data.py [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

DISTRIBUTOR = "瑞丰机电批发（虚构）"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- minimal CJK text PDF


def _pdf_hex(s: str) -> str:
    return "<" + s.encode("utf-16-be").hex().upper() + ">"


def _to_unicode_cmap() -> bytes:
    high = [h for h in range(256) if not 0xD8 <= h <= 0xDF]
    parts = ["/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def /CMapType 2 def",
             "1 begincodespacerange <0000> <FFFF> endcodespacerange"]
    for i in range(0, len(high), 100):
        chunk = high[i:i + 100]
        parts.append(f"{len(chunk)} beginbfrange")
        parts += [f"<{h:02X}00> <{h:02X}FF> <{h:02X}00>" for h in chunk]
        parts.append("endbfrange")
    parts.append("endcmap CMapName currentdict /CMap defineresource pop end end")
    return "\n".join(parts).encode()


def text_width(s: str, size: float) -> float:
    """Advance width with the font's metrics: ASCII half-width, everything else full-width."""
    return sum(size * (0.5 if ord(ch) < 128 else 1.0) for ch in s)


class PdfDoc:
    """A4 pages using the PDF-standard (non-embedded) CJK font STSong-Light, UCS-2 encoded, with ToUnicode."""

    def __init__(self) -> None:
        self.pages: list[list[str]] = []

    def new_page(self) -> None:
        self.pages.append([])

    def text(self, x: float, y: float, s: str, size: float = 10) -> None:
        self.pages[-1].append(f"BT /F1 {size} Tf {x:.1f} {y:.1f} Td {_pdf_hex(s)} Tj ET")

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.pages[-1].append(f"0.6 w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def save(self, path: Path) -> None:
        n = len(self.pages)
        font_obj = 3 + 2 * n
        objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
        kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
        objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
        for i, ops in enumerate(self.pages):
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {4 + 2 * i} 0 R "
                        f"/Resources << /Font << /F1 {font_obj} 0 R >> >> >>".encode())
            stream = "\n".join(ops).encode()
            objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        cmap = _to_unicode_cmap()
        objs += [
            f"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H "
            f"/DescendantFonts [{font_obj + 1} 0 R] /ToUnicode {font_obj + 3} 0 R >>".encode(),
            f"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo "
            f"<< /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> /FontDescriptor {font_obj + 2} 0 R "
            f"/DW 1000 /W [1 95 500] >>".encode(),
            b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 /FontBBox [-25 -254 1000 880] "
            b"/ItalicAngle 0 /Ascent 880 /Descent -120 /CapHeight 880 /StemV 93 >>",
            b"<< /Length " + str(len(cmap)).encode() + b" >>\nstream\n" + cmap + b"\nendstream",
        ]
        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        path.write_bytes(bytes(out))


# --------------------------------------------------------------------------- product attributes (table rows)

Row = dict[str, str]

BEARING_DIMS = {  # standard boundary dimensions d x D x B (mm)
    "6200": (10, 30, 9), "6201": (12, 32, 10), "6202": (15, 35, 11), "6203": (17, 40, 12), "6204": (20, 47, 14),
    "6205": (25, 52, 15), "6206": (30, 62, 16), "6207": (35, 72, 17), "6208": (40, 80, 18), "6209": (45, 85, 19),
    "6210": (50, 90, 20), "6300": (10, 35, 11), "6301": (12, 37, 12), "6302": (15, 42, 13), "6303": (17, 47, 14),
    "6304": (20, 52, 15), "6305": (25, 62, 17), "6306": (30, 72, 19), "6307": (35, 80, 21), "6308": (40, 90, 23),
}


def _num(v: float) -> str:
    return f"{v:g}"


def bearing_row(p: dict) -> Row:
    series, seal = p["spec"].split("-")
    d, outer, b = BEARING_DIMS[series]
    zz_speed = round(1_000_000 / ((d + outer) / 2) / 2 / 100) * 100
    speed = zz_speed if seal == "ZZ" else round(zz_speed * 0.65 / 100) * 100
    weight = math.pi / 4 * (outer ** 2 - d ** 2) * b * 7.85e-6 * 0.6
    return {"内径d(mm)": str(d), "外径D(mm)": str(outer), "宽度B(mm)": str(b),
            "密封形式": "双面胶封" if seal == "2RS" else "双面铁封", "材质": "GCr15轴承钢",
            "极限转速(r/min)": str(speed), "重量(kg)": f"{weight:.3f}"}


CONTACTOR = {"0910": (9, "4", "1NO"), "1210": (12, "5.5", "1NO"), "1810": (18, "7.5", "1NO"),
             "2510": (25, "11", "1NO"), "3210": (32, "15", "1NO"), "4011": (40, "18.5", "1NO+1NC"),
             "5011": (50, "22", "1NO+1NC"), "6511": (65, "30", "1NO+1NC")}


def contactor_row(p: dict) -> Row:
    m = re.match(r"CJX2-(\d{4}) 线圈(AC\d+V)", p["spec"])
    assert m
    amps, kw, aux = CONTACTOR[m.group(1)]
    return {"额定电流Ie(A)": str(amps), "AC-3可控电机(kW)": kw, "辅助触点": aux, "线圈电压": m.group(2),
            "材质": "银合金触点", "安装方式": "35mm导轨/螺钉" if amps <= 32 else "螺钉安装"}


def mcb_row(p: dict) -> Row:
    m = re.match(r"DZ47-63 (\d)P C(\d+)", p["spec"])
    assert m
    poles = int(m.group(1))
    return {"极数": f"{poles}P", "额定电流In(A)": m.group(2), "脱扣曲线": "C", "分断能力(kA)": "6",
            "宽度(mm)": str(18 * poles), "材质": "阻燃PC外壳"}


BV = {"1.5": ("1/1.38", "0.7", "3.3"), "2.5": ("1/1.78", "0.8", "3.9"), "4": ("1/2.25", "0.8", "4.4"),
      "6": ("1/2.76", "0.8", "4.9"), "10": ("7/1.35", "1.0", "7.0")}


def bv_row(p: dict) -> Row:
    m = re.match(r"BV (\S+)mm2 (\S+) 100m", p["spec"])
    assert m
    structure, insulation, od = BV[m.group(1)]
    return {"截面(mm2)": m.group(1), "导体结构(根/mm)": structure, "绝缘厚度(mm)": insulation,
            "平均外径(mm)": od, "颜色": m.group(2) + "色", "材质": "铜导体/PVC绝缘", "每卷(m)": "100"}


RVV_OD = {"2×1.0": "6.2", "3×1.5": "7.6", "3×2.5": "9.0", "4×1.5": "8.4", "4×2.5": "9.9", "5×2.5": "10.9"}


def rvv_row(p: dict) -> Row:
    cores = p["spec"].split()[1].replace("mm2", "")
    return {"芯数×截面(mm2)": cores, "额定电压": "300/500V", "平均外径(mm)": RVV_OD[cores], "护套颜色": "黑色",
            "材质": "铜导体/PVC护套", "每卷(m)": "100"}


SCREW = {4: ("0.7", "7", "3"), 5: ("0.8", "8.5", "4"), 6: ("1.0", "10", "5"), 8: ("1.25", "13", "6"),
         10: ("1.5", "16", "8"), 12: ("1.75", "18", "10")}


def screw_row(p: dict) -> Row:
    m = re.match(r"M(\d+)×(\d+)", p["spec"])
    assert m
    d, length = int(m.group(1)), m.group(2)
    pitch, dk, s = SCREW[d]
    return {"螺纹规格": f"M{d}", "螺距(mm)": pitch, "长度L(mm)": length, "头部直径dk(mm)": dk,
            "头部高度k(mm)": str(d), "内六角对边s(mm)": s, "材质": "合金钢12.9级", "表面处理": "发黑",
            "包装": "100件/箱"}


CYLINDER = {32: ("G1/8", "M10×1.25"), 40: ("G1/4", "M12×1.25"), 50: ("G1/4", "M16×1.5"), 63: ("G3/8", "M16×1.5")}


def cylinder_row(p: dict) -> Row:
    m = re.match(r"SC(\d+)×(\d+)", p["spec"])
    assert m
    port, rod = CYLINDER[int(m.group(1))]
    return {"缸径(mm)": m.group(1), "行程(mm)": m.group(2), "接管螺纹": port, "活塞杆螺纹": rod,
            "使用压力(MPa)": "0.1~0.9", "材质": "铝合金缸筒", "磁性开关": "标配磁环"}


TUBE_BEND = {4: "10", 6: "15", 8: "20", 10: "27", 12: "35"}


def tube_row(p: dict) -> Row:
    m = re.match(r"(\d+)×([\d.]+) (\S+) 100m", p["spec"])
    assert m
    return {"外径(mm)": m.group(1), "内径(mm)": m.group(2), "颜色": m.group(3), "最高使用压力(MPa)": "0.8",
            "最小弯曲半径(mm)": TUBE_BEND[int(m.group(1))], "材质": "聚氨酯PU", "每卷(m)": "100"}


FITTING = {"PC4-01": ("4", "R1/8"), "PC6-01": ("6", "R1/8"), "PC6-02": ("6", "R1/4"), "PC8-02": ("8", "R1/4"),
           "PC10-03": ("10", "R3/8"), "PC12-04": ("12", "R1/2")}


def fitting_row(p: dict) -> Row:
    tube, thread = FITTING[p["spec"]]
    return {"适用管外径(mm)": tube, "接口螺纹": thread, "材质": "黄铜镀镍+PBT", "包装": "10件/箱"}


MOTOR = {  # kW: (frame 2P, frame 4P, speed 2P, speed 4P, current 2P, current 4P)
    "0.75": ("80M1", "80M2", 2860, 1400, "1.7", "1.9"), "1.1": ("80M2", "90S", 2870, 1410, "2.4", "2.7"),
    "1.5": ("90S", "90L", 2880, 1420, "3.2", "3.6"), "2.2": ("90L", "100L1", 2880, 1430, "4.6", "4.9"),
    "3": ("100L", "100L2", 2890, 1430, "6.0", "6.5"), "4": ("112M", "112M", 2900, 1440, "7.9", "8.5"),
    "5.5": ("132S1", "132S", 2910, 1450, "10.7", "11.4"), "7.5": ("132S2", "132M", 2920, 1460, "14.3", "15.2"),
}


def motor_row(p: dict) -> Row:
    m = re.match(r"YE3 ([\d.]+)kW (\dP)", p["spec"])
    assert m
    kw, poles = m.group(1), m.group(2)
    f2, f4, s2, s4, c2, c4 = MOTOR[kw]
    two = poles == "2P"
    return {"功率(kW)": kw, "极数": poles, "同步转速(r/min)": "3000" if two else "1500",
            "额定转速(r/min)": str(s2 if two else s4), "机座号": f2 if two else f4,
            "额定电流(A)": c2 if two else c4, "效率等级": "IE3", "材质": "铸铁机壳"}


INVERTER = {"0.75": "2.5", "1.5": "4.2", "2.2": "5.6", "4": "9.4", "5.5": "13", "7.5": "17", "11": "25", "15": "32"}


def inverter_row(p: dict) -> Row:
    kw = p["spec"].split("-")[1].split("kW")[0]
    return {"适配电机(kW)": kw, "输入电压": "三相380V", "额定输出电流(A)": INVERTER[kw], "过载能力": "150% 60s",
            "制动单元": "内置", "材质": "阻燃塑料外壳" if float(kw) < 11 else "钣金外壳"}


def belt_row(p: dict) -> Row:
    typ, length = p["spec"][0], p["spec"][1:]
    top, height = ("13", "8") if typ == "A" else ("17", "11")
    return {"型别": typ, "基准长度Ld(mm)": length, "顶宽(mm)": top, "高度(mm)": height,
            "材质": "氯丁橡胶+聚酯线绳"}


def oring_row(p: dict) -> Row:
    m = re.match(r"(\d+)×([\d.]+) (NBR|FKM)", p["spec"])
    assert m
    nbr = m.group(3) == "NBR"
    return {"内径(mm)": m.group(1), "线径(mm)": m.group(2), "材质": "NBR丁腈橡胶" if nbr else "FKM氟橡胶",
            "硬度(Shore A)": "70" if nbr else "75", "使用温度(℃)": "-30~+100" if nbr else "-20~+200",
            "包装": "50件/箱"}


TIE = {"3×100": ("22", "8"), "3×150": ("35", "8"), "4×200": ("50", "18"), "5×250": ("65", "22"),
       "5×300": ("82", "22"), "8×400": ("105", "55")}


def tie_row(p: dict) -> Row:
    size, color = p["spec"].split()
    w, length = size.split("×")
    bundle, tensile = TIE[size]
    return {"宽度(mm)": w, "长度(mm)": length, "最大捆扎直径(mm)": bundle, "最小拉力(kg)": tensile,
            "颜色": color, "材质": "尼龙66", "包装": "100条/包,10包/箱"}


def tape_row(p: dict) -> Row:
    return {"宽度(mm)": "18", "长度(m)": "20", "厚度(mm)": "0.13", "颜色": p["spec"].split()[1],
            "材质": "PVC", "击穿电压(kV)": "≥5", "包装": "10卷/箱"}


RAIL = {"HGR15": ("15", "15", "60"), "HGR20": ("20", "17.5", "60"), "HGR25": ("23", "22", "60"),
        "HGR30": ("28", "26", "80")}


def rail_row(p: dict) -> Row:
    model, length = p["spec"].split()
    w, h, pitch = RAIL[model]
    return {"导轨宽度(mm)": w, "导轨高度(mm)": h, "长度(mm)": length[1:], "安装孔距P(mm)": pitch,
            "材质": "S55C中碳钢", "热处理": "高频淬火"}


SENSOR_OUT = {"BX": ("NPN", "常开"), "AX": ("NPN", "常闭"), "BY": ("PNP", "常开"), "AY": ("PNP", "常闭")}


def sensor_row(p: dict) -> Row:
    m = re.match(r"LJ(\d+)A3-(\d+)-Z/(\w+)", p["spec"])
    assert m
    out, state = SENSOR_OUT[m.group(3)]
    return {"螺纹规格": f"M{m.group(1)}", "检测距离(mm)": m.group(2), "输出类型": out, "输出状态": state,
            "工作电压": "DC6~36V", "材质": "镀镍黄铜外壳", "安装方式": "非埋入式"}


def relay_row(p: dict) -> Row:
    model, coil = p["spec"].split()
    four = model == "MY4N"
    return {"触点形式": "4组转换" if four else "2组转换", "触点容量": "3A 250VAC" if four else "5A 250VAC",
            "线圈电压": coil, "引脚数": "14" if four else "8", "材质": "银合金触点"}


def button_row(p: dict) -> Row:
    return {"安装孔径(mm)": "22", "触点": "1NO+1NC", "按钮颜色": p["spec"].split()[1], "额定": "10A 380VAC",
            "材质": "PC按钮/银触点"}


TERMINAL = {"UK2.5B": ("2.5", "24", "5.2"), "UK5N": ("4", "32", "6.2"), "UK6N": ("6", "41", "8.2"),
            "UK10N": ("10", "57", "10.2"), "UK16N": ("16", "76", "12.2")}


def terminal_row(p: dict) -> Row:
    section, amps, width = TERMINAL[p["spec"]]
    return {"额定截面(mm2)": section, "额定电流(A)": amps, "额定电压(V)": "800", "宽度(mm)": width,
            "材质": "阻燃PA66/镀锡铜件", "包装": "100件/箱"}


VALVE_THREAD = {15: "G1/2", 20: "G3/4", 25: "G1", 32: "G1-1/4", 40: "G1-1/2", 50: "G2"}


def valve_row(p: dict) -> Row:
    m = re.match(r"DN(\d+) (\d+)", p["spec"])
    assert m
    dn = int(m.group(1))
    return {"公称通径": f"DN{dn}", "连接螺纹": VALVE_THREAD[dn], "公称压力(MPa)": "6.4" if dn <= 25 else "4.0",
            "材质": f"{m.group(2)}不锈钢", "阀座密封": "PTFE", "适用温度(℃)": "-20~+180"}


def fuse_row(p: dict) -> Row:
    amps = p["spec"].split()[1].rstrip("A")
    return {"额定电流(A)": amps, "额定电压": "AC500V", "分断能力(kA)": "100", "熔体尺寸(mm)": "10×38",
            "材质": "陶瓷熔管"}


# --------------------------------------------------------------------------- the 20 spec sheets


@dataclass
class Table:
    category: str
    title: str
    row: Callable[[dict], Row]


@dataclass
class SpecSheet:
    file: str
    title: str
    doc_no: str
    overview: str
    features: list[str]
    materials: list[str]
    applications: str
    tables: list[Table]
    selection: list[str]
    installation: list[str]
    warranty_months: int
    extra_service: str = ""
    truth: dict = field(default_factory=dict)


SHEETS: list[SpecSheet] = [
    SpecSheet("RF-01_深沟球轴承规格书.pdf", "深沟球轴承 62/63 系列 产品规格书", "RF-SS-2026-01",
              "62 系列为轻窄系列，63 系列为中窄系列，适用于中高转速、以径向载荷为主的场合。每个尺寸提供 -2RS（双面接触式橡胶密封）"
              "和 -ZZ（双面金属防尘盖）两种结构，出厂已填充锂基润滑脂，免维护。",
              ["精度等级 P0，径向游隙 C0", "保持架：钢板冲压浪形保持架", "润滑：锂基润滑脂，填充量约为内部空间的 30%",
               "-2RS 工作温度 -20~+110℃；-ZZ 工作温度 -30~+120℃"],
              ["套圈与钢球：GCr15 轴承钢，淬火硬度 HRC60~64", "密封件：丁腈橡胶（-2RS）；防尘盖：08F 冷轧钢板（-ZZ）",
               "执行标准：GB/T 276 滚动轴承 深沟球轴承 外形尺寸"],
              "电机、减速机、风机、水泵、输送设备、农机等。",
              [Table("深沟球轴承", "表1 深沟球轴承规格参数", bearing_row)],
              ["同一尺寸代号的 -2RS 与 -ZZ 外形尺寸完全相同，可以互相替代；-2RS 防水防尘更好，但极限转速约为 -ZZ 的 65%。",
               "62 系列与 63 系列即使内径相同，外径和宽度也不同，不能互相替代。",
               "粉尘大、有水汽的场合优先选 -2RS；高转速、干燥环境优先选 -ZZ。"],
              ["安装时压力只能加在过盈配合的套圈上，禁止锤击外圈传力到内圈",
               "轴的推荐公差 k5/k6，轴承座孔推荐公差 H7", "密封型轴承无需清洗、不可加热超过 80℃"],
              12),
    SpecSheet("RF-02_交流接触器规格书.pdf", "CJX2 系列交流接触器 产品规格书", "RF-SS-2026-02",
              "CJX2 系列交流接触器用于交流 50Hz、额定工作电压至 660V 的电路中，远距离接通和分断电路，频繁启动和控制交流电动机。"
              "型号后两位“10”表示带 1 个常开辅助触点，“11”表示 1 常开 + 1 常闭。",
              ["额定绝缘电压 690V", "机械寿命 1000 万次，AC-3 电寿命 100 万次", "线圈电压 AC220V 与 AC380V 两种",
               "可加装 F4 辅助触头组和 LA2-D 空气延时头"],
              ["触点：银合金", "外壳：阻燃增强尼龙", "执行标准：GB/T 14048.4"],
              "电动机直接启动、正反转控制、星三角启动回路及照明、加热负载控制。",
              [Table("交流接触器", "表1 CJX2 接触器规格参数", contactor_row)],
              ["线圈电压必须与控制回路电压一致，AC220V 与 AC380V 线圈不能互换使用。",
               "相同线圈电压时，可以用额定电流更大一档的型号替代（例如 CJX2-1810 可由 CJX2-2510 替代），不可用小一档替代。",
               "按电机功率选型：AC-3 可控电机功率应不小于电机额定功率。"],
              ["垂直安装，倾斜度不超过 5°", "接线端子紧固力矩按端子标识执行，导线须压接冷压端头", "首次通电前检查线圈电压"],
              18),
    SpecSheet("RF-03_小型断路器规格书.pdf", "DZ47-63 系列小型断路器 产品规格书", "RF-SS-2026-03",
              "DZ47-63 系列小型断路器适用于交流 50Hz、额定电压 230/400V、额定电流至 63A 的线路，作过载和短路保护，"
              "也可在正常情况下作线路的不频繁操作转换。本目录只收录 C 型脱扣曲线产品。",
              ["额定短路分断能力 6kA", "C 型脱扣：瞬时脱扣电流 5~10 In，适合一般配电与照明", "机械寿命 20000 次",
               "35mm 标准导轨安装，单极宽度 18mm"],
              ["外壳：阻燃 PC", "触头：银合金", "执行标准：GB/T 10963.1"],
              "住宅与商业配电箱、照明回路、控制柜进线保护。",
              [Table("小型断路器", "表1 DZ47-63 断路器规格参数", mcb_row)],
              ["断路器额定电流应与导线载流量匹配，不得以大电流规格替代小电流规格。",
               "极数必须与回路相同：单相照明回路用 1P 或 2P，三相回路用 3P。",
               "需要漏电保护时须另配漏电模块，本目录未收录漏电保护产品。"],
              ["安装前确认导轨牢固，进线端在上方", "接线截面：In≤20A 用 2.5mm2 及以上，32A 用 6mm2 及以上",
               "严禁带电安装或拆卸"],
              24),
    SpecSheet("RF-04_BV单芯铜线规格书.pdf", "BV 单芯聚氯乙烯绝缘铜线 产品规格书", "RF-SS-2026-04",
              "BV 电线为铜芯聚氯乙烯绝缘电线，额定电压 450/750V，适用于固定敷设的动力、照明线路。每卷 100 米，"
              "提供红、蓝、黄绿三种颜色，黄绿双色线专用于接地（PE）。",
              ["额定电压 450/750V", "导体长期允许工作温度 70℃", "1.5~6mm2 为单根实心导体，10mm2 为 7 股绞合",
               "绝缘层印字间隔不大于 500mm"],
              ["导体：无氧铜（第 1/2 类导体）", "绝缘：PVC 聚氯乙烯", "执行标准：GB/T 5023.3"],
              "建筑物内照明与动力线路、配电箱内部接线，穿管或线槽敷设。",
              [Table("BV单芯铜线", "表1 BV 电线规格参数", bv_row)],
              ["同截面不同颜色的电线电气性能相同；黄绿色只能用作保护接地，不能替代相线或零线。",
               "需要移动或频繁弯折的场合不要使用 BV 线，应改用多股软线（本目录未收录 BVR）。"],
              ["穿管敷设时管内导线总截面不超过管内截面的 40%", "弯曲半径不小于电线外径的 4 倍", "施工环境温度不低于 0℃"],
              12),
    SpecSheet("RF-05_RVV护套电缆规格书.pdf", "RVV 聚氯乙烯护套软电缆 产品规格书", "RF-SS-2026-05",
              "RVV 为铜芯聚氯乙烯绝缘聚氯乙烯护套软电缆，额定电压 300/500V，适用于家用电器、小型电动工具、仪器仪表及动力照明的"
              "移动连接。每卷 100 米，护套黑色。",
              ["额定电压 300/500V", "导体长期允许工作温度 70℃", "第 5 类多股细铜丝软导体", "芯线按颜色区分"],
              ["导体：多股无氧铜丝", "绝缘与护套：PVC", "执行标准：GB/T 5023.5"],
              "设备电源线、控制柜之间的软连接、临时用电。",
              [Table("RVV护套电缆", "表1 RVV 电缆规格参数", rvv_row)],
              ["芯数和截面都相同才能替代；三相设备至少选 4 芯（3 相线 + PE），带零线时选 5 芯。"],
              ["移动使用时弯曲半径不小于外径的 6 倍", "不宜长期浸水或在阳光下暴晒使用", "接头处应使用冷压端子"],
              12),
    SpecSheet("RF-06_内六角圆柱头螺钉规格书.pdf", "内六角圆柱头螺钉（12.9 级） 产品规格书", "RF-SS-2026-06",
              "内六角圆柱头螺钉按 GB/T 70.1 制造，性能等级 12.9 级，表面发黑处理。按箱销售，每箱 100 件。本目录只收录 12.9 级"
              "合金钢产品。",
              ["性能等级 12.9 级，抗拉强度 ≥1220MPa", "粗牙螺纹，螺纹公差 5g6g", "表面发黑（氧化）处理，具一定防锈能力"],
              ["材质：合金钢（SCM435）", "执行标准：GB/T 70.1、GB/T 3098.1"],
              "模具、机床、自动化设备结构件的高强度连接。",
              [Table("内六角圆柱头螺钉", "表1 内六角圆柱头螺钉规格参数", screw_row)],
              ["同螺纹规格时可选用更长一档的螺钉，但需确认螺纹旋合长度不小于 1 倍螺纹直径且不顶底。",
               "12.9 级不可用 8.8 级替代用于受力连接。"],
              ["推荐拧紧力矩（12.9 级，干燥）：M4 4.6N·m，M5 9N·m，M6 15.5N·m，M8 37N·m，M10 73N·m，M12 128N·m",
               "发黑件在潮湿环境中应涂防锈油", "使用对应规格的内六角扳手，避免滑丝"],
              6),
    SpecSheet("RF-07_标准气缸规格书.pdf", "SC 系列标准气缸 产品规格书", "RF-SS-2026-07",
              "SC 系列为双作用单活塞杆标准气缸，缸筒为铝合金型材，活塞标配磁环，可直接安装磁性开关检测行程位置。"
              "缸径 32~63mm，行程 50~200mm。",
              ["使用压力 0.1~0.9MPa，耐压 1.35MPa", "使用速度 50~800mm/s", "两端可调缓冲",
               "工作介质：经过滤的压缩空气"],
              ["缸筒：铝合金（阳极氧化）", "活塞杆：45 号钢镀硬铬", "密封：NBR"],
              "自动化生产线的推、拉、夹紧、顶升动作。",
              [Table("标准气缸", "表1 SC 气缸规格参数", cylinder_row)],
              ["相同缸径、行程更长的气缸可以安装替代，但需确认安装空间；缸径变化会改变输出力，不能直接替代。",
               "理论推力（0.5MPa）：缸径 32 约 402N，40 约 628N，50 约 982N，63 约 1559N。"],
              ["安装时活塞杆不得承受径向载荷", "接管前吹净管路杂质", "磁性开关安装在缸筒槽内，用螺丝刀轻压固定"],
              12),
    SpecSheet("RF-08_PU气管与快插接头规格书.pdf", "PU 气管与快插直通接头 产品规格书", "RF-SS-2026-08",
              "PU 气管采用聚氨酯材料，柔软耐磨，每卷 100 米；快插直通接头一端为外螺纹、一端为快插口，插入气管即可锁紧，"
              "按箱销售，每箱 10 件。",
              ["气管最高使用压力 0.8MPa（20℃）", "气管使用温度 -20~+60℃", "接头使用压力 0~0.9MPa，带螺纹密封胶",
               "气管外径与接头适用管径必须一致"],
              ["气管：聚氨酯 PU", "接头：黄铜镀镍本体 + PBT 释放环", "密封：NBR"],
              "气动元件之间的连接管路。",
              [Table("PU气管", "表1 PU 气管规格参数", tube_row),
               Table("快插直通接头", "表2 快插直通接头规格参数", fitting_row)],
              ["蓝色与透明气管性能相同，可互相替代；不同外径的气管不能互换。",
               "接头型号“PC6-01”中 6 表示适用 6mm 外径气管，01 表示 R1/8 螺纹，02 为 R1/4，03 为 R3/8，04 为 R1/2。"],
              ["切管请用专用切管刀，切口垂直", "拔管时先按下释放环", "温度升高时最高使用压力下降，60℃ 时约为 0.5MPa"],
              6),
    SpecSheet("RF-09_三相异步电机规格书.pdf", "YE3 系列超高效三相异步电动机 产品规格书", "RF-SS-2026-09",
              "YE3 系列为全封闭自扇冷式笼型三相异步电动机，效率达到 GB 18613 规定的 2 级能效（IE3）。额定电压 380V，"
              "频率 50Hz，3kW 及以下 Y 接法，4kW 及以上 △ 接法。",
              ["防护等级 IP55，冷却方式 IC411", "绝缘等级 F 级，温升按 B 级考核", "工作制 S1（连续）",
               "安装方式 B3（卧式底脚），其他安装方式需订货时说明"],
              ["机壳：铸铁", "转子：铸铝", "执行标准：GB/T 28575、GB 18613"],
              "风机、水泵、压缩机、输送机、机床等通用机械。",
              [Table("三相异步电机", "表1 YE3 电机规格参数", motor_row)],
              ["同功率、同极数、同机座号的电机才能直接替换安装；2 极与 4 极转速不同，不能互相替代。",
               "用变频器调速时，建议在 5~50Hz 范围内运行，长期低速运行需加装独立风机（本目录未收录）。"],
              ["安装前用 500V 兆欧表测量绝缘电阻，应不低于 1MΩ", "联轴器对中偏差不超过 0.05mm", "首次运行检查转向"],
              24),
    SpecSheet("RF-10_通用变频器规格书.pdf", "FV 系列通用变频器 产品规格书", "RF-SS-2026-10",
              "FV 系列为三相 380V 输入的通用矢量变频器，功率 0.75~15kW，内置制动单元，支持 V/F 与无速度传感器矢量控制，"
              "标配 RS485 通讯口（Modbus-RTU）。",
              ["输出频率 0~600Hz", "过载能力 150% 额定电流 60 秒", "内置 PID、多段速、简易 PLC 功能",
               "键盘可拆卸外引"],
              ["外壳：11kW 以下阻燃塑料，11kW 及以上钣金", "执行标准：GB/T 12668.2"],
              "风机水泵节能改造、输送线、机床主轴调速。",
              [Table("通用变频器", "表1 FV 变频器规格参数", inverter_row)],
              ["变频器额定输出电流应不小于电机额定电流；重载启动（如提升、挤出）应放大一档选型。",
               "同输入电压时，可用功率更大一档的变频器替代。"],
              ["垂直安装，上下各留 100mm 以上散热空间", "电机线超过 50m 时加装输出电抗器", "断电 10 分钟后再打开机盖"],
              18),
    SpecSheet("RF-11_三角带规格书.pdf", "普通 V 带（三角带） 产品规格书", "RF-SS-2026-11",
              "普通 V 带按截面分 A 型和 B 型，型号中的数字为基准长度 Ld（mm），例如 A1200 表示 A 型、基准长度 1200mm。",
              ["包布式结构，抗拉体为聚酯线绳", "使用温度 -30~+70℃", "成组使用时请选同一批次配组"],
              ["胶料：氯丁橡胶", "抗拉体：聚酯线绳", "执行标准：GB/T 1171"],
              "电机与风机、水泵、空压机、机床之间的动力传递。",
              [Table("三角带", "表1 普通 V 带规格参数", belt_row)],
              ["A 型与 B 型截面不同，不能互相替代；带轮槽型必须与带型一致。",
               "多根带传动时，应整组更换，不要新旧混用。"],
              ["安装时先缩小中心距再装带，禁止用工具撬入", "张紧后以拇指压下约 15mm 为宜", "运行 24 小时后复查张紧力"],
              6),
    SpecSheet("RF-12_O型密封圈规格书.pdf", "O 型密封圈 产品规格书", "RF-SS-2026-12",
              "O 型圈规格以 内径×线径 表示，单位 mm。提供 NBR 丁腈橡胶与 FKM 氟橡胶两种材质，按箱销售，每箱 50 件。",
              ["尺寸公差按 GB/T 3452.1 G 级", "NBR 适合矿物油、液压油、水", "FKM 耐高温、耐燃油与多数化学品"],
              ["NBR 丁腈橡胶，硬度 70 Shore A", "FKM 氟橡胶，硬度 75 Shore A"],
              "液压缸、气缸、阀门、泵和管接头的静密封与往复密封。",
              [Table("O型密封圈", "表1 O 型密封圈规格参数", oring_row)],
              ["同尺寸时 FKM 可以替代 NBR（耐温从 100℃ 提高到 200℃），但 FKM 低温性能较差，-20℃ 以下不建议使用。",
               "NBR 不能替代 FKM 用于 100℃ 以上或接触燃油、芳烃溶剂的场合。"],
              ["安装前在 O 型圈和沟槽涂抹与介质相容的润滑脂", "避免经过螺纹和锐边，必要时使用安装套",
               "存放时避光、远离臭氧源"],
              6),
    SpecSheet("RF-13_尼龙扎带规格书.pdf", "尼龙扎带 产品规格书", "RF-SS-2026-13",
              "自锁式尼龙扎带，型号以 宽度×长度 表示（mm），黑色和白色两种。每包 100 条，每箱 10 包，按箱销售。",
              ["材质尼龙 66，阻燃等级 UL94 V-2", "使用温度 -40~+85℃", "黑色添加炭黑，抗紫外线，适合户外"],
              ["尼龙 66（PA66）"],
              "线束捆扎、电缆固定、包装扎口。",
              [Table("尼龙扎带", "表1 尼龙扎带规格参数", tie_row)],
              ["户外使用请选黑色；白色扎带长期日晒会变脆。", "拉力要求高时选更宽的规格，不要多根串接。"],
              ["使用扎带枪可保证张紧力一致并平齐剪断", "低温环境下扎带变脆，避免冲击"],
              6),
    SpecSheet("RF-14_PVC电工胶带规格书.pdf", "PVC 电气绝缘胶带 产品规格书", "RF-SS-2026-14",
              "PVC 电工胶带宽 18mm、长 20m，厚度 0.13mm，提供黑、白、红、蓝、黄、绿六种颜色，每箱 10 卷，按箱销售。",
              ["击穿电压 ≥5kV", "使用温度 0~+80℃", "阻燃，可用于 600V 以下导线接头绝缘"],
              ["基材：软质 PVC", "胶层：橡胶型压敏胶"],
              "导线接头绝缘包扎、线束捆扎、相色标识。",
              [Table("PVC电工胶带", "表1 PVC 电工胶带规格参数", tape_row)],
              ["各颜色电气性能相同；用作相色标识时，L1/L2/L3 分别对应黄、绿、红。"],
              ["包扎时拉伸约 20%，每圈重叠一半带宽", "低于 5℃ 时胶粘性下降，建议先预热胶带"],
              6),
    SpecSheet("RF-15_直线导轨规格书.pdf", "HGR 系列直线导轨（导轨条） 产品规格书", "RF-SS-2026-15",
              "HGR 系列为四列式滚珠直线导轨的导轨条，型号中的数字为规格（15/20/25/30），L 后为长度（mm）。"
              "本目录仅销售导轨条，滑块需按同规格另行订购。",
              ["精度等级：普通级（C）", "导轨表面高频淬火，硬度 HRC58~62", "标准长度 500mm 与 1000mm，可按需切割（另收加工费）"],
              ["导轨：S55C 中碳钢"],
              "自动化设备、雕刻机、检测设备的直线运动导向。",
              [Table("直线导轨", "表1 HGR 导轨规格参数", rail_row)],
              ["同规格不同长度的导轨可以截短替代（长换短），截短后两端孔距需重新确认。",
               "不同规格（如 HGR20 与 HGR25）的导轨宽度和高度不同，不能互换。"],
              ["安装基准面平面度应不大于 0.02mm/500mm", "按对角顺序分 2~3 次拧紧固定螺钉", "使用前在滚道涂抹锂基润滑脂"],
              12),
    SpecSheet("RF-16_电感式接近开关规格书.pdf", "LJ 系列电感式接近开关 产品规格书", "RF-SS-2026-16",
              "LJ 系列为圆柱形电感式接近开关，检测金属物体。型号尾缀表示输出：BX=NPN 常开，AX=NPN 常闭，BY=PNP 常开，"
              "AY=PNP 常闭。M12 检测距离 4mm，M18 检测距离 8mm（以 Q235 钢为标准检测体）。",
              ["工作电压 DC6~36V，三线制", "响应频率 M12 为 500Hz，M18 为 300Hz", "带短路与反极性保护",
               "引线 2 米 PVC 电缆"],
              ["外壳：镀镍黄铜", "感应面：PBT"],
              "金属工件到位检测、计数、限位。",
              [Table("电感式接近开关", "表1 LJ 接近开关规格参数", sensor_row)],
              ["NPN 与 PNP 输出不能互换，必须与 PLC 输入类型一致（漏型/源型）。",
               "检测铝、铜等非铁金属时检测距离会明显缩短，约为标称值的 40%~50%。",
               "同螺纹规格、同输出类型时，常开与常闭不能直接替代，需修改程序逻辑。"],
              ["非埋入式安装：感应面周围需留出无金属区域", "螺母拧紧力矩：M12 不超过 15N·m，M18 不超过 35N·m",
               "电缆不要与动力线同槽敷设"],
              12),
    SpecSheet("RF-17_中间继电器与按钮规格书.pdf", "MY 系列中间继电器与 LA38 按钮开关 产品规格书", "RF-SS-2026-17",
              "MY2N/MY4N 为小型通用中间继电器（带指示灯），需配插座使用；LA38-11 为 22mm 安装孔径的平头按钮，"
              "带 1 常开 + 1 常闭触点。",
              ["继电器机械寿命 2000 万次，电寿命 20 万次", "继电器带 LED 动作指示", "按钮防护等级 IP40（面板前）",
               "按钮机械寿命 100 万次"],
              ["继电器触点：银合金", "按钮：PC 按钮头，银触点"],
              "控制回路信号放大与隔离、设备启停操作。",
              [Table("中间继电器", "表1 MY 中间继电器规格参数", relay_row),
               Table("按钮开关", "表2 LA38 按钮规格参数", button_row)],
              ["MY4N 触点数多于 MY2N，但引脚数不同（14 脚与 8 脚），替代时必须同时更换插座。",
               "线圈电压 DC24V 与 AC220V 不能互换。",
               "按钮颜色约定：绿色启动，红色停止或急停复位，黄色复位，蓝色或黑色用于其他功能。"],
              ["继电器插座需单独订购（本目录未收录插座）", "按钮安装孔径 22.3mm，面板厚度 1~6mm"],
              12),
    SpecSheet("RF-18_导轨式接线端子规格书.pdf", "UK 系列导轨式接线端子 产品规格书", "RF-SS-2026-18",
              "UK 系列为螺钉连接的导轨式接线端子，安装在 35mm 标准导轨上，按箱销售，每箱 100 件。",
              ["额定电压 800V", "阻燃等级 UL94 V-0", "可配端板、隔板和标记条（本目录未收录）"],
              ["绝缘体：阻燃 PA66", "导电件：镀锡铜", "螺钉：镀锌钢"],
              "控制柜、配电箱内部导线的连接与分配。",
              [Table("导轨式接线端子", "表1 UK 接线端子规格参数", terminal_row)],
              ["可以用额定截面更大的端子替代较小的端子，但端子宽度会增加，需确认导轨上的安装空间。"],
              ["剥线长度 8~10mm，多股线须压冷压端头", "螺钉拧紧力矩 0.6~1.5N·m（按规格）"],
              24),
    SpecSheet("RF-19_不锈钢球阀规格书.pdf", "不锈钢内螺纹两片式球阀 产品规格书", "RF-SS-2026-19",
              "两片式内螺纹球阀，阀体材质 304 或 316 不锈钢，阀座为 PTFE，手柄操作，规格 DN15~DN50。",
              ["公称压力：DN15~DN25 为 6.4MPa，DN32~DN50 为 4.0MPa", "适用温度 -20~+180℃（PTFE 阀座）",
               "连接螺纹：G（55° 圆柱管螺纹）", "全通径设计，流阻小"],
              ["阀体：304（CF8）或 316（CF8M）不锈钢", "阀座：PTFE", "执行标准：GB/T 12237"],
              "水、蒸汽、油品、弱腐蚀性介质管路的切断。",
              [Table("不锈钢球阀", "表1 不锈钢球阀规格参数", valve_row)],
              ["同通径时 316 可以替代 304（耐氯离子和酸性介质更好）；304 替代 316 前需确认介质不含氯离子。",
               "海水、含盐介质请选 316。"],
              ["安装时扳手夹在靠近螺纹一端的阀体上，不要扳动另一半阀体", "生料带缠绕 8~10 圈，顺螺纹方向",
               "球阀只能全开或全关，不可用于节流"],
              12),
    SpecSheet("RF-20_圆筒形熔断器规格书.pdf", "RT18-32 圆筒形帽熔断器 产品规格书", "RF-SS-2026-20",
              "RT18-32 熔断器熔体尺寸 10×38mm，额定电压 AC500V，额定电流 1~32A，用于线路和设备的短路保护。",
              ["分断能力 100kA", "gG 全范围分断，一般用途", "熔体需配 RT18-32 底座使用"],
              ["熔管：高强度陶瓷", "熔体：银铜合金", "填料：石英砂"],
              "控制回路、电源回路的短路保护。",
              [Table("圆筒形熔断器", "表1 RT18-32 熔断器规格参数", fuse_row)],
              ["只能用相同额定电流的熔体更换，严禁用大电流熔体或铜丝替代。"],
              ["更换熔体前必须断电", "熔体熔断后应先查明短路原因再更换"],
              12),
]

FIRST_COLS = ["型号", "物料编码"]
ROWS_PER_PAGE = 30
TOP_Y = 790
LEFT_X = 50


def _wrap(text: str, size: float, width: float) -> list[str]:
    lines, current = [], ""
    for ch in text:
        if text_width(current + ch, size) > width:
            lines.append(current)
            current = ""
        current += ch
    if current:
        lines.append(current)
    return lines


class SheetWriter:
    """Lays out one spec sheet; tracks page numbers for the ground truth."""

    def __init__(self, sheet: SpecSheet):
        self.sheet = sheet
        self.pdf = PdfDoc()
        self.y = 0.0
        self.page_titles: list[list[str]] = []

    @property
    def page(self) -> int:
        return len(self.pdf.pages)

    def new_page(self) -> None:
        self.pdf.new_page()
        self.page_titles.append([])
        self.pdf.text(LEFT_X, 815, f"{DISTRIBUTOR}  产品资料  {self.sheet.doc_no}", 8)
        self.pdf.line(LEFT_X, 808, 545, 808)
        self.y = TOP_Y

    def heading(self, text: str) -> None:
        if self.y < 120:
            self.new_page()
        self.y -= 8
        self.pdf.text(LEFT_X, self.y, text, 12)
        self.page_titles[-1].append(text)
        self.y -= 22

    def paragraph(self, text: str, indent: float = 0, size: float = 10) -> None:
        for chunk in _wrap(text, size, 495 - indent):
            if self.y < 70:
                self.new_page()
            self.pdf.text(LEFT_X + indent, self.y, chunk, size)
            self.y -= size + 6
        self.y -= 4

    def bullets(self, items: list[str]) -> None:
        for item in items:
            self.paragraph("· " + item, indent=10)

    def table(self, table: Table, products: list[dict], truth: dict) -> None:
        rows = []
        for p in products:
            values = table.row(p)
            rows.append({"型号": p["spec"], "物料编码": p["sku"], **values})
        columns = FIRST_COLS + [c for c in rows[0] if c not in FIRST_COLS]
        size = 8.0
        gap = 16.0
        widths = [max(text_width(c, size), *(text_width(r[c], size) for r in rows)) for c in columns]
        while sum(widths) + gap * (len(columns) - 1) > 495 and size > 6:
            size -= 0.5
            gap = max(10.0, gap - 1)
            widths = [max(text_width(c, size), *(text_width(r[c], size) for r in rows)) for c in columns]
        xs, x = [], float(LEFT_X)
        for w in widths:
            xs.append(x)
            x += w + gap

        def header(title: str) -> None:
            if self.y < 140:
                self.new_page()
            self.pdf.text(LEFT_X, self.y, title, 10)
            self.page_titles[-1].append(title)
            self.y -= 18
            for cx, c in zip(xs, columns):
                self.pdf.text(cx, self.y, c, size)
            self.pdf.line(LEFT_X, self.y - 5, 545, self.y - 5)
            self.y -= 17

        header(table.title)
        on_page = 0
        for r in rows:
            if self.y < 70 or on_page >= ROWS_PER_PAGE:
                self.new_page()
                header(table.title + "（续）")
                on_page = 0
            for cx, c in zip(xs, columns):
                self.pdf.text(cx, self.y, r[c], size)
            truth[r["物料编码"]] = {"doc": self.sheet.file, "page": self.page, "table": table.title,
                                    "category": table.category, "row": r}
            self.y -= size + 7
            on_page += 1
        self.y -= 14

    def finish(self, path: Path) -> list[list[str]]:
        total = len(self.pdf.pages)
        for i, ops in enumerate(self.pdf.pages, 1):
            ops.append(f"BT /F1 8 Tf {LEFT_X} 40 Td {_pdf_hex(f'第 {i} 页 / 共 {total} 页')} Tj ET")
            ops.append(f"BT /F1 7 Tf 360 40 Td {_pdf_hex('本资料为演示用虚构资料，参数仅供演示')} Tj ET")
        self.pdf.save(path)
        return self.page_titles


def write_sheet(sheet: SpecSheet, by_category: dict[str, list[dict]], path: Path, truth: dict) -> dict:
    w = SheetWriter(sheet)
    w.new_page()
    w.pdf.text(LEFT_X, w.y, sheet.title, 16)
    w.y -= 22
    w.pdf.text(LEFT_X, w.y, f"文档编号：{sheet.doc_no}    版本：A/2    发布日期：2026-09-15    编制：{DISTRIBUTOR} 技术部", 8)
    w.y -= 26
    w.heading("1 产品概述")
    w.paragraph(sheet.overview)
    w.heading("2 主要特点")
    w.bullets(sheet.features)
    w.heading("3 材质与执行标准")
    w.bullets(sheet.materials)
    w.heading("4 适用范围")
    w.paragraph(sheet.applications)
    w.new_page()
    w.heading("5 规格参数")
    for table in sheet.tables:
        w.table(table, by_category[table.category], truth)
    w.new_page()
    w.heading("6 选型与替代说明")
    w.bullets(sheet.selection)
    w.heading("7 安装与使用注意事项")
    w.bullets(sheet.installation)
    w.heading("8 质保与服务")
    w.paragraph(f"质保期：自发货之日起 {sheet.warranty_months} 个月。质保期内因材料或制造缺陷造成的损坏，免费更换同型号产品；"
                "因安装不当、超规格使用或人为损坏不在质保范围内。" + sheet.extra_service)
    w.paragraph(f"技术咨询：{DISTRIBUTOR} 技术部，工作日 8:30-17:30。")
    titles = w.finish(path)
    return {"file": sheet.file, "title": sheet.title, "doc_no": sheet.doc_no, "pages": len(titles),
            "page_sections": {str(i): t for i, t in enumerate(titles, 1)}, "warranty_months": sheet.warranty_months}


# --------------------------------------------------------------------------- price list (Excel)

PRICE_FILE = "瑞丰价目表_2026Q4.xlsx"
SLOW_CATEGORIES = {"三相异步电机": "5", "通用变频器": "3", "直线导轨": "7", "标准气缸": "3"}


def pack_note(p: dict) -> str:
    if p["unit"] == "箱":
        return f"{p['pack_qty']}{p['base_unit']}/箱"
    if p["unit"] == "卷":
        return "100m/卷"
    return f"按{p['unit']}"


def write_price_list(products: list[dict], path: Path) -> dict[str, int]:
    wb = Workbook()
    ws = wb.active
    ws.title = "价目表"
    ws["A1"] = f"{DISTRIBUTOR} 2026 年第四季度目录价（演示数据，均为虚构）"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = "生效日期 2026-10-01，价格含 13% 增值税；协议客户以合同协议价为准；本表仅限销售人员内部使用"
    headers = ["物料编码", "品名", "规格型号", "销售单位", "包装", "目录含税单价(元)", "税率", "起订量", "标准交期(天)"]
    ws.append([])
    ws.append(headers)
    for cell in ws[4]:
        cell.font = Font(bold=True)
    rows: dict[str, int] = {}
    for p in products:
        lead = SLOW_CATEGORIES.get(p["category"], "现货")
        ws.append([p["sku"], p["name"], p["spec"], p["unit"], pack_note(p), p["list_price"], "13%", 1, lead])
        rows[p["sku"]] = ws.max_row
    for col, width in zip("ABCDEFGHI", (12, 16, 26, 9, 12, 16, 7, 7, 12)):
        ws.column_dimensions[col].width = width
    policy = wb.create_sheet("价格政策")
    for line in [
        "价格政策（2026 年第四季度）",
        "1. 目录价为含税价，税率 13%，可开具增值税专用发票。",
        "2. 已签订年度协议的客户，按协议价执行；协议中未列出的物料按目录价执行。",
        "3. 客户提出的目标价与协议价偏差超过 ±2% 时，报价须经销售经理审批。",
        "4. 单笔订单含税金额满 3000 元免运费，不满 3000 元收取运费 15 元/单（华东地区）。",
        "5. 按箱销售的物料不拆箱；零散数量向上取整到整箱，或由销售与客户确认。",
        "6. 本价目表有效期至 2026-12-31。",
    ]:
        policy.append([line])
    policy.column_dimensions["A"].width = 80
    policy["A1"].font = Font(bold=True)
    for ws_ in wb.worksheets:
        for row in ws_.iter_rows():
            for cell in row:
                cell.alignment = Alignment(vertical="center")
    wb.properties.creator = DISTRIBUTOR
    wb.properties.created = wb.properties.modified = __import__("datetime").datetime(2026, 10, 1)
    wb.save(path)
    return rows


# --------------------------------------------------------------------------- FAQ

FAQ_FILE = "常见问题FAQ.md"
FAQ: list[tuple[str, str]] = [
    ("怎么下单？", "可通过邮件、飞书或电话向对接销售下单，订单需写明物料编码或规格型号、数量和收货地址。新客户首单需先开户。"),
    ("几点前下单可以当天发货？", "现货物料工作日 16:00 前确认的订单当天发货，16:00 以后次日发货。非现货物料按价目表中的标准交期发货。"),
    ("发什么快递？多久能到？", "华东地区由自有车辆或德邦物流配送，一般 1~2 天送达；其他地区走快递或物流，一般 2~4 天。"),
    ("运费怎么算？", "单笔订单含税金额满 3000 元免运费；不满 3000 元的华东地区订单收取运费 15 元，其他地区按实际运费结算。"),
    ("可以开什么发票？", "可开具 13% 增值税专用发票或普通发票，发票随货寄出或开具电子发票。"),
    ("可以退换货吗？", "产品未使用、包装完好的，自签收之日起 15 天内可以退换；按箱销售的物料须整箱退回。定制切割的导轨、电线电缆截断后不予退换。"),
    ("质保期是多久？", "质保期按品类不同，在各产品规格书第 8 节“质保与服务”中注明，自发货之日起计算。"),
    ("质量问题怎么处理？", "请提供订单号、产品照片和故障现象，技术部在 1 个工作日内答复；确认为质量问题的免费更换同型号产品并承担运费。"),
    ("可以提供样品吗？", "单价 200 元以下的物料可申请免费样品，每个型号限 1 件（按箱销售的物料提供 1 小包）；运费由客户承担。"),
    ("有没有账期？", "签订年度协议的客户可申请月结，账期一般为 30 天，具体以协议为准；新客户默认款到发货。"),
    ("能提供材质证明或检测报告吗？", "轴承、球阀、电线电缆可随货提供出厂检验报告；第三方检测报告需另行申请，费用由客户承担。"),
    ("能做非标定制吗？", "不接受非标定制。直线导轨可按长度切割，电线电缆可按米截断（最少 10 米），其他物料只销售目录内规格。"),
    ("技术咨询找谁？", "技术部工作日 8:30-17:30 在线，可帮助选型、核对替代型号；复杂选型请提供设备型号和工况。"),
    ("库存可以实时查吗？", "目录中的“标准交期”标为“现货”的物料通常有库存，但不代表实时库存；大批量订单请先找销售确认库存。"),
    ("最小起订量是多少？", "目录内物料起订量均为 1 个销售单位：按件、台、卷销售的为 1 件/台/卷，按箱销售的为 1 箱。"),
    ("可以送货上门吗？", "华东地区订单可送货到厂区收货点；需要送到车间或楼层的请在订单中注明，可能产生额外费用。"),
    ("包装是什么样的？", "轴承、开关类为单个纸盒包装，螺钉、O 型圈、扎带、接头、端子按箱包装，电线电缆、气管按卷包装。"),
    ("报价单多久有效？", "报价单自出具之日起 15 天内有效，遇原材料价格大幅波动时以重新确认的价格为准。"),
    ("可以看产品实物吗？", "宁波仓库展示区工作日开放参观，请提前一天预约（地址为演示用虚构地址）。"),
    ("客户资料和订单信息怎么保护？", "客户资料仅对接销售和财务可见，客服只能查看产品与物流信息，不能查看价格和协议条款。"),
]


def write_faq(path: Path) -> None:
    lines = [f"# {DISTRIBUTOR} 常见问题（FAQ）", "", "> 演示资料，内容均为虚构。更新日期：2026-10-08", ""]
    for i, (q, a) in enumerate(FAQ, 1):
        lines += [f"## Q{i}. {q}", "", a, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- main


def generate(out: Path) -> dict:
    master = out / "master"
    products: list[dict] = json.loads((master / "products.json").read_text(encoding="utf-8"))
    by_category: dict[str, list[dict]] = {}
    for p in products:
        by_category.setdefault(p["category"], []).append(p)
    docs = out / "docs"
    if docs.exists():
        shutil.rmtree(docs)
    docs.mkdir(parents=True)
    sku_truth: dict[str, dict] = {}
    sheets = [write_sheet(s, by_category, docs / s.file, sku_truth) for s in SHEETS]
    covered = {t.category for s in SHEETS for t in s.tables}
    assert covered == set(by_category), set(by_category) - covered
    assert len(sku_truth) == len(products) == 300
    price_rows = write_price_list(products, docs / PRICE_FILE)
    for sku, row in price_rows.items():
        sku_truth[sku]["price_row"] = row
    write_faq(docs / FAQ_FILE)
    truth = {"note": "演示数据均为虚构。每个 SKU 的规格表所在文档与页码、表格行内容，以及价目表行号。",
             "documents": sheets, "price_list": PRICE_FILE, "faq": FAQ_FILE,
             "faq_count": len(FAQ), "skus": sku_truth}
    (out / "catalog_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    return truth


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "data")
    args = parser.parse_args()
    truth = generate(args.out)
    pages = sum(d["pages"] for d in truth["documents"])
    print(f"已生成 {len(truth['documents'])} 份规格书（共 {pages} 页）、价目表、FAQ（{truth['faq_count']} 条）到 {args.out / 'docs'}")


if __name__ == "__main__":
    main()
