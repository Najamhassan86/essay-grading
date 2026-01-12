# annotate_pdf_with_essay_rubric.py
#
# CSS Essay annotation:
#   - Left margin: per-page improvement suggestions (2-6 bullets)
#   - Inline boxes: for flow/grammar/relevance/etc. using OCR snippet matching

import io
import re
from typing import Any, Dict, List, Tuple, Optional

import fitz  # PyMuPDF
import numpy as np
from PIL import Image
import cv2


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "as", "by", "is",
    "are", "was", "were", "be", "been", "this", "that", "these", "those", "it", "its",
    "at", "from", "but", "not", "so", "if", "then", "than", "also", "into", "about",
}


def _tokenize(text: str) -> List[str]:
    clean = re.sub(r"[^a-z0-9\s]", " ", _normalize(text))
    tokens = [t for t in clean.split() if t and t not in _STOPWORDS and len(t) > 2]
    return tokens


def _bbox_to_rect(bbox: List[Tuple[int, int]], pad: int, w: int, h: int) -> Tuple[int, int, int, int]:
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    x1 = max(0, min(xs) - pad)
    y1 = max(0, min(ys) - pad)
    x2 = min(w - 1, max(xs) + pad)
    y2 = min(h - 1, max(ys) + pad)
    return x1, y1, x2, y2


def _union_bboxes(bboxes: List[List[Tuple[int, int]]], pad: int, w: int, h: int) -> Optional[Tuple[int, int, int, int]]:
    if not bboxes:
        return None
    xs: List[int] = []
    ys: List[int] = []
    for bb in bboxes:
        for (x, y) in bb:
            xs.append(x)
            ys.append(y)
    if not xs or not ys:
        return None
    x1 = max(0, min(xs) - pad)
    y1 = max(0, min(ys) - pad)
    x2 = min(w - 1, max(xs) + pad)
    y2 = min(h - 1, max(ys) + pad)
    return x1, y1, x2, y2


def _find_word_or_line_rect(
    page_ocr: Dict[str, Any],
    target_text: str,
    w: int,
    h: int,
) -> Optional[Tuple[int, int, int, int]]:
    """
    Find snippet in OCR by preferring a single line match rather than unioning
    common tokens across the page.
    """
    target_norm = _normalize(target_text)
    if not target_norm or len(target_norm) < 4:
        return None
    target_tokens = _tokenize(target_text)
    if not target_tokens:
        return None

    target_set = set(target_tokens)
    best_rect = None
    best_score = 0.0

    for line in page_ocr.get("lines", []):
        line_text = _normalize(line.get("text", ""))
        line_words = line.get("words") or []
        if not line_text or not line_words:
            continue

        line_word_tokens = [_normalize(w.get("text", "")) for w in line_words]
        line_tokens = [t for t in line_word_tokens if t and t not in _STOPWORDS and len(t) > 2]
        if not line_tokens:
            continue

        overlap = len(set(line_tokens) & target_set) / max(1, len(target_set))
        score = overlap
        if target_norm in line_text:
            score += 0.6

        # Prefer contiguous token match within a single line
        seq_rect = None
        if len(target_tokens) >= 2:
            for i in range(0, len(line_word_tokens) - len(target_tokens) + 1):
                window = line_word_tokens[i : i + len(target_tokens)]
                if window == target_tokens:
                    seq_boxes = [
                        line_words[j].get("bbox")
                        for j in range(i, i + len(target_tokens))
                        if line_words[j].get("bbox")
                    ]
                    seq_rect = _union_bboxes(seq_boxes, pad=6, w=w, h=h)
                    if seq_rect:
                        break

        candidate = None
        if seq_rect:
            candidate = seq_rect
            score += 0.8
        elif overlap >= 0.4:
            matched_boxes = [
                line_words[i].get("bbox")
                for i, tok in enumerate(line_word_tokens)
                if tok in target_set and line_words[i].get("bbox")
            ]
            if matched_boxes:
                candidate = _union_bboxes(matched_boxes, pad=6, w=w, h=h)
            elif line.get("bbox"):
                candidate = _bbox_to_rect(line["bbox"], pad=6, w=w, h=h)

        if candidate and score > best_score:
            best_score = score
            best_rect = candidate

    return best_rect


def _shift_rect(rect: Tuple[int, int, int, int], x_shift: int, y_shift: int) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = rect
    return (x1 + x_shift, y1 + y_shift, x2 + x_shift, y2 + y_shift)


def _draw_wrapped_text(
    img: np.ndarray,
    x: int,
    y: int,
    text: str,
    font_scale: float,
    thickness: int,
    max_width_px: int,
    color,
    line_gap: int = 8,
) -> int:
    font_face = cv2.FONT_HERSHEY_SIMPLEX
    words = (text or "").split()
    if not words:
        return 0

    lines: List[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        (tw, th), _ = cv2.getTextSize(test, font_face, font_scale, thickness)
        if tw <= max_width_px or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    used = 0
    for ln in lines:
        (tw, th), _ = cv2.getTextSize(ln, font_face, font_scale, thickness)
        cv2.putText(img, ln, (x, y + used + th), font_face, font_scale, color, thickness, cv2.LINE_AA)
        used += th + line_gap
    return used


def _wrap_text_lines(text: str, font_scale: float, thickness: int, max_width_px: int) -> List[str]:
    font_face = cv2.FONT_HERSHEY_SIMPLEX
    words = (text or "").split()
    if not words:
        return []
    lines: List[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        (tw, _), _ = cv2.getTextSize(test, font_face, font_scale, thickness)
        if tw <= max_width_px or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _estimate_text_height(text: str, font_scale: float, thickness: int, max_width_px: int, line_gap: int = 8) -> int:
    lines = _wrap_text_lines(text, font_scale, thickness, max_width_px)
    if not lines:
        return 0
    (_, th), _ = cv2.getTextSize("Ag", cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    return len(lines) * th + (len(lines) - 1) * line_gap


def _draw_connector(img: np.ndarray, rect: Tuple[int, int, int, int], box: Tuple[int, int, int, int], color=(0, 0, 255)):
    x1, y1, x2, y2 = rect
    rx = (x1 + x2) // 2
    ry = (y1 + y2) // 2
    bx1, by1, bx2, by2 = box
    bx = bx2 if rx < bx1 else bx1
    by = (by1 + by2) // 2
    cv2.line(img, (rx, ry), (bx, by), color, 2, cv2.LINE_AA)


def annotate_pdf_essay_pages(
    pdf_path: str,
    ocr_data: Dict[str, Any],
    structure: Dict[str, Any],
    grading: Dict[str, Any],
    annotations: List[Dict[str, Any]],
    page_suggestions: Optional[List[Dict[str, Any]]] = None,
) -> List[Image.Image]:
    page_suggestions = page_suggestions or []

    doc = fitz.open(pdf_path)
    pil_pages: List[Image.Image] = []
    for page in doc:
        pix = page.get_pixmap(dpi=200)
        pil_pages.append(Image.open(io.BytesIO(pix.tobytes("png"))))

    annotated_pages: List[Image.Image] = []
    ocr_pages_by_num: Dict[int, Dict[str, Any]] = {p.get("page_number"): p for p in ocr_data.get("pages", [])}

    suggestions_by_page: Dict[int, List[str]] = {}
    for s in page_suggestions:
        pno = s.get("page")
        sug = s.get("suggestions") or []
        if isinstance(pno, int) and pno >= 1:
            suggestions_by_page[pno] = [str(x) for x in sug if str(x).strip()]

    RED = (0, 0, 255)

    for page_idx, pil_img in enumerate(pil_pages):
        page_number = page_idx + 1
        orig_cv = np.array(pil_img)[:, :, ::-1].copy()
        orig_h, orig_w, _ = orig_cv.shape

        left_width = int(0.40 * orig_w)
        right_width = int(0.40 * orig_w)
        new_w = left_width + orig_w + right_width

        min_page_height = 3500
        h = max(orig_h, min_page_height)
        y_offset = (h - orig_h) // 2
        margin_px = int(0.03 * orig_w)

        canvas = np.full((h, new_w, 3), 255, dtype=np.uint8)
        canvas[y_offset:y_offset + orig_h, left_width:left_width + orig_w] = orig_cv

        cv2.putText(
            canvas,
            f"Page {page_number} - Improvements",
            (margin_px, y_offset + 60),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )

        y_cur = y_offset + 120
        for bullet in suggestions_by_page.get(page_number, [])[:6]:
            bullet_text = str(bullet).strip()
            if not bullet_text:
                continue
            if len(bullet_text) > 180:
                bullet_text = bullet_text[:177] + "..."
            used = _draw_wrapped_text(
                canvas,
                margin_px,
                y_cur,
                "- " + bullet_text,
                font_scale=0.72,
                thickness=2,
                max_width_px=left_width - 2 * margin_px,
                color=(0, 0, 0),
                line_gap=10,
            )
            y_cur += max(used, 55) + 12

        anns = [a for a in annotations if a.get("page") == page_number]
        page_ocr = ocr_pages_by_num.get(page_number, {})

        callouts = []
        for a in anns:
            a_type = (a.get("type") or "").strip()
            rubric_point = (a.get("rubric_point") or "").strip()
            target = (a.get("target_word_or_sentence") or "").strip()
            context_before = (a.get("context_before") or "").strip()
            context_after = (a.get("context_after") or "").strip()
            correction = (a.get("correction") or "").strip()
            comment = (a.get("comment") or "").strip()

            if len(target) < 8:
                target = " ".join([context_before, target, context_after]).strip()
            if len(target) < 8 and comment:
                target = (comment.split(".")[0] or "").strip()
            target = re.sub(r"\s+", " ", target).strip()
            if len(target) > 140:
                target = target[:137] + "..."

            rect = _find_word_or_line_rect(page_ocr, target, orig_w, orig_h) if page_ocr else None
            rect_s = _shift_rect(rect, left_width, y_offset) if rect else None

            # Grammar: inline correction only
            if a_type == "grammar_language":
                if not rect_s:
                    continue
                x1, y1, x2, y2 = rect_s
                cv2.rectangle(canvas, (x1, y1), (x2, y2), RED, 2)
                if correction:
                    _draw_wrapped_text(
                        canvas,
                        x2 + 10,
                        y1,
                        f"-> {correction}",
                        font_scale=0.65,
                        thickness=2,
                        max_width_px=right_width - 2 * margin_px,
                        color=RED,
                    )
                continue

            # Repetition: margin note if not found
            if a_type == "repetitiveness" and not rect_s and comment:
                note_y = min(h - margin_px - 60, y_cur + 20)
                note = f"[Repetitiveness] {comment}".strip()
                if len(note) > 160:
                    note = note[:157] + "..."
                _draw_wrapped_text(
                    canvas,
                    margin_px,
                    note_y,
                    note,
                    font_scale=0.68,
                    thickness=2,
                    max_width_px=left_width - 2 * margin_px,
                    color=RED,
                )
                continue

            if rect_s:
                callouts.append({
                    "rect": rect_s,
                    "header": f"[{a_type}] {rubric_point}".strip(),
                    "body": (comment + ("  Suggestion: " + correction if correction and a_type != "grammar_language" else "")).strip(),
                })

        # Place right-side callouts with collision avoidance
        box_w = int(right_width - 2 * margin_px)
        placed_boxes: List[Dict[str, int]] = []
        gap = 12

        for item in sorted(callouts, key=lambda x: x["rect"][1]):
            x1, y1, x2, y2 = item["rect"]
            header = item["header"]
            body = item["body"]

            header_h = _estimate_text_height(header, 0.65, 2, box_w - 24, line_gap=6)
            body_h = _estimate_text_height(body, 0.60, 2, box_w - 24, line_gap=6)
            box_h = max(180, min(360, header_h + body_h + 40))

            bx1 = left_width + orig_w + margin_px
            desired_y = y1 - 20
            by1 = max(margin_px, min(h - box_h - margin_px, desired_y))

            def _overlaps(y_test: int) -> bool:
                for p in placed_boxes:
                    if not (y_test + box_h + gap <= p["y1"] or y_test >= p["y2"] + gap):
                        return True
                return False

            if _overlaps(by1):
                y_scan = by1
                for p in sorted(placed_boxes, key=lambda d: d["y1"]):
                    if y_scan < p["y2"] + gap and y_scan + box_h > p["y1"] - gap:
                        y_scan = p["y2"] + gap
                by1 = min(y_scan, h - box_h - margin_px)

            if _overlaps(by1):
                y_scan = margin_px
                for p in sorted(placed_boxes, key=lambda d: d["y1"]):
                    if y_scan + box_h + gap <= p["y1"]:
                        break
                    y_scan = p["y2"] + gap
                if y_scan + box_h <= h - margin_px:
                    by1 = y_scan
                else:
                    continue

            by2 = by1 + box_h
            bx2 = bx1 + box_w

            cv2.rectangle(canvas, (bx1, by1), (bx2, by2), RED, 2)

            _draw_wrapped_text(
                canvas,
                bx1 + 12,
                by1 + 18,
                header,
                font_scale=0.65,
                thickness=2,
                max_width_px=box_w - 24,
                color=RED,
            )

            _draw_wrapped_text(
                canvas,
                bx1 + 12,
                by1 + 70,
                body,
                font_scale=0.60,
                thickness=2,
                max_width_px=box_w - 24,
                color=(0, 0, 0),
            )

            _draw_connector(canvas, (x1, y1, x2, y2), (bx1, by1, bx2, by2), color=RED)
            placed_boxes.append({"y1": by1, "y2": by2})

        annotated_pages.append(Image.fromarray(canvas[:, :, ::-1]))

    return annotated_pages
