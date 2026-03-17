import io
import unittest
import zipfile

from preyherbtracker.tracking import (
    parse_forum_cat_count,
    parse_spreadsheet_cat_count,
    parse_spreadsheet_rows_from_bytes,
    parse_tracking_cat_count,
    parse_tracking_cat_count_from_bytes,
)


class TrackingParserTests(unittest.TestCase):
    @staticmethod
    def _build_simple_xlsx_bytes() -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as workbook:
            workbook.writestr(
                "xl/worksheets/sheet1.xml",
                """
                <worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">
                  <sheetData>
                    <row r=\"1\">
                      <c r=\"A1\" t=\"inlineStr\"><is><t>clan</t></is></c>
                      <c r=\"B1\" t=\"inlineStr\"><is><t>cat_count</t></is></c>
                    </row>
                    <row r=\"2\">
                      <c r=\"A2\" t=\"inlineStr\"><is><t>birchclan</t></is></c>
                      <c r=\"B2\"><v>19</v></c>
                    </row>
                  </sheetData>
                </worksheet>
                """,
            )
        return buffer.getvalue()

    def test_parse_forum_cat_count(self) -> None:
        text = """
        BirchClan Census
        Active Cats: 17
        Apprentices: 4
        """
        self.assertEqual(parse_forum_cat_count(text), 17)

    def test_parse_forum_cat_count_ignores_zero_noise(self) -> None:
        text = """
        <html>
        <body>
            <p>0 cats online</p>
            <p>Cat count: 14</p>
        </body>
        </html>
        """
        self.assertEqual(parse_forum_cat_count(text), 14)

    def test_parse_forum_cat_count_requires_positive_count(self) -> None:
        with self.assertRaises(ValueError):
            parse_forum_cat_count("0 cats online")

    def test_parse_spreadsheet_cat_count_with_header(self) -> None:
        csv_text = "clan,cat_count\nbirchclan,21\n"
        self.assertEqual(parse_spreadsheet_cat_count(csv_text), 21)

    def test_parse_spreadsheet_fallback_number(self) -> None:
        csv_text = "name,total\nbirchclan,15\n"
        self.assertEqual(parse_spreadsheet_cat_count(csv_text), 15)

    def test_parse_tracking_cat_count_modes(self) -> None:
        self.assertEqual(parse_tracking_cat_count("forum", "Cats total: 9"), 9)
        self.assertEqual(parse_tracking_cat_count("spreadsheet", "cats\n11\n"), 11)

    def test_parse_tracking_cat_count_invalid_mode(self) -> None:
        with self.assertRaises(ValueError):
            parse_tracking_cat_count("manual", "cat_count,4")

    def test_parse_tracking_cat_count_from_xlsx_bytes(self) -> None:
        data = self._build_simple_xlsx_bytes()
        self.assertEqual(parse_tracking_cat_count_from_bytes("spreadsheet", data), 19)

    def test_parse_spreadsheet_rows_from_xlsx_bytes(self) -> None:
        data = self._build_simple_xlsx_bytes()
        rows = parse_spreadsheet_rows_from_bytes(data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["clan"], "birchclan")
        self.assertEqual(rows[0]["cat_count"], "19")


if __name__ == "__main__":
    unittest.main()
