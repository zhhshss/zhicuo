# -*- coding: utf-8 -*-
"""整页试卷切题：RapidOCR 题号锚点 + 选项 A 骨架补齿。"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

NUM_PREFIX = re.compile(r"^(?:第)?\s*([1-9]|1[0-9]|20)\s*(?:[.．。、)）])")
OPTION_A = re.compile(r"^A\s*[.．、]?")
HEADER = re.compile(r"(单项|多项|选择题|填空|解答|计算|证明|^[一二三四五六七八九十]+[、.．]|练习|Wordlist)")
FOOTER = re.compile(r"(?:试卷)?第\s*\d+\s*页.*共\s*\d+\s*页")

_ENGINE = None


def _engine() -> RapidOCR:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOCR()
    return _ENGINE


def _box_xy(box) -> tuple[float, float, float, float]:
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def upright_image(image: np.ndarray) -> tuple[np.ndarray, bool]:
    return image, False


def decode_image(data: bytes) -> np.ndarray | None:
    buffer = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def _run_ocr(image: np.ndarray, max_side: int = 1400) -> list[dict[str, Any]]:
    height, width = image.shape[:2]
    scale = 1.0
    work = image
    if max(height, width) > max_side:
        scale = max_side / max(height, width)
        work = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
    result, _ = _engine()(work)
    items = []
    for row in result or []:
        box, text, score = row[0], str(row[1]).strip(), float(row[2])
        points = [[float(point[0]) / scale, float(point[1]) / scale] for point in box]
        x1, y1, x2, y2 = _box_xy(points)
        items.append({
            "text": text,
            "score": score,
            "points": points,
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "cx": (x1 + x2) / 2,
            "cy": (y1 + y2) / 2,
        })
    items.sort(key=lambda item: (item["cy"], item["cx"]))
    return items


def estimate_deskew_angle(items: list[dict[str, Any]]) -> float:
    """返回浏览器 Canvas 所需的顺时针校正角度；小角度噪声直接忽略。"""
    angles = []
    for item in items:
        points = item.get("points") or []
        if len(points) < 2 or item["score"] < 0.72 or len(re.sub(r"\s+", "", item["text"])) < 4:
            continue
        dx, dy = points[1][0] - points[0][0], points[1][1] - points[0][1]
        if dx <= 0 or abs(dx) < abs(dy) * 2:
            continue
        angle = math.degrees(math.atan2(dy, dx))
        if abs(angle) <= 8:
            angles.append(angle)
    if len(angles) < 4:
        return 0.0
    skew = float(statistics.median(angles))
    # Canvas 正角度是顺时针，OpenCV 正角度是逆时针，两边均用 correction 语义。
    correction = -skew
    return round(correction, 3) if 0.35 <= abs(correction) <= 6 else 0.0


def deskew_image_and_items(image: np.ndarray, items: list[dict[str, Any]], correction: float) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """以固定画布中心纠偏，并把 OCR 四点框映射到同一坐标系。"""
    if not correction:
        return image, items
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), -correction, 1.0)
    deskewed = cv2.warpAffine(image, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    transformed = []
    for item in items:
        points = np.asarray(item.get("points") or [], dtype=np.float64)
        if points.shape != (4, 2):
            transformed.append(item)
            continue
        homogeneous = np.hstack([points, np.ones((4, 1), dtype=np.float64)])
        mapped = homogeneous @ matrix.T
        x1, y1 = mapped.min(axis=0)
        x2, y2 = mapped.max(axis=0)
        transformed.append({
            **item,
            "points": mapped.tolist(),
            "x1": float(x1), "y1": float(y1), "x2": float(x2), "y2": float(y2),
            "cx": float((x1 + x2) / 2), "cy": float((y1 + y2) / 2),
        })
    transformed.sort(key=lambda item: (item["cy"], item["cx"]))
    return deskewed, transformed


def _reliable_top(item: dict[str, Any], height: float) -> float:
    box_h = item["y2"] - item["y1"]
    line = max(height * 0.016, 14.0)
    if box_h > line * 2.8:
        return max(item["y1"], item["cy"] - line * 0.65)
    return item["y1"]


def _extract_number(text: str) -> int | None:
    compact = re.sub(r"\s+", "", text)
    if HEADER.search(compact):
        return None
    match = NUM_PREFIX.match(compact)
    return int(match.group(1)) if match else None


def collect_stems(items: list[dict[str, Any]], width: float, height: float) -> list[dict[str, Any]]:
    found = []
    for item in items:
        if item["cx"] > width * 0.62 or item["score"] < 0.5:
            continue
        number = _extract_number(item["text"])
        if number is None or number > 20:
            continue
        found.append({**item, "n": number, "y": _reliable_top(item, height), "inferred": False})
    found.sort(key=lambda item: (item["y"], item["n"]))
    chosen, used = [], set()
    for item in found:
        if item["n"] in used or (chosen and item["n"] <= chosen[-1]["n"]):
            continue
        chosen.append(item)
        used.add(item["n"])
    return chosen


def collect_option_as(items: list[dict[str, Any]], width: float, height: float) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        if item["cx"] > width * 0.48 or item["score"] < 0.65:
            continue
        compact = re.sub(r"\s+", "", item["text"])
        if HEADER.search(compact):
            continue
        if OPTION_A.match(compact):
            rows.append({**item, "y": _reliable_top(item, height)})
    rows.sort(key=lambda item: item["y"])
    merged = []
    min_gap = height * 0.016
    for item in rows:
        if merged and item["y"] - merged[-1]["y"] < min_gap:
            continue
        merged.append(item)
    return merged


def _typical_stem_gap(stems: list[dict[str, Any]], option_as: list[dict[str, Any]], height: float) -> float:
    gaps = []
    for stem in stems:
        after = [item for item in option_as if item["y"] > stem["y"] + 4]
        if not after:
            continue
        gap = after[0]["y"] - stem["y"]
        if height * 0.006 < gap < height * 0.055:
            gaps.append(gap)
    if gaps:
        return float(statistics.median(gaps))
    return height * 0.024


def assemble_questions(stems: list[dict[str, Any]], option_as: list[dict[str, Any]], height: float) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not stems and not option_as:
        return [], ["no-anchors"]
    typical = _typical_stem_gap(stems, option_as, height)
    typical = min(max(typical, height * 0.012), height * 0.04)
    used_a: set[int] = set()
    stem_a: dict[int, int] = {}
    for index, stem in enumerate(stems):
        next_y = stems[index + 1]["y"] if index + 1 < len(stems) else height
        for opt_index, option in enumerate(option_as):
            if opt_index in used_a:
                continue
            if stem["y"] - height * 0.004 <= option["y"] < next_y:
                stem_a[stem["n"]] = opt_index
                used_a.add(opt_index)
                break
    by_n = {stem["n"]: dict(stem) for stem in stems}
    unused = [option for index, option in enumerate(option_as) if index not in used_a]
    ordered = sorted(stems, key=lambda item: item["n"]) if stems else []
    if not ordered and unused:
        for index, option in enumerate(unused):
            prev_y = 0.0 if index == 0 else by_n[index]["y"]
            start = max(prev_y + height * 0.01, option["y"] - typical)
            by_n[index + 1] = {
                "n": index + 1, "y": start, "text": f"{index + 1}.(inferred)",
                "score": 0.42, "inferred": True, "cx": option["cx"],
            }
        warnings.append("all-from-options")
        return [by_n[key] for key in sorted(by_n)], warnings
    for index in range(len(ordered) - 1):
        current, nxt = ordered[index], ordered[index + 1]
        missing = list(range(current["n"] + 1, nxt["n"]))
        if not missing:
            continue
        between = [option for option in unused if current["y"] < option["y"] < nxt["y"]]
        for miss_index, number in enumerate(missing):
            prev_y = by_n[number - 1]["y"]
            limit = nxt["y"] - height * 0.01
            if miss_index < len(between):
                option = between[miss_index]
                prev_bound = option_as[stem_a[current["n"]]]["y"] if current["n"] in stem_a else prev_y
                gap_prev = max(option["y"] - prev_bound, height * 0.02)
                start = option["y"] - min(typical, max(height * 0.012, 0.38 * gap_prev))
                start = max(start, prev_y + height * 0.018, prev_bound + height * 0.008)
                start = min(start, option["y"] - height * 0.006, limit)
                by_n[number] = {
                    "n": number, "y": start, "text": f"{number}.(inferred)",
                    "score": 0.42, "inferred": True, "cx": option["cx"],
                }
                warnings.append(f"inferred-from-options-{number}")
            else:
                remain = len(missing) - miss_index + 1
                start = prev_y + (nxt["y"] - prev_y) / remain
                start = min(max(start, prev_y + height * 0.02), limit)
                by_n[number] = {
                    "n": number, "y": start, "text": f"{number}.(inferred)",
                    "score": 0.35, "inferred": True, "cx": current.get("cx", 0),
                }
                warnings.append(f"inferred-gap-{number}")
    starts = [by_n[key] for key in sorted(by_n)]
    return _space_starts(starts, option_as, height), warnings


def _space_starts(starts: list[dict[str, Any]], option_as: list[dict[str, Any]], height: float) -> list[dict[str, Any]]:
    """Keep consecutive question starts far enough apart so short inferred items are not slits."""
    if len(starts) < 2:
        return starts
    min_gap = max(height * 0.034, 36.0)
    ys = [float(item["y"]) for item in starts]
    for index in range(1, len(ys)):
        prev_y = ys[index - 1]
        limit = starts[index + 1]["y"] - min_gap if index + 1 < len(ys) else height * 0.96
        needed = prev_y + min_gap
        if ys[index] >= needed:
            continue
        option = next((item for item in option_as if item["y"] > prev_y + height * 0.01), None)
        target = option["y"] - height * 0.01 if option and option["y"] - height * 0.01 > needed else needed
        ys[index] = min(max(target, needed), max(needed, limit))
        starts[index]["y"] = ys[index]
    return starts


def regions_from_starts(starts: list[dict[str, Any]], width: float, height: float, page_bottom: float | None = None) -> list[dict[str, Any]]:
    regions = []
    min_h = max(height * 0.034, 44.0)
    page_bottom = min(height * 0.992, page_bottom if page_bottom is not None else height * 0.992)
    for index, item in enumerate(starts):
        nxt = starts[index + 1]["y"] if index + 1 < len(starts) else page_bottom
        prev = starts[index - 1]["y"] if index else max(0.0, item["y"] - height * 0.05)
        pad_top = min((item["y"] - prev) * 0.08, height * 0.008, 16.0)
        y1 = max(0.0, item["y"] - pad_top)
        y2 = min(height, nxt - max(3.0, height * 0.002))
        if y2 - y1 < min_h:
            y2 = min(height if index + 1 >= len(starts) else nxt - height * 0.002, y1 + min_h)
            if y2 - y1 < min_h and index:
                y1 = max(prev + height * 0.01, y2 - min_h)
        x1, x2 = width * 0.025, width * 0.975
        regions.append({
            "order": index + 1,
            "detectedNumber": item["n"],
            "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            "norm": [round(x1 / width, 4), round(y1 / height, 4), round(x2 / width, 4), round(y2 / height, 4)],
            "score": round(float(item.get("score", 0)), 3),
            "inferred": bool(item.get("inferred")),
            "anchorText": str(item.get("text", ""))[:80],
            "detector": "rapidocr-option-skeleton",
        })
    return regions


def detect_question_regions(image: np.ndarray) -> dict[str, Any]:
    started = time.time()
    image, rotated = upright_image(image)
    height, width = image.shape[:2]
    items = _run_ocr(image)
    deskew_angle = estimate_deskew_angle(items)
    image, items = deskew_image_and_items(image, items, deskew_angle)
    stems = collect_stems(items, width, height)
    option_as = collect_option_as(items, width, height)
    starts, warnings = assemble_questions(stems, option_as, height)
    footer_tops = [item["y1"] for item in items if FOOTER.search(re.sub(r"\s+", "", item["text"]))]
    page_bottom = min(footer_tops) - height * 0.008 if footer_tops else height * 0.992
    regions = regions_from_starts(starts, width, height, page_bottom)
    slit = [region["detectedNumber"] for region in regions if (region["bbox"][3] - region["bbox"][1]) < height * 0.03]
    if slit:
        warnings.append("thin-regions:" + ",".join(str(number) for number in slit))
    return {
        "width": width,
        "height": height,
        "rotated": rotated,
        "deskewAngle": deskew_angle,
        "seconds": round(time.time() - started, 2),
        "ocrCount": len(items),
        "rawNumbers": [item["n"] for item in stems],
        "finalNumbers": [item["n"] for item in starts],
        "warnings": warnings,
        "regions": regions,
        "detector": "rapidocr-option-skeleton",
        "anchors": [{"n": item["n"], "y": round(item["y"], 1), "text": str(item.get("text", ""))[:70], "inferred": bool(item.get("inferred"))} for item in starts],
        "optionA": [round(item["y"], 1) for item in option_as],
    }


def detect_from_bytes(data: bytes) -> dict[str, Any]:
    image = decode_image(data)
    if image is None:
        raise ValueError("unreadable-image")
    return detect_question_regions(image)


def detect_path(path: str | Path) -> dict[str, Any]:
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return {"path": str(path), "error": "unreadable"}
    result = detect_question_regions(image)
    result["path"] = str(path)
    return result


def draw_regions(image: np.ndarray, regions: list[dict[str, Any]], dest: str | Path) -> str:
    canvas = image.copy()
    if canvas.shape[1] > canvas.shape[0] * 1.12:
        canvas = cv2.rotate(canvas, cv2.ROTATE_90_CLOCKWISE)
    colors = [(219, 86, 57), (46, 140, 116), (52, 103, 184), (176, 122, 36),
              (128, 64, 160), (30, 120, 160), (180, 70, 110), (70, 130, 50),
              (90, 90, 90), (20, 90, 160), (150, 80, 40)]
    for region in regions:
        x1, y1, x2, y2 = [int(value) for value in region["bbox"]]
        color = colors[(region["detectedNumber"] - 1) % len(colors)]
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 4)
        label = str(region["detectedNumber"]) + ("~" if region["inferred"] else "")
        cv2.rectangle(canvas, (x1, max(0, y1 - 36)), (x1 + 70, y1), color, -1)
        cv2.putText(canvas, label, (x1 + 8, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    dest.write_bytes(buffer.tobytes())
    return str(dest)


def main() -> None:
    raw_args = list(sys.argv[1:])
    out_path = ""
    if "--out" in raw_args:
        index = raw_args.index("--out")
        if index + 1 >= len(raw_args):
            raise SystemExit("--out needs a path")
        out_path = raw_args[index + 1]
        del raw_args[index:index + 2]
    as_json = "--json" in raw_args
    args = [item for item in raw_args if item != "--json"]
    silent = bool(out_path or as_json)
    if not args:
        raise SystemExit("请提供至少一张图片路径")
    paths = args
    report = []
    viz_dir = Path(os.environ.get("QUESTION_REGIONS_VIZ_DIR", "/tmp/question-regions-viz"))
    for path in paths:
        info = detect_path(path)
        if (not silent) and "error" not in info:
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            info["viz"] = draw_regions(image, info["regions"], viz_dir / (Path(path).stem + "_boxes.jpg"))
        report.append(info)
        if not silent:
            print("====", Path(path).name, info.get("size") or [info.get("width"), info.get("height")], "sec", info.get("seconds"))
            print(" raw", info.get("rawNumbers"), "final", info.get("finalNumbers"))
            print(" warn", info.get("warnings"))
            print(" optionA", info.get("optionA"))
            for region in info.get("regions", []):
                x1, y1, x2, y2 = region["bbox"]
                print(f"  Q{region['detectedNumber']:02d} h={y2-y1:7.1f} ({(y2-y1)/info['height']*100:4.1f}%) y={y1:7.1f}-{y2:7.1f} inf={region['inferred']}")
            print(" viz", info.get("viz"))
    payload = report[0] if len(report) == 1 else report
    if out_path:
        Path(out_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return
    if as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
        return
    out = Path(os.environ.get("QUESTION_REGIONS_REPORT", "/tmp/question-regions.json"))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("WROTE", out)


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    main()
