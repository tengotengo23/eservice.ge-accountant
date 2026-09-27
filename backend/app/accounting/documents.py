"""ატვირთული ფაილის მომზადება LLM-ისთვის: ტიპის ამოცნობა magic bytes-ით, Excel -> CSV.

PDF/სურათი იგზავნება პირდაპირ (სამივე პროვაიდერს აქვს vision/PDF). Excel-ს LLM-ები
ნატიურად არ იღებენ, ამიტომ ვაქცევთ CSV ტექსტად. ცნობილი ბანკების ამონაწერები (TBC, BOG,...)
უმჯობესია დეტერმინისტული პარსერით დამუშავდეს - LLM მხოლოდ უცნობ ფორმატებზე.
"""

from __future__ import annotations

import csv
import io
from pathlib import PurePath

from ..llm.base import Attachment

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_MAX_SHEET_ROWS = 5000


class UnsupportedDocumentError(ValueError):
    pass


def detect_media_type(data: bytes, filename: str) -> str:
    ext = PurePath(filename).suffix.lower()
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"PK\x03\x04") and ext in (".xlsx", ".xlsm"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if ext in (".csv", ".txt"):
        return "text/csv" if ext == ".csv" else "text/plain"
    raise UnsupportedDocumentError(f"{filename}: მხარდაუჭერელი ფორმატი (PDF, JPG, PNG, WEBP, XLSX, CSV)")


def xlsx_to_csv_text(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = io.StringIO()
    writer = csv.writer(out)
    for ws in wb.worksheets:
        out.write(f"# sheet: {ws.title}\n")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= _MAX_SHEET_ROWS:
                out.write(f"# ... truncated after {_MAX_SHEET_ROWS} rows\n")
                break
            if any(cell is not None for cell in row):
                writer.writerow(["" if c is None else c for c in row])
    return out.getvalue()


def to_attachment(data: bytes, filename: str) -> Attachment:
    if len(data) > MAX_UPLOAD_BYTES:
        raise UnsupportedDocumentError(f"{filename}: ფაილი 25MB-ზე დიდია")
    media_type = detect_media_type(data, filename)
    if media_type.endswith("spreadsheetml.sheet"):
        csv_name = PurePath(filename).with_suffix(".csv").name
        return Attachment(xlsx_to_csv_text(data).encode(), "text/csv", csv_name)
    if media_type.startswith("text/"):
        text = data.decode("utf-8-sig", errors="replace")
        return Attachment(text.encode(), media_type, filename)
    return Attachment(data, media_type, filename)
