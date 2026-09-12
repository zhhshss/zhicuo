# -*- coding: utf-8 -*-
"""
中国移动云盘「AI 解题」本地代理服务 
源自 yun.139.com/archive-book-h5（uni-app H5）
token 由 config.json / 页面配置提供。
"""
import base64
import asyncio
import hashlib
import io
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, UnidentifiedImageError

import storage
import credential_store
from exporter import (
    build_ai_docx, build_docx, build_layout_image, build_pdf, build_visual_docx,
    normalize_formula_text, plain_text, question_image_sources, remote_question_image_url,
    rich_events,
)

QUESTION_OCR_PYTHON = Path(os.environ.get("MISTAKE_BOOK_OCR_PYTHON", "/root/ocr-venv/bin/python"))
QUESTION_OCR_SCRIPT = Path(__file__).parent / "question_regions.py"
CLIPROXY_BASE_URL = os.environ.get("MISTAKE_BOOK_CLIPROXY_URL", "http://127.0.0.1:50002").rstrip("/")
CLIPROXY_MODEL = os.environ.get("MISTAKE_BOOK_SEGMENT_MODEL", "pp/gemini-3.8-flash").strip()
CLIPROXY_FALLBACK_MODEL = os.environ.get("MISTAKE_BOOK_CLIPROXY_FALLBACK_MODEL", "pp/gemini-3.7-flash").strip()
CLIPROXY_SEGMENT_TIMEOUT = float(os.environ.get("MISTAKE_BOOK_SEGMENT_TIMEOUT", "8"))
CLIPROXY_AI_WORD_TIMEOUT = float(os.environ.get("MISTAKE_BOOK_AI_WORD_TIMEOUT", "60"))
CLIPROXY_AI_WORD_RETRIES = max(0, min(3, int(os.environ.get("MISTAKE_BOOK_AI_WORD_RETRIES", "2"))))
CLIPROXY_CONFIG = Path(os.environ.get("MISTAKE_BOOK_CLIPROXY_CONFIG", "/root/.cli-proxy-api/config.yaml"))
CAMSCANNER_SCRIPT = Path(os.environ.get("MISTAKE_BOOK_CAMSCANNER_SCRIPT", "/root/camscanner-img2word/camscanner_img2word.py"))
EXPORT_DIR = storage.DATA_DIR / "exports"
EXPORT_JOB_TTL_SECONDS = 24 * 60 * 60
EXPORT_JOB_LIMIT = 20
AI_WORD_MAX_PAGES = 8
EXPORT_JOBS: dict[str, dict[str, Any]] = {}
EXPORT_TASKS: set[asyncio.Task] = set()
EXPORT_WORKERS = asyncio.Semaphore(2)
CAMSCANNER_WORKER = asyncio.Semaphore(1)
CLIPROXY_MODEL_CACHE: tuple[float, set[str]] = (0.0, set())

# ----------------------------- 常量 -----------------------------
BASE_BP = "https://orches.yun.139.com/adaptor"
BASE_AI = "https://ai.yun.139.com"
BASE_AI_CHAT = "https://ai.yun.139.com/api/outer"
BASE_MEMBER = "https://ypqy.mcloud.139.com/isbo2/openApi"
BASE_PERSONAL = "https://personal-kd-njs.yun.139.com/hcy"

VERSION = "1.2.4"
CLIENT_CODE = "10805"
CHANNEL_SRC = "10252400"
TERMINAL_TYPE = 21
APP_CHANNEL = "101"
SOLVE_CHANNEL = "501"

# ----------------------------- 配置 -----------------------------
def load_config() -> dict:
    """读取加密凭证，并把旧版明文 config.json 自动迁移为 AES-256-GCM。"""
    cfg = {}
    try:
        cfg, source = credential_store.load()
        if cfg:
            print(f"[config] ✅ 已解密 139 云盘凭证（密钥来源：{source}）")
    except RuntimeError as exc:
        print(f"[config] ❌ {exc}")
    if not cfg:
        try:
            legacy = credential_store.load_legacy()
            if legacy:
                credential_store.save(legacy)
                credential_store.remove_legacy()
                cfg = legacy
                print("[config] ✅ 旧版明文 config.json 已迁移为 AES-256-GCM 加密存储")
        except Exception as exc:
            print(f"[config] ⚠️ 迁移旧配置失败：{exc}")
    # 环境变量仅作无持久化兜底
    if not cfg.get("token"):
        cfg["token"] = os.environ.get("YUN139_TOKEN", "")
    if not cfg.get("userDomainId"):
        cfg["userDomainId"] = os.environ.get("YUN139_USER_DOMAIN_ID", "")
    if not cfg.get("camScannerS2"):
        cfg["camScannerS2"] = os.environ.get("CAMSCANNER_S2", "")
    if not cfg.get("camScannerCssu"):
        cfg["camScannerCssu"] = os.environ.get("CAMSCANNER_CSSU", "")
    if not cfg.get("camScannerCsste"):
        cfg["camScannerCsste"] = os.environ.get("CAMSCANNER_CSSTE", "")
    # 3) 启动提示
    if cfg.get("token"):
        print("[config] ✅ token 就绪（来源：加密凭证或环境变量）")
    else:
        print("[config] ⚠️ 未检测到 token —— 请导入油猴脚本导出的 config.json，")
        print("         或启动后在页面「AI 与数据」中导入配置")
    return cfg

config = load_config()


def _cam_scanner_configured() -> bool:
    return all(config.get(field) for field in ("camScannerS2", "camScannerCssu", "camScannerCsste"))

def save_config() -> None:
    """使用 AES-256-GCM 保存凭证；磁盘上不写入 Token 明文。"""
    credential_store.save(config)
    credential_store.remove_legacy()

def client_info() -> str:
    return f"||{TERMINAL_TYPE}|{VERSION}|||{uuid.uuid4().hex[:32]}|||||||"

# ----------------------------- 请求头 -----------------------------
def common_headers(channel: str = APP_CHANNEL) -> dict:
    ci = client_info()
    return {
        "Content-Type": "application/json",
        "Authorization": config.get("token", ""),
        "x-huawei-channelSrc": CHANNEL_SRC, "x-inner-ntwk": "2", "x-NetType": "4g",
        "x-DeviceInfo": ci, "mcloud-channel": "1000101", "mcloud-client": CLIENT_CODE,
        "mcloud-version": VERSION, "mcloud-network": "4g", "mcloud-skey": "",
        "x-yun-api-version": "v1", "x-yun-app-channel": channel,
        "x-yun-client-info": ci, "INNER-HCY-ROUTER-HTTPS": "1", "x-yun-tid": uuid.uuid4().hex,
    }

def ai_headers() -> dict:
    ci = client_info()
    return {
        "Content-Type": "application/json;charset=UTF-8", "accept": "application/json",
        "Authorization": config.get("token", ""), "x-yun-api-version": "v1",
        "x-yun-client-info": ci, "x-yun-tid": uuid.uuid4().hex,
    }

def chat_headers() -> dict:
    ci = client_info()
    return {
        "Content-Type": "application/json", "Authorization": config.get("token", ""),
        "x-yun-api-version": "v1", "x-yun-app-channel": SOLVE_CHANNEL,
        "x-yun-client-info": ci, "x-yun-tid": uuid.uuid4().hex,
    }

def member_headers() -> dict:
    ci = client_info()
    return {
        "Content-Type": "application/json", "Authorization": config.get("token", ""),
        "x-yun-api-version": "v1", "x-yun-client-info": ci, "x-yun-tid": uuid.uuid4().hex,
    }

def require_auth() -> None:
    if not config.get("token"):
        raise HTTPException(400, "未配置 token，请先在页面「配置」里粘贴登录 token")

# ----------------------------- 工具 -----------------------------
IMG_SRC_RE = re.compile(r'(<img[^>]*?src=")([^"]+)(")', re.I)

def rewrite_images(html: str) -> str:
    if not html:
        return html
    def rep(m: re.Match) -> str:
        url = m.group(2)
        if url.startswith(("http://", "https://")):
            return m.group(1) + "/proxy?url=" + urllib.parse.quote(url, safe="") + m.group(3)
        return m.group(0)
    return IMG_SRC_RE.sub(rep, html)


def optimize_image(data: bytes, max_side: int = 2560, target_bytes: int = 2_600_000) -> tuple[bytes, dict]:
    """纠正 EXIF 方向并压缩到适合拍题识别的尺寸，兼顾文字清晰度与上传大小。"""
    if not data:
        raise HTTPException(400, "空文件")
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            original_width, original_height = image.size
            if max(image.size) > max_side:
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            quality = 88
            output = io.BytesIO()
            while True:
                output.seek(0)
                output.truncate(0)
                image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True, dpi=(144, 144))
                if output.tell() <= target_bytes or quality <= 68:
                    break
                quality -= 5
            optimized = output.getvalue()
            return optimized, {
                "originalSize": len(data), "size": len(optimized),
                "originalWidth": original_width, "originalHeight": original_height,
                "width": image.width, "height": image.height, "quality": quality,
            }
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(400, "无法识别图片格式") from exc

async def _post_json(client: httpx.AsyncClient, url: str, headers: dict, payload: dict, timeout: float = 60.0) -> dict:
    r = await client.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code != 200:
        raise HTTPException(502, f"上游 {url} 返回 {r.status_code}: {r.text[:300]}")
    j = r.json()
    if not j.get("success"):
        raise HTTPException(502, f"上游错误 {j.get('code')}: {j.get('message')}")
    return j

# ----------------------------- 上传协议 -----------------------------
async def upload_image(name: str, data: bytes) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    async with httpx.AsyncClient(timeout=120) as c:
        j = await _post_json(c, f"{BASE_BP}/ai-camera/scans/createTmp", common_headers(), {})
        temp_id = j["data"]["tempFolderId"]
        j = await _post_json(c, f"{BASE_BP}/ai-camera/scans/uploads/prepare", common_headers(), {
            "tempId": temp_id, "name": name, "size": len(data),
            "contentHashAlgorithm": "sha256", "contentHash": sha,
        })
        d = j["data"]
        if d.get("rapidUpload") or d.get("exist"):
            return {"fileId": d["fileId"], "rapid": True, "size": len(data)}
        r = await c.put(d["fileUploadUrl"], content=data,
                        headers={"Content-Type": "application/octet-stream"}, timeout=300)
        if r.status_code != 200:
            raise HTTPException(502, f"EOS PUT 失败: {r.status_code} {r.text[:200]}")
        await _post_json(c, f"{BASE_BP}/ai-camera/scans/uploads/complete", common_headers(), {
            "fileId": d["fileId"], "uploadId": d["uploadId"],
            "contentHash": sha, "contentHashAlgorithm": "sha256",
        })
        return {"fileId": d["fileId"], "rapid": False, "size": len(data)}

# ----------------------------- 项目文件清单（浏览器端打包用） -----------------------------
def project_file_map() -> dict:
    """返回 {相对路径: 内容}；config.json（含 token）绝不返回"""
    root = Path(__file__).parent
    disk_files = ["server.py", "requirements.txt", "README.md", "config.json.example",
                  "static/index.html", "tampermonkey/139-token-export.user.js"]
    out = {}
    for f in disk_files:
        p = root / f
        if p.exists():
            out[f"ai-solve-proxy/{f}"] = p.read_text(encoding="utf-8")
    out["ai-solve-proxy/start.bat"] = (
        "@echo off\r\nchcp 65001 >nul\r\n"
        "echo ==============================\r\n"
        "echo   AI\u89e3\u9898\u4ee3\u7406 - \u4e00\u952e\u542f\u52a8\r\n"
        "echo ==============================\r\n"
        "pip install -r requirements.txt\r\n"
        "python server.py\r\n"
        "start http://localhost:18666\r\n"
        "pause\r\n"
    )
    out["ai-solve-proxy/run.sh"] = (
        "#!/usr/bin/env bash\n"
        "pip3 install -r requirements.txt\n"
        "python3 server.py &\n"
        "sleep 2\n"
        "(open http://localhost:18666 || xdg-open http://localhost:18666) 2>/dev/null\n"
    )
    return out

# ----------------------------- FastAPI -----------------------------
app = FastAPI(title="知错 · AI 错题本", version="2.0.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.mount("/media", StaticFiles(directory=storage.MEDIA_DIR), name="media")
app.mount("/vendor/katex", StaticFiles(directory="/usr/share/javascript/katex", follow_symlink=True), name="katex")

@app.get("/", response_class=HTMLResponse)
async def index():
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

# ---- 配置 ----
class ConfigReq(BaseModel):
    token: str = ""
    userDomainId: str = ""
    account: str = ""
    camScannerS2: str = ""
    camScannerCssu: str = ""
    camScannerCsste: str = ""


if hasattr(ConfigReq, "model_fields"):
    CONFIG_FIELDS = set(ConfigReq.model_fields)
else:
    CONFIG_FIELDS = set(ConfigReq.__fields__)


def _update_config(req: ConfigReq) -> None:
    fields = ("token", "userDomainId", "account", "camScannerS2", "camScannerCssu", "camScannerCsste")
    for field in fields:
        value = getattr(req, field, "")
        if value:
            config[field] = value.strip()


def _config_req_from_mapping(data: dict[str, Any]) -> ConfigReq:
    if not isinstance(data, dict):
        raise HTTPException(400, "配置必须是 JSON 对象")
    aliases = {"S2": "camScannerS2", "_cssu": "camScannerCssu", "_csste": "camScannerCsste"}
    normalized = {aliases.get(key, key): str(value) for key, value in data.items() if aliases.get(key, key) in CONFIG_FIELDS}
    return ConfigReq(**normalized)

@app.post("/api/config")
async def set_config(req: ConfigReq):
    _update_config(req)
    save_config()
    return {"success": True, "configured": bool(config.get("token")),
            "hasUserDomainId": bool(config.get("userDomainId")),
            "camScannerConfigured": _cam_scanner_configured()}

@app.post("/api/config/import")
async def config_import(data: dict):
    """接收油猴脚本导出的 config.json 内容（JSON body / 粘贴）"""
    return await set_config(_config_req_from_mapping(data))

@app.post("/api/config/import-file")
async def config_import_file(file: UploadFile = File(...)):
    try:
        data = json.loads(await file.read())
    except Exception:
        raise HTTPException(400, "不是有效的 JSON 文件")
    return await set_config(_config_req_from_mapping(data))

@app.get("/api/config")
async def get_config():
    return {"configured": bool(config.get("token")), "hasUserDomainId": bool(config.get("userDomainId")),
            "camScannerConfigured": _cam_scanner_configured(),
            **credential_store.status()}

@app.get("/api/config/export")
async def config_export():
    require_auth()
    out = {"token": config.get("token", ""), "userDomainId": config.get("userDomainId", "")}
    if config.get("account"): out["account"] = config["account"]
    if _cam_scanner_configured():
        out.update({
            "camScannerS2": config["camScannerS2"],
            "camScannerCssu": config["camScannerCssu"],
            "camScannerCsste": config["camScannerCsste"],
        })
    return Response(content=json.dumps(out, ensure_ascii=False, indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="config.json"'})

# ---- 项目文件内容（打包由浏览器端 JSZip 完成） ----
@app.get("/api/project-files")
async def project_files():
    """返回项目所有文件内容 JSON，浏览器端用 JSZip 打包下载"""
    return project_file_map()

# ---- 图片代理 ----
@app.get("/proxy")
async def proxy(url: str):
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "仅允许 http(s) 地址")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url)
        return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))

# ---- 上传 ----
@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    require_auth()
    data = await file.read()
    if not data: raise HTTPException(400, "空文件")
    if len(data) > 50 * 1024 * 1024: raise HTTPException(400, "原图过大（最大 50MB）")
    data, image_info = optimize_image(data)
    try:
        safe_name = f"{Path(file.filename or 'image').stem}.jpg"
        result = await upload_image(safe_name, data)
        print(f"[upload] ✅ {file.filename} -> {result.get('fileId')} ({len(data)}B)")
        return {"success": True, **result, "image": image_info}
    except HTTPException as e:
        print(f"[upload] ❌ {file.filename} 上传失败: {e.detail}")
        raise

def detect_question_regions_bytes(data: bytes) -> dict:
    """用独立 OCR 环境切题，避免把 RapidOCR 塞进主服务进程。"""
    if not QUESTION_OCR_PYTHON.exists() or not QUESTION_OCR_SCRIPT.exists():
        raise HTTPException(500, "本地切题环境未就绪，请先安装 RapidOCR")
    stamp = uuid.uuid4().hex
    source = Path("/tmp") / f"qr-in-{stamp}.jpg"
    output = Path("/tmp") / f"qr-out-{stamp}.json"
    source.write_bytes(data)
    try:
        proc = subprocess.run(
            [str(QUESTION_OCR_PYTHON), str(QUESTION_OCR_SCRIPT), "--out", str(output), str(source)],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "OMP_NUM_THREADS": "1"},
        )
        if proc.returncode != 0 or not output.exists():
            detail = (proc.stderr or proc.stdout or "ocr failed").strip()[-800:]
            raise HTTPException(500, f"切题失败：{detail or 'unknown'}")
        payload = json.loads(output.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "切题超时，请换更清晰的试卷照片") from exc
    finally:
        source.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
    return {
        "width": payload.get("width"),
        "height": payload.get("height"),
        "rotated": bool(payload.get("rotated")),
        "deskewAngle": float(payload.get("deskewAngle") or 0),
        "seconds": payload.get("seconds"),
        "warnings": payload.get("warnings") or [],
        "regions": payload.get("regions") or [],
        "detector": payload.get("detector") or "rapidocr-option-skeleton",
        "rawNumbers": payload.get("rawNumbers") or [],
        "finalNumbers": payload.get("finalNumbers") or [],
    }


def load_cliproxy_api_key() -> str:
    """优先读环境变量，否则只读取 CLIProxyAPI 配置中的第一枚接入密钥。"""
    key = os.environ.get("MISTAKE_BOOK_CLIPROXY_API_KEY", "").strip()
    if key:
        return key
    try:
        text = CLIPROXY_CONFIG.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(503, "找不到本机 CLIProxyAPI 配置") from exc
    match = re.search(r"(?m)^api-keys:\s*\n\s*-\s*[\"']?([^\s\"'#]+)", text)
    if not match:
        raise HTTPException(503, "CLIProxyAPI 未配置接入密钥")
    return match.group(1)


async def _cliproxy_model_ids() -> set[str] | None:
    """读取本机 CLIProxyAPI 已公开的模型 ID，并短时缓存结果。"""
    global CLIPROXY_MODEL_CACHE
    now = asyncio.get_running_loop().time()
    cached_at, cached_models = CLIPROXY_MODEL_CACHE
    if now - cached_at < 60:
        return cached_models
    try:
        key = load_cliproxy_api_key()
        timeout = httpx.Timeout(5, connect=2)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(
                f"{CLIPROXY_BASE_URL}/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
        if response.status_code != 200:
            return None
        payload = response.json()
        models = {
            str(item.get("id")) for item in payload.get("data") or []
            if isinstance(item, dict) and item.get("id")
        }
        CLIPROXY_MODEL_CACHE = (now, models)
        return models
    except (HTTPException, httpx.HTTPError, json.JSONDecodeError, TypeError, AttributeError):
        return None


async def _cliproxy_model_available(model: str) -> bool | None:
    models = await _cliproxy_model_ids()
    return None if models is None else model in models


async def _preferred_ai_word_model() -> str:
    if not CLIPROXY_FALLBACK_MODEL or CLIPROXY_FALLBACK_MODEL == CLIPROXY_MODEL:
        return CLIPROXY_MODEL
    available = await _cliproxy_model_available(CLIPROXY_MODEL)
    if available is False:
        fallback_available = await _cliproxy_model_available(CLIPROXY_FALLBACK_MODEL)
        if fallback_available is not False:
            print(f"[ai-word] ℹ️ {CLIPROXY_MODEL} 未在 CLIProxyAPI 模型列表中，优先使用 {CLIPROXY_FALLBACK_MODEL}")
            return CLIPROXY_FALLBACK_MODEL
    return CLIPROXY_MODEL


async def _resolve_ai_model(requested: str = "") -> str:
    """校验用户选择的模型；未指定时使用默认模型/回退模型。"""
    model = str(requested or "").strip() or await _preferred_ai_word_model()
    available = await _cliproxy_model_available(model)
    if available is False:
        raise HTTPException(400, f"CLIProxyAPI 当前不可用模型：{model}")
    return model


@app.get("/api/ai/models")
async def ai_models():
    """返回本机代理可用的视觉模型，供前端选择；不返回任何密钥。"""
    models = await _cliproxy_model_ids()
    if models is None:
        candidates = [CLIPROXY_MODEL, CLIPROXY_FALLBACK_MODEL]
        return {"success": True, "available": False, "models": [m for m in dict.fromkeys(candidates) if m]}
    preferred = [CLIPROXY_MODEL, CLIPROXY_FALLBACK_MODEL]
    visual = [model for model in sorted(models) if re.search(
        r"gemini|vision|multimodal|image-preview|(?:best|pro)-vision|auto/(?:vision|multimodal|gemini)",
        model, re.I,
    )]
    selected = list(dict.fromkeys([*preferred, *visual]))
    return {"success": True, "available": True, "models": selected[:80], "default": await _preferred_ai_word_model()}


def extract_ai_text(payload: dict) -> str:
    """兼容 Responses API 的标准 output 与部分代理返回的 output_text。"""
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"].strip()
    chunks = []
    for item in payload.get("output") or []:
        content_items = item.get("content") or []
        if isinstance(content_items, str):
            chunks.append(content_items)
            continue
        for content in content_items:
            if content.get("type") in ("output_text", "text") and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    for choice in payload.get("choices") or []:
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str):
            chunks.append(content)
    return "\n".join(chunks).strip()


def parse_ai_regions(text: str, width: int, height: int, detector_model: str = CLIPROXY_MODEL) -> list[dict]:
    """把模型返回的 0–1000 坐标转为图像像素，并过滤明显无效框。"""
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = fenced.find("{"), fenced.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型未返回 JSON")
    payload = json.loads(fenced[start:end + 1])
    raw_regions = payload.get("regions")
    if not isinstance(raw_regions, list):
        raise ValueError("模型结果缺少 regions")
    regions = []
    for order, item in enumerate(raw_regions[:100]):
        bbox = item.get("bbox") if isinstance(item, dict) else None
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = [max(0.0, min(1000.0, float(value))) for value in bbox]
        except (TypeError, ValueError):
            continue
        if x2 <= x1 or y2 <= y1 or x2 - x1 < 40 or y2 - y1 < 12:
            continue
        pixel_bbox = [
            round(x1 * width / 1000), round(y1 * height / 1000),
            round(x2 * width / 1000), round(y2 * height / 1000),
        ]
        regions.append({
            "detectedNumber": item.get("number") or order + 1,
            "bbox": pixel_bbox,
            "inferred": False,
            "score": 0.95,
            "detector": f"cliproxy:{detector_model}",
        })
    regions.sort(key=lambda region: (region["bbox"][1], region["bbox"][0]))
    for order, region in enumerate(regions):
        region["detectedNumber"] = order + 1
    if not regions:
        raise ValueError("模型没有识别到有效题框")
    return regions


async def detect_question_regions_ai(data: bytes, width: int, height: int, requested_model: str = "") -> dict:
    """通过本机 CLIProxyAPI 的视觉模型识别整页题目边界。"""
    model = await _resolve_ai_model(requested_model)
    prompt = f"""你是严谨的中文试卷切题器。图片尺寸为 {width}×{height} 像素。
逐一识别图片中真实存在的每一道完整题目，为每题给出矩形 bbox。
坐标必须归一化为 0 到 1000 的整数，格式为 [x1,y1,x2,y2]，原点在左上角。
每个框应包含该题题号、完整题干、选项、表格和配图；排除页眉、页脚、讲解按钮、批注和大片空白。
不同题目绝不合并，同一道题也不要拆成多个框；不要臆造图片中不存在的题。
只返回 JSON，不要解释：{{"regions":[{{"number":1,"bbox":[x1,y1,x2,y2]}}]}}"""
    request = {
        "model": model,
        "max_output_tokens": 4096,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")},
            ],
        }],
    }
    headers = {"Authorization": f"Bearer {load_cliproxy_api_key()}", "Content-Type": "application/json"}
    last_error = "模型返回空内容"
    async with httpx.AsyncClient(timeout=httpx.Timeout(CLIPROXY_SEGMENT_TIMEOUT, connect=5), trust_env=False) as client:
        try:
            response = await client.post(f"{CLIPROXY_BASE_URL}/v1/responses", headers=headers, json=request)
            if response.status_code != 200:
                detail = response.text[:300]
                last_error = f"CLIProxyAPI 返回 {response.status_code}：{detail}"
            else:
                text = extract_ai_text(response.json())
                if text:
                    regions = parse_ai_regions(text, width, height, model)
                    return {
                        "width": width, "height": height, "rotated": False,
                        "warnings": [], "regions": regions,
                        "detector": f"AI · {model}",
                        "rawNumbers": [], "finalNumbers": [region["detectedNumber"] for region in regions],
                    }
        except httpx.TimeoutException:
            last_error = f"{model} 视觉请求超过 {CLIPROXY_SEGMENT_TIMEOUT:g} 秒"
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc) or type(exc).__name__
    raise HTTPException(502, f"AI 拆题失败：{last_error}")


def parse_ai_document_blocks(text: str, width: int, height: int) -> list[dict]:
    """解析视觉模型返回的阅读顺序块，并将归一化框转换为像素。"""
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = fenced.find("{"), fenced.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型未返回 JSON")
    raw_json = fenced[start:end + 1]
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        # 视觉模型经常把 LaTeX 的反斜杠直接写进 JSON；修复常见命令后再解析，
        # 避免公式中的 \frac、\text、\( 等被 JSON 当作非法转义。
        repaired = re.sub(
            r"(?<!\\)\\(?=(?:[A-Za-z]{2,}|[()[\]{}]|[,;:!|]))",
            r"\\\\", raw_json,
        )
        repaired = re.sub(r"(?<!\\)\\u(?![0-9a-fA-F]{4})", r"\\\\u", repaired)
        repaired = re.sub(r"(?<!\\)\\(?![\"\\/bfnrtu])", r"\\\\", repaired)
        try:
            payload = json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型 JSON 无法解析：{exc.msg}") from exc
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        raise ValueError("模型结果缺少 blocks")
    blocks: list[dict] = []
    for raw in raw_blocks[:240]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or raw.get("kind") or "text").lower()
        kind = "image" if kind in {"image", "picture", "photo", "figure", "diagram", "chart"} else "text"
        bbox = raw.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            bbox = [0, 0, 1000, 1000]
        try:
            coords = [max(0.0, min(1000.0, float(value))) for value in bbox]
        except (TypeError, ValueError):
            continue
        x1, y1, x2, y2 = coords
        if x2 <= x1 or y2 <= y1:
            continue
        pixel_bbox = [
            round(x1 * width / 1000), round(y1 * height / 1000),
            round(x2 * width / 1000), round(y2 * height / 1000),
        ]
        if kind == "text":
            value = raw.get("text") or raw.get("content") or ""
            if not isinstance(value, str) or not value.strip():
                continue
            blocks.append({"type": "text", "bbox": pixel_bbox, "text": value.strip()})
        else:
            blocks.append({
                "type": "image", "bbox": pixel_bbox,
                "caption": str(raw.get("caption") or "").strip(),
            })
    if not blocks:
        raise ValueError("模型没有识别到文字或图片")
    return blocks


async def _analyze_ai_word_page_once(data: bytes, model: str = CLIPROXY_MODEL) -> dict:
    """单次调用 CLIProxyAPI 识别一页文字和图片版面。"""
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(400, "无法识别图片格式") from exc
    prompt = f"""你是中文文档 OCR 和版面分析器。图片尺寸为 {width}×{height} 像素。
按从上到下、从左到右的阅读顺序识别图片里的所有内容，输出 blocks 数组。
文字块 type 必须是 text，text 必须逐字保留；数学公式请用 LaTeX（行内 \\( \\)，独立公式 \\[ \\]）。
非文字内容（几何图、函数图、表格截图、照片、手写图、插图）用 type=image，并给出只包住图本身的 bbox；不要把普通文字当图片，也不要重复识别图片里的文字。
bbox 是 0 到 1000 的归一化坐标 [x1,y1,x2,y2]，原点在左上角。不要漏掉题号、选项、单位和标点。
JSON 字符串里的反斜杠必须写成两个反斜杠（例如 \\\\frac、\\\\sqrt、\\\\(），否则 JSON 无法解析。
只返回 JSON，不要 Markdown 或解释：{{"blocks":[{{"type":"text","bbox":[x1,y1,x2,y2],"text":"..."}},{{"type":"image","bbox":[x1,y1,x2,y2],"caption":"可选说明"}}]}}"""
    request = {
        "model": model,
        "max_output_tokens": 6000,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")},
            ],
        }],
    }
    headers = {"Authorization": f"Bearer {load_cliproxy_api_key()}", "Content-Type": "application/json"}
    try:
        timeout = httpx.Timeout(CLIPROXY_AI_WORD_TIMEOUT, connect=8)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(f"{CLIPROXY_BASE_URL}/v1/responses", headers=headers, json=request)
        if response.status_code != 200:
            raise HTTPException(502, f"AI OCR 返回 {response.status_code}：{response.text[:300]}")
        result = response.json()
        output = extract_ai_text(result)
        if not output:
            raise ValueError("模型返回空内容")
        blocks = parse_ai_document_blocks(output, width, height)
        return {"width": width, "height": height, "blocks": blocks}
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        raise HTTPException(504, f"AI OCR 超时（{CLIPROXY_AI_WORD_TIMEOUT:g} 秒）") from exc
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(502, f"AI OCR 结果解析失败：{exc}") from exc


def _unknown_model_error(exc: HTTPException) -> bool:
    detail = str(exc.detail or "")
    return exc.status_code == 502 and bool(re.search(r"unknown provider|model .*not found|模型不存在", detail, re.I))


async def _retry_ai_word_page(data: bytes, model: str, retries: int) -> dict:
    retryable_statuses = {429, 502, 503, 504}
    last_error: HTTPException | None = None
    for attempt in range(retries + 1):
        try:
            return await _analyze_ai_word_page_once(data, model)
        except HTTPException as exc:
            last_error = exc
            if (_unknown_model_error(exc) or exc.status_code not in retryable_statuses
                    or attempt >= retries):
                raise
            delay = min(2.0, 0.6 * (attempt + 1))
            print(f"[ai-word] ⚠️ CLIProxyAPI 暂时不可用（{model}，{exc.status_code}），{delay:g}s 后重试 {attempt + 1}/{retries}")
            await asyncio.sleep(delay)
    raise last_error or HTTPException(502, "AI OCR 暂时不可用")


async def analyze_ai_word_page(data: bytes, requested_model: str = "") -> dict:
    """调用视觉模型；临时错误有限重试，模型不可用时切到本机已配置的视觉模型。"""
    selected_model = await _resolve_ai_model(requested_model)
    try:
        return await _retry_ai_word_page(data, selected_model, CLIPROXY_AI_WORD_RETRIES)
    except HTTPException as exc:
        if (requested_model.strip() or selected_model != CLIPROXY_MODEL or not CLIPROXY_FALLBACK_MODEL
                or CLIPROXY_FALLBACK_MODEL == CLIPROXY_MODEL or not _unknown_model_error(exc)):
            raise
        print(f"[ai-word] ⚠️ {CLIPROXY_MODEL} 在 CLIProxyAPI 中不可用，改用 {CLIPROXY_FALLBACK_MODEL}")
        return await _retry_ai_word_page(data, CLIPROXY_FALLBACK_MODEL, min(1, CLIPROXY_AI_WORD_RETRIES))


def crop_ai_image_blocks(data: bytes, blocks: list[dict]) -> list[dict]:
    """按 AI 返回的图片框裁切原图，输出可直接嵌入 Word 的 JPEG。"""
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            width, height = image.size
            output_blocks = []
            for block in blocks:
                if block.get("type") != "image":
                    output_blocks.append(block)
                    continue
                bbox = block.get("bbox") or [0, 0, width, height]
                x1 = max(0, min(width - 1, int(bbox[0])))
                y1 = max(0, min(height - 1, int(bbox[1])))
                x2 = max(x1 + 2, min(width, int(bbox[2])))
                y2 = max(y1 + 2, min(height, int(bbox[3])))
                if x2 - x1 < 8 or y2 - y1 < 8:
                    continue
                cropped = image.crop((x1, y1, x2, y2))
                stream = io.BytesIO()
                cropped.save(stream, format="JPEG", quality=92, optimize=True, progressive=True)
                output_blocks.append({**block, "data": stream.getvalue()})
            return output_blocks
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(400, "无法裁切图片") from exc


async def _read_ai_source_image(source: str) -> bytes:
    """读取错题中的本地/远程图片；本地路径严格限制在 data/media 内。"""
    source = str(source or "").strip()
    if source.startswith("/media/"):
        relative = urllib.parse.unquote(source.removeprefix("/media/")).lstrip("/")
        path = (storage.MEDIA_DIR / relative).resolve()
        media_root = storage.MEDIA_DIR.resolve()
        if not path.is_relative_to(media_root) or not path.is_file():
            raise HTTPException(404, "错题图片不存在")
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise HTTPException(404, "错题图片读取失败") from exc
    else:
        remote = remote_question_image_url(source) or source
        if not _safe_remote_image_url(remote):
            raise HTTPException(400, "图片地址不安全或不可访问")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(remote)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502, "远程题图读取失败") from exc
        if len(response.content) > 15 * 1024 * 1024:
            raise HTTPException(400, "图片超过 15MB")
        data = response.content
    optimized, _ = optimize_image(data, max_side=1800, target_bytes=2_000_000)
    return optimized


async def _ai_blocks_for_image(data: bytes, caption: str = "", model: str = "") -> list[dict]:
    """只把图片本身交给视觉模型；模型识别到的图片块再按框裁切。"""
    try:
        result = await analyze_ai_word_page(data, model)
        return crop_ai_image_blocks(data, result["blocks"])
    except HTTPException as exc:
        # 上游失败时保留原图，避免导出结果丢失；不伪造 OCR 文字。
        print(f"[ai-word] ⚠️ 图片 AI 识别失败，保留原图：{exc.detail}")
        return [{"type": "image", "bbox": [0, 0, 1, 1], "caption": caption or "原图", "data": data}]


async def _build_ai_item_page(item: dict[str, Any], index: int, options: dict[str, Any]) -> dict:
    """直接复用错题已有文字，只对其中的图片调用 AI，并保持原内容顺序。"""
    blocks: list[dict] = []
    answer_blocks: list[dict] = []
    seen_sources: set[str] = set()
    image_tasks: list[tuple[dict, str, str, list[dict]]] = []

    def prepare_direct_html(value: Any) -> str:
        """保留常见上下标的公式语义，再交给富文本解析器处理。"""
        text = str(value or "")
        text = re.sub(r"<sub\b[^>]*>(.*?)</sub>", r"_{\1}", text, flags=re.I | re.S)
        text = re.sub(r"<sup\b[^>]*>(.*?)</sup>", r"^{\1}", text, flags=re.I | re.S)
        return text

    def append_text(text: str, style: str = "text") -> None:
        normalized = normalize_formula_text(text).strip()
        if not normalized:
            return
        if blocks and blocks[-1].get("type") == "text" and blocks[-1].get("style") == style:
            blocks[-1]["text"] += "\n" + normalized
        else:
            blocks.append({"type": "text", "style": style, "text": normalized})

    def append_rich(value: Any, section_title: str = "") -> None:
        if section_title:
            append_text(section_title, "heading")
        pending: list[str] = []
        for kind, value in rich_events(prepare_direct_html(value)):
            if kind == "text":
                pending.append(value)
            elif kind == "break":
                text = "".join(pending).strip()
                if text:
                    append_text(text)
                pending.clear()
            elif kind == "image":
                text = "".join(pending).strip()
                if text:
                    append_text(text)
                pending.clear()
                source = str(value or "").strip()
                key = source
                if key and key not in seen_sources:
                    seen_sources.add(key)
                    placeholder = {"type": "image-pending", "source": source}
                    image_tasks.append((placeholder, source, "题目配图", blocks))
                    blocks.append(placeholder)
        text = "".join(pending).strip()
        if text:
            append_text(text)

    append_text(str(item.get("title") or f"第 {index} 题"), "title")
    if options.get("include_meta", True):
        meta = "  |  ".join(filter(None, [
            str(item.get("subject") or ""), str(item.get("grade") or ""),
            str(item.get("question_type") or ""), f"难度 {item.get('difficulty', 3)}/5",
            " · ".join(item.get("tags") or []),
        ]))
        if meta:
            append_text(meta, "meta")
    append_rich(item.get("question"), "题目")
    for source in item.get("image_urls") or []:
        source = str(source or "").strip()
        if source and source not in seen_sources:
            seen_sources.add(source)
            placeholder = {"type": "image-pending", "source": source}
            image_tasks.append((placeholder, source, "题目图片", blocks))
            blocks.append(placeholder)
    if options.get("include_my_answer", True) and plain_text(item.get("my_answer")):
        append_rich(item.get("my_answer"), "原作答")
    if options.get("answer_mode") == "inline":
        append_rich(item.get("answer"), "答案")
        if options.get("include_analysis", True):
            append_rich(item.get("analysis"), "解析")
    elif options.get("answer_mode") == "separate":
        main_blocks = blocks
        blocks = answer_blocks
        append_rich(item.get("answer"), "答案")
        if options.get("include_analysis", True):
            append_rich(item.get("analysis"), "解析")
        blocks = main_blocks
    if options.get("include_note", True):
        append_rich(item.get("note"), "复盘笔记")

    for placeholder, source, caption, target_blocks in image_tasks:
        try:
            data = await _read_ai_source_image(source)
            ai_blocks = await _ai_blocks_for_image(data, caption, str(options.get("ai_model") or ""))
        except HTTPException as exc:
            print(f"[ai-word] ⚠️ 图片读取失败，跳过图片：{exc.detail}")
            ai_blocks = []
        try:
            block_index = target_blocks.index(placeholder)
        except ValueError:
            continue
        target_blocks[block_index:block_index + 1] = ai_blocks
    return {
        "title": str(item.get("title") or f"第 {index} 题"),
        "blocks": blocks, "answer_blocks": answer_blocks, "mixed": True,
    }


@app.post("/api/question-regions")
async def api_question_regions(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(400, "原图过大（最大 50MB）")
    data, image_info = optimize_image(data)
    result = await asyncio.to_thread(detect_question_regions_bytes, data)
    print(f"[regions] ✅ {file.filename} -> {len(result.get('regions') or [])} boxes via {result.get('detector')}")
    return {"success": True, "image": image_info, **result}


@app.post("/api/question-regions/ai")
async def api_question_regions_ai(file: UploadFile = File(...), model: str = Form("")):
    """并行运行 Gemini 与本地 OCR，优先返回最先成功的切题结果。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(400, "原图过大（最大 50MB）")
    data, image_info = optimize_image(data, max_side=1800, target_bytes=1_500_000)
    ai_data = data
    ai_width, ai_height = image_info["width"], image_info["height"]
    ai_rotated = ai_width > ai_height * 1.12
    if ai_rotated:
        with Image.open(io.BytesIO(data)) as source:
            upright = source.transpose(Image.Transpose.ROTATE_270)
            output = io.BytesIO()
            upright.save(output, format="JPEG", quality=86, optimize=True, progressive=True)
            ai_data = output.getvalue()
            ai_width, ai_height = upright.size
    async def run_ai() -> tuple[str, dict | None, HTTPException | None]:
        try:
            result = await detect_question_regions_ai(ai_data, ai_width, ai_height, model)
            result["rotated"] = ai_rotated
            return "ai", result, None
        except HTTPException as exc:
            return "ai", None, exc

    async def run_ocr() -> tuple[str, dict | None, HTTPException | None]:
        try:
            return "ocr", await asyncio.to_thread(detect_question_regions_bytes, data), None
        except HTTPException as exc:
            return "ocr", None, exc

    tasks = {asyncio.create_task(run_ai()), asyncio.create_task(run_ocr())}
    errors: dict[str, HTTPException] = {}
    while tasks:
        done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        outcomes = [task.result() for task in done]
        outcomes.sort(key=lambda outcome: 0 if outcome[0] == "ai" else 1)
        for detector, result, error in outcomes:
            if error:
                errors[detector] = error
                continue
            for pending in tasks:
                pending.cancel()
            if detector == "ai":
                print(f"[regions-ai] ✅ {file.filename} -> {len(result['regions'])} boxes via {result.get('detector') or CLIPROXY_MODEL}")
            else:
                ai_error = errors.get("ai")
                if ai_error:
                    result["warnings"] = [
                        f"AI 拆题暂不可用，已使用本地 OCR：{ai_error.detail}",
                        *(result.get("warnings") or []),
                    ]
                    result["detector"] = "本地 OCR（AI 回退）"
                else:
                    result["detector"] = "本地 OCR（快速结果）"
                print(f"[regions-ai] ⚡ {file.filename} -> {len(result['regions'])} boxes via {result['detector']}")
            return {"success": True, "image": image_info, **result}
    preferred = errors.get("ai") or errors.get("ocr")
    raise preferred or HTTPException(502, "AI 与本地 OCR 均未返回结果")


# ---- 普通拍题 ----
class SearchReq(BaseModel):
    fileId: str

@app.post("/api/search")
async def api_search(req: SearchReq):
    require_auth()
    async with httpx.AsyncClient(timeout=35) as c:
        j = await _post_json(c, f"{BASE_BP}/question/api/analysisQuestion", common_headers(),
                             {"cloudFileId": req.fileId}, timeout=35)
    data = j.get("data") or {}
    for q in data.get("questions") or []:
        q["questionContent"] = rewrite_images(q.get("questionContent") or "")
        q["questionAnswer"] = rewrite_images(q.get("questionAnswer") or "")
        q["questionAnalysis"] = rewrite_images(q.get("questionAnalysis") or "")
    return {"success": True, "data": data}

# ---- AI 解原题（SSE） ----
class SolveReq(BaseModel):
    fileIds: List[str]
    names: Optional[List[str]] = None
    mode: str = "single"
    enableModelThinking: bool = False

@app.post("/api/solve")
async def api_solve(req: SolveReq):
    require_auth()
    if not req.fileIds: raise HTTPException(400, "请先上传图片")
    if req.mode == "across" and len(req.fileIds) < 2: raise HTTPException(400, "跨页模式需要 2 张图片")
    names = req.names or [f"img{i + 1}.jpg" for i in range(len(req.fileIds))]
    file_list = [{"fileId": fid, "name": names[i]} for i, fid in enumerate(req.fileIds)]
    prompt = "MCLOUD_VLM_SOLVE_PROBLEM_ACROSS_PAGE" if req.mode == "across" else "MCLOUD_VLM_SOLVE_PROBLEM"
    import time as _t
    payload = {
        "userId": config.get("userDomainId") or "",
        "applicationType": "chat", "applicationId": "", "sourceChannel": SOLVE_CHANNEL,
        "dialogueInput": {
            "dialogue": "帮我解题", "prompt": prompt, "inputTime": int(_t.time() * 1000),
            "extInfo": None, "enableForceLlm": False, "enableForceNetworkSearch": False,
            "enableModelThinking": req.enableModelThinking, "enableAllNetworkSearch": False,
            "enableKnowledgeAndNetworkSearch": False, "enableRegenerate": False,
            "versionInfo": {"h5Version": "2.2.0"},
            "command": {"command": "036", "subCommand": "036004"},
            "toolSetting": {"imageToolSetting": {"enableLlmDescribe": True}},
            "attachment": {"attachmentTypeList": [3], "fileList": file_list},
        },
    }
    headers = chat_headers()
    async def gen():
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", f"{BASE_AI_CHAT}/assistant/chat/v2/add",
                                json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="ignore")[:500]
                    yield f"event: error\ndata: {json.dumps({'error': body}, ensure_ascii=False)}\n\n"
                    return
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        yield line + "\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

# ---- 停止生成 ----
class StopReq(BaseModel):
    sessionId: str
    dialogueId: str

@app.post("/api/solve/stop")
async def api_stop(req: StopReq):
    require_auth()
    payload = {"userId": config.get("userDomainId") or "", "sourceChannel": SOLVE_CHANNEL,
               "sessionId": req.sessionId, "dialogueId": req.dialogueId}
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            return await _post_json(c, f"{BASE_AI_CHAT}/assistant/chat/stop", chat_headers(), payload)
        except HTTPException as e:
            return {"success": False, "message": str(e.detail)}

# ---- 学科解析 / 视频 / 举一反三 / 权益 ----
@app.post("/api/analyze")
async def api_analyze(req: SearchReq):
    require_auth()
    async with httpx.AsyncClient(timeout=120) as c:
        j = await _post_json(c, f"{BASE_BP}/question/xueke/xuekeAnalysisQuestion", common_headers(),
                             {"cloudFileId": req.fileId}, timeout=120)
    return {"success": True, "data": j.get("data")}

class VideoReq(BaseModel):
    questionId: str
    channel: int = 0
    videoId: str = ""

@app.post("/api/video")
async def api_video(req: VideoReq):
    require_auth()
    payload = {"questionId": req.questionId, "channel": req.channel}
    if req.videoId:
        payload["videoId"] = req.videoId
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{BASE_BP}/question/api/parseVideo", headers=common_headers(),
                         json=payload, timeout=60)
        j = r.json()
    return {"success": j.get("success"), "message": j.get("message", ""), "data": j.get("data")}

class ExtraReq(BaseModel):
    questionId: str
    cloudFileId: str = ""

@app.post("/api/extrapolate")
async def api_extrapolate(req: ExtraReq):
    require_auth()
    payload = {"questionId": req.questionId, "consumeType": 1}
    if req.cloudFileId: payload["cloudFileId"] = req.cloudFileId
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{BASE_BP}/question/api/extrapolateQuestionV2", headers=common_headers(),
                         json=payload, timeout=120)
        j = r.json()
    return {"success": j.get("success"), "code": j.get("code"), "message": j.get("message"), "data": j.get("data")}

@app.get("/api/benefits")
async def api_benefits():
    require_auth()
    account = {"userDomainId": config.get("userDomainId") or ""}
    async with httpx.AsyncClient(timeout=60) as c:
        j1 = await _post_json(c, f"{BASE_MEMBER}/queryUserBenefits", member_headers(),
                              {"account": account, "isNeedBenefit": 0}, timeout=60)
        members = [{"memberLevel": m.get("memberLevel"), "memberLvName": m.get("memberLvName")}
                   for m in j1.get("userSubMemberList") or []]
        j2 = await _post_json(c, f"{BASE_MEMBER}/queryAvailableBenefitV2", member_headers(),
                              {"account": account}, timeout=60)
        benefits = []
        for b in j2.get("userAvailableBenefitList") or []:
            name = b.get("benefitName") or ""
            if b.get("benefitNo") in ("RHR078", "RHR124", "RHR106") or "AI" in name or "错题" in name:
                benefits.append({"benefitNo": b.get("benefitNo"), "name": name,
                                 "value": b.get("benefitValue"), "used": b.get("benefitUsed")})
    return {"success": True, "members": members, "benefits": benefits[:20]}

# ----------------------------- 错题本 -----------------------------
class MistakeReq(BaseModel):
    title: str = ""
    question: str = ""
    answer: str = ""
    analysis: str = ""
    my_answer: str = ""
    subject: str = "未分类"
    grade: str = ""
    source: str = ""
    question_type: str = ""
    difficulty: int = 3
    mastery: int = 0
    error_type: str = "知识盲区"
    tags: List[str] = Field(default_factory=list)
    knowledge_points: List[str] = Field(default_factory=list)
    image_urls: List[str] = Field(default_factory=list)
    is_starred: bool = False
    note: str = ""
    notebook: str = "默认错题本"
    status: str = "active"
    next_review_at: Optional[str] = None

class MistakePatch(BaseModel):
    title: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    analysis: Optional[str] = None
    my_answer: Optional[str] = None
    subject: Optional[str] = None
    grade: Optional[str] = None
    source: Optional[str] = None
    question_type: Optional[str] = None
    difficulty: Optional[int] = None
    mastery: Optional[int] = None
    error_type: Optional[str] = None
    tags: Optional[List[str]] = None
    knowledge_points: Optional[List[str]] = None
    image_urls: Optional[List[str]] = None
    is_starred: Optional[bool] = None
    note: Optional[str] = None
    notebook: Optional[str] = None
    status: Optional[str] = None
    next_review_at: Optional[str] = None

class BatchReq(BaseModel):
    ids: List[int]
    action: str
    value: Any = None

class ReviewReq(BaseModel):
    rating: int
    note: str = ""

class ExportReq(BaseModel):
    ids: List[int] = Field(default_factory=list)
    filters: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)


@app.get("/api/mistakes")
async def mistakes_list(
    q: str = "", subject: str = "", notebook: str = "", error_type: str = "",
    starred: Optional[bool] = None, status: str = "active", due: bool = False,
    sort: str = "updated_desc", limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
):
    return {"success": True, **storage.list_mistakes(
        query=q, subject=subject, notebook=notebook, error_type=error_type,
        starred=starred, status=status, due=due, sort=sort, limit=limit, offset=offset,
    )}


@app.post("/api/mistakes")
async def mistakes_create(req: MistakeReq):
    if not req.question.strip() and not req.image_urls:
        raise HTTPException(400, "题目内容和题目图片不能同时为空")
    item = storage.create_mistake(req.model_dump())
    return {"success": True, "item": item}


@app.get("/api/mistakes/{item_id}")
async def mistakes_get(item_id: int):
    item = storage.get_mistake(item_id)
    if not item:
        raise HTTPException(404, "错题不存在")
    return {"success": True, "item": item, "reviews": storage.review_history(item_id)}


@app.patch("/api/mistakes/{item_id}")
async def mistakes_update(item_id: int, req: MistakePatch):
    item = storage.update_mistake(item_id, req.model_dump(exclude_unset=True))
    if not item:
        raise HTTPException(404, "错题不存在")
    return {"success": True, "item": item}


@app.delete("/api/mistakes/{item_id}")
async def mistakes_delete(item_id: int):
    if not storage.delete_mistake(item_id):
        raise HTTPException(404, "错题不存在")
    return {"success": True}


@app.post("/api/mistakes/batch")
async def mistakes_batch(req: BatchReq):
    try:
        count = storage.batch_update(req.ids, req.action, req.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"success": True, "count": count}


@app.post("/api/mistakes/{item_id}/review")
async def mistakes_review(item_id: int, req: ReviewReq):
    item = storage.record_review(item_id, req.rating, req.note)
    if not item:
        raise HTTPException(404, "错题不存在")
    return {"success": True, "item": item}


@app.get("/api/dashboard")
async def dashboard():
    return {"success": True, **storage.dashboard()}


@app.get("/api/options")
async def options():
    return {"success": True, **storage.distinct_options()}


@app.post("/api/media")
async def upload_local_media(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(400, "原图过大（最大 50MB）")
    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(400, "仅支持图片")
    data, image_info = optimize_image(data)
    name = f"{datetime.now():%Y%m}/{uuid.uuid4().hex}.jpg"
    target = storage.MEDIA_DIR / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"success": True, "url": f"/media/{name}", "name": file.filename,
            "size": len(data), "image": image_info}


def _export_items(req: ExportReq) -> list[dict]:
    if req.ids:
        return [item for item_id in req.ids if (item := storage.get_mistake(item_id))]
    filters = req.filters or {}
    return storage.list_mistakes(
        query=str(filters.get("q", "")), subject=str(filters.get("subject", "")),
        notebook=str(filters.get("notebook", "")), error_type=str(filters.get("error_type", "")),
        starred=filters.get("starred"), status=str(filters.get("status", "active")),
        due=bool(filters.get("due", False)), limit=500,
    )["items"]


def _safe_remote_image_url(url: str) -> bool:
    """导出只读取外部 HTTP 图片，拒绝把错题 HTML 当作本机网络探针。"""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
            return False
        try:
            address = ipaddress.ip_address(parsed.hostname)
            return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)
        except ValueError:
            return True
    except ValueError:
        return False


async def _layout_items_with_139_images(items: list[dict]) -> list[dict]:
    """下载 139 题干、答案和解析内的图片；本机拍摄图不会参与。"""
    rich_fields = ("question", "answer", "analysis", "my_answer", "note")
    sources = list(dict.fromkeys(
        source for item in items for field in rich_fields for source in question_image_sources(item.get(field))
        if _safe_remote_image_url(source)
    ))
    cache: dict[str, Image.Image | None] = {}
    semaphore = asyncio.Semaphore(6)

    async def fetch_image(client: httpx.AsyncClient, url: str) -> None:
        try:
            async with semaphore:
                response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > 15 * 1024 * 1024:
                raise ValueError("图片超过 15MB")
            with Image.open(io.BytesIO(response.content)) as source:
                if source.width * source.height > 60_000_000:
                    raise ValueError("图片像素过大")
                cache[url] = ImageOps.exif_transpose(source).convert("RGB")
        except Exception as exc:
            cache[url] = None
            print(f"[export] ⚠️ 139 题图下载失败：{url[:120]} ({exc})")

    if sources:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            await asyncio.gather(*(fetch_image(client, source) for source in sources))

    prepared = []
    for item in items:
        current = dict(item)
        current["_remote_images"] = {source: cache.get(source) for field in rich_fields for source in question_image_sources(item.get(field))}
        current["_question_images"] = [cache.get(source) for source in question_image_sources(item.get("question"))]
        prepared.append(current)
    return prepared


def _camscanner_ocr(layout_png: bytes, output_path: Path) -> bytes:
    """把排版整图交给 camscanner-img2word 脚本，并返回真实 docx。"""
    script = CAMSCANNER_SCRIPT
    if not script.exists():
        raise RuntimeError(f"CamScanner OCR 脚本不存在：{script}")
    input_path = output_path.with_suffix(".png")
    input_path.write_bytes(layout_png)
    process_env = os.environ.copy()
    process_env.update({
        "CAMSCANNER_S2": str(config.get("camScannerS2", "")),
        "CAMSCANNER_CSSU": str(config.get("camScannerCssu", "")),
        "CAMSCANNER_CSSTE": str(config.get("camScannerCsste", "")),
    })
    if not all(process_env.get(key) for key in ("CAMSCANNER_S2", "CAMSCANNER_CSSU", "CAMSCANNER_CSSTE")):
        raise RuntimeError("未配置 CamScanner Cookie，请在“AI 与数据”中填入 S2、_cssu、_csste")
    local_proxy = "http://127.0.0.1:7890"
    try:
        with httpx.Client(proxy=local_proxy, timeout=8, follow_redirects=True) as client:
            probe = client.get("https://www.camscanner.com/")
            if probe.status_code < 500:
                process_env.update({"HTTP_PROXY": local_proxy, "HTTPS_PROXY": local_proxy})
                print("[export] ✅ 本机 7890 代理可用，CamScanner OCR 已启用代理")
    except Exception as exc:
        print(f"[export] ℹ️ 本机 7890 代理不可用，CamScanner OCR 使用直连：{exc}")
    result = subprocess.run(
        [sys.executable, str(script), str(input_path), str(output_path)],
        capture_output=True, text=True, check=False, timeout=120, env=process_env,
    )
    if result.returncode != 0 or not output_path.exists():
        detail = (result.stderr or result.stdout or "未知错误").strip()[-800:]
        detail = re.sub(r"(?i)(token=)[^&\s]+", r"\1***", detail)
        detail = re.sub(r"(?i)(S2[=:]\s*)[^\s,;]+", r"\1***", detail)
        raise RuntimeError(f"CamScanner OCR 失败：{detail}")
    content = output_path.read_bytes()
    if len(content) < 1024 or not content.startswith(b"PK"):
        raise RuntimeError("CamScanner 返回的文件不是有效 Word")
    return content


def _export_filename(file_type: str, req: ExportReq) -> str:
    safe_title = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(req.options.get("title") or "我的错题集")).strip("_")
    suffix = "pdf" if file_type == "pdf" else "docx"
    return f"{safe_title or 'mistakes'}_{datetime.now():%Y%m%d_%H%M}.{suffix}"


async def _generate_export(
    file_type: str,
    req: ExportReq,
    items: list[dict],
    progress: Callable[[int, str], None] | None = None,
    input_paths: list[Path] | None = None,
    ai_items: list[dict] | None = None,
) -> tuple[bytes, str, str, str]:
    """生成导出内容；同步排版和 OCR 始终放在线程中，避免阻塞 API。"""
    report = progress or (lambda _value, _message: None)
    fallback = ""
    if file_type == "camscanner":
        report(12, "正在下载 139 题目配图")
        prepared = await _layout_items_with_139_images(items)
        report(35, "正在排版题目、图片和公式")
        layout_png = await asyncio.to_thread(build_layout_image, prepared, req.options)
        report(55, "正在进行 CamScanner OCR")
        try:
            async with CAMSCANNER_WORKER:
                with tempfile.TemporaryDirectory(prefix="mistake-camscanner-") as temp_dir:
                    target = Path(temp_dir) / "mistakes.docx"
                    content = await asyncio.to_thread(_camscanner_ocr, layout_png, target)
            media_type, suffix = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
        except Exception as exc:
            print(f"[export] ⚠️ CamScanner OCR 不可用，回退保真版式 Word：{exc}")
            report(80, "OCR 暂不可用，正在生成保真版式 Word")
            content = await asyncio.to_thread(build_visual_docx, layout_png, req.options)
            media_type, suffix, fallback = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx", "visual"
    elif file_type == "docx":
        report(30, "正在生成可编辑 Word")
        content = await asyncio.to_thread(build_docx, items, req.options)
        media_type, suffix = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
    elif file_type == "pdf":
        report(18, "正在下载 139 题目配图")
        prepared = await _layout_items_with_139_images(items)
        report(55, "正在排版并渲染公式")
        content = await asyncio.to_thread(build_pdf, prepared, req.options)
        media_type, suffix = "application/pdf", "pdf"
    elif file_type == "aiword":
        if not input_paths and not ai_items:
            raise HTTPException(400, "没有可供 AI 识别的图片或错题")
        pages = []
        if ai_items:
            total = len(ai_items)
            for index, item in enumerate(ai_items, 1):
                report(8 + round((index - 1) / total * 68), f"正在整理第 {index} / {total} 道错题（文字直排，图片 AI 识别）")
                pages.append(await _build_ai_item_page(item, index, req.options))
                report(8 + round(index / total * 68), f"已完成第 {index} / {total} 道错题的文字与图片排版")
        else:
            total = len(input_paths)
            selected_model = str(req.options.get("ai_model") or "")
            for index, path in enumerate(input_paths, 1):
                report(8 + round((index - 1) / total * 68), f"AI 正在识别第 {index} / {total} 张图片")
                data = await asyncio.to_thread(path.read_bytes)
                try:
                    page = await analyze_ai_word_page(data, selected_model)
                    page["blocks"] = await asyncio.to_thread(crop_ai_image_blocks, data, page["blocks"])
                except Exception as exc:
                    # 上游视觉服务偶发 502/超时或返回畸形 JSON 时，仍然交付可用 Word。
                    # 原图不会丢失，用户可以先下载，待服务恢复后再重新识别。
                    fallback = "ai-fallback-image"
                    detail = _safe_job_error(exc) if isinstance(exc, Exception) else "未知错误"
                    print(f"[ai-word] ⚠️ 第 {index} 张图片 AI 识别失败，保留原图：{detail}")
                    report(8 + round(index / total * 68), f"第 {index} 张图片 AI 暂不可用，已保留原图")
                    page = {
                        "blocks": [{
                            "type": "image", "bbox": [0, 0, 1, 1],
                            "caption": "原图（AI 暂不可用）", "data": data,
                        }],
                    }
                pages.append(page)
        report(82, "正在生成可编辑 Word（文字与裁切图片）")
        content = await asyncio.to_thread(build_ai_docx, pages, req.options)
        media_type, suffix = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
    else:
        raise HTTPException(400, "仅支持 camscanner、docx、pdf 或 aiword")
    report(92, "正在保存导出文件")
    return content, media_type, suffix, fallback


def _update_export_job(job_id: str, progress: int, message: str, **changes: Any) -> None:
    job = EXPORT_JOBS.get(job_id)
    if not job:
        return
    job.update(changes)
    job["progress"] = max(0, min(100, progress))
    job["message"] = message
    job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    job["updated_epoch"] = datetime.now().timestamp()


def _public_export_job(job: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in job.items() if key not in {"path", "input_paths", "input_items", "updated_epoch"}}
    if job["status"] == "completed":
        result["download_url"] = f"/api/export/jobs/{job['id']}/download"
    return result


def _cleanup_export_jobs() -> None:
    cutoff = datetime.now().timestamp() - EXPORT_JOB_TTL_SECONDS
    expired = [job_id for job_id, job in EXPORT_JOBS.items() if job.get("updated_epoch", 0) < cutoff]
    for job_id in expired:
        job = EXPORT_JOBS.pop(job_id)
        path = Path(job.get("path") or "")
        if path.is_file() and path.parent == EXPORT_DIR:
            path.unlink(missing_ok=True)
        for input_path in job.get("input_paths") or []:
            Path(input_path).unlink(missing_ok=True)
    if EXPORT_DIR.is_dir():
        referenced = {Path(job["path"]) for job in EXPORT_JOBS.values() if job.get("path")}
        for path in EXPORT_DIR.iterdir():
            if path.is_file() and path not in referenced and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def _safe_job_error(exc: Exception) -> str:
    detail = str(exc).strip() or "导出失败"
    detail = re.sub(r"(?i)(authorization|token|cookie|S2)([=:]\s*)[^\s,;]+", r"\1\2***", detail)
    return detail[-500:]


async def _run_export_job(
    job_id: str, file_type: str, req: ExportReq, items: list[dict], input_paths: list[Path] | None = None,
    ai_items: list[dict] | None = None,
) -> None:
    try:
        async with EXPORT_WORKERS:
            _update_export_job(job_id, 5, "后台任务已开始", status="running")
            content, media_type, suffix, fallback = await _generate_export(
                file_type, req, items, lambda value, message: _update_export_job(job_id, value, message), input_paths,
                ai_items,
            )
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            output_path = EXPORT_DIR / f"{job_id}.{suffix}"
            await asyncio.to_thread(output_path.write_bytes, content)
            complete_message = "导出完成"
            if fallback == "ai-fallback-image":
                complete_message = "导出完成（AI 暂不可用，已保留原图）"
            elif fallback:
                complete_message = "导出完成（已使用保真回退）"
            _update_export_job(
                job_id, 100, complete_message, status="completed", path=str(output_path),
                media_type=media_type, fallback=fallback,
            )
    except Exception as exc:
        print(f"[export-job] ❌ {job_id}: {exc}")
        _update_export_job(job_id, 100, _safe_job_error(exc), status="failed")
    finally:
        for input_path in input_paths or []:
            input_path.unlink(missing_ok=True)


@app.post("/api/export/jobs/{file_type}", status_code=202)
async def create_export_job(file_type: str, req: ExportReq):
    if file_type not in {"camscanner", "docx", "pdf"}:
        raise HTTPException(400, "仅支持 camscanner、docx 或 pdf")
    _cleanup_export_jobs()
    active_count = sum(job["status"] in {"queued", "running"} for job in EXPORT_JOBS.values())
    if active_count >= EXPORT_JOB_LIMIT:
        raise HTTPException(429, "后台导出任务过多，请稍后再试")
    items = _export_items(req)
    if not items:
        raise HTTPException(400, "没有可导出的错题")
    now = datetime.now().astimezone()
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "type": file_type,
        "status": "queued",
        "progress": 0,
        "message": "已进入后台队列",
        "count": len(items),
        "filename": _export_filename(file_type, req),
        "media_type": "",
        "fallback": "",
        "path": "",
        "created_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "updated_epoch": now.timestamp(),
    }
    EXPORT_JOBS[job_id] = job
    task = asyncio.create_task(_run_export_job(job_id, file_type, req.model_copy(deep=True), items))
    EXPORT_TASKS.add(task)
    task.add_done_callback(EXPORT_TASKS.discard)
    return {"success": True, "job": _public_export_job(job)}


@app.post("/api/ai-word/jobs", status_code=202)
async def create_ai_word_job(
    files: List[UploadFile] = File(default=[]),
    options: str = Form("{}"),
    title: str = Form(""),
    items: str = Form(""),
):
    """提交混合 AI Word 任务：已有文字直排，图片单独交给 AI 识别和裁切。"""
    _cleanup_export_jobs()
    parsed_items: list[dict] = []
    if items.strip():
        try:
            parsed_items = json.loads(items)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "items 不是有效的 JSON") from exc
        if not isinstance(parsed_items, list) or not all(isinstance(item, dict) for item in parsed_items):
            raise HTTPException(400, "items 必须是 JSON 数组")
        parsed_items = parsed_items[:50]
    if not files and not parsed_items:
        raise HTTPException(400, "请至少选择一张图片或一组错题")
    if files and parsed_items:
        raise HTTPException(400, "图片文件和错题数据请二选一")
    if len(files) > AI_WORD_MAX_PAGES:
        raise HTTPException(400, f"一次最多识别 {AI_WORD_MAX_PAGES} 张图片")
    try:
        parsed_options = json.loads(options or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "options 不是有效的 JSON") from exc
    if not isinstance(parsed_options, dict):
        raise HTTPException(400, "options 必须是 JSON 对象")
    if title.strip():
        parsed_options["title"] = title.strip()
    parsed_options.setdefault("title", "AI 识别文档")
    parsed_options.setdefault("answer_mode", "inline")
    parsed_options.setdefault("include_analysis", True)
    parsed_options.setdefault("include_my_answer", True)
    parsed_options.setdefault("include_note", True)
    parsed_options.setdefault("include_meta", True)
    parsed_options["ai_model"] = await _resolve_ai_model(str(parsed_options.get("ai_model") or ""))
    active_count = sum(job["status"] in {"queued", "running"} for job in EXPORT_JOBS.values())
    if active_count >= EXPORT_JOB_LIMIT:
        raise HTTPException(429, "后台导出任务过多，请稍后再试")
    job_id = uuid.uuid4().hex
    input_paths: list[Path] = []
    try:
        for index, file in enumerate(files, 1):
            data = await file.read()
            if not data:
                raise HTTPException(400, f"第 {index} 张图片为空")
            if len(data) > 50 * 1024 * 1024:
                raise HTTPException(400, f"第 {index} 张图片超过 50MB")
            if file.content_type and not file.content_type.startswith("image/"):
                raise HTTPException(400, f"第 {index} 个文件不是图片")
            optimized, _ = optimize_image(data, max_side=1800, target_bytes=2_000_000)
            path = Path(tempfile.gettempdir()) / f"mistake-aiword-{job_id}-{index}.jpg"
            path.write_bytes(optimized)
            input_paths.append(path)
    except HTTPException:
        for path in input_paths:
            path.unlink(missing_ok=True)
        raise
    now = datetime.now().astimezone()
    req = ExportReq(options=parsed_options)
    job_count = len(parsed_items) if parsed_items else len(input_paths)
    job = {
        "id": job_id, "type": "aiword", "status": "queued", "progress": 0,
        "message": "已进入混合 AI Word 队列", "count": job_count,
        "filename": _export_filename("aiword", req),
        "media_type": "", "fallback": "", "path": "",
        "input_paths": [str(path) for path in input_paths],
        "input_items": parsed_items,
        "created_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"), "updated_epoch": now.timestamp(),
    }
    EXPORT_JOBS[job_id] = job
    task = asyncio.create_task(_run_export_job(job_id, "aiword", req, [], input_paths, parsed_items or None))
    EXPORT_TASKS.add(task)
    task.add_done_callback(EXPORT_TASKS.discard)
    return {"success": True, "job": _public_export_job(job)}


@app.get("/api/export/jobs/{job_id}")
async def get_export_job(job_id: str):
    _cleanup_export_jobs()
    job = EXPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "导出任务不存在或已过期")
    return {"success": True, "job": _public_export_job(job)}


@app.get("/api/export/jobs/{job_id}/download")
async def download_export_job(job_id: str):
    _cleanup_export_jobs()
    job = EXPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "导出任务不存在或已过期")
    if job["status"] != "completed":
        raise HTTPException(409, "导出任务尚未完成")
    path = Path(job.get("path") or "")
    if not path.is_file() or path.parent != EXPORT_DIR:
        raise HTTPException(410, "导出文件已失效，请重新生成")
    return FileResponse(
        path, media_type=job["media_type"], filename=job["filename"],
        headers={
            "X-Export-Count": str(job["count"]),
            **({"X-OCR-Fallback": job["fallback"]} if job["fallback"] else {}),
        },
    )


@app.post("/api/export/{file_type}")
async def export_mistakes(file_type: str, req: ExportReq):
    """兼容旧客户端；新版页面使用后台任务接口。"""
    items = _export_items(req)
    if not items:
        raise HTTPException(400, "没有可导出的错题")
    content, media_type, suffix, fallback = await _generate_export(file_type, req, items)
    filename = _export_filename(file_type, req)
    quoted = urllib.parse.quote(filename)
    return Response(content=content, media_type=media_type, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        "X-Export-Count": str(len(items)),
        **({"X-OCR-Fallback": fallback} if fallback else {}),
    })


@app.get("/api/backup")
async def backup_data():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if storage.DB_FILE.exists():
            archive.write(storage.DB_FILE, "data/mistakes.db")
        for path in storage.MEDIA_DIR.rglob("*"):
            if path.is_file():
                archive.write(path, "data/media/" + str(path.relative_to(storage.MEDIA_DIR)))
    return Response(content=buffer.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="mistake-backup-{datetime.now():%Y%m%d}.zip"'
    })

if __name__ == "__main__":
    import uvicorn
    import threading
    import webbrowser

    def tailscale_ipv4() -> str:
        """优先监听 Tailscale 地址，避免把含本地凭证的页面暴露到其他网卡。"""
        try:
            result = subprocess.run(
                ["tailscale", "ip", "-4"], capture_output=True, text=True,
                check=False, timeout=2,
            )
            return next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
        except (FileNotFoundError, subprocess.SubprocessError):
            return ""

    tailscale_ip = tailscale_ipv4()
    HOST = os.environ.get("MISTAKE_BOOK_HOST") or tailscale_ip or "127.0.0.1"
    PORT = int(os.environ.get("MISTAKE_BOOK_PORT", "18666"))
    access_host = tailscale_ip if tailscale_ip and HOST in (tailscale_ip, "0.0.0.0") else "127.0.0.1"
    access_url = f"http://{access_host}:{PORT}"
    print(f"==============================================")
    print(f"  ✅ 知错 AI 错题本已启动：{access_url}")
    print(f"  监听地址：{HOST}:{PORT}")
    if tailscale_ip:
        print(f"  Tailscale 访问：{access_url}")
    print(f"  （浏览器若未自动打开，请手动访问上面地址）")
    print(f"==============================================")
    if os.environ.get("MISTAKE_BOOK_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(access_url)).start()
    uvicorn.run(app, host=HOST, port=PORT)
