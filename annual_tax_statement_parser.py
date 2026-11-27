"""
Parser for the NEW Annual Tax Statement (Form 26AS) format issued under the
Income-tax Act, 2025 (Tax Year 2026-27 onwards).

Differences from the old (Income-tax Act, 1961) Form 26AS that the original
TwentySixASParser handles:
  * "Tax Year" replaces "Financial Year" / "Assessment Year"
  * Section is a 4-digit "Section Code" (e.g. 1031) instead of 194Q etc.
  * Status of Booking is a word ("Final", "Unmatched", ...) instead of F/U/...
  * A "Remarks**" column (e.g. "G" = reprocessing of statement)
  * Deductor PAN column in the deductor summary row
  * Reversal (negative) entries are shown in brackets: (33,509.00)
  * Section-code legend table is included at the end of the PDF

This module also exposes `detect_26as_format()` so the app can route each
uploaded PDF to the right parser and keep supporting both formats.
"""

import re
from typing import Dict, List, Optional, Tuple

import pdfplumber

FORMAT_OLD = "old"
FORMAT_NEW = "new"

TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
DATE_RE = re.compile(r"^\d{2}-[A-Za-z]{3}-\d{4}$")
AMOUNT_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d{1,2})?\)?$")
SECTION_CODE_RE = re.compile(r"^\d{4}$")
DEDUCTOR_SNO_RE = re.compile(r"^\d+$")
TXN_SNO_RE = re.compile(r"^\d+\.\d+$")

# Text-line fallback patterns (used only if table extraction finds nothing)
_AMT = r"\(?-?[\d,]+\.\d{2}\)?"
_DATE = r"\d{2}-[A-Za-z]{3}-\d{4}"
LINE_DEDUCTOR_RE = re.compile(
    rf"^(\d+)\s+(.+?)\s+([A-Z]{{4}}\d{{5}}[A-Z])\s+(?:([A-Z]{{5}}\d{{4}}[A-Z])\s+)?({_AMT})\s+({_AMT})\s+({_AMT})$"
)
LINE_TXN_RE = re.compile(
    rf"^(\d+\.\d+)\s+(\d{{4}})\s+({_DATE})\s+([A-Za-z]+)\s+({_DATE}|-)?\s*(.*?)\s*({_AMT})\s+({_AMT})\s+({_AMT})$"
)

# Parts of the new Annual Tax Statement whose rows share the
# "deductor summary row + transaction detail rows" layout.
PART_LABELS = {
    "I": "Part I - TDS",
    "II": "Part II - TDS (Form No. 121)",
    "VI": "Part VI - TCS",
}

# Best-effort mapping of new section codes to the equivalent section of the
# Income-tax Act, 1961 - helps reconcile with books / prior-year working.
# Codes not listed here are left blank rather than guessed.
OLD_SECTION_EQUIVALENT = {
    "1001": "192", "1002": "192", "1003": "192", "1004": "192A",
    "1005": "194D", "1006": "194H", "1007": "194IB", "1008": "194I(a)",
    "1009": "194I(b)", "1010": "194IA", "1011": "194IC", "1012": "194LA",
    "1013": "194K", "1014": "194LBA", "1015": "194LBA", "1016": "194LBA",
    "1017": "194LBB", "1018": "194LBC", "1019": "193", "1020": "194A",
    "1021": "194A", "1022": "194A", "1023": "194C", "1024": "194C",
    "1025": "194M", "1026": "194J(a)", "1027": "194J(b)", "1028": "194J",
    "1029": "194", "1030": "194DA", "1031": "194Q", "1032": "194P",
    "1033": "194R", "1034": "194R", "1035": "194O", "1036": "194S",
    "1037": "194S", "1038": "194S", "1057": "195", "1058": "194B",
    "1059": "194B", "1060": "194BA", "1061": "194BA", "1062": "194BB",
    "1063": "194G", "1064": "194N", "1065": "194N", "1066": "194EE",
    "1067": "194T",
    "1068": "206C(1)", "1069": "206C(1)", "1070": "206C(1)", "1071": "206C(1)",
    "1072": "206C(1)", "1073": "206C(1)", "1074": "206C(1)",
    "1075": "206C(1F)", "1076": "206C(1F)", "1077": "206C(1F)", "1078": "206C(1F)",
    "1079": "206C(1F)", "1080": "206C(1F)", "1081": "206C(1F)", "1082": "206C(1F)",
    "1083": "206C(1F)", "1084": "206C(1F)", "1085": "206C(1F)",
    "1086": "206C(1G)", "1087": "206C(1G)", "1088": "206C(1G)", "1089": "206C(1G)",
    "1090": "206C(1C)", "1091": "206C(1C)", "1092": "206C(1C)",
}


def parse_amount(value) -> float:
    """'1,19,256.00' -> 119256.0 ; '(33,509.00)' -> -33509.0 ; '-' -> 0.0"""
    if value is None:
        return 0.0
    s = str(value).strip().replace("₹", "").replace(" ", "")
    if not s or s == "-":
        return 0.0
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "")
    try:
        num = float(s)
    except ValueError:
        return 0.0
    return -num if negative else num


def _clean(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell).replace("\n", " ")).strip()


def detect_26as_format(pdf_path: str) -> str:
    """
    Detect whether a 26AS PDF is in the old (IT Act 1961) layout or the new
    Annual Tax Statement layout (IT Act 2025, Tax Year based).
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            sample = ""
            for page in pdf.pages[:3]:
                sample += (page.extract_text(x_tolerance=1) or "") + "\n"
    except Exception:
        return FORMAT_OLD

    new_score = 0
    old_score = 0

    if re.search(r"\bTax\s*Year\b", sample):
        new_score += 2
    if re.search(r"Summary\s*for\s*TY", sample, re.IGNORECASE):
        new_score += 2
    if re.search(r"Section\s*Code", sample):
        new_score += 1
    # Transaction rows: "1.1 1031 29-Sep-2026 Final ..."
    if re.search(r"^\d+\.\d+\s+10\d{2}\s+\d{2}-[A-Za-z]{3}-\d{4}", sample, re.MULTILINE):
        new_score += 3
    if re.search(r"Form\s*No\.\s*121|section\s*39[2-4]", sample, re.IGNORECASE):
        new_score += 1

    if re.search(r"Assessment\s*Year", sample, re.IGNORECASE):
        old_score += 2
    if re.search(r"Financial\s*Year", sample, re.IGNORECASE):
        old_score += 2
    if re.search(r"\b19[2-6][A-Z]{0,3}\b\s+\d{2}-[A-Za-z]{3}-\d{4}", sample):
        old_score += 3
    if re.search(r"15G\s*/\s*15H|203AA", sample):
        old_score += 1

    return FORMAT_NEW if new_score > old_score else FORMAT_OLD


class AnnualTaxStatementParser:
    """Parser for the new (IT Act 2025) Annual Tax Statement PDF."""

    TABLE_SETTINGS = {"text_x_tolerance": 1}

    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self.page_texts: List[str] = []
        self.page_tables: List[list] = []
        self.assessee_info = {
            "name": "",
            "pan": "",
            "financial_year": "",
            "tax_year": "",
            "assessment_year": "",
            "address": "",
            "pan_status": "",
            "data_updated_till": "",
            "format": FORMAT_NEW,
            "format_label": "New Annual Tax Statement (Income-tax Act, 2025)",
        }
        self.section_descriptions: Dict[str, Dict[str, str]] = {}
        self.deductors: List[Dict] = []
        self.transactions: List[Dict] = []
        self.summary: Dict[str, float] = {}
        self.gross_totals: Dict[str, Dict[str, float]] = {}

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    def extract(self):
        with pdfplumber.open(self.pdf_path) as pdf:
            for page in pdf.pages:
                self.page_texts.append(page.extract_text(x_tolerance=1) or "")
                try:
                    self.page_tables.append(page.extract_tables(self.TABLE_SETTINGS) or [])
                except Exception:
                    self.page_tables.append([])

    # ------------------------------------------------------------------ #
    # Assessee / header info
    # ------------------------------------------------------------------ #
    def parse_assessee_info(self):
        if not self.page_texts:
            return
        text = self.page_texts[0]
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        m = re.search(r"Data\s*updated\s*till\s*(\d{2}-[A-Za-z]{3}-\d{4})", text)
        if m:
            self.assessee_info["data_updated_till"] = m.group(1)

        # "AABCO1540R Active 2026-27" line follows the PAN/Status/Tax Year header
        m = re.search(r"\b([A-Z]{5}[0-9]{4}[A-Z])\s+([A-Za-z ]+?)\s+(\d{4}-\d{2,4})\b", text)
        if m:
            self.assessee_info["pan"] = m.group(1)
            self.assessee_info["pan_status"] = m.group(2).strip()
            ty = m.group(3)
        else:
            pm = re.search(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b", text)
            if pm:
                self.assessee_info["pan"] = pm.group(1)
            tm = re.search(r"(?:Tax\s*Year|TY)\s*:?\s*(\d{4}-\d{2,4})", text)
            ty = tm.group(1) if tm else ""

        if ty:
            start, end = ty.split("-", 1)
            ty = f"{start}-{end[-2:]}"
            self.assessee_info["tax_year"] = ty
            # Kept for UI / Excel compatibility with the old format
            self.assessee_info["financial_year"] = ty
            self.assessee_info["assessment_year"] = "Not applicable (Tax Year regime)"

        def value_after(label_re):
            for i, line in enumerate(lines):
                if re.fullmatch(label_re, line, re.IGNORECASE):
                    collected = []
                    for nxt in lines[i + 1:]:
                        if re.match(r"(Address of Assessee|Above data|Refer www|\(All amount|Summary for)", nxt, re.IGNORECASE):
                            break
                        collected.append(nxt)
                    return " ".join(collected).strip()
            return ""

        self.assessee_info["name"] = value_after(r"Name\s*of\s*Assessee")
        self.assessee_info["address"] = value_after(r"Address\s*of\s*Assessee")

        # Summary for TY: TDS / TCS / demand / refund
        m = re.search(
            r"Summary\s*for\s*TY.*?\n.*?\n\s*₹?\s*([\d,().-]+)\s*₹?\s*([\d,().-]+)\s*₹?\s*([\d,().-]+)\s*₹?\s*([\d,().-]+)",
            text, re.IGNORECASE | re.DOTALL,
        )
        if m:
            self.summary = {
                "total_tds": parse_amount(m.group(1)),
                "total_tcs": parse_amount(m.group(2)),
                "demand_outstanding": parse_amount(m.group(3)),
                "refund_issued": parse_amount(m.group(4)),
            }

    # ------------------------------------------------------------------ #
    # Section-code legend
    # ------------------------------------------------------------------ #
    def parse_section_legend(self):
        for tables in self.page_tables:
            for table in tables:
                if not table or not table[0]:
                    continue
                header = [_clean(c).lower() for c in table[0]]
                if not header or header[0] != "section code":
                    continue
                for row in table[1:]:
                    cells = [_clean(c) for c in row]
                    if len(cells) >= 3 and SECTION_CODE_RE.match(cells[0]):
                        self.section_descriptions[cells[0]] = {
                            "section": cells[1],
                            "description": cells[2],
                        }

    # ------------------------------------------------------------------ #
    # Transactions - table based (primary)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _detect_part(cell_text: str) -> Optional[str]:
        m = re.match(r"^PART\s*[-–]?\s*([IVX]+)\b", cell_text, re.IGNORECASE)
        return m.group(1).upper() if m else None

    def _parse_deductor_cells(self, cells: List[str]) -> Optional[Dict]:
        cells = [c for c in cells if c]
        if len(cells) < 5 or not DEDUCTOR_SNO_RE.match(cells[0]):
            return None
        amounts = cells[-3:]
        if not all(AMOUNT_RE.match(a) for a in amounts):
            return None
        middle = cells[1:-3]
        tan, pan, name_parts = "", "", []
        for c in middle:
            if TAN_RE.match(c) and not tan:
                tan = c
            elif PAN_RE.match(c) and not pan:
                pan = c
            else:
                name_parts.append(c)
        if not tan and not pan:
            return None
        return {
            "serial": cells[0],
            "name": " ".join(name_parts).strip() or "Not Available",
            "tan": tan or "Not Available",
            "pan": pan,
            "total_amount": parse_amount(amounts[0]),
            "total_tax": parse_amount(amounts[1]),
            "total_tds": parse_amount(amounts[2]),
        }

    def _parse_txn_cells(self, cells: List[str]) -> Optional[Dict]:
        cells = [c for c in cells if c]
        if len(cells) < 7 or not TXN_SNO_RE.match(cells[0]):
            return None
        amounts = cells[-3:]
        if not all(AMOUNT_RE.match(a) for a in amounts):
            return None
        rest = cells[1:-3]
        code = rest[0] if rest and SECTION_CODE_RE.match(rest[0]) else ""
        dates = [c for c in rest if DATE_RE.match(c)]
        txn_date = dates[0] if dates else ""
        booking_date = dates[1] if len(dates) > 1 else ""
        status, remarks = "", ""
        others = [c for c in rest[1:] if not DATE_RE.match(c)]
        if others:
            status = others[0]
            remarks = " ".join(others[1:])
        if remarks.strip() == "-":
            remarks = ""
        return {
            "serial": cells[0],
            "section_code": code,
            "transaction_date": txn_date,
            "status": status,
            "date_of_booking": booking_date,
            "remarks": remarks,
            "amount_paid": parse_amount(amounts[0]),
            "tax_deducted": parse_amount(amounts[1]),
            "tds_deposited": parse_amount(amounts[2]),
        }

    def parse_from_tables(self):
        current_part = None
        current_deductor = None
        for page_idx, tables in enumerate(self.page_tables, 1):
            for table in tables:
                for row in table:
                    if not row:
                        continue
                    cells = [_clean(c) for c in row]
                    first = next((c for c in cells if c), "")
                    if not first:
                        continue

                    part = self._detect_part(first)
                    if part:
                        current_part = part
                        current_deductor = None
                        continue
                    if first.lower().startswith("gross total"):
                        amts = [c for c in cells[1:] if c and AMOUNT_RE.match(c)]
                        if len(amts) >= 3 and current_part:
                            self.gross_totals[current_part] = {
                                "amount": parse_amount(amts[-3]),
                                "tax": parse_amount(amts[-2]),
                                "deposited": parse_amount(amts[-1]),
                            }
                        continue
                    if current_part not in PART_LABELS:
                        continue

                    if TXN_SNO_RE.match(first):
                        txn = self._parse_txn_cells(cells)
                        if txn and current_deductor:
                            self._add_transaction(txn, current_deductor, current_part, page_idx)
                        continue

                    if DEDUCTOR_SNO_RE.match(first):
                        ded = self._parse_deductor_cells(cells)
                        if ded:
                            ded["part"] = current_part
                            ded["page_number"] = page_idx
                            self.deductors.append(ded)
                            current_deductor = ded

    # ------------------------------------------------------------------ #
    # Transactions - text based (fallback)
    # ------------------------------------------------------------------ #
    def parse_from_text(self):
        current_part = None
        current_deductor = None
        for page_idx, text in enumerate(self.page_texts, 1):
            for raw in text.split("\n"):
                line = raw.strip()
                if not line:
                    continue
                part = self._detect_part(line)
                if part:
                    current_part = part
                    current_deductor = None
                    continue
                if current_part not in PART_LABELS:
                    continue
                m = LINE_TXN_RE.match(line)
                if m and current_deductor:
                    remarks = m.group(6).strip()
                    txn = {
                        "serial": m.group(1),
                        "section_code": m.group(2),
                        "transaction_date": m.group(3),
                        "status": m.group(4),
                        "date_of_booking": m.group(5) if m.group(5) and m.group(5) != "-" else "",
                        "remarks": "" if remarks == "-" else remarks,
                        "amount_paid": parse_amount(m.group(7)),
                        "tax_deducted": parse_amount(m.group(8)),
                        "tds_deposited": parse_amount(m.group(9)),
                    }
                    self._add_transaction(txn, current_deductor, current_part, page_idx)
                    continue
                m = LINE_DEDUCTOR_RE.match(line)
                if m:
                    ded = {
                        "serial": m.group(1),
                        "name": m.group(2).strip(),
                        "tan": m.group(3),
                        "pan": m.group(4) or "",
                        "total_amount": parse_amount(m.group(5)),
                        "total_tax": parse_amount(m.group(6)),
                        "total_tds": parse_amount(m.group(7)),
                        "part": current_part,
                        "page_number": page_idx,
                    }
                    self.deductors.append(ded)
                    current_deductor = ded

    # ------------------------------------------------------------------ #
    def _add_transaction(self, txn: Dict, deductor: Dict, part: str, page: int):
        code = txn["section_code"]
        legend = self.section_descriptions.get(code, {})
        amount_paid = txn["amount_paid"]
        tax = txn["tax_deducted"]
        deposited = txn["tds_deposited"]
        self.transactions.append({
            "serial_in_pdf": txn["serial"],
            "deductor_name": deductor["name"],
            "deductor_tan": deductor["tan"],
            "deductor_pan": deductor.get("pan", ""),
            "deductor_total_amount": deductor["total_amount"],
            "deductor_total_tax": deductor["total_tax"],
            "deductor_total_tds": deductor["total_tds"],
            "part": PART_LABELS.get(part, f"Part {part}"),
            "section": code,
            "section_code": code,
            "section_reference": legend.get("section", ""),
            "section_description": legend.get("description", ""),
            "old_section": OLD_SECTION_EQUIVALENT.get(code, ""),
            "transaction_date": txn["transaction_date"],
            "status": txn["status"],
            "date_of_booking": txn["date_of_booking"] or txn["transaction_date"],
            "remarks": txn["remarks"],
            "amount_paid": round(amount_paid, 2),
            "tax_deducted": round(tax, 2),
            "tds_deposited": round(deposited, 2),
            "net_amount": round(amount_paid - deposited, 2),
            "rate": round(tax / amount_paid * 100, 2) if amount_paid else 0,
            "page_number": page,
            "source": "table",
        })

    def _refresh_section_info(self):
        # Legend pages come after the transactions, so fill descriptions now.
        for t in self.transactions:
            legend = self.section_descriptions.get(t["section_code"], {})
            t["section_reference"] = legend.get("section", t["section_reference"])
            t["section_description"] = legend.get("description", t["section_description"])

    def validate(self) -> Dict:
        """Cross-check transaction sums against deductor and gross totals."""
        mismatches = []
        for ded in self.deductors:
            rows = [t for t in self.transactions
                    if t["deductor_tan"] == ded["tan"]
                    and t["deductor_name"] == ded["name"]
                    and t["part"] == PART_LABELS.get(ded["part"])]
            s_amt = round(sum(t["amount_paid"] for t in rows), 2)
            s_tax = round(sum(t["tax_deducted"] for t in rows), 2)
            s_dep = round(sum(t["tds_deposited"] for t in rows), 2)
            if (abs(s_amt - ded["total_amount"]) > 0.05 or abs(s_tax - ded["total_tax"]) > 0.05
                    or abs(s_dep - ded["total_tds"]) > 0.05):
                mismatches.append({
                    "deductor": ded["name"], "tan": ded["tan"],
                    "expected": [ded["total_amount"], ded["total_tax"], ded["total_tds"]],
                    "parsed": [s_amt, s_tax, s_dep],
                })
        gross_checks = {}
        for part, totals in self.gross_totals.items():
            rows = [t for t in self.transactions if t["part"] == PART_LABELS.get(part)]
            parsed = round(sum(t["tds_deposited"] for t in rows), 2)
            gross_checks[PART_LABELS.get(part, part)] = {
                "expected_deposited": totals["deposited"],
                "parsed_deposited": parsed,
                "match": abs(parsed - totals["deposited"]) <= 0.05,
            }
        return {
            "deductor_count": len(self.deductors),
            "transaction_count": len(self.transactions),
            "deductor_mismatches": mismatches,
            "gross_total_checks": gross_checks,
            "all_totals_match": not mismatches and all(g["match"] for g in gross_checks.values()),
        }

    def parse(self) -> Dict:
        self.extract()
        self.parse_assessee_info()
        self.parse_section_legend()
        self.parse_from_tables()
        if not self.transactions:
            self.deductors, self.gross_totals = [], {}
            self.parse_from_text()
        self._refresh_section_info()

        for i, t in enumerate(self.transactions, 1):
            t["sr_no"] = i

        validation = self.validate()
        print(f"[AnnualTaxStatementParser] {validation['deductor_count']} deductors, "
              f"{validation['transaction_count']} transactions, "
              f"totals match: {validation['all_totals_match']}")

        return {
            "format": FORMAT_NEW,
            "assessee_info": self.assessee_info,
            "transactions": self.transactions,
            "total_transactions": len(self.transactions),
            "deductors": self.deductors,
            "summary": self.summary,
            "validation": validation,
        }
