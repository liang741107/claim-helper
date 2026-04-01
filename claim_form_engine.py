from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
import os
from pathlib import Path
from typing import Any

import fitz

from fill_claim_form import (
    expand_field_values,
    extract_document_lines,
    fill_pdf,
    merge_bank_book_data,
    merge_extracted_claim_data,
    normalize_data,
    ocr_bank_book_lines,
    parse_bank_book_info,
    parse_diagnosis_info,
)

BASE_DIR = Path(__file__).resolve().parent
CLAIM_FORMS_DIR = BASE_DIR / "claim_forms"
APP_OUTPUT_DIR = BASE_DIR / "claim_app_output"
APP_OUTPUT_DIR.mkdir(exist_ok=True)

OVERLAY_FONT_NAME = "claim_cjk"


def _cjk_font_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured = os.environ.get("CLAIM_CJK_FONT", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        (
            Path(r"C:\Windows\Fonts\kaiu.ttf"),
            Path(r"C:\Windows\Fonts\msjh.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/arphic/ukai.ttc"),
        )
    )
    return tuple(candidates)


@lru_cache(maxsize=1)
def _overlay_font_path() -> str:
    for candidate in _cjk_font_candidates():
        if candidate.exists():
            return str(candidate)
    checked = "\n".join(f"- {path}" for path in _cjk_font_candidates())
    raise FileNotFoundError(
        "No CJK font file was found for PDF overlay rendering.\n"
        "Set CLAIM_CJK_FONT or install a supported font.\n"
        f"Checked:\n{checked}"
    )


@dataclass(frozen=True)
class AnchorFieldSpec:
    key: str
    anchor: str
    page: int
    dx: float
    dy: float
    width: float
    height: float
    font_size: float = 10.0
    occurrence: int = 0
    multiline: bool = False
    fallback_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaimFormProfile:
    profile_id: str
    label: str
    company: str
    insurance_type: str
    template_path: Path
    detection_keywords: tuple[str, ...]
    mode: str
    sample_preset: bool = False
    required_widgets: tuple[str, ...] = ()
    anchor_fields: tuple[AnchorFieldSpec, ...] = ()
    notes: str = ""


def _profile_template(name: str) -> Path:
    return CLAIM_FORMS_DIR / name


LEGACY_PROFILES: dict[str, ClaimFormProfile] = {
    "fubon_pnc_personal": ClaimFormProfile(
        profile_id="fubon_pnc_personal",
        label="富邦產險 個人保險理賠申請書",
        company="富邦產險",
        insurance_type="產險",
        template_path=_profile_template("fubon_pnc_ri_claim.pdf"),
        detection_keywords=("個人保險理賠申請書", "簽收單編號", "富邦"),
        mode="widget",
        sample_preset=True,
        required_widgets=("fill_10", "fill_151", "comb_2"),
        notes="可直接寫入 PDF 欄位。",
    ),
    "fubon_life_personal": ClaimFormProfile(
        profile_id="fubon_life_personal",
        label="富邦人壽 個人保險理賠保險金申請書",
        company="富邦人壽",
        insurance_type="壽險",
        template_path=_profile_template("fubon_life_personal_claim.pdf"),
        detection_keywords=("富邦人壽", "個人保險理賠保險金申請書", "事故人", "給付方式"),
        mode="overlay",
        anchor_fields=(
            AnchorFieldSpec("insured_name", "事故人", 0, 42, -4, 140, 16),
            AnchorFieldSpec("insured_id_number", "身分證統一編號", 0, 74, -4, 130, 16),
            AnchorFieldSpec("accident_datetime", "意外時間", 0, 48, 0, 135, 16),
            AnchorFieldSpec(
                "accident_reason",
                "請說明保險事故發生地點、原因、經過情形及診斷(如為車禍事故，請填寫車號)",
                0,
                0,
                10,
                310,
                72,
                font_size=9.5,
                occurrence=0,
                multiline=True,
                fallback_keys=("accident_location",),
            ),
            AnchorFieldSpec("account_holder_name", "戶名", 0, 34, -4, 130, 16),
            AnchorFieldSpec("account_holder_id_number", "身分證統一編號", 0, 72, -4, 120, 16, occurrence=1),
            AnchorFieldSpec("bank_name", "銀行/郵局", 0, 45, -4, 120, 16),
            AnchorFieldSpec("bank_branch", "分行/支局", 0, 46, -4, 100, 16),
            AnchorFieldSpec("bank_account", "帳號", 0, 30, -4, 130, 16),
            AnchorFieldSpec("phone", "行動電話", 0, 43, -3, 120, 16),
            AnchorFieldSpec("mailing_address", "聯絡地址", 0, 47, -3, 450, 16, fallback_keys=("address",)),
        ),
        notes="以文字錨點方式疊字到官方 PDF。",
    ),
    "chubb_life_c04": ClaimFormProfile(
        profile_id="chubb_life_c04",
        label="安達人壽 理賠申請書",
        company="安達人壽",
        insurance_type="壽險",
        template_path=_profile_template("chubb_life_c04_claim.pdf"),
        detection_keywords=("CHUBB", "安達人壽", "理賠申請書", "被保險人"),
        mode="overlay",
        anchor_fields=(
            AnchorFieldSpec("insured_name", "被保險人：", 0, 58, 4, 95, 16),
            AnchorFieldSpec("insured_id_number", "身分證字號", 0, 56, 4, 120, 16),
            AnchorFieldSpec("bank_name", "銀行名稱：", 0, 62, 7, 100, 16),
            AnchorFieldSpec("bank_branch", "分行名稱：", 0, 65, 7, 110, 16),
            AnchorFieldSpec("account_holder_name", "帳戶姓名：", 0, 68, 6, 120, 16),
            AnchorFieldSpec("bank_account", "帳號：", 0, 38, 6, 180, 16),
            AnchorFieldSpec("accident_datetime", "事故日期：民國", 0, 100, -4, 140, 16),
            AnchorFieldSpec("accident_location", "意外事故地點(地址)：", 0, 155, -4, 240, 16, fallback_keys=("mailing_address", "address")),
            AnchorFieldSpec(
                "accident_reason",
                "意外事故原因：□車禍",
                0,
                335,
                -4,
                175,
                28,
                font_size=9.0,
                multiline=True,
            ),
        ),
        notes="以文字錨點方式疊字到官方 PDF。",
    ),
}


PROFILES = {
    "fubon_pnc_personal": ClaimFormProfile(
        profile_id="fubon_pnc_personal",
        label="\u5bcc\u90a6\u7522\u96aa \u500b\u4eba\u4fdd\u96aa\u7406\u8ce0\u7533\u8acb\u66f8",
        company="\u5bcc\u90a6\u7522\u96aa",
        insurance_type="\u7522\u96aa",
        template_path=_profile_template("fubon_pnc_ri_claim.pdf"),
        detection_keywords=(
            "\u500b\u4eba\u4fdd\u96aa\u7406\u8ce0\u7533\u8acb\u66f8",
            "\u7c3d\u6536\u55ae\u7de8\u865f",
            "\u5bcc\u90a6",
        ),
        mode="widget",
        sample_preset=True,
        required_widgets=("fill_10", "fill_151", "comb_2"),
        notes="\u4f7f\u7528 PDF \u539f\u751f\u8868\u55ae\u6b04\u4f4d\u5beb\u5165\u3002",
    ),
    "fubon_life_personal": ClaimFormProfile(
        profile_id="fubon_life_personal",
        label="\u5bcc\u90a6\u4eba\u58fd \u500b\u4eba\u4fdd\u96aa\u7406\u8ce0\u4fdd\u96aa\u91d1\u7533\u8acb\u66f8",
        company="\u5bcc\u90a6\u4eba\u58fd",
        insurance_type="\u58fd\u96aa",
        template_path=_profile_template("fubon_life_personal_claim.pdf"),
        detection_keywords=(
            "\u5bcc\u90a6\u4eba\u58fd",
            "\u500b\u4eba\u4fdd\u96aa\u7406\u8ce0\u4fdd\u96aa\u91d1\u7533\u8acb\u66f8",
            "\u4e8b\u6545\u4eba",
            "\u7d66\u4ed8\u65b9\u5f0f",
        ),
        mode="overlay",
        anchor_fields=(
            AnchorFieldSpec("insured_name", "\u4e8b\u6545\u4eba", 0, 42, -4, 140, 16),
            AnchorFieldSpec("insured_id_number", "\u8eab\u5206\u8b49\u7d71\u4e00\u7de8\u865f", 0, 74, -4, 130, 16),
            AnchorFieldSpec("accident_datetime", "\u610f\u5916\u6642\u9593", 0, 48, 0, 135, 16),
            AnchorFieldSpec(
                "accident_reason",
                "\u8acb\u8aaa\u660e\u4fdd\u96aa\u4e8b\u6545\u767c\u751f\u5730\u9ede\u3001\u539f\u56e0\u3001\u7d93\u904e\u60c5\u5f62\u53ca\u8a3a\u65b7(\u5982\u70ba\u8eca\u798d\u4e8b\u6545\uff0c\u8acb\u586b\u5beb\u8eca\u865f)",
                0,
                0,
                10,
                310,
                72,
                font_size=9.5,
                occurrence=0,
                multiline=True,
                fallback_keys=("accident_location",),
            ),
            AnchorFieldSpec("account_holder_name", "\u6236\u540d", 0, 34, -4, 130, 16),
            AnchorFieldSpec(
                "account_holder_id_number",
                "\u8eab\u5206\u8b49\u7d71\u4e00\u7de8\u865f",
                0,
                72,
                -4,
                120,
                16,
                occurrence=1,
            ),
            AnchorFieldSpec("bank_name", "\u9280\u884c/\u90f5\u5c40", 0, 45, -4, 120, 16),
            AnchorFieldSpec("bank_branch", "\u5206\u884c/\u652f\u5c40", 0, 46, -4, 100, 16),
            AnchorFieldSpec("bank_account", "\u5e33\u865f", 0, 30, -4, 130, 16),
            AnchorFieldSpec("phone", "\u884c\u52d5\u96fb\u8a71", 0, 43, -3, 120, 16),
            AnchorFieldSpec("mailing_address", "\u806f\u7d61\u5730\u5740", 0, 47, -3, 450, 16, fallback_keys=("address",)),
        ),
        notes="\u4f7f\u7528\u6587\u5b57\u9328\u9ede\u5b9a\u4f4d\u8986\u5beb PDF\u3002",
    ),
    "chubb_life_c04": ClaimFormProfile(
        profile_id="chubb_life_c04",
        label="\u5b89\u9054\u4eba\u58fd \u7406\u8ce0\u7533\u8acb\u66f8",
        company="\u5b89\u9054\u4eba\u58fd",
        insurance_type="\u58fd\u96aa",
        template_path=_profile_template("chubb_life_c04_claim.pdf"),
        detection_keywords=(
            "CHUBB",
            "\u5b89\u9054\u4eba\u58fd",
            "\u7d66\u4ed8\u65b9\u5f0f",
            "\u610f\u5916\u4e8b\u6545\u539f\u56e0",
        ),
        mode="overlay",
        anchor_fields=(
            AnchorFieldSpec("insured_name", "\u88ab\u4fdd\u96aa\u4eba\uff1a", 0, 58, 4, 95, 16),
            AnchorFieldSpec("insured_id_number", "\u8eab\u5206\u8b49\u5b57\u865f", 0, 56, 4, 120, 16),
            AnchorFieldSpec("bank_name", "\u9280\u884c\u540d\u7a31\uff1a", 0, 62, 7, 100, 16),
            AnchorFieldSpec("bank_branch", "\u5206\u884c\u540d\u7a31\uff1a", 0, 65, 7, 110, 16),
            AnchorFieldSpec("account_holder_name", "\u5e33\u6236\u59d3\u540d\uff1a", 0, 68, 6, 120, 16),
            AnchorFieldSpec("bank_account", "\u5e33\u865f\uff1a", 0, 38, 6, 180, 16),
            AnchorFieldSpec("accident_datetime", "\u4e8b\u6545\u65e5\u671f\uff1a\u6c11\u570b", 0, 100, -4, 140, 16),
            AnchorFieldSpec(
                "accident_location",
                "\u610f\u5916\u4e8b\u6545\u5730\u9ede(\u5730\u5740)\uff1a",
                0,
                155,
                -4,
                240,
                16,
                fallback_keys=("mailing_address", "address"),
            ),
            AnchorFieldSpec(
                "accident_reason",
                "\u610f\u5916\u4e8b\u6545\u539f\u56e0\uff1a\u25a1\u8eca\u798d",
                0,
                335,
                -4,
                175,
                28,
                font_size=9.0,
                multiline=True,
            ),
        ),
        notes="\u4f7f\u7528\u6587\u5b57\u9328\u9ede\u5b9a\u4f4d\u8986\u5beb PDF\u3002",
    ),
    "cathay_life_group": ClaimFormProfile(
        profile_id="cathay_life_group",
        label="\u570b\u6cf0\u4eba\u58fd \u7406\u8ce0\u7533\u8acb\u66f8",
        company="\u570b\u6cf0\u4eba\u58fd",
        insurance_type="\u58fd\u96aa",
        template_path=_profile_template("cathay_life_claim.pdf"),
        detection_keywords=(
            "\u570b\u6cf0\u4eba\u58fd\u4fdd\u96aa\u80a1\u4efd\u6709\u9650\u516c\u53f8",
            "\u7737\u5c6c\u91ab\u7642\u4fdd\u96aa\u91d1\u6307\u5b9a\u532f\u6b3e\u540c\u610f\u66f8",
            "\u570b\u58fd\u670d\u52d9\u4eba\u54e1",
        ),
        mode="overlay",
        anchor_fields=(
            AnchorFieldSpec("insured_name", "\u59d3\u540d", 0, -30, 17, 125, 16),
            AnchorFieldSpec("insured_id_number", "\u8eab\u5206\u8b49\u5b57\u865f", 0, -4, 17, 130, 16),
            AnchorFieldSpec("birth_date", "\u51fa\u751f\u65e5\u671f", 0, -2, 17, 78, 16),
            AnchorFieldSpec("mailing_address", "\u4f4f\u6240\u5730\u5740", 0, 55, 0, 420, 16, fallback_keys=("address",)),
            AnchorFieldSpec("phone", "\u884c\u52d5\u96fb\u8a71", 0, 55, 0, 125, 16),
            AnchorFieldSpec("accident_datetime", "\u4e8b\u6545\u65e5\u671f", 0, 50, 0, 120, 16),
            AnchorFieldSpec(
                "accident_reason",
                "\u4e8b\u6545\u8aaa\u660e",
                0,
                55,
                0,
                278,
                32,
                font_size=8.4,
                multiline=True,
            ),
            AnchorFieldSpec(
                "accident_location",
                "\u610f\u5916\u4e8b\u6545\u5730\u9ede",
                0,
                72,
                0,
                165,
                16,
                fallback_keys=("mailing_address", "address"),
            ),
            AnchorFieldSpec(
                "account_holder_name",
                "\u6236\u540d",
                0,
                35,
                9,
                250,
                16,
                fallback_keys=("insured_name", "claimant_name"),
            ),
            AnchorFieldSpec(
                "account_holder_id_number",
                "\u8eab\u5206\u8b49\u5b57\u865f",
                0,
                58,
                9,
                82,
                16,
                occurrence=4,
                fallback_keys=("insured_id_number", "claimant_id_number"),
            ),
            AnchorFieldSpec("bank_name", "\u91d1\u878d\u6a5f\u69cb", 0, 20, 10, 150, 16, font_size=8.2),
            AnchorFieldSpec("bank_code", "\u5206\u884c\u901a\u532f", 0, 28, 10, 110, 16, fallback_keys=("bank_branch",)),
            AnchorFieldSpec("bank_account", "\u5e33\u865f", 0, 25, 10, 145, 16, occurrence=1),
        ),
        notes="\u4f7f\u7528\u6587\u5b57\u9328\u9ede\u5b9a\u4f4d\u8986\u5beb PDF\u3002",
    ),
    "cathay_pnc_personal": ClaimFormProfile(
        profile_id="cathay_pnc_personal",
        label="\u570b\u6cf0\u7522\u96aa \u50b7\u5bb3\u96aa\u3001\u5065\u5eb7\u96aa\u66a8\u65c5\u7d9c\u96aa\u7406\u8ce0\u7533\u8acb\u66f8",
        company="\u570b\u6cf0\u7522\u96aa",
        insurance_type="\u7522\u96aa",
        template_path=_profile_template("cathay_pnc_claim.pdf"),
        detection_keywords=(
            "\u570b\u6cf0\u7522\u96aa\u50b7\u5bb3\u96aa\u3001\u5065\u5eb7\u96aa\u66a8\u65c5\u7d9c\u96aa\u7406\u8ce0\u7533\u8acb\u66f8",
            "\u4e8b\u6545\u8005\u57fa\u672c\u8cc7\u6599",
            "\u4fdd\u96aa\u91d1\u7d66\u4ed8\u65b9\u5f0f",
        ),
        mode="overlay",
        anchor_fields=(
            AnchorFieldSpec("insured_name", "(*)\u59d3", 0, 55, 0, 150, 16),
            AnchorFieldSpec("insured_id_number", "\u8eab\u5206(\u5c45\u7559)\u8b49\u5b57\u865f", 0, 92, -1, 210, 16),
            AnchorFieldSpec("birth_date", "\u51fa\u751f\u65e5\u671f", 0, 60, 0, 140, 16),
            AnchorFieldSpec("mailing_address", "\u5c45\u4f4f\u5730\u5740", 0, 105, 0, 220, 16, fallback_keys=("address",)),
            AnchorFieldSpec("phone", "\u884c\u52d5\u96fb\u8a71", 0, 55, 0, 150, 16),
            AnchorFieldSpec("phone", "\u806f\u7d61\u96fb\u8a71", 0, 105, 0, 150, 16),
            AnchorFieldSpec("accident_datetime", "\u4e8b\u6545\u65e5\u671f", 0, 60, 0, 165, 16),
            AnchorFieldSpec(
                "accident_location",
                "\u4e8b\u6545\u5730\u9ede",
                0,
                60,
                0,
                140,
                16,
                fallback_keys=("mailing_address", "address"),
            ),
            AnchorFieldSpec(
                "accident_reason",
                "\u76f8\u95dc\u7d93\u904e",
                0,
                55,
                0,
                245,
                84,
                font_size=8.4,
                multiline=True,
            ),
            AnchorFieldSpec(
                "account_holder_name",
                "\u6236",
                0,
                30,
                0,
                70,
                16,
                occurrence=1,
                fallback_keys=("insured_name", "claimant_name"),
            ),
            AnchorFieldSpec("bank_name", "\u91d1\u878d\u6a5f\u69cb", 0, 40, 0, 100, 16),
            AnchorFieldSpec("bank_branch", "\u5206\u884c\u540d", 0, 30, 0, 75, 16),
            AnchorFieldSpec("bank_account", "\u5e33", 0, 30, 0, 280, 16, occurrence=1),
        ),
        notes="\u4f7f\u7528\u6587\u5b57\u9328\u9ede\u5b9a\u4f4d\u8986\u5beb PDF\u3002",
    ),
    "hotai_pnc_health": ClaimFormProfile(
        profile_id="hotai_pnc_health",
        label="\u548c\u6cf0\u7522\u96aa \u50b7\u5bb3\u66a8\u5065\u5eb7\u96aa\u7406\u8ce0\u7533\u8acb\u66f8",
        company="\u548c\u6cf0\u7522\u96aa",
        insurance_type="\u7522\u96aa",
        template_path=_profile_template("hotai_pnc_health_claim.pdf"),
        detection_keywords=(
            "\u50b7\u5bb3\u66a8\u5065\u5eb7\u96aa\u7406\u8ce0\u7533\u8acb\u66f8",
            "BasicInformation",
            "AccidentDetails",
            "PaymentDetails",
        ),
        mode="overlay",
        anchor_fields=(
            AnchorFieldSpec("insured_name", "\u88ab\u4fdd\u96aa\u4eba", 0, 55, 0, 110, 16),
            AnchorFieldSpec("insured_id_number", "\u8eab\u5206\u8b49\u5b57\u865f", 0, 62, 0, 120, 16),
            AnchorFieldSpec("mailing_address", "\u806f\u7d61\u5730\u5740", 0, 55, 0, 315, 16, fallback_keys=("address",)),
            AnchorFieldSpec("phone", "\u884c\u52d5\u96fb\u8a71", 0, 55, 0, 110, 16),
            AnchorFieldSpec("accident_datetime", "\u4e8b\u6545\u65e5\u671f", 0, 55, 0, 110, 16),
            AnchorFieldSpec(
                "accident_location",
                "\u4e8b\u6545\u5730\u9ede",
                0,
                60,
                0,
                135,
                16,
                fallback_keys=("mailing_address", "address"),
            ),
            AnchorFieldSpec(
                "accident_reason",
                "\u8acb\u6558\u8ff0\u4e8b\u6545\u767c\u751f\u7d93\u904e",
                0,
                0,
                34,
                352,
                46,
                font_size=8.6,
                multiline=True,
            ),
            AnchorFieldSpec(
                "account_holder_name",
                "\u6236\u540d",
                0,
                -18,
                38,
                60,
                18,
                fallback_keys=("insured_name", "claimant_name"),
            ),
            AnchorFieldSpec("bank_name", "\u91d1\u878d\u6a5f\u69cb\u540d\u7a31", 0, -24, 38, 120, 18),
            AnchorFieldSpec("bank_branch", "\u5206\u884c\u540d\u7a31", 0, -8, 32, 135, 18),
            AnchorFieldSpec("bank_account", "Account No.", 0, -5, 18, 145, 18),
        ),
        notes="\u4f7f\u7528\u6587\u5b57\u9328\u9ede\u5b9a\u4f4d\u8986\u5beb PDF\u3002",
    ),
    "shinkong_life_general": ClaimFormProfile(
        profile_id="shinkong_life_general",
        label="新光人壽 保險金申請書",
        company="新光人壽",
        insurance_type="壽險",
        template_path=_profile_template("shinkong_life_claim.pdf"),
        detection_keywords=(
            "新光人壽保險股份有限公司",
            "保險金申請書",
            "理賠案號",
            "SKL-B#DB*B94!5",
        ),
        mode="overlay",
        anchor_fields=(
            AnchorFieldSpec("insured_name", "被保險人姓名", 0, 62, -2, 170, 16),
            AnchorFieldSpec("insured_id_number", "被保險人身分證字號", 0, 88, -2, 180, 16),
            AnchorFieldSpec("birth_date", "出生日期", 0, 42, -1, 160, 16),
            AnchorFieldSpec("accident_datetime", "事故日期", 0, 44, -1, 190, 16),
            AnchorFieldSpec("accident_datetime", "【發生時間】於", 0, 66, -2, 150, 16),
            AnchorFieldSpec(
                "accident_reason",
                "事故經過",
                0,
                -42,
                12,
                500,
                34,
                font_size=8.6,
                multiline=True,
            ),
            AnchorFieldSpec(
                "accident_location",
                "【發生地點】",
                0,
                60,
                -2,
                180,
                16,
                fallback_keys=("mailing_address", "address"),
            ),
            AnchorFieldSpec(
                "account_holder_name",
                "戶名：",
                0,
                38,
                -2,
                130,
                16,
                fallback_keys=("insured_name", "claimant_name"),
            ),
            AnchorFieldSpec("bank_name", "金融機構：", 0, 48, -2, 120, 16),
            AnchorFieldSpec("bank_branch", "分行/支局", 0, 42, -2, 95, 16),
            AnchorFieldSpec("bank_account", "帳號：", 0, 34, -2, 150, 16),
            AnchorFieldSpec(
                "claimant_id_number",
                "身分證統一編號：",
                0,
                75,
                -2,
                155,
                16,
                occurrence=1,
                fallback_keys=("insured_id_number",),
            ),
            AnchorFieldSpec("phone", "聯絡(行動)電話：", 0, 75, -2, 170, 16),
            AnchorFieldSpec("mailing_address", "聯絡地址：(郵遞區號", 0, 115, -2, 320, 16, fallback_keys=("address",)),
        ),
        notes="使用文字錨點定位覆寫官方 PDF。",
    ),
    "taiwan_life_claim": ClaimFormProfile(
        profile_id="taiwan_life_claim",
        label="台灣人壽 保險金申請書",
        company="台灣人壽",
        insurance_type="壽險",
        template_path=_profile_template("taiwan_life_claim.pdf"),
        detection_keywords=(
            "台灣人壽保險股份有限公司",
            "保險金申請書",
            "病歷資料調閱及事故確認授權書",
            "立書人(即被保險人)/受益人簽名",
        ),
        mode="overlay",
        anchor_fields=(
            AnchorFieldSpec("insured_name", "人姓名", 0, 42, -2, 135, 16),
            AnchorFieldSpec("insured_id_number", "統一編號", 0, 52, -2, 130, 16, occurrence=0),
            AnchorFieldSpec("birth_date", "出生日期", 0, 48, -2, 120, 16),
            AnchorFieldSpec("phone", "日行動電話", 0, 64, -2, 135, 16),
            AnchorFieldSpec("accident_datetime", "□新事故，事故發生日：＿＿年＿＿月＿＿日＿＿時＿＿分）", 0, 148, -2, 210, 16),
            AnchorFieldSpec(
                "accident_reason",
                "請詳述保險事故發生地點、原因、經過情形、事故時職業及工作內容：",
                0,
                0,
                12,
                455,
                42,
                font_size=8.6,
                multiline=True,
                fallback_keys=("accident_location",),
            ),
            AnchorFieldSpec(
                "account_holder_name",
                "戶名：",
                0,
                40,
                -2,
                105,
                16,
                fallback_keys=("insured_name", "claimant_name"),
            ),
            AnchorFieldSpec(
                "account_holder_id_number",
                "受款人身分證統一編號：",
                0,
                126,
                -2,
                125,
                16,
                fallback_keys=("insured_id_number", "claimant_id_number"),
            ),
            AnchorFieldSpec("bank_name", "金融機構名稱：", 0, 82, -2, 110, 16),
            AnchorFieldSpec("bank_branch", "金融機構分行：", 0, 82, -2, 110, 16),
            AnchorFieldSpec("bank_account", "□□□－□□□□－□□□□□□□□□□□□□□", 0, 0, -2, 435, 16),
            AnchorFieldSpec("insured_name", "姓名：", 2, 40, -2, 110, 16),
            AnchorFieldSpec("birth_date", "、生日：民國", 2, 76, -2, 115, 16),
            AnchorFieldSpec("insured_id_number", "日、身分證字號：", 2, 100, -2, 145, 16),
            AnchorFieldSpec("phone", "聯絡電話：", 2, 68, -2, 170, 16),
            AnchorFieldSpec("mailing_address", "聯絡地址：", 2, 68, -2, 420, 16, fallback_keys=("address",)),
        ),
        notes="使用文字錨點定位覆寫官方 PDF。",
    ),
}


def list_profiles() -> list[ClaimFormProfile]:
    return list(PROFILES.values())


def get_profile(profile_id: str) -> ClaimFormProfile:
    return PROFILES[profile_id]


def _extract_widget_names(pdf_path: Path) -> set[str]:
    widget_names: set[str] = set()
    if pdf_path.suffix.lower() != ".pdf":
        return widget_names
    doc = fitz.open(pdf_path)
    try:
        for page in doc:
            for widget in page.widgets() or []:
                if widget.field_name:
                    widget_names.add(widget.field_name)
    finally:
        doc.close()
    return widget_names


def detect_claim_form_profile(document_path: Path) -> tuple[ClaimFormProfile | None, dict[str, int], list[str]]:
    lines = extract_document_lines(document_path)
    joined = "\n".join(lines)
    widget_names = _extract_widget_names(document_path)
    scores: dict[str, int] = {}

    for profile in PROFILES.values():
        score = 0
        for keyword in profile.detection_keywords:
            if keyword in joined:
                score += 2
        if profile.required_widgets and all(widget in widget_names for widget in profile.required_widgets):
            score += 5
        scores[profile.profile_id] = score

    if not scores:
        return None, scores, lines

    best_profile_id = max(scores, key=scores.get)
    if scores[best_profile_id] <= 0:
        return None, scores, lines
    return PROFILES[best_profile_id], scores, lines


def build_claim_data(
    *,
    diagnosis_document: Path | None = None,
    bank_book_image: Path | None = None,
    manual_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    manual_overrides = manual_overrides or {}
    base_data = normalize_data(
        {key: str(value) for key, value in manual_overrides.items() if value not in (None, "")},
        sample_preset=False,
    )

    diagnosis_data: dict[str, str] = {}
    if diagnosis_document:
        diagnosis_data = parse_diagnosis_info(extract_document_lines(diagnosis_document))
        base_data = merge_extracted_claim_data(base_data, diagnosis_data)

    bank_data: dict[str, str] = {}
    if bank_book_image:
        bank_data = parse_bank_book_info(ocr_bank_book_lines(bank_book_image))
        base_data = merge_bank_book_data(base_data, bank_data)

    if "address" in base_data:
        base_data.setdefault("mailing_address", base_data["address"])
        base_data.setdefault("accident_location", base_data["address"])

    return base_data, diagnosis_data, bank_data


def _value_for_spec(data: dict[str, str], spec: AnchorFieldSpec) -> str:
    value = data.get(spec.key, "")
    if value:
        return value
    for key in spec.fallback_keys:
        fallback = data.get(key, "")
        if fallback:
            return fallback
    return ""


def _anchor_rect(page: fitz.Page, anchor: str, occurrence: int = 0) -> fitz.Rect:
    words = sorted(page.get_text("words"), key=lambda item: (item[1], item[0]))
    matches = [fitz.Rect(word[:4]) for word in words if anchor in word[4]]
    if len(matches) > occurrence:
        return matches[occurrence]

    search_matches = page.search_for(anchor)
    if len(search_matches) > occurrence:
        return search_matches[occurrence]

    raise ValueError(f"Anchor '{anchor}' not found on page {page.number + 1}.")


def _ensure_overlay_font(page: fitz.Page) -> None:
    page.insert_font(fontname=OVERLAY_FONT_NAME, fontfile=_overlay_font_path())


def fill_overlay_profile(profile: ClaimFormProfile, data: dict[str, str], output_pdf: Path) -> None:
    doc = fitz.open(profile.template_path)
    try:
        for spec in profile.anchor_fields:
            value = _value_for_spec(data, spec).strip()
            if not value:
                continue
            page = doc[spec.page]
            _ensure_overlay_font(page)
            anchor = _anchor_rect(page, spec.anchor, spec.occurrence)
            rect = fitz.Rect(
                anchor.x0 + spec.dx,
                anchor.y0 + spec.dy,
                anchor.x0 + spec.dx + spec.width,
                anchor.y0 + spec.dy + spec.height,
            )
            inserted = page.insert_textbox(
                rect,
                value,
                fontname=OVERLAY_FONT_NAME,
                fontsize=spec.font_size,
                color=(0, 0, 0),
                align=0,
            )
            if inserted < 0:
                page.insert_textbox(
                    rect,
                    value,
                    fontname=OVERLAY_FONT_NAME,
                    fontsize=max(7.5, spec.font_size - 1.0),
                    color=(0, 0, 0),
                    align=0,
                )
        doc.save(output_pdf)
    finally:
        doc.close()


def fill_widget_profile(profile: ClaimFormProfile, data: dict[str, str], output_pdf: Path) -> None:
    normalized = normalize_data(data, sample_preset=profile.sample_preset)
    field_values = expand_field_values(normalized)
    fill_pdf(profile.template_path, output_pdf, field_values, checkbox_states=None)


def fill_claim_form(profile: ClaimFormProfile, data: dict[str, str], output_pdf: Path) -> None:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    if profile.mode == "widget":
        fill_widget_profile(profile, data, output_pdf)
        return
    if profile.mode == "overlay":
        fill_overlay_profile(profile, data, output_pdf)
        return
    raise ValueError(f"Unsupported profile mode: {profile.mode}")


def next_output_path(profile: ClaimFormProfile) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return APP_OUTPUT_DIR / f"{profile.profile_id}_{timestamp}.pdf"
