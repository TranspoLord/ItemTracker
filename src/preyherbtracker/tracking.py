from __future__ import annotations

import csv
import html
import io
import re
import zipfile
from xml.etree import ElementTree as ET
from urllib.request import Request, urlopen


_CAT_COLUMNS = {
    "cat_count",
    "cats",
    "cat count",
    "total_cats",
    "total cats",
    "active_cats",
    "active cats",
}


def fetch_tracking_bytes(link: str, *, timeout_seconds: int = 8) -> bytes:
    request = Request(link, headers={"User-Agent": "PreyHerbTracker/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:  # nosec: B310 - trusted admin-provided URLs
        return response.read()


def fetch_tracking_text(link: str, *, timeout_seconds: int = 8) -> str:
    return fetch_tracking_bytes(link, timeout_seconds=timeout_seconds).decode("utf-8", errors="replace")


def _normalize_forum_text(text: str) -> str:
    # Strip common HTML noise so regex checks run on human-readable content.
    without_script = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    without_style = re.sub(r"<style\b[^>]*>.*?</style>", " ", without_script, flags=re.IGNORECASE | re.DOTALL)
    no_tags = re.sub(r"<[^>]+>", " ", without_style)
    decoded = html.unescape(no_tags)
    return re.sub(r"\s+", " ", decoded).strip().lower()


def parse_forum_cat_count(text: str) -> int:
    normalized = _normalize_forum_text(text)

    labeled_patterns = [
        r"(?:cat\s*count|cats\s*total|total\s*cats|active\s*cats|census\s*total|total\s*population)\D{0,20}(\d{1,4})",
        r"(\d{1,4})\D{0,20}(?:cat\s*count|cats\s*total|total\s*cats|active\s*cats|census\s*total|total\s*population)",
    ]
    labeled_values: list[int] = []
    for pattern in labeled_patterns:
        for match in re.finditer(pattern, normalized):
            value = int(match.group(1))
            if value > 0:
                labeled_values.append(value)
    if labeled_values:
        return max(labeled_values)

    generic_values = [
        int(match.group(1))
        for match in re.finditer(r"(\d{1,4})\s*(?:cats?)", normalized)
        if int(match.group(1)) > 0
    ]
    if generic_values:
        return max(generic_values)

    raise ValueError(
        "Could not find a positive cat count in forum text. Include explicit text like 'Cat count: 14'."
    )


def parse_spreadsheet_cat_count(text: str) -> int:
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        normalized = {str(key).strip().lower(): value for key, value in row.items() if key is not None}
        for key in _CAT_COLUMNS:
            if key in normalized:
                value = str(normalized[key] or "").strip()
                if value.isdigit():
                    return int(value)
                numeric_match = re.search(r"(\d{1,4})", value)
                if numeric_match is not None:
                    return int(numeric_match.group(1))
    fallback = re.search(r"(\d{1,4})", text)
    if fallback is not None:
        return int(fallback.group(1))
    raise ValueError("Could not find cat count in spreadsheet data")


def _column_letters_to_index(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + (ord(char.upper()) - ord("A") + 1)
    return value


def parse_xlsx_cat_count(data: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(data)) as workbook:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            shared_xml = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for item in shared_xml.findall(".//main:si", namespace):
                text_nodes = item.findall(".//main:t", namespace)
                shared_strings.append("".join(node.text or "" for node in text_nodes))

        worksheet_name = None
        for candidate in workbook.namelist():
            if candidate.startswith("xl/worksheets/") and candidate.endswith(".xml"):
                worksheet_name = candidate
                break
        if worksheet_name is None:
            raise ValueError("XLSX does not contain any worksheet")

        sheet_xml = ET.fromstring(workbook.read(worksheet_name))
        namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rows: list[dict[int, str]] = []

        for row in sheet_xml.findall(".//main:sheetData/main:row", namespace):
            values_by_col: dict[int, str] = {}
            for cell in row.findall("main:c", namespace):
                reference = str(cell.get("r") or "")
                letters = "".join(ch for ch in reference if ch.isalpha())
                if not letters:
                    continue
                col_idx = _column_letters_to_index(letters)
                cell_type = str(cell.get("t") or "")
                value_node = cell.find("main:v", namespace)
                inline_node = cell.find("main:is/main:t", namespace)
                raw_value = ""
                if inline_node is not None and inline_node.text is not None:
                    raw_value = inline_node.text
                elif value_node is not None and value_node.text is not None:
                    raw_value = value_node.text
                if cell_type == "s" and raw_value.isdigit():
                    idx = int(raw_value)
                    if 0 <= idx < len(shared_strings):
                        raw_value = shared_strings[idx]
                values_by_col[col_idx] = raw_value
            if values_by_col:
                rows.append(values_by_col)

        if not rows:
            raise ValueError("XLSX worksheet is empty")

        header_row = rows[0]
        headers = {col: str(value).strip().lower() for col, value in header_row.items()}
        for row in rows[1:]:
            for col, header in headers.items():
                if header in _CAT_COLUMNS and col in row:
                    value = str(row[col]).strip()
                    if value.isdigit():
                        return int(value)
                    numeric_match = re.search(r"(\d{1,4})", value)
                    if numeric_match is not None:
                        return int(numeric_match.group(1))

        flattened = " ".join(str(value) for row in rows for value in row.values())
        fallback = re.search(r"(\d{1,4})", flattened)
        if fallback is not None:
            return int(fallback.group(1))
        raise ValueError("Could not find cat count in XLSX data")


def parse_xlsx_rows(data: bytes) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as workbook:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            shared_xml = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for item in shared_xml.findall(".//main:si", namespace):
                text_nodes = item.findall(".//main:t", namespace)
                shared_strings.append("".join(node.text or "" for node in text_nodes))

        worksheet_name = None
        for candidate in workbook.namelist():
            if candidate.startswith("xl/worksheets/") and candidate.endswith(".xml"):
                worksheet_name = candidate
                break
        if worksheet_name is None:
            raise ValueError("XLSX does not contain any worksheet")

        sheet_xml = ET.fromstring(workbook.read(worksheet_name))
        namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rows: list[dict[int, str]] = []

        for row in sheet_xml.findall(".//main:sheetData/main:row", namespace):
            values_by_col: dict[int, str] = {}
            for cell in row.findall("main:c", namespace):
                reference = str(cell.get("r") or "")
                letters = "".join(ch for ch in reference if ch.isalpha())
                if not letters:
                    continue
                col_idx = _column_letters_to_index(letters)
                cell_type = str(cell.get("t") or "")
                value_node = cell.find("main:v", namespace)
                inline_node = cell.find("main:is/main:t", namespace)
                raw_value = ""
                if inline_node is not None and inline_node.text is not None:
                    raw_value = inline_node.text
                elif value_node is not None and value_node.text is not None:
                    raw_value = value_node.text
                if cell_type == "s" and raw_value.isdigit():
                    idx = int(raw_value)
                    if 0 <= idx < len(shared_strings):
                        raw_value = shared_strings[idx]
                values_by_col[col_idx] = raw_value
            if values_by_col:
                rows.append(values_by_col)

    if not rows:
        return []
    header_row = rows[0]
    headers = {col: str(value).strip().lower() for col, value in header_row.items()}
    out_rows: list[dict[str, str]] = []
    for row in rows[1:]:
        out: dict[str, str] = {}
        for col, header in headers.items():
            if not header:
                continue
            out[header] = str(row.get(col, "")).strip()
        if any(value for value in out.values()):
            out_rows.append(out)
    return out_rows


def parse_spreadsheet_cat_count_from_bytes(data: bytes) -> int:
    if data.startswith(b"PK"):
        return parse_xlsx_cat_count(data)
    return parse_spreadsheet_cat_count(data.decode("utf-8", errors="replace"))


def parse_spreadsheet_rows_from_bytes(data: bytes) -> list[dict[str, str]]:
    if data.startswith(b"PK"):
        return parse_xlsx_rows(data)
    reader = csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace")))
    rows: list[dict[str, str]] = []
    for row in reader:
        normalized: dict[str, str] = {}
        for key, value in row.items():
            if key is None:
                continue
            normalized[str(key).strip().lower()] = str(value or "").strip()
        if normalized:
            rows.append(normalized)
    return rows


def parse_tracking_cat_count(mode: str, raw_text: str) -> int:
    normalized_mode = mode.strip().lower()
    if normalized_mode == "forum":
        return parse_forum_cat_count(raw_text)
    if normalized_mode == "spreadsheet":
        return parse_spreadsheet_cat_count(raw_text)
    raise ValueError("Tracking parser only supports forum and spreadsheet modes")


def parse_tracking_cat_count_from_bytes(mode: str, data: bytes) -> int:
    normalized_mode = mode.strip().lower()
    if normalized_mode == "forum":
        return parse_forum_cat_count(data.decode("utf-8", errors="replace"))
    if normalized_mode == "spreadsheet":
        return parse_spreadsheet_cat_count_from_bytes(data)
    raise ValueError("Tracking parser only supports forum and spreadsheet modes")
