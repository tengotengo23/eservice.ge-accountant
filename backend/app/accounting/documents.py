"""ატვირთული ფაილის მომზადება LLM-ისთვის: ტიპის ამოცნობა magic bytes-ით, Excel -> CSV.

PDF/სურათი იგზავნება პირდაპირ (სამივე პროვაიდერს აქვს vision/PDF). Excel-ს LLM-ები
ნატიურად არ იღებენ, ამიტომ ვაქცევთ CSV ტექსტად. ცნობილი ბანკების ამონაწერები (TBC, BOG,...)
უმჯობესია დეტერმინისტული პარსერით დამუშავდეს - LLM მხოლოდ უცნობ ფორმატებზე.
"""

from __future__ import annotations

import codecs
import csv
import io
import re
import unicodedata
from pathlib import PurePath

from ..llm.base import Attachment

# 20MB base64-ის შემდეგ ≈ 26.7MB - ეტევა LLM-ის მოთხოვნის ლიმიტში (router.MAX_ENCODED_REQUEST_BYTES)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MAX_SHEET_ROWS = 5000
_MAX_SHEETS = 20
_MAX_CSV_CHARS = 2_000_000  # ~0.5-1M ტოკენი; უფრო დიდი ამონაწერი LLM-ს ერთბაშად არ უნდა გადაეცეს
_MAX_FILENAME_LEN = 100
_UNSAFE_FILENAME_CHARS = re.compile(r"""[<>"'`&|\\/\x00-\x1f\x7f]""")


class UnsupportedDocumentError(ValueError):
    pass


class DocumentTooLargeError(UnsupportedDocumentError):
    pass


def safe_filename(filename: str) -> str:
    """ფაილის სახელი მიდის LLM-ის prompt-ში (`<file name="...">`): ვაშორებთ გზას, საკონტროლო და
    მარკაპის სიმბოლოებს, რათა სახელით prompt-ის სტრუქტურა ვერ "გატყდეს", და ვზღუდავთ სიგრძეს."""
    name = PurePath(filename.replace("\\", "/")).name
    name = unicodedata.normalize("NFC", name)
    name = " ".join(_UNSAFE_FILENAME_CHARS.sub("_", name).split())
    if len(name) > _MAX_FILENAME_LEN:
        suffix = PurePath(name).suffix[:10]
        name = name[: _MAX_FILENAME_LEN - len(suffix)] + suffix
    return name or "document"


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
    """Excel -> CSV ტექსტი. ლიმიტები იცავს მეხსიერებას (xlsx = zip, შეკუმშვის შემდეგ შეიძლება უზარმაზარი
    იყოს) და LLM-ის კონტექსტს. ლიმიტის გადაჭარბება ჩუმად არ იჭრება - ფაილი უარყოფილია."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        if len(wb.worksheets) > _MAX_SHEETS:
            raise DocumentTooLargeError(f"Excel ფაილში {_MAX_SHEETS}-ზე მეტი ფურცელია")
        out = io.StringIO()
        writer = csv.writer(out)
        for ws in wb.worksheets:
            out.write(f"# sheet: {ws.title}\n")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= _MAX_SHEET_ROWS:
                    raise DocumentTooLargeError(f"ფურცელში '{ws.title}' {_MAX_SHEET_ROWS}-ზე მეტი სტრიქონია; "
                                                "დაყავით ფაილი პერიოდების მიხედვით")
                if any(cell is not None for cell in row):
                    writer.writerow(["" if c is None else c for c in row])
                if out.tell() > _MAX_CSV_CHARS:
                    raise DocumentTooLargeError("Excel ფაილი ზედმეტად დიდია ერთბაშად დასამუშავებლად")
        return out.getvalue()
    finally:
        wb.close()


def decode_text(data: bytes) -> str:
    """CSV/TXT: UTF-16 (Excel-ის "Unicode Text" ექსპორტი) BOM-ით, სხვა შემთხვევაში UTF-8."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def to_attachment(data: bytes, filename: str) -> Attachment:
    filename = safe_filename(filename)
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentTooLargeError(f"{filename}: ფაილი {MAX_UPLOAD_BYTES // 1024 // 1024}MB-ზე დიდია")
    if not data:
        raise UnsupportedDocumentError(f"{filename}: ფაილი ცარიელია")
    media_type = detect_media_type(data, filename)
    if media_type.endswith("spreadsheetml.sheet"):
        csv_name = PurePath(filename).with_suffix(".csv").name
        return Attachment(xlsx_to_csv_text(data).encode(), "text/csv", csv_name)
    if media_type.startswith("text/"):
        return Attachment(decode_text(data).encode(), media_type, filename)
    return Attachment(data, media_type, filename)
