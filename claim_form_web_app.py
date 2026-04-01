from __future__ import annotations

import base64
import io
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
import webbrowser
from functools import lru_cache
from pathlib import Path

import fitz
import qrcode
from PIL import Image, ImageDraw
from flask import Flask, render_template_string, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from claim_form_engine import (
    APP_OUTPUT_DIR,
    build_claim_data,
    detect_claim_form_profile,
    fill_claim_form,
    get_profile,
    list_profiles,
    next_output_path,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "claim_app_uploads"
PREVIEW_DIR = BASE_DIR / "claim_app_preview"
PREVIEW_STATE_DIR = BASE_DIR / "claim_app_preview_state"

for directory in (UPLOAD_DIR, PREVIEW_DIR, PREVIEW_STATE_DIR):
    directory.mkdir(exist_ok=True)

app = Flask(__name__)


def _configured_server_port() -> int:
    configured = os.environ.get("CLAIM_FORM_PORT", os.environ.get("PORT", "")).strip()
    if not configured:
        return 5050
    try:
        return int(configured)
    except ValueError:
        return 5050


app.config["SERVER_PORT"] = _configured_server_port()
app.config["PUBLIC_BASE_URL"] = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
app.config["APP_BUILD"] = os.environ.get("APP_BUILD", "dev").strip()


def _save_upload(file_storage, prefix: str) -> Path | None:
    if not file_storage or not file_storage.filename:
        return None
    suffix = Path(file_storage.filename).suffix or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, dir=UPLOAD_DIR, prefix=prefix, suffix=suffix) as handle:
        temp_path = Path(handle.name)
    file_storage.save(temp_path)
    return temp_path


def _save_first_upload(files, prefix: str, *field_names: str) -> Path | None:
    for field_name in field_names:
        saved = _save_upload(files.get(field_name), prefix)
        if saved:
            return saved
    return None


def _manual_overrides(form) -> dict[str, str]:
    overrides = {
        "name": form.get("name", "").strip(),
        "id_number": form.get("id_number", "").strip(),
        "address": form.get("address", "").strip(),
        "phone": form.get("phone", "").strip(),
        "accident_reason": form.get("accident_reason", "").strip(),
        "accident_datetime": form.get("accident_datetime", "").strip(),
    }
    return {key: value for key, value in overrides.items() if value}


def _default_form_values(form) -> dict[str, str]:
    return {
        "profile_id": form.get("profile_id", "auto"),
        "name": form.get("name", ""),
        "id_number": form.get("id_number", ""),
        "address": form.get("address", ""),
        "phone": form.get("phone", ""),
        "accident_datetime": form.get("accident_datetime", ""),
        "accident_reason": form.get("accident_reason", ""),
    }


def _preview_pdf_path(preview_id: str) -> Path:
    return PREVIEW_DIR / f"{preview_id}.pdf"


def _preview_state_path(preview_id: str) -> Path:
    return PREVIEW_STATE_DIR / f"{preview_id}.json"


def _save_preview_state(preview_id: str, payload: dict) -> None:
    _preview_state_path(preview_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_preview_state(preview_id: str) -> dict | None:
    state_path = _preview_state_path(preview_id)
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def _render_preview_images(preview_id: str, pdf_path: Path) -> list[str]:
    for existing in PREVIEW_DIR.glob(f"{preview_id}_p*.png"):
        existing.unlink(missing_ok=True)

    image_names: list[str] = []
    doc = fitz.open(pdf_path)
    try:
        for index, page in enumerate(doc, start=1):
            image_name = f"{preview_id}_p{index:02d}.png"
            image_path = PREVIEW_DIR / image_name
            page.get_pixmap(matrix=fitz.Matrix(1.45, 1.45), alpha=False).save(image_path)
            image_names.append(image_name)
    finally:
        doc.close()
    return image_names


def _build_result(
    *,
    profile_id: str,
    preview_id: str,
    preview_pdf_name: str,
    preview_images: list[str],
    summary: dict,
    exported_name: str | None = None,
) -> dict:
    profile = get_profile(profile_id)
    return {
        "profile": profile,
        "preview_id": preview_id,
        "preview_pdf_name": preview_pdf_name,
        "preview_images": preview_images,
        "summary": json.dumps(summary, ensure_ascii=False, indent=2),
        "exported_name": exported_name,
    }


def _effective_public_base_url() -> str:
    configured = str(app.config.get("PUBLIC_BASE_URL", "")).strip().rstrip("/")
    if configured:
        return configured
    try:
        return request.host_url.rstrip("/")
    except RuntimeError:
        return ""


def _lan_ip_candidates() -> list[str]:
    candidates: list[str] = []
    try:
        host_name = socket.gethostname()
        for _, _, _, _, sockaddr in socket.getaddrinfo(host_name, None, socket.AF_INET):
            ip = sockaddr[0]
            if ip.startswith("127.") or ip.startswith("169.254.") or ip == "0.0.0.0":
                continue
            if ip not in candidates:
                candidates.append(ip)
    except Exception:
        pass
    return candidates


def _windows_ipv4_candidates() -> list[tuple[str, str]]:
    command = (
        "Get-NetIPConfiguration | "
        "Where-Object { $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up' } | "
        "Select-Object "
        "@{Name='IPAddress';Expression={$_.IPv4Address.IPAddress}},"
        "InterfaceAlias | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            check=False,
        )
    except Exception:
        return []

    payload_bytes = completed.stdout or b""
    if not payload_bytes and completed.returncode != 0:
        return []

    payload = ""
    for encoding in ("utf-8", "cp950", "big5", "mbcs"):
        try:
            payload = payload_bytes.decode(encoding).strip()
            break
        except (LookupError, UnicodeDecodeError):
            continue

    if not payload:
        payload = payload_bytes.decode("utf-8", errors="replace").strip()

    if not payload:
        return []

    try:
        records = json.loads(payload)
    except json.JSONDecodeError:
        return []

    if isinstance(records, dict):
        records = [records]

    candidates: list[tuple[str, str]] = []
    for item in records:
        ip = str(item.get("IPAddress", "")).strip()
        alias = str(item.get("InterfaceAlias", "")).strip()
        if ip:
            candidates.append((ip, alias))
    return candidates


def _score_ip_candidate(ip: str, alias: str = "") -> int:
    score = 0
    alias_lower = alias.lower()
    excluded_terms = (
        "wsl",
        "hyper-v",
        "vethernet",
        "virtual",
        "vpn",
        "expressvpn",
        "docker",
        "vmware",
        "bluetooth",
        "loopback",
    )
    preferred_terms = ("wi-fi", "wifi", "wireless", "wlan", "ethernet", "乙太網路")

    try:
        parsed = ipaddress.ip_address(ip)
        if parsed.is_private:
            score += 100
        if str(parsed).startswith("100.64."):
            score -= 60
    except ValueError:
        return -999

    is_excluded_alias = any(term in alias_lower for term in excluded_terms)
    if is_excluded_alias:
        score -= 120
    if not is_excluded_alias and any(term in alias_lower for term in preferred_terms):
        score += 40
    if ip.startswith("192.168."):
        score += 30
    if ip.startswith("10."):
        score += 20
    return score


def _best_lan_urls(port: int = 5050) -> list[str]:
    ranked: dict[str, int] = {}

    for ip, alias in _windows_ipv4_candidates():
        ranked[ip] = max(ranked.get(ip, -9999), _score_ip_candidate(ip, alias))

    for ip in _lan_ip_candidates():
        if ip not in ranked:
            ranked[ip] = _score_ip_candidate(ip)

    ordered_ips = [ip for ip, score in sorted(ranked.items(), key=lambda item: item[1], reverse=True) if score > 0]
    return [f"http://{ip}:{port}" for ip in ordered_ips]


def _find_available_port(preferred_port: int = 5050, *, max_tries: int = 20) -> int:
    for port in range(preferred_port, preferred_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                continue
        return port
    raise OSError(f"找不到可用的連接埠。已嘗試 {preferred_port} 到 {preferred_port + max_tries - 1}。")


def _qr_data_uri(text: str | None) -> str | None:
    if not text:
        return None
    qr = qrcode.QRCode(border=2, box_size=6)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


@lru_cache(maxsize=8)
def _app_icon_png(size: int) -> bytes:
    image = Image.new("RGBA", (size, size), "#f3f0e8")
    draw = ImageDraw.Draw(image, "RGBA")

    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=int(size * 0.22),
        fill=(11, 107, 111, 255),
    )
    draw.ellipse(
        (int(size * 0.56), int(size * -0.04), int(size * 1.02), int(size * 0.40)),
        fill=(243, 198, 111, 80),
    )
    draw.rounded_rectangle(
        (int(size * 0.18), int(size * 0.14), int(size * 0.82), int(size * 0.86)),
        radius=int(size * 0.12),
        fill=(252, 253, 250, 255),
    )
    draw.polygon(
        (
            (int(size * 0.66), int(size * 0.14)),
            (int(size * 0.82), int(size * 0.14)),
            (int(size * 0.82), int(size * 0.30)),
        ),
        fill=(226, 238, 235, 255),
    )
    draw.rounded_rectangle(
        (int(size * 0.28), int(size * 0.28), int(size * 0.72), int(size * 0.40)),
        radius=int(size * 0.05),
        fill=(11, 107, 111, 255),
    )
    for index in range(3):
        top = int(size * (0.49 + index * 0.11))
        draw.rounded_rectangle(
            (int(size * 0.28), top, int(size * 0.72), top + int(size * 0.05)),
            radius=int(size * 0.02),
            fill=(197, 210, 206, 255),
        )
    draw.rounded_rectangle(
        (int(size * 0.34), int(size * 0.31), int(size * 0.45), int(size * 0.37)),
        radius=int(size * 0.02),
        fill=(243, 198, 111, 255),
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@lru_cache(maxsize=1)
def _app_icon_ico() -> bytes:
    image = Image.open(io.BytesIO(_app_icon_png(192)))
    buffer = io.BytesIO()
    image.save(buffer, format="ICO", sizes=[(64, 64), (32, 32)])
    return buffer.getvalue()


def _delete_preview_artifacts(preview_id: str) -> None:
    if not preview_id:
        return

    state = _load_preview_state(preview_id)
    if state:
        preview_pdf_name = state.get("preview_pdf_name", "")
        if preview_pdf_name:
            (PREVIEW_DIR / preview_pdf_name).unlink(missing_ok=True)
        for image_name in state.get("preview_images", []):
            (PREVIEW_DIR / image_name).unlink(missing_ok=True)

    for existing in PREVIEW_DIR.glob(f"{preview_id}_p*.png"):
        existing.unlink(missing_ok=True)
    _preview_pdf_path(preview_id).unlink(missing_ok=True)
    _preview_state_path(preview_id).unlink(missing_ok=True)


TEMPLATE = """
<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0b6b6f">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <meta name="apple-mobile-web-app-title" content="理賠助手">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="application-name" content="理賠助手">
  <meta name="format-detection" content="telephone=no">
  <link rel="manifest" href="/manifest.webmanifest?v={{ app_build }}">
  <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v={{ app_build }}">
  <link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png?v={{ app_build }}">
  <link rel="icon" type="image/png" sizes="512x512" href="/icon-512.png?v={{ app_build }}">
  <link rel="shortcut icon" href="/favicon.ico?v={{ app_build }}">
  <title>理賠申請書助手</title>
  <style>
    :root {
      --bg: #f3f0e8;
      --panel: #fffdf8;
      --ink: #1e2a2f;
      --muted: #61727b;
      --accent: #0b6b6f;
      --accent-soft: #d8ecec;
      --accent-deep: #132328;
      --line: #d7d0c4;
      --warn: #a63f24;
      --ok: #1b6a45;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Microsoft JhengHei", "PingFang TC", sans-serif;
      background:
        radial-gradient(circle at 8% 12%, rgba(11, 107, 111, 0.14) 0, transparent 28%),
        radial-gradient(circle at 92% 6%, rgba(230, 190, 112, 0.14) 0, transparent 24%),
        linear-gradient(180deg, #f8f4ec 0%, var(--bg) 100%);
      color: var(--ink);
    }
    .shell {
      max-width: 1180px;
      margin: 0 auto;
      padding: 22px 16px 40px;
    }
    .mobile-shell {
      max-width: 760px;
      padding-top: 14px;
    }
    .hero {
      display: grid;
      gap: 12px;
      margin-bottom: 18px;
    }
    .hero-card {
      background: linear-gradient(135deg, rgba(11, 107, 111, 0.96), rgba(15, 62, 73, 0.96));
      color: #f7fbfb;
      border-radius: 26px;
      padding: 22px 22px 20px;
      box-shadow: 0 16px 40px rgba(22, 32, 35, 0.08);
      position: relative;
      overflow: hidden;
    }
    .hero-card::after {
      content: "";
      position: absolute;
      inset: auto -40px -52px auto;
      width: 180px;
      height: 180px;
      border-radius: 50%;
      background: rgba(243, 198, 111, 0.18);
    }
    .mobile-shell {
      background: transparent;
    }
    .mobile-shell .hero-card {
      background: linear-gradient(180deg, #fffdf9 0%, #f2f8f6 100%);
      color: var(--ink);
      border: 1px solid #d8e4df;
    }
    .mobile-shell .hero-card::after {
      display: none;
    }
    .eyebrow {
      color: rgba(239, 250, 248, 0.82);
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      font-size: 12px;
    }
    .mobile-shell .eyebrow {
      color: var(--accent);
    }
    h1 {
      margin: 0;
      font-size: clamp(30px, 4vw, 44px);
      line-height: 1.05;
    }
    .sub {
      margin: 0;
      color: rgba(244, 252, 251, 0.88);
      line-height: 1.65;
      max-width: 840px;
    }
    .mobile-shell .sub {
      color: var(--muted);
    }
    .mode-switch {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 4px;
    }
    .mode-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid rgba(255, 255, 255, 0.28);
      background: rgba(255, 255, 255, 0.08);
      color: rgba(247, 251, 251, 0.88);
      text-decoration: none;
      font-weight: 700;
      font-size: 14px;
    }
    .mode-link.active {
      background: #fffaf3;
      border-color: transparent;
      color: var(--accent-deep);
    }
    .mobile-shell .mode-link {
      background: #eef4f2;
      color: #284147;
      border-color: #d5e2de;
    }
    .mobile-shell .mode-link.active {
      background: linear-gradient(135deg, #0b6b6f, #1b8d7a);
      color: #fff;
    }
    .stepper {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 14px;
    }
    .step {
      display: grid;
      grid-template-columns: 42px 1fr;
      gap: 10px;
      align-items: center;
      border-radius: 18px;
      padding: 12px 14px;
      background: rgba(255, 255, 255, 0.1);
      border: 1px solid rgba(255, 255, 255, 0.08);
    }
    .mobile-shell .step {
      background: #f7fbfa;
      border-color: #dce8e4;
    }
    .step.active {
      background: rgba(255, 255, 255, 0.18);
      border-color: rgba(255, 255, 255, 0.22);
    }
    .mobile-shell .step.active {
      background: #e8f4f1;
      border-color: #cfe1db;
    }
    .step.done .step-badge {
      background: #f2c97d;
      color: var(--accent-deep);
    }
    .step-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 42px;
      height: 42px;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.14);
      color: #fff;
      font-size: 16px;
      font-weight: 800;
    }
    .mobile-shell .step-badge {
      background: #0b6b6f;
      color: #fff;
    }
    .step-title {
      font-size: 14px;
      font-weight: 700;
      color: #fff;
    }
    .mobile-shell .step-title {
      color: var(--accent-deep);
    }
    .step-copy {
      font-size: 12px;
      line-height: 1.45;
      color: rgba(244, 252, 251, 0.8);
    }
    .mobile-shell .step-copy {
      color: var(--muted);
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(320px, 0.95fr);
      gap: 18px;
      align-items: start;
    }
    .mobile-shell .grid,
    .mobile-shell .fields {
      grid-template-columns: 1fr;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 18px;
      box-shadow: 0 10px 28px rgba(25, 37, 42, 0.05);
    }
    .result-card {
      position: sticky;
      top: 14px;
    }
    .mobile-shell .result-card {
      order: -1;
      position: static;
    }
    .card h2 {
      margin: 0 0 10px;
      font-size: 20px;
    }
    .card-head {
      display: grid;
      gap: 8px;
      margin-bottom: 14px;
    }
    .card-kicker {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      padding: 8px 12px;
      border-radius: 999px;
      background: #edf7f5;
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
    }
    .card-kicker.done {
      background: #e8f6ee;
      color: var(--ok);
    }
    .build-tag {
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
    }
    .help {
      margin: 0 0 14px;
      color: var(--muted);
      line-height: 1.55;
      font-size: 14px;
    }
    form {
      display: grid;
      gap: 14px;
    }
    .section-title {
      margin: 6px 0 0;
      font-size: 13px;
      font-weight: 700;
      color: var(--accent);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .fields {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 14px;
      color: var(--ink);
    }
    .mobile-shell label {
      font-size: 15px;
    }
    input, select, textarea, button {
      font: inherit;
    }
    input[type="text"], select, textarea, input[type="file"] {
      width: 100%;
      border: 1px solid #c8c0b3;
      border-radius: 12px;
      background: #fff;
      padding: 11px 12px;
      color: var(--ink);
    }
    .capture-input {
      min-height: 74px;
      padding: 12px;
      border-style: dashed;
      border-width: 1.5px;
      border-color: #b4cac5;
      background: linear-gradient(180deg, #ffffff 0%, #eef7f4 100%);
    }
    .capture-input::file-selector-button,
    .capture-input::-webkit-file-upload-button {
      border: 0;
      border-radius: 12px;
      padding: 12px 16px;
      margin-right: 12px;
      font-weight: 800;
      background: linear-gradient(135deg, #0b6b6f, #138777);
      color: #fff;
      cursor: pointer;
    }
    .mobile-shell input[type="text"],
    .mobile-shell select,
    .mobile-shell textarea,
    .mobile-shell input[type="file"] {
      padding: 14px 14px;
    }
    .mobile-shell .capture-input {
      min-height: 96px;
      border-radius: 20px;
      font-size: 16px;
    }
    .mobile-shell .capture-input::file-selector-button,
    .mobile-shell .capture-input::-webkit-file-upload-button {
      min-height: 50px;
      min-width: 118px;
      font-size: 16px;
      border-radius: 14px;
    }
    .upload-picker {
      display: grid;
      gap: 10px;
      margin-top: 8px;
      padding: 14px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid #d9e3df;
    }
    .upload-launcher {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      min-height: 52px;
      border: 0;
      border-radius: 16px;
      padding: 14px 16px;
      background: linear-gradient(135deg, #0b6b6f, #138777);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }
    .picker-summary {
      font-size: 13px;
      line-height: 1.5;
      color: var(--muted);
    }
    .picker-panel {
      display: none;
      gap: 10px;
      padding-top: 2px;
    }
    .picker-panel:not(.hidden) {
      display: grid;
    }
    .picker-choice {
      display: grid;
      gap: 4px;
      width: 100%;
      text-align: left;
      border: 1px solid #d5e2de;
      border-radius: 16px;
      padding: 12px 14px;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
    }
    .picker-choice strong {
      font-size: 14px;
      color: var(--accent-deep);
    }
    .picker-choice span {
      font-size: 13px;
      line-height: 1.45;
      color: var(--muted);
    }
    .picker-choice.secondary {
      background: #f6fbf9;
    }
    .picker-input {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .upload-hint {
      font-size: 13px;
      line-height: 1.55;
      color: var(--muted);
    }
    .hidden {
      display: none !important;
    }
    textarea {
      min-height: 96px;
      resize: vertical;
    }
    .full { grid-column: 1 / -1; }
    .note {
      padding: 12px 14px;
      border-radius: 14px;
      background: var(--accent-soft);
      color: #13474a;
      line-height: 1.55;
      font-size: 14px;
    }
    .note.ok {
      background: #e5f6eb;
      color: var(--ok);
    }
    .note.warn {
      background: #fff0eb;
      color: var(--warn);
    }
    .note a {
      color: inherit;
      word-break: break-all;
    }
    .errors {
      display: grid;
      gap: 8px;
      margin-bottom: 12px;
    }
    .error {
      padding: 10px 12px;
      border-radius: 12px;
      background: #fff0eb;
      color: var(--warn);
      border: 1px solid #f1c3b6;
      font-size: 14px;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .mobile-shell .actions,
    .mobile-shell .actions form {
      width: 100%;
    }
    .submit,
    .ghost,
    .download {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 0;
      border-radius: 999px;
      padding: 13px 18px;
      text-decoration: none;
      cursor: pointer;
      font-weight: 700;
    }
    .mobile-shell .submit,
    .mobile-shell .ghost,
    .mobile-shell .download {
      width: 100%;
      min-height: 52px;
      font-size: 16px;
    }
    .submit {
      background: linear-gradient(135deg, #0b6b6f, #1b8d7a);
      color: #fff;
    }
    .ghost {
      background: #eef4f2;
      color: #284147;
      border: 1px solid #d5e2de;
    }
    .download {
      background: var(--accent-deep);
      color: #fff;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .chip {
      border-radius: 999px;
      padding: 7px 11px;
      background: #eef4f2;
      border: 1px solid #d5e2de;
      font-size: 13px;
      color: #284147;
    }
    .connection-list {
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }
    .qr-wrap {
      display: grid;
      gap: 12px;
      justify-items: start;
      margin-top: 12px;
    }
    .qr-wrap img {
      width: 180px;
      max-width: 100%;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: #fff;
      padding: 8px;
    }
    .mobile-shell .qr-wrap img {
      width: 140px;
    }
    .result-grid {
      display: grid;
      gap: 12px;
    }
    .status-panel {
      display: grid;
      gap: 12px;
      padding: 16px;
      border-radius: 20px;
      background: linear-gradient(180deg, #f7fbfa 0%, #edf5f3 100%);
      border: 1px solid #d4e3df;
    }
    .status-panel.done {
      background: linear-gradient(180deg, #eef8f2 0%, #e4f3eb 100%);
      border-color: #c7dfd1;
    }
    .status-title {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 800;
      font-size: 20px;
      color: var(--accent-deep);
    }
    .status-dot {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 6px rgba(11, 107, 111, 0.12);
    }
    .status-panel.done .status-dot {
      background: var(--ok);
      box-shadow: 0 0 0 6px rgba(31, 112, 72, 0.12);
    }
    .status-copy {
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
    }
    .preview-stack {
      display: grid;
      gap: 14px;
    }
    .preview-page {
      display: grid;
      gap: 8px;
    }
    .preview-page strong {
      font-size: 13px;
      color: var(--muted);
    }
    .preview-page img {
      width: 100%;
      display: block;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: #fff;
    }
    pre {
      margin: 0;
      padding: 14px;
      border-radius: 14px;
      background: #182126;
      color: #dce8e3;
      overflow: auto;
      font-size: 12px;
      line-height: 1.45;
    }
    details {
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.56);
      border: 1px solid #e2d8ca;
      padding: 0 14px 12px;
    }
    summary {
      cursor: pointer;
      list-style: none;
      font-weight: 800;
      padding: 14px 0;
      color: var(--accent-deep);
    }
    summary::-webkit-details-marker { display: none; }
    @media (max-width: 900px) {
      .grid, .fields, .stepper {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main class="shell {{ 'mobile-shell' if mobile_mode else '' }}">
    <section class="hero">
      <div class="hero-card">
        <div class="eyebrow">{{ 'Mobile Claim Flow' if mobile_mode else 'Claims Autofill App' }}</div>
        <h1>{{ '手機理賠填寫 App' if mobile_mode else '理賠申請書自動填寫工作台' }}</h1>
        <p class="sub">
          {% if mobile_mode %}
          把理賠申請書、診斷證明書、存摺封面拍清楚，系統會先做 OCR 與版型比對，讓你先確認預覽，再匯出正式 PDF。
          {% else %}
          上傳理賠申請書、診斷證明書與存摺照片後，系統會先產生可檢查的預覽，再匯出正式 PDF。簽名欄會保持空白。
          {% endif %}
        </p>
        <div class="mode-switch">
          <a class="mode-link {{ '' if mobile_mode else 'active' }}" href="/">完整模式</a>
          <a class="mode-link {{ 'active' if mobile_mode else '' }}" href="/mobile">手機快速模式</a>
        </div>
        <div class="build-tag">Build {{ app_build }}</div>
        <div class="stepper">
          <div class="step {{ 'active' if current_stage == 'start' else 'done' }}">
            <div class="step-badge">1</div>
            <div>
              <div class="step-title">拍照上傳</div>
              <div class="step-copy">理賠書、診斷書、存摺</div>
            </div>
          </div>
          <div class="step {{ 'active' if current_stage == 'preview' else 'done' if current_stage == 'done' else '' }}">
            <div class="step-badge">2</div>
            <div>
              <div class="step-title">確認預覽</div>
              <div class="step-copy">先檢查內容與位置</div>
            </div>
          </div>
          <div class="step {{ 'active' if current_stage == 'done' else '' }}">
            <div class="step-badge">3</div>
            <div>
              <div class="step-title">匯出下載</div>
              <div class="step-copy">下載正式 PDF</div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="grid">
      <div class="card">
        {% if errors %}
        <div class="errors">
          {% for error in errors %}
          <div class="error">{{ error }}</div>
          {% endfor %}
        </div>
        {% endif %}

        <div class="card-head">
          <div class="card-kicker">{{ '手機拍照入口' if mobile_mode else '建立預覽' }}</div>
          <h2>{{ '大按鈕拍照上傳' if mobile_mode else '上傳文件與補資料' }}</h2>
          <p class="help">
            {% if mobile_mode %}
            建議照順序拍三份文件。拍得不清楚時，可以直接按一鍵清空重拍；若 OCR 少抓到資料，再用下方欄位補上。
            {% else %}
            你可以讓系統自動辨識保險公司，也可以手動指定。照片越正、越清楚，預覽就越準。
            {% endif %}
          </p>
        </div>

        <form method="post" enctype="multipart/form-data">
          <div class="section-title">表單來源</div>
          <div class="fields">
            <label class="full">
              保險公司表單
              <select name="profile_id" id="profile-select">
                <option value="auto">自動辨識</option>
                {% for profile in profiles %}
                <option value="{{ profile.profile_id }}" {% if form_values.profile_id == profile.profile_id %}selected{% endif %}>
                  {{ profile.label }}
                </option>
                {% endfor %}
              </select>
            </label>
            <div class="full upload-hint" id="claim-form-hint-auto" {% if form_values.profile_id != 'auto' %}style="display:none"{% endif %}>
              目前是自動辨識模式，所以這份理賠申請書需要上傳，系統才知道是哪一家保險公司的版型。
            </div>
            <div class="full upload-hint" id="claim-form-hint-manual" {% if form_values.profile_id == 'auto' %}style="display:none"{% endif %}>
              你已經指定保險公司表單，所以理賠申請書可略過，不用另外再拍這一份。
            </div>
            <label class="full" id="claim-form-upload-block" {% if form_values.profile_id != 'auto' %}style="display:none"{% endif %}>
              {{ '拍理賠申請書' if mobile_mode else '理賠申請書 PDF 或照片' }}
              <input class="capture-input" type="file" name="claim_form" accept=".pdf,image/*" {% if mobile_mode %}capture="environment"{% endif %}>
            </label>
          </div>

          <div class="section-title">OCR 來源</div>
          <div class="fields">
            <label>
              {{ '診斷證明書' if mobile_mode else '診斷證明書' }}
              <div class="upload-picker" data-picker-group="diagnosis">
                <button class="upload-launcher" type="button" data-open-picker="diagnosis">選擇診斷證明書</button>
                <div class="picker-summary" id="diagnosis-summary">
                  {% if mobile_mode %}
                  按一下後可選拍照、從相簿選取，或改用檔案/PDF。
                  {% else %}
                  按一下後可選掃描/拍照、從相簿選取，或直接從檔案選 PDF。
                  {% endif %}
                </div>
                <div class="picker-panel hidden" data-picker-panel="diagnosis">
                  <button class="picker-choice" type="button" data-picker-input="diagnosis-camera">
                    <strong>直接拍照</strong>
                    <span>用相機拍目前的診斷證明書紙本。</span>
                  </button>
                  <button class="picker-choice secondary" type="button" data-picker-input="diagnosis-gallery">
                    <strong>從相簿選取</strong>
                    <span>優先打開手機照片圖庫，選現有照片。</span>
                  </button>
                  <button class="picker-choice secondary" type="button" data-picker-input="diagnosis-file">
                    <strong>從檔案選取</strong>
                    <span>可改選 PDF 或其他已存好的檔案。</span>
                  </button>
                </div>
                <input class="picker-input" id="diagnosis-camera" type="file" name="diagnosis_document" accept="image/*" {% if mobile_mode %}capture="environment"{% endif %}>
                <input class="picker-input" id="diagnosis-gallery" type="file" name="diagnosis_document_gallery" accept="image/*">
                <input class="picker-input" id="diagnosis-file" type="file" name="diagnosis_document_file" accept=".pdf,image/*">
              </div>
            </label>
            <label>
              {{ '存摺封面或影本' if mobile_mode else '存摺封面或影本' }}
              <div class="upload-picker" data-picker-group="bank">
                <button class="upload-launcher" type="button" data-open-picker="bank">選擇存摺封面</button>
                <div class="picker-summary" id="bank-summary">
                  {% if mobile_mode %}
                  按一下後可選拍照、從相簿選取，或改用檔案/PDF。
                  {% else %}
                  按一下後可選掃描/拍照、從相簿選取，或直接從檔案選 PDF。
                  {% endif %}
                </div>
                <div class="picker-panel hidden" data-picker-panel="bank">
                  <button class="picker-choice" type="button" data-picker-input="bank-camera">
                    <strong>直接拍照</strong>
                    <span>用相機拍存摺封面，抓銀行、分行與帳號。</span>
                  </button>
                  <button class="picker-choice secondary" type="button" data-picker-input="bank-gallery">
                    <strong>從相簿選取</strong>
                    <span>優先打開手機照片圖庫，選現有存摺照片。</span>
                  </button>
                  <button class="picker-choice secondary" type="button" data-picker-input="bank-file">
                    <strong>從檔案選取</strong>
                    <span>可改選 PDF 或其他已存好的檔案。</span>
                  </button>
                </div>
                <input class="picker-input" id="bank-camera" type="file" name="bank_book_image" accept="image/*" {% if mobile_mode %}capture="environment"{% endif %}>
                <input class="picker-input" id="bank-gallery" type="file" name="bank_book_image_gallery" accept="image/*">
                <input class="picker-input" id="bank-file" type="file" name="bank_book_image_file" accept=".pdf,image/*">
              </div>
            </label>
          </div>

          <div class="section-title">手動補資料</div>
          <div class="fields">
            <label>
              姓名
              <input type="text" name="name" value="{{ form_values.name }}">
            </label>
            <label>
              身分證字號
              <input type="text" name="id_number" value="{{ form_values.id_number }}">
            </label>
            <label class="full">
              地址
              <input type="text" name="address" value="{{ form_values.address }}">
            </label>
            <label>
              電話
              <input type="text" name="phone" value="{{ form_values.phone }}">
            </label>
            <label>
              事故時間
              <input type="text" name="accident_datetime" value="{{ form_values.accident_datetime }}" placeholder="115/04/01 14:30">
            </label>
            <label class="full">
              事故原因 / 診斷摘要
              <textarea name="accident_reason">{{ form_values.accident_reason }}</textarea>
            </label>
          </div>

          <div class="note">目前內建支援：富邦產險、富邦人壽、安達人壽、國泰人壽、國泰產險、和泰產險、新光人壽、台灣人壽。簽名欄會維持空白，複選框與少數特殊欄位仍建議最後人工檢查。</div>

          {% if mobile_mode %}
          <div class="note">
            iPhone 可直接當 App 使用：用 Safari 開這頁後，按分享，再選「加入主畫面」。
          </div>
          {% endif %}

          <div class="actions">
            <button class="submit" type="submit" name="action" value="preview">{{ '拍完先看預覽' if mobile_mode else '產生預覽' }}</button>
            {% if mobile_mode %}
            <a class="ghost" href="/mobile?reset=1">一鍵清空重拍</a>
            {% endif %}
          </div>
        </form>
      </div>

      <div class="card result-card">
        <div class="card-head">
          <div class="card-kicker {{ 'done' if current_stage == 'done' else '' }}">
            {{ '已完成' if current_stage == 'done' else '預覽與狀態' if result else '使用流程' }}
          </div>
          <h2>
            {% if result and result.exported_name %}
            正式 PDF 已準備好
            {% elif result %}
            先核對預覽，再決定匯出
            {% else %}
            {{ '三步驟完成一次理賠填表' if mobile_mode else '桌機控制台與手機入口' }}
            {% endif %}
          </h2>
          <p class="help">
            {% if result and result.exported_name %}
            這頁就是你的完成狀態頁。可以直接下載正式 PDF，或回到前一步重新產生。
            {% elif result %}
            這裡會顯示已辨識到的表單類型、預覽圖，以及正式匯出按鈕。
            {% elif mobile_mode %}
            手機上建議固定照這個流程走：拍照、看預覽、沒問題再匯出。
            {% else %}
            桌機版可看到 QR code、區網網址與所有已支援的保險公司，方便你從手機接續操作。
            {% endif %}
          </p>
        </div>

        {% if not mobile_mode %}
        <div class="chips">
          {% for profile in profiles %}
          <div class="chip">{{ profile.company }} / {{ profile.insurance_type }}</div>
          {% endfor %}
        </div>
        {% endif %}

        {% if quick_mobile_url and not mobile_mode %}
        <div class="qr-wrap">
          {% if mobile_qr_data_uri %}
          <img src="{{ mobile_qr_data_uri }}" alt="mobile quick mode qr">
          {% endif %}
          <div class="note">
            {% if public_base_url %}
            這個 QR code 會打開公開手機模式，不受同 Wi-Fi 或 Windows 防火牆限制。<br>
            {% else %}
            手機和電腦連同一個 Wi-Fi 後，直接掃這個 QR code，就會打開手機快速模式。<br>
            {% endif %}
            <a href="{{ quick_mobile_url }}">{{ quick_mobile_url }}</a>
          </div>
        </div>
        {% endif %}

        {% if connection_urls and not mobile_mode %}
        <div class="connection-list">
          <div class="note">
            電腦本機：<a href="{{ local_url }}">{{ local_url }}</a><br>
            區網網址：
            {% for url in connection_urls %}
            <div>{{ url }}</div>
            {% endfor %}
          </div>
        </div>
        {% endif %}

        {% if result %}
        <div class="result-grid" style="margin-top: 18px;">
          <div class="status-panel {{ 'done' if result.exported_name else '' }}">
            <div class="status-title">
              <span class="status-dot"></span>
              <span>{{ '已匯出正式 PDF' if result.exported_name else '預覽已產生' }}</span>
            </div>
            <div class="status-copy">
              辨識結果：<strong>{{ result.profile.label }}</strong><br>
              填寫模式：{{ "PDF 欄位寫入" if result.profile.mode == "widget" else "文字錨點覆寫" }}
            </div>
          </div>

          {% if result.exported_name %}
          <div class="note ok">正式 PDF 已匯出完成。你可以直接下載正式檔案，或回頭重新拍一份新的案件。</div>
          {% else %}
          <div class="note">這是預覽階段，請先確認文字內容和位置，再按「確認沒問題，匯出 PDF」。</div>
          {% endif %}

          <div class="actions">
            <a class="ghost" href="{{ url_for('preview_file', filename=result.preview_pdf_name) }}" target="_blank">開啟預覽 PDF</a>
            {% if result.exported_name %}
            <a class="download" href="{{ url_for('download_file', filename=result.exported_name) }}">下載正式 PDF</a>
            {% else %}
            <form method="post">
              <input type="hidden" name="action" value="export">
              <input type="hidden" name="preview_id" value="{{ result.preview_id }}">
              <button class="download" type="submit">確認沒問題，匯出 PDF</button>
            </form>
            {% endif %}
            {% if mobile_mode %}
            <a class="ghost" href="/mobile?reset=1&preview_id={{ result.preview_id }}">一鍵清空重拍</a>
            {% endif %}
          </div>

          <details {% if current_stage != 'done' %}open{% endif %}>
            <summary>{{ '檢視預覽圖片' if mobile_mode else '展開預覽圖片' }}</summary>
            <div class="preview-stack">
              {% for image_name in result.preview_images %}
              <div class="preview-page">
                <strong>預覽頁 {{ loop.index }}</strong>
                <img src="{{ url_for('preview_file', filename=image_name) }}" alt="preview page {{ loop.index }}">
              </div>
              {% endfor %}
            </div>
          </details>

          {% if not mobile_mode %}
          <details>
            <summary>查看 OCR / 匯出摘要</summary>
            <pre>{{ result.summary }}</pre>
          </details>
          {% endif %}
        </div>
        {% else %}
        <div class="result-grid" style="margin-top: 18px;">
          <div class="status-panel">
            <div class="status-title">
              <span class="status-dot"></span>
              <span>準備開始</span>
            </div>
            <div class="status-copy">
              {% if mobile_mode %}
              先拍理賠申請書，再拍診斷證明書和存摺封面。系統會先給你看預覽，再匯出正式 PDF。
              {% else %}
              你可以先從桌機上看 QR code，再用手機進入快速拍照模式，最後在同一頁確認預覽與匯出。
              {% endif %}
            </div>
          </div>
          <div class="note">
            {% if mobile_mode %}
            1. 拍理賠申請書。<br>
            2. 拍診斷證明書與存摺封面。<br>
            3. 看預覽核對內容。<br>
            4. 確認沒問題後再匯出正式 PDF。
            {% else %}
            1. 上傳理賠申請書做保險公司辨識。<br>
            2. 上傳診斷證明書與存摺照片讓系統抓資料。<br>
            3. 先看預覽圖確認文字內容與位置。<br>
            4. 確認沒問題後，才正式匯出 PDF。
            {% endif %}
          </div>
        </div>
        {% endif %}
      </div>
    </section>
  </main>
  <script>
    (function() {
      const profileSelect = document.getElementById('profile-select');
      const claimFormBlock = document.getElementById('claim-form-upload-block');
      const autoHint = document.getElementById('claim-form-hint-auto');
      const manualHint = document.getElementById('claim-form-hint-manual');
      const pickerPanels = Array.from(document.querySelectorAll('[data-picker-panel]'));
      const pickerButtons = Array.from(document.querySelectorAll('[data-open-picker]'));
      const pickerChoiceButtons = Array.from(document.querySelectorAll('[data-picker-input]'));
      const pickerInputs = Array.from(document.querySelectorAll('.picker-input'));

      function syncClaimFormRequirement() {
        if (!profileSelect) return;
        const isAuto = profileSelect.value === 'auto';
        if (claimFormBlock) {
          claimFormBlock.style.display = isAuto ? '' : 'none';
        }
        if (autoHint) {
          autoHint.style.display = isAuto ? '' : 'none';
        }
        if (manualHint) {
          manualHint.style.display = isAuto ? 'none' : '';
        }
      }

      function hideAllPanels() {
        pickerPanels.forEach((panel) => panel.classList.add('hidden'));
      }

      function openPickerInput(input) {
        if (!input) return;
        if (typeof input.showPicker === 'function') {
          try {
            input.showPicker();
            return;
          } catch (error) {
          }
        }
        input.click();
      }

      pickerButtons.forEach((button) => {
        button.addEventListener('click', () => {
          const pickerName = button.dataset.openPicker;
          const panel = document.querySelector(`[data-picker-panel="${pickerName}"]`);
          if (!panel) return;
          const shouldOpen = panel.classList.contains('hidden');
          hideAllPanels();
          if (shouldOpen) {
            panel.classList.remove('hidden');
          }
        });
      });

      pickerChoiceButtons.forEach((button) => {
        button.addEventListener('click', () => {
          const input = document.getElementById(button.dataset.pickerInput || '');
          hideAllPanels();
          openPickerInput(input);
        });
      });

      pickerInputs.forEach((input) => {
        input.addEventListener('change', () => {
          const pickerGroup = input.closest('[data-picker-group]');
          if (!pickerGroup || !input.files || !input.files.length) return;
          pickerGroup.querySelectorAll('.picker-input').forEach((otherInput) => {
            if (otherInput !== input) {
              otherInput.value = '';
            }
          });
          const summary = pickerGroup.querySelector('.picker-summary');
          if (summary) {
            summary.textContent = `已選擇：${input.files[0].name}`;
          }
          hideAllPanels();
        });
      });

      document.addEventListener('click', (event) => {
        if (!event.target.closest('[data-picker-group]')) {
          hideAllPanels();
        }
      });

      if (profileSelect) {
        profileSelect.addEventListener('change', syncClaimFormRequirement);
        syncClaimFormRequirement();
      }
    })();
  </script>
</body>
</html>
"""


def _render_app(mobile_mode: bool = False):
    errors: list[str] = []
    result = None
    form_values = _default_form_values(request.form)
    app_build = str(app.config.get("APP_BUILD", os.environ.get("APP_BUILD", "dev"))).strip()
    port = int(app.config.get("SERVER_PORT", 5050))
    local_url = f"http://127.0.0.1:{port}"
    connection_urls = _best_lan_urls(port)
    public_base_url = _effective_public_base_url()
    if public_base_url:
        quick_mobile_url = f"{public_base_url}/mobile?v={app_build}"
    else:
        quick_mobile_url = f"{connection_urls[0]}/mobile?v={app_build}" if connection_urls else None
    mobile_qr_data_uri = _qr_data_uri(quick_mobile_url) if quick_mobile_url else None

    if request.method == "GET" and request.args.get("reset") == "1":
        _delete_preview_artifacts(request.args.get("preview_id", "").strip())

    if request.method == "POST":
        action = request.form.get("action", "preview")

        if action == "export":
            preview_id = request.form.get("preview_id", "").strip()
            if not preview_id:
                errors.append("缺少預覽識別碼，請先重新產生預覽。")
            else:
                state = _load_preview_state(preview_id)
                if not state:
                    errors.append("找不到預覽資料，請重新產生預覽。")
                else:
                    preview_pdf_path = PREVIEW_DIR / state["preview_pdf_name"]
                    if not preview_pdf_path.exists():
                        errors.append("找不到預覽 PDF，請重新產生預覽。")
                    else:
                        profile = get_profile(state["profile_id"])
                        output_path = next_output_path(profile)
                        shutil.copy2(preview_pdf_path, output_path)
                        form_values = state.get("form_values", form_values)
                        result = _build_result(
                            profile_id=state["profile_id"],
                            preview_id=preview_id,
                            preview_pdf_name=state["preview_pdf_name"],
                            preview_images=state["preview_images"],
                            summary=state["summary"],
                            exported_name=output_path.name,
                        )
        else:
            claim_form_path = _save_upload(request.files.get("claim_form"), "claim_form_")
            diagnosis_document_path = _save_first_upload(
                request.files,
                "diagnosis_",
                "diagnosis_document",
                "diagnosis_document_gallery",
                "diagnosis_document_file",
            )
            bank_book_path = _save_first_upload(
                request.files,
                "bank_",
                "bank_book_image",
                "bank_book_image_gallery",
                "bank_book_image_file",
            )

            try:
                selected_profile_id = form_values["profile_id"]
                detected_profile = None
                detection_scores: dict[str, int] = {}

                if selected_profile_id == "auto":
                    if not claim_form_path:
                        errors.append("使用自動辨識時，請至少上傳一份理賠申請書 PDF 或照片。")
                    else:
                        detected_profile, detection_scores, _ = detect_claim_form_profile(claim_form_path)
                        if not detected_profile:
                            errors.append("目前無法辨識這份理賠申請書的保險公司或版型，請改用手動指定。")
                else:
                    try:
                        detected_profile = get_profile(selected_profile_id)
                    except KeyError:
                        errors.append("指定的保險公司表單不存在。")

                if detected_profile and not detected_profile.template_path.exists():
                    errors.append(f"找不到內建模板：{detected_profile.template_path}")

                if not errors and detected_profile:
                    claim_data, diagnosis_data, bank_data = build_claim_data(
                        diagnosis_document=diagnosis_document_path,
                        bank_book_image=bank_book_path,
                        manual_overrides=_manual_overrides(request.form),
                    )
                    preview_id = uuid.uuid4().hex[:12]
                    preview_pdf_path = _preview_pdf_path(preview_id)
                    fill_claim_form(detected_profile, claim_data, preview_pdf_path)
                    preview_images = _render_preview_images(preview_id, preview_pdf_path)
                    summary = {
                        "profile": detected_profile.profile_id,
                        "company": detected_profile.company,
                        "mode": detected_profile.mode,
                        "detection_scores": detection_scores,
                        "claim_data": claim_data,
                        "diagnosis_ocr": diagnosis_data,
                        "bank_ocr": bank_data,
                        "preview_pdf": str(preview_pdf_path),
                    }
                    state = {
                        "profile_id": detected_profile.profile_id,
                        "preview_pdf_name": preview_pdf_path.name,
                        "preview_images": preview_images,
                        "summary": summary,
                        "form_values": form_values,
                    }
                    _save_preview_state(preview_id, state)
                    result = _build_result(
                        profile_id=detected_profile.profile_id,
                        preview_id=preview_id,
                        preview_pdf_name=preview_pdf_path.name,
                        preview_images=preview_images,
                        summary=summary,
                    )
            finally:
                # Keep uploaded files while iterating on OCR and form layouts.
                pass

    current_stage = "done" if result and result.get("exported_name") else "preview" if result else "start"

    return render_template_string(
        TEMPLATE,
        profiles=list_profiles(),
        errors=errors,
        result=result,
        form_values=form_values,
        mobile_mode=mobile_mode,
        local_url=local_url,
        connection_urls=connection_urls,
        quick_mobile_url=quick_mobile_url,
        mobile_qr_data_uri=mobile_qr_data_uri,
        current_stage=current_stage,
        public_base_url=public_base_url,
        app_build=app_build,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    return _render_app(mobile_mode=False)


@app.route("/mobile", methods=["GET", "POST"])
def mobile_index():
    return _render_app(mobile_mode=True)


@app.route("/download/<path:filename>")
def download_file(filename: str):
    safe_name = secure_filename(filename)
    return send_from_directory(APP_OUTPUT_DIR, safe_name, as_attachment=True)


@app.route("/manifest.webmanifest")
@app.route("/site.webmanifest")
def web_manifest():
    manifest = {
        "name": "理賠申請書助手",
        "short_name": "理賠助手",
        "description": "拍理賠申請書、診斷證明書和存摺後，自動預覽並匯出理賠 PDF。",
        "lang": "zh-Hant",
        "dir": "ltr",
        "id": "/mobile",
        "start_url": "/mobile",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#f3f0e8",
        "theme_color": "#0b6b6f",
        "icons": [
            {
                "src": "/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    response = app.response_class(
        json.dumps(manifest, ensure_ascii=False),
        mimetype="application/manifest+json",
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    return send_file(
        io.BytesIO(_app_icon_png(180)),
        mimetype="image/png",
        download_name="apple-touch-icon.png",
        max_age=3600,
    )


@app.route("/icon-192.png")
def icon_192():
    return send_file(
        io.BytesIO(_app_icon_png(192)),
        mimetype="image/png",
        download_name="icon-192.png",
        max_age=3600,
    )


@app.route("/icon-512.png")
def icon_512():
    return send_file(
        io.BytesIO(_app_icon_png(512)),
        mimetype="image/png",
        download_name="icon-512.png",
        max_age=3600,
    )


@app.route("/favicon.ico")
def favicon():
    return send_file(
        io.BytesIO(_app_icon_ico()),
        mimetype="image/x-icon",
        download_name="favicon.ico",
        max_age=3600,
    )


@app.route("/preview/<path:filename>")
def preview_file(filename: str):
    safe_name = secure_filename(filename)
    return send_from_directory(PREVIEW_DIR, safe_name, as_attachment=False)


if __name__ == "__main__":
    configured_port = os.environ.get("CLAIM_FORM_PORT", "").strip()
    port = int(configured_port) if configured_port else _find_available_port(5050)
    app.config["SERVER_PORT"] = port
    app.config["PUBLIC_BASE_URL"] = os.environ.get("PUBLIC_BASE_URL", "").strip()
    app.config["APP_BUILD"] = os.environ.get("APP_BUILD", "dev").strip()
    local_url = f"http://127.0.0.1:{port}"
    connection_urls = _best_lan_urls(port)
    public_base_url = str(app.config.get("PUBLIC_BASE_URL", "")).strip().rstrip("/")
    if public_base_url:
        quick_mobile_url = f"{public_base_url}/mobile?v={app.config['APP_BUILD']}"
    else:
        quick_mobile_url = f"{connection_urls[0]}/mobile?v={app.config['APP_BUILD']}" if connection_urls else None
    if os.environ.get("CLAIM_FORM_NO_BROWSER", "").strip() != "1":
        try:
            webbrowser.open(local_url)
        except Exception:
            pass
    print(f"Local: {local_url}")
    for url in connection_urls:
        print(f"LAN:   {url}")
    if public_base_url:
        print(f"Public: {public_base_url}")
    if quick_mobile_url:
        print(f"Mobile quick: {quick_mobile_url}")
    app.run(host="0.0.0.0", port=port, debug=False)
