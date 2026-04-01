from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageOps


if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

warnings.filterwarnings(
    "ignore",
    message="Palette images with Transparency expressed in bytes should be converted to RGBA images",
    category=UserWarning,
)


DEFAULT_INPUT_PDF = Path.home() / "Desktop" / "\u7406\u8ce0\u7533\u8acb\u66f8.pdf"
DEFAULT_SAMPLE_PDF = Path.home() / "Desktop" / "RI_claim \u5df2\u586b\u5beb.pdf"

# These are the fixed values that appear stable in the sample form.
SAMPLE_PRESET_DEFAULTS = {
    "company_name": "\u4e2d\u83ef\u822a\u7a7a",
    "claimant_relation": "\u672c\u4eba",
}

TEXT_FIELD_GROUPS = {
    "insured_name": ["fill_10"],
    "insured_id_number": ["fill_16"],
    "claimant_name": ["fill_12"],
    "claimant_id_number": ["fill_17"],
    "claimant_relation": ["fill_21"],
    "company_name": ["fill_15"],
    "accident_location": ["fill_20"],
    "other_insurer": ["fill_23"],
    "bank_name": ["fill_5"],
    "bank_branch": ["fill_6"],
    "bank_code": ["comb_1"],
    "bank_account": ["comb_2"],
    "phone": ["fill_136"],
    "mailing_address": ["fill_142"],
    "accident_reason": ["fill_148"],
    "account_holder_name": ["fill_151"],
    "account_holder_id_number": ["fill_152"],
}

# Deliberately excluded from auto-fill:
# - any signature / handwritten confirmation areas
# - the customer signature section on the last page

DATE_FIELD_GROUPS = {
    "birth_date": ["fill_130", "fill_131", "fill_132", "fill_133", "fill_134", "fill_135"],
    "accident_datetime": ["fill_125", "fill_126", "fill_127", "fill_128", "fill_129"],
}

FULL_KEYS = [
    "insured_name",
    "insured_id_number",
    "claimant_name",
    "claimant_id_number",
    "claimant_relation",
    "company_name",
    "accident_location",
    "other_insurer",
    "bank_name",
    "bank_branch",
    "bank_code",
    "bank_account",
    "phone",
    "mailing_address",
    "accident_reason",
    "account_holder_name",
    "account_holder_id_number",
    "birth_date",
    "accident_datetime",
]

QUICK_KEYS = [
    "name",
    "id_number",
    "address",
    "accident_reason",
    "birth_date",
    "phone",
    "accident_datetime",
    "bank_name",
    "bank_branch",
    "bank_code",
    "bank_account",
    "other_insurer",
]

INSTITUTION_KEYWORDS = (
    "\u9280\u884c",
    "\u90f5\u5c40",
    "\u8fb2\u6703",
    "\u4fe1\u7528\u5408\u4f5c\u793e",
    "\u6f01\u6703",
)

BRANCH_KEYWORDS = (
    "\u5206\u884c",
    "\u5206\u793e",
    "\u652f\u884c",
    "\u652f\u5c40",
    "\u71df\u696d\u90e8",
)

BANK_ACCOUNT_LABELS = (
    "\u5b58\u6236\u5e33\u865f",
    "\u5b58\u6236\u5e33\u6236",
    "\u5e33\u865f",
    "\u5e33\u6236",
    "\u5b58\u6236\u677f\u865f",
    "\u6236\u865f",
    "\u8d26\u53f7",
    "\u8d26\u6236",
)

BANK_CODE_LABELS = (
    "\u9280\u884c\u4ee3\u78bc",
    "\u9280\u884c\u4ee3\u865f",
    "\u91d1\u878d\u6a5f\u69cb\u4ee3\u78bc",
    "\u884c\u5eab\u4ee3\u78bc",
    "\u4ee3\u78bc",
    "\u4ee3\u865f",
)

BANK_HOLDER_LABELS = (
    "\u6236\u540d",
    "\u5b58\u6236\u540d",
    "\u6237\u540d",
    "\u5b58\u6237\u540d",
)

DIAGNOSIS_NAME_LABELS = (
    "\u75c5\u60a3\u59d3\u540d",
    "\u60a3\u8005\u59d3\u540d",
    "\u75c5\u4eba\u59d3\u540d",
    "\u59d3\u540d",
)

DIAGNOSIS_ID_LABELS = (
    "\u570b\u6c11\u8eab\u4efd\u8b49\u7d71\u4e00\u7de8\u865f",
    "\u570b\u6c11\u8eab\u4efd\u8b49\u7d71\u4e00\u7f16\u865f",
    "\u8eab\u4efd\u8b49\u7d71\u4e00\u7de8\u865f",
    "\u8eab\u4efd\u8b49\u7d71\u4e00\u7f16\u865f",
    "\u8eab\u5206\u8b49\u5b57\u865f",
    "\u8eab\u4efd\u8b49\u5b57\u865f",
    "\u8eab\u5206\u8b49\u865f",
    "\u8eab\u4efd\u8b49\u865f",
    "\u7d71\u4e00\u7de8\u865f",
    "\u7d71\u4e00\u7f16\u865f",
    "\u8b49\u865f",
)

DIAGNOSIS_BIRTH_LABELS = (
    "\u51fa\u751f\u65e5\u671f",
    "\u51fa\u751f\u5e74\u6708\u65e5",
    "\u751f\u65e5",
    "\u51fa\u751f",
)

DIAGNOSIS_ADDRESS_LABELS = (
    "\u5730\u5740",
    "\u4f4f\u5740",
    "\u6236\u7c4d\u5730\u5740",
    "\u73fe\u4f4f\u5730\u5740",
    "\u901a\u8a0a\u5730\u5740",
)

DIAGNOSIS_ACCIDENT_LABELS = (
    "\u4e8b\u6545\u65e5\u671f",
    "\u53d7\u50b7\u65e5\u671f",
    "\u53d7\u50b7\u6642\u9593",
    "\u767c\u751f\u65e5\u671f",
    "\u61c9\u8a3a",
    "\u61c9\u8a3a\u81ea",
    "\u9580\u8a3a\u65e5\u671f",
    "\u5c31\u8a3a\u65e5\u671f",
    "\u521d\u8a3a\u65e5\u671f",
    "\u4f4f\u9662\u65e5\u671f",
    "\u5165\u9662\u65e5\u671f",
    "\u65e5\u671f\u81f3",
)

DIAGNOSIS_REASON_LABELS = (
    "\u8a3a\u65b7",
    "\u8a3a\u65b7\u7d50\u679c",
    "\u75c5\u540d",
    "\u50b7\u75c5\u540d\u7a31",
    "\u4e3b\u8a34",
    "\u50b7\u52e2",
)

DIAGNOSIS_REASON_HINT_KEYWORDS = (
    "\u6301\u7e8c",
    "\u4f11\u990a",
    "\u5efa\u8b70",
    "\u75bc\u75db",
    "\u8170",
    "\u819d",
    "\u9aa8\u6298",
    "\u632b\u50b7",
    "\u64e6\u50b7",
    "\u6495\u88c2",
    "\u53d7\u50b7",
    "\u767c\u708e",
    "\u611f\u67d3",
    "\u51fa\u8840",
    "\u75c5",
    "\u50b7",
    "\u708e",
    "\u75c7",
)

DIAGNOSIS_IGNORE_LINES = (
    "\u672c\u8b49\u660e\u66f8\u9808",
    "\u672c\u8b49\u660e\u66f8\u987b",
    "\u5370\u7ae0\u5426\u5247\u7121\u6548",
    "\u5370\u7ae0\u5426\u5219\u7121\u6548",
    "\u4ee5\u4e0a\u75c5\u4eba\u7d93\u672c\u9662",
    "\u4ee5\u4e0a\u75c5\u4eba\u7ecf\u672c\u9662",
    "\u75c5\u6b77\u865f\u78bc",
    "\u75c5\u5386\u53f7\u7801",
    "\u8a3a\u6cbb\u91ab\u5e2b",
    "\u91ab\u5e2b\u8b49\u66f8\u5b57\u865f",
    "\u9662\u9577",
    "\u4e2d\u83ef\u6c11\u570b",
)

OCR_NUMERIC_TRANSLATION = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "I": "1",
        "l": "1",
        "|": "1",
        "\uff10": "0",
        "\uff11": "1",
        "\uff12": "2",
        "\uff13": "3",
        "\uff14": "4",
        "\uff15": "5",
        "\uff16": "6",
        "\uff17": "7",
        "\uff18": "8",
        "\uff19": "9",
        "\uff0d": "-",
        "\u2014": "-",
        "\u2013": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\uff0f": "/",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill the claim application PDF using PDF form fields."
    )
    parser.add_argument(
        "--input-pdf",
        type=Path,
        default=DEFAULT_INPUT_PDF,
        help=f"Blank form PDF. Default: {DEFAULT_INPUT_PDF}",
    )
    parser.add_argument(
        "--output-pdf",
        type=Path,
        default=None,
        help="Output PDF path. Default: same folder as input with _\u5df2\u586b\u5beb suffix.",
    )
    parser.add_argument(
        "--data-json",
        type=Path,
        help="Optional JSON file with claim data. CLI args override JSON values.",
    )
    parser.add_argument(
        "--bank-book-image",
        type=Path,
        help="Optional bank-book cover image. OCR is used to fill bank name / branch / code / account.",
    )
    parser.add_argument(
        "--diagnosis-document",
        type=Path,
        help="Optional diagnosis certificate image or PDF. OCR is used to extract claim data.",
    )
    parser.add_argument(
        "--sample-pdf",
        type=Path,
        default=DEFAULT_SAMPLE_PDF,
        help=f"Filled sample PDF used for checkbox copy. Default: {DEFAULT_SAMPLE_PDF}",
    )
    parser.add_argument(
        "--copy-sample-checkboxes",
        dest="copy_sample_checkboxes",
        action="store_true",
        default=True,
        help="Copy checkbox states from the sample PDF. Default: enabled.",
    )
    parser.add_argument(
        "--no-copy-sample-checkboxes",
        dest="copy_sample_checkboxes",
        action="store_false",
        help="Do not copy checkbox states from the sample PDF.",
    )
    parser.add_argument(
        "--sample-preset",
        dest="sample_preset",
        action="store_true",
        default=True,
        help="Use the stable fixed values from the sample form. Default: enabled.",
    )
    parser.add_argument(
        "--no-sample-preset",
        dest="sample_preset",
        action="store_false",
        help="Do not preload fixed values from the sample preset.",
    )

    # Quick mode: the user only needs these values in the common case.
    parser.add_argument("--name", dest="name", help="Insured person name.")
    parser.add_argument("--id-number", dest="id_number", help="Insured ID number.")
    parser.add_argument(
        "--address",
        dest="address",
        help="Used for both accident location and mailing address.",
    )

    for key in QUICK_KEYS:
        if key in {"name", "id_number", "address"}:
            continue
        parser.add_argument(f"--{key.replace('_', '-')}", dest=key)

    # Advanced overrides, only needed when the claimant is not the insured person.
    for key in FULL_KEYS:
        if key in QUICK_KEYS:
            continue
        parser.add_argument(f"--{key.replace('_', '-')}", dest=key)

    return parser.parse_args()


def load_json_data(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object.")
    return data


def parse_date_parts(value: str, expected_parts: int) -> list[str]:
    parts = re.findall(r"\d+", value)
    if len(parts) != expected_parts:
        raise ValueError(
            f"Expected {expected_parts} numeric parts in '{value}', got {len(parts)}."
        )
    return parts


def normalize_text(text: str) -> str:
    text = text.strip()
    text = text.replace(" ", "")
    text = text.replace("|", "")
    text = text.replace("\uff1a", ":")
    return text


def dedupe_lines(texts: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for text in texts:
        normalized = normalize_text(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def ocr_image_lines(image: Image.Image) -> list[str]:
    image = ImageOps.exif_transpose(image)
    image = ImageOps.autocontrast(image.convert("L"))

    temp_name = None
    texts: list[str] = []
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as handle:
            temp_name = handle.name
        image.save(temp_name)

        try:
            from rapidocr_onnxruntime import RapidOCR

            engine = RapidOCR()
            result, _ = engine(temp_name)
            if result:
                texts.extend(item[1] for item in result if len(item) > 1 and item[1].strip())
        except Exception:
            pass

        if not texts:
            try:
                import easyocr

                reader = easyocr.Reader(["ch_tra", "en"], gpu=False, verbose=False)
                texts = [text.strip() for text in reader.readtext(temp_name, detail=0) if text.strip()]
            except Exception:
                pass
    finally:
        if temp_name:
            temp_path = Path(temp_name)
            if temp_path.exists():
                temp_path.unlink()

    return dedupe_lines(texts)


def extract_document_lines(document_path: Path) -> list[str]:
    if not document_path.exists():
        raise FileNotFoundError(f"Document not found: {document_path}")

    suffix = document_path.suffix.lower()
    if suffix == ".pdf":
        doc = fitz.open(document_path)
        try:
            text_layer_lines: list[str] = []
            for page in doc:
                raw_text = page.get_text("text")
                if raw_text:
                    text_layer_lines.extend(
                        normalize_text(line)
                        for line in raw_text.splitlines()
                        if normalize_text(line)
                    )
            text_layer_lines = dedupe_lines(text_layer_lines)
            if len(text_layer_lines) >= 5:
                return text_layer_lines

            ocr_lines: list[str] = []
            for page in doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                ocr_lines.extend(ocr_image_lines(image))
            return dedupe_lines(ocr_lines)
        finally:
            doc.close()

    image = Image.open(document_path)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return ocr_image_lines(image)


def ocr_bank_book_lines(image_path: Path) -> list[str]:
    return extract_document_lines(image_path)


def parse_bank_book_info(lines: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not lines:
        return parsed

    account_candidates: list[str] = []
    bank_code_candidates: list[str] = []

    for line in lines:
        digits = re.findall(r"\d{3,}", line)
        if "\u4ee3\u78bc" in line or "\u4ee3\u865f" in line:
            for value in digits:
                if len(value) == 3:
                    bank_code_candidates.append(value)
        for value in digits:
            if len(value) >= 10:
                account_candidates.append(value)

    if account_candidates:
        account_candidates.sort(key=lambda value: (-len(value), value))
        parsed["bank_account"] = account_candidates[0]

    if bank_code_candidates:
        parsed["bank_code"] = bank_code_candidates[0]

    bank_line = ""
    branch_line = ""
    for line in lines:
        if any(keyword in line for keyword in INSTITUTION_KEYWORDS):
            bank_line = line
            if any(keyword in line for keyword in BRANCH_KEYWORDS):
                branch_line = line
            break

    if not branch_line:
        for line in lines:
            if any(keyword in line for keyword in BRANCH_KEYWORDS):
                branch_line = line
                break

    if bank_line:
        match = re.match(
            rf"(.+?(?:{'|'.join(INSTITUTION_KEYWORDS)}))(.*(?:{'|'.join(BRANCH_KEYWORDS)}))?$",
            bank_line,
        )
        if match:
            parsed["bank_name"] = match.group(1)
            if match.group(2):
                parsed["bank_branch"] = match.group(2)
        else:
            parsed["bank_name"] = bank_line

    if branch_line and "bank_branch" not in parsed:
        parsed["bank_branch"] = branch_line

    for key in ("bank_name", "bank_branch"):
        if key not in parsed:
            continue
        value = parsed[key]
        value = re.sub(r"^(?:\u9280\u884c|\u5206\u884c|\u5e33\u865f|\u6236\u540d|:|\uff1a)+", "", value)
        value = value.strip(":：")
        parsed[key] = value

    return {key: value for key, value in parsed.items() if value}


def normalize_id_number(value: str) -> str:
    value = value.upper().replace(" ", "")
    if len(value) >= 2:
        value = value[0] + value[1:].replace("O", "0")
    return value


def format_roc_date(parts: list[str], include_time: bool) -> str:
    year = int(parts[0])
    if year > 1911:
        year -= 1911
    month = int(parts[1])
    day = int(parts[2])
    if include_time and len(parts) >= 5:
        hour = int(parts[3])
        minute = int(parts[4])
        return f"{year}/{month}/{day} {hour:02d}:{minute:02d}"
    return f"{year}/{month}/{day}"


def extract_labeled_segment(lines: list[str], labels: tuple[str, ...]) -> str:
    for index, line in enumerate(lines):
        for label in labels:
            match = re.search(rf"{re.escape(label)}[:：]?(.*)", line)
            if not match:
                continue
            value = match.group(1).strip()
            if value:
                return value
            if index + 1 < len(lines):
                next_line = lines[index + 1].strip()
                if next_line:
                    return next_line
    return ""


def extract_name(lines: list[str]) -> str:
    joined = "\n".join(lines)
    for label in DIAGNOSIS_NAME_LABELS:
        match = re.search(rf"{re.escape(label)}[:：]?([\u4e00-\u9fff]{{2,4}})", joined)
        if match:
            return match.group(1)
    return ""


def extract_id_number(lines: list[str]) -> str:
    joined = "\n".join(lines)
    for label in DIAGNOSIS_ID_LABELS:
        match = re.search(rf"{re.escape(label)}[:：]?([A-Za-z][0-9OolI]{{9}})", joined)
        if match:
            return normalize_id_number(match.group(1))
    fallback = re.search(r"\b([A-Za-z][0-9OolI]{9})\b", joined)
    if fallback:
        return normalize_id_number(fallback.group(1))
    return ""


def extract_date_from_labels(lines: list[str], labels: tuple[str, ...], allow_time: bool) -> str:
    joined = "\n".join(lines)
    pattern = r"(\d{2,4}[年/\-.]\d{1,2}[月/\-.]\d{1,2}(?:日)?(?:[ T]\d{1,2}[:時]\d{1,2}(?:分)?)?)"
    for label in labels:
        match = re.search(rf"{re.escape(label)}[:：]?{pattern}", joined)
        if not match:
            continue
        parts = re.findall(r"\d+", match.group(1))
        if len(parts) >= 3:
            return format_roc_date(parts, include_time=allow_time and len(parts) >= 5)
    return ""


def clean_reason_text(value: str) -> str:
    if not value:
        return ""
    stop_labels = (
        DIAGNOSIS_NAME_LABELS
        + DIAGNOSIS_ID_LABELS
        + DIAGNOSIS_BIRTH_LABELS
        + DIAGNOSIS_ADDRESS_LABELS
        + DIAGNOSIS_ACCIDENT_LABELS
    )
    for label in stop_labels:
        if label in value:
            value = value.split(label, 1)[0]
    value = value.strip(":：;；,.，。 ")
    return value[:120]


def clean_address_text(value: str) -> str:
    if not value:
        return ""
    stop_labels = (
        DIAGNOSIS_NAME_LABELS
        + DIAGNOSIS_ID_LABELS
        + DIAGNOSIS_BIRTH_LABELS
        + DIAGNOSIS_ACCIDENT_LABELS
        + DIAGNOSIS_REASON_LABELS
    )
    for label in stop_labels:
        if label in value:
            value = value.split(label, 1)[0]
    value = value.strip(":：;；,.，。 ")
    return value[:120]


def parse_diagnosis_info(lines: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    name = extract_name(lines)
    if name:
        parsed["insured_name"] = name
        parsed["claimant_name"] = name
        parsed["account_holder_name"] = name

    id_number = extract_id_number(lines)
    if id_number:
        parsed["insured_id_number"] = id_number
        parsed["claimant_id_number"] = id_number
        parsed["account_holder_id_number"] = id_number

    birth_date = extract_date_from_labels(lines, DIAGNOSIS_BIRTH_LABELS, allow_time=False)
    if birth_date:
        parsed["birth_date"] = birth_date

    address_candidate = clean_address_text(extract_labeled_segment(lines, DIAGNOSIS_ADDRESS_LABELS))
    if address_candidate:
        parsed["address"] = address_candidate
        parsed["accident_location"] = address_candidate
        parsed["mailing_address"] = address_candidate

    accident_datetime = extract_date_from_labels(lines, DIAGNOSIS_ACCIDENT_LABELS, allow_time=True)
    if accident_datetime:
        parsed["accident_datetime"] = accident_datetime

    reason_candidate = clean_reason_text(extract_labeled_segment(lines, DIAGNOSIS_REASON_LABELS))
    if reason_candidate:
        parsed["accident_reason"] = reason_candidate

    return parsed


def merge_extracted_claim_data(data: dict[str, str], extracted_data: dict[str, str]) -> dict[str, str]:
    for key, value in extracted_data.items():
        data.setdefault(key, value)
    return data


def normalize_data(base_data: dict[str, str], sample_preset: bool = True) -> dict[str, str]:
    data: dict[str, str] = {}
    if sample_preset:
        data.update(SAMPLE_PRESET_DEFAULTS)

    data.update({key: str(value) for key, value in base_data.items() if value not in (None, "")})

    # Quick mode mapping.
    if "name" in data:
        data.setdefault("insured_name", data["name"])
        data.setdefault("claimant_name", data["name"])
        data.setdefault("account_holder_name", data["name"])
    if "id_number" in data:
        data.setdefault("insured_id_number", data["id_number"])
        data.setdefault("claimant_id_number", data["id_number"])
        data.setdefault("account_holder_id_number", data["id_number"])
    if "address" in data:
        data.setdefault("accident_location", data["address"])
        data.setdefault("mailing_address", data["address"])

    # Safe defaults for self-claims.
    if "claimant_relation" not in data:
        data["claimant_relation"] = "\u672c\u4eba"
    if "claimant_name" not in data and "insured_name" in data:
        data["claimant_name"] = data["insured_name"]
    if "claimant_id_number" not in data and "insured_id_number" in data:
        data["claimant_id_number"] = data["insured_id_number"]
    if "account_holder_name" not in data and "claimant_name" in data:
        data["account_holder_name"] = data["claimant_name"]
    if "account_holder_id_number" not in data and "claimant_id_number" in data:
        data["account_holder_id_number"] = data["claimant_id_number"]

    return data


def merge_data(args: argparse.Namespace, json_data: dict[str, Any]) -> dict[str, str]:
    base_data: dict[str, str] = {}

    for key in QUICK_KEYS + FULL_KEYS:
        value = json_data.get(key)
        if value not in (None, ""):
            base_data[key] = str(value)

    for key in QUICK_KEYS + FULL_KEYS:
        value = getattr(args, key, None)
        if value not in (None, ""):
            base_data[key] = str(value)

    return normalize_data(base_data, sample_preset=args.sample_preset)


def expand_field_values(data: dict[str, str]) -> dict[str, str]:
    field_values: dict[str, str] = {}

    for key, field_names in TEXT_FIELD_GROUPS.items():
        if key not in data:
            continue
        for field_name in field_names:
            field_values[field_name] = data[key]

    if "birth_date" in data:
        year, month, day = parse_date_parts(data["birth_date"], 3)
        values = [year, month, day, year, month, day]
        for idx, field_name in enumerate(DATE_FIELD_GROUPS["birth_date"]):
            field_values[field_name] = values[idx]

    if "accident_datetime" in data:
        parts = re.findall(r"\d+", data["accident_datetime"])
        if len(parts) < 3:
            raise ValueError(
                f"Expected at least 3 numeric parts in '{data['accident_datetime']}', got {len(parts)}."
            )
        if len(parts) >= 5:
            year, month, day, hour, minute = parts[:5]
        else:
            year, month, day = parts[:3]
            hour, minute = "", ""
        values = [year, month, day, hour, minute]
        for idx, field_name in enumerate(DATE_FIELD_GROUPS["accident_datetime"]):
            field_values[field_name] = values[idx]

    return field_values


def merge_bank_book_data(data: dict[str, str], bank_book_data: dict[str, str]) -> dict[str, str]:
    for key, value in bank_book_data.items():
        data.setdefault(key, value)
    return data


def load_sample_checkbox_states(sample_pdf: Path) -> dict[str, str]:
    if not sample_pdf.exists():
        raise FileNotFoundError(f"Sample PDF not found: {sample_pdf}")

    doc = fitz.open(sample_pdf)
    try:
        states: dict[str, str] = {}
        for page in doc:
            for widget in page.widgets() or []:
                if widget.field_type_string == "CheckBox" and widget.field_value not in ("", None):
                    states[widget.field_name] = str(widget.field_value)
        return states
    finally:
        doc.close()


def fill_pdf(
    input_pdf: Path,
    output_pdf: Path,
    field_values: dict[str, str],
    checkbox_states: dict[str, str] | None = None,
) -> None:
    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF not found: {input_pdf}")

    checkbox_states = checkbox_states or {}
    doc = fitz.open(input_pdf)
    try:
        for page in doc:
            for widget in page.widgets() or []:
                if widget.field_type_string == "CheckBox":
                    if widget.field_name in checkbox_states:
                        widget.field_value = widget.on_state()
                        widget.update()
                    continue

                value = field_values.get(widget.field_name)
                if value is None:
                    continue
                widget.field_value = value
                widget.update()

        doc.save(output_pdf)
    finally:
        doc.close()


def default_output_path(input_pdf: Path) -> Path:
    return input_pdf.with_name(f"{input_pdf.stem}_\u5df2\u586b\u5beb.pdf")


def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def normalize_ocr_numeric_text(value: str) -> str:
    return value.translate(OCR_NUMERIC_TRANSLATION)


def normalize_bank_account(value: str) -> str:
    if not value:
        return ""
    value = normalize_ocr_numeric_text(value)
    value = value.split("承前頁", 1)[0]
    value = value.split("承前页", 1)[0]
    value = re.sub(r"[^0-9\-]", "", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    digits_only = re.sub(r"\D", "", value)
    if len(digits_only) < 10:
        return ""
    return value or digits_only


def extract_bank_code(value: str) -> str:
    if not value:
        return ""
    normalized = normalize_ocr_numeric_text(value)
    match = re.search(r"(\d{3})", normalized)
    if match:
        if match.group(1) == "000" and not re.search(r"\d", value):
            return ""
        return match.group(1)
    return ""


def clean_bank_text(value: str) -> str:
    value = re.sub(r"^(?:銀行|分行|帳號|戶名|:|：)+", "", value)
    value = re.sub(r"\s+", "", value).strip(":：")
    if re.search(r"[\u4e00-\u9fff]", value):
        value = re.sub(r"[A-Za-z.&\s]+$", "", value)
    return value


def extract_chinese_name(value: str) -> str:
    if not value:
        return ""
    value = value.replace("先生/女士", "")
    value = value.replace("先生", "")
    value = value.replace("女士", "")
    matches = re.findall(r"[\u4e00-\u9fff]{2,5}", value)
    if matches:
        return max(matches, key=len)
    return ""


def normalize_id_number(value: str) -> str:
    value = normalize_ocr_numeric_text(value).upper().replace(" ", "")
    if len(value) >= 2:
        value = value[0] + value[1:].replace("O", "0")
    return value


def format_roc_date(parts: list[str], include_time: bool, meridiem: str = "") -> str:
    year = int(parts[0])
    if year > 1911:
        year -= 1911
    month = int(parts[1])
    day = int(parts[2])
    if include_time and len(parts) >= 5:
        hour = int(parts[3])
        minute = int(parts[4])
        if meridiem == "下午" and hour < 12:
            hour += 12
        if meridiem == "上午" and hour == 12:
            hour = 0
        return f"{year}/{month}/{day} {hour:02d}:{minute:02d}"
    return f"{year}/{month}/{day}"


def extract_labeled_segment(
    lines: list[str],
    labels: tuple[str, ...],
    *,
    max_following_lines: int = 1,
    stop_labels: tuple[str, ...] = (),
    joiner: str = " ",
) -> str:
    for index, line in enumerate(lines):
        for label in labels:
            if label not in line:
                continue
            match = re.search(rf"{re.escape(label)}\s*[:：]?\s*(.*)", line)
            if not match:
                continue
            values: list[str] = []
            same_line_value = match.group(1).strip()
            if same_line_value:
                values.append(same_line_value)
            next_index = index + 1
            while next_index < len(lines) and len(values) < max_following_lines:
                next_line = lines[next_index].strip()
                if not next_line:
                    break
                if any(stop_label in next_line for stop_label in stop_labels):
                    break
                values.append(next_line)
                next_index += 1
            if values:
                return joiner.join(values)
    return ""


def extract_date_from_text(value: str, allow_time: bool) -> str:
    if not value:
        return ""
    normalized = normalize_ocr_numeric_text(value)
    meridiem = ""
    if "上午" in normalized:
        meridiem = "上午"
    elif "下午" in normalized:
        meridiem = "下午"
    normalized = normalized.replace("民國", "")

    pattern = re.compile(
        r"(\d{2,4})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})(?:\s*日)?"
        r"(?:\s*(\d{1,2})\s*[:：時]\s*(\d{1,2})(?:\s*分)?)?"
    )
    match = pattern.search(normalized)
    if match:
        parts = [part for part in match.groups() if part is not None]
        if len(parts) >= 3:
            return format_roc_date(parts, include_time=allow_time and len(parts) >= 5, meridiem=meridiem)
    return ""


def extract_date_from_labels(
    lines: list[str],
    labels: tuple[str, ...],
    allow_time: bool,
    *,
    max_following_lines: int = 6,
) -> str:
    stop_labels = (
        DIAGNOSIS_NAME_LABELS
        + DIAGNOSIS_ID_LABELS
        + DIAGNOSIS_ADDRESS_LABELS
        + DIAGNOSIS_REASON_LABELS
    )
    for label in labels:
        candidate = extract_labeled_segment(
            lines,
            (label,),
            max_following_lines=max_following_lines,
            stop_labels=stop_labels,
        )
        extracted = extract_date_from_text(candidate, allow_time=allow_time)
        if extracted:
            return extracted

    joined = "\n".join(lines)
    for label in labels:
        match = re.search(rf"{re.escape(label)}[:：]?(.*)", joined)
        if not match:
            continue
        extracted = extract_date_from_text(match.group(1), allow_time=allow_time)
        if extracted:
            return extracted
    return ""


def clean_reason_text(value: str) -> str:
    if not value:
        return ""
    value = re.sub(
        r"^(?:診斷結果|診斷|病名|傷病名稱|主訴|傷勢|病)[:：\s]*",
        "",
        value,
    )
    stop_labels = (
        DIAGNOSIS_NAME_LABELS
        + DIAGNOSIS_ID_LABELS
        + DIAGNOSIS_BIRTH_LABELS
        + DIAGNOSIS_ADDRESS_LABELS
        + DIAGNOSIS_ACCIDENT_LABELS
    )
    for label in stop_labels:
        if label in value:
            value = value.split(label, 1)[0]
    for marker in DIAGNOSIS_IGNORE_LINES:
        if marker in value:
            value = value.split(marker, 1)[0]
    value = value.strip(":：；,，。 ")
    return value[:120]


def clean_address_text(value: str) -> str:
    if not value:
        return ""
    value = re.sub(
        r"^(?:地址|住址|戶籍地址|現住地址|通訊地址)[:：\s]*",
        "",
        value,
    )
    stop_labels = (
        DIAGNOSIS_NAME_LABELS
        + DIAGNOSIS_ID_LABELS
        + DIAGNOSIS_BIRTH_LABELS
        + DIAGNOSIS_ACCIDENT_LABELS
        + DIAGNOSIS_REASON_LABELS
    )
    for label in stop_labels:
        if label in value:
            value = value.split(label, 1)[0]
    for marker in DIAGNOSIS_IGNORE_LINES:
        if marker in value:
            value = value.split(marker, 1)[0]
    value = re.sub(r"[①-⑩]+$", "", value)
    value = value.strip(":：；,，。 ")
    return value[:120]


def extract_name(lines: list[str]) -> str:
    stop_labels = (
        DIAGNOSIS_ID_LABELS
        + DIAGNOSIS_BIRTH_LABELS
        + DIAGNOSIS_ADDRESS_LABELS
        + DIAGNOSIS_ACCIDENT_LABELS
        + DIAGNOSIS_REASON_LABELS
    )
    candidate = extract_labeled_segment(
        lines,
        DIAGNOSIS_NAME_LABELS,
        max_following_lines=2,
        stop_labels=stop_labels,
    )
    name = extract_chinese_name(candidate)
    if name:
        return name

    joined = "\n".join(lines)
    for label in DIAGNOSIS_NAME_LABELS:
        match = re.search(rf"{re.escape(label)}[:：]?([\u4e00-\u9fff]{{2,5}})", joined)
        if match:
            return match.group(1)
    return ""


def extract_id_number(lines: list[str]) -> str:
    stop_labels = (
        DIAGNOSIS_NAME_LABELS
        + DIAGNOSIS_BIRTH_LABELS
        + DIAGNOSIS_ADDRESS_LABELS
        + DIAGNOSIS_ACCIDENT_LABELS
        + DIAGNOSIS_REASON_LABELS
    )
    candidate = extract_labeled_segment(
        lines,
        DIAGNOSIS_ID_LABELS,
        max_following_lines=2,
        stop_labels=stop_labels,
    )
    match = re.search(r"([A-Za-z][0-9OolI]{9})", candidate)
    if match:
        return normalize_id_number(match.group(1))

    joined = "\n".join(lines)
    for label in DIAGNOSIS_ID_LABELS:
        match = re.search(rf"{re.escape(label)}[:：]?([A-Za-z][0-9OolI]{{9}})", joined)
        if match:
            return normalize_id_number(match.group(1))
    fallback = re.search(r"([A-Za-z][0-9OolI]{9})", joined)
    if fallback:
        return normalize_id_number(fallback.group(1))
    return ""


def extract_birth_date(lines: list[str]) -> str:
    birth_date = extract_date_from_labels(lines, DIAGNOSIS_BIRTH_LABELS, allow_time=False)
    if birth_date:
        return birth_date

    for index, line in enumerate(lines):
        if "日生" not in line and "出生" not in line and "生日" not in line:
            continue
        candidate = " ".join(lines[max(0, index - 1): index + 2])
        birth_date = extract_date_from_text(candidate, allow_time=False)
        if birth_date:
            return birth_date
    return ""


def extract_accident_datetime(lines: list[str]) -> str:
    accident_datetime = extract_date_from_labels(lines, DIAGNOSIS_ACCIDENT_LABELS, allow_time=True, max_following_lines=8)
    if accident_datetime:
        return accident_datetime

    for index, line in enumerate(lines):
        if not any(token in line for token in ("應診", "就診", "門診", "事故", "受傷", "日期")):
            continue
        candidate = " ".join(lines[index:index + 8])
        accident_datetime = extract_date_from_text(candidate, allow_time=True)
        if accident_datetime:
            return accident_datetime
    return ""


def extract_reason(lines: list[str]) -> str:
    stop_labels = (
        DIAGNOSIS_NAME_LABELS
        + DIAGNOSIS_ID_LABELS
        + DIAGNOSIS_BIRTH_LABELS
        + DIAGNOSIS_ADDRESS_LABELS
        + DIAGNOSIS_ACCIDENT_LABELS
    )
    candidate = clean_reason_text(
        extract_labeled_segment(
            lines,
            DIAGNOSIS_REASON_LABELS,
            max_following_lines=3,
            stop_labels=stop_labels,
        )
    )
    if candidate:
        return candidate

    for line in lines:
        if any(marker in line for marker in DIAGNOSIS_IGNORE_LINES):
            continue
        if len(re.findall(r"[\u4e00-\u9fff]", line)) < 4:
            continue
        if any(keyword in line for keyword in DIAGNOSIS_REASON_HINT_KEYWORDS):
            candidate = clean_reason_text(line)
            if candidate:
                return candidate
    return ""


def parse_bank_book_info(lines: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not lines:
        return parsed

    account_candidates: list[str] = []
    bank_code_candidates: list[str] = []
    holder_name = ""
    institution_pattern = "|".join(map(re.escape, INSTITUTION_KEYWORDS))
    branch_pattern = "|".join(map(re.escape, BRANCH_KEYWORDS))

    for index, line in enumerate(lines):
        next_line = lines[index + 1] if index + 1 < len(lines) else ""

        for label in BANK_ACCOUNT_LABELS:
            if label not in line:
                continue
            candidate = line.split(label, 1)[1]
            if not re.search(r"\d", candidate) and next_line:
                candidate = next_line
            account_value = normalize_bank_account(candidate)
            if account_value:
                account_candidates.append(account_value)

        for label in BANK_CODE_LABELS:
            if label not in line:
                continue
            candidate = line.split(label, 1)[1]
            if not re.search(r"\d", candidate) and next_line:
                candidate = next_line
            bank_code = extract_bank_code(candidate)
            if bank_code:
                bank_code_candidates.append(bank_code)

        for label in BANK_HOLDER_LABELS:
            if label not in line:
                continue
            candidate = line.split(label, 1)[1]
            if not candidate.strip() and next_line:
                candidate = next_line
            name = extract_chinese_name(candidate)
            if name and not holder_name:
                holder_name = name

        generic_account_matches = re.findall(r"[0-9OolI]{2,}(?:[-\u2010-\u2015\uff0d][0-9OolI]{1,}){1,}", line)
        generic_account_matches.extend(re.findall(r"[0-9OolI]{10,}", line))
        for candidate in generic_account_matches:
            account_value = normalize_bank_account(candidate)
            if account_value:
                account_candidates.append(account_value)

    account_candidates = unique_preserve_order(account_candidates)
    bank_code_candidates = unique_preserve_order(bank_code_candidates)

    if account_candidates:
        account_candidates.sort(
            key=lambda value: (
                -len(re.sub(r"\D", "", value)),
                0 if "-" in value else 1,
                value,
            )
        )
        parsed["bank_account"] = account_candidates[0]

    if bank_code_candidates:
        parsed["bank_code"] = bank_code_candidates[0]

    if holder_name:
        parsed["account_holder_name"] = holder_name

    bank_line = ""
    branch_line = ""
    for line in lines:
        if any(label in line for label in BANK_CODE_LABELS):
            continue
        if any(keyword in line for keyword in INSTITUTION_KEYWORDS):
            bank_line = line
            if any(keyword in line for keyword in BRANCH_KEYWORDS):
                branch_line = line
            break

    if not branch_line:
        for line in lines:
            if any(keyword in line for keyword in BRANCH_KEYWORDS):
                branch_line = line
                break

    if bank_line:
        bank_match = re.search(rf"([\u4e00-\u9fffA-Za-z.&\s]+?(?:{institution_pattern}))", bank_line)
        if bank_match:
            parsed["bank_name"] = clean_bank_text(bank_match.group(1))
            tail = bank_line[bank_match.end():].strip()
            branch_match = re.search(rf"([\u4e00-\u9fffA-Za-z0-9]+?(?:{branch_pattern}))", tail)
            if branch_match:
                parsed["bank_branch"] = clean_bank_text(branch_match.group(1))
        else:
            parsed["bank_name"] = clean_bank_text(bank_line)

    if branch_line and "bank_branch" not in parsed:
        parsed["bank_branch"] = clean_bank_text(branch_line)

    return {key: value for key, value in parsed.items() if value}


def parse_diagnosis_info(lines: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    name = extract_name(lines)
    if name:
        parsed["insured_name"] = name
        parsed["claimant_name"] = name
        parsed["account_holder_name"] = name

    id_number = extract_id_number(lines)
    if id_number:
        parsed["insured_id_number"] = id_number
        parsed["claimant_id_number"] = id_number
        parsed["account_holder_id_number"] = id_number

    birth_date = extract_birth_date(lines)
    if birth_date:
        parsed["birth_date"] = birth_date

    address_candidate = clean_address_text(
        extract_labeled_segment(
            lines,
            DIAGNOSIS_ADDRESS_LABELS,
            max_following_lines=2,
            stop_labels=(
                DIAGNOSIS_NAME_LABELS
                + DIAGNOSIS_ID_LABELS
                + DIAGNOSIS_BIRTH_LABELS
                + DIAGNOSIS_ACCIDENT_LABELS
                + DIAGNOSIS_REASON_LABELS
            ),
        )
    )
    if address_candidate:
        parsed["address"] = address_candidate
        parsed["accident_location"] = address_candidate
        parsed["mailing_address"] = address_candidate

    accident_datetime = extract_accident_datetime(lines)
    if accident_datetime:
        parsed["accident_datetime"] = accident_datetime

    reason_candidate = extract_reason(lines)
    if reason_candidate:
        parsed["accident_reason"] = reason_candidate

    return parsed


def main() -> int:
    args = parse_args()
    json_data = load_json_data(args.data_json)
    merged_data = merge_data(args, json_data)
    diagnosis_data: dict[str, str] = {}
    if args.diagnosis_document:
        diagnosis_lines = extract_document_lines(args.diagnosis_document)
        diagnosis_data = parse_diagnosis_info(diagnosis_lines)
        merged_data = merge_extracted_claim_data(merged_data, diagnosis_data)
    if args.bank_book_image:
        bank_book_lines = ocr_bank_book_lines(args.bank_book_image)
        bank_book_data = parse_bank_book_info(bank_book_lines)
        merged_data = merge_bank_book_data(merged_data, bank_book_data)
    field_values = expand_field_values(merged_data)
    checkbox_states = (
        load_sample_checkbox_states(args.sample_pdf)
        if args.copy_sample_checkboxes
        else None
    )

    output_pdf = args.output_pdf or default_output_path(args.input_pdf)
    fill_pdf(args.input_pdf, output_pdf, field_values, checkbox_states)

    print(
        json.dumps(
            {
                "output_pdf": str(output_pdf),
                "filled_fields": sorted(field_values.keys()),
                "copied_checkbox_count": 0 if not checkbox_states else len(checkbox_states),
                "diagnosis_document": None if not args.diagnosis_document else str(args.diagnosis_document),
                "diagnosis_ocr": diagnosis_data,
                "bank_book_image": None if not args.bank_book_image else str(args.bank_book_image),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
