# grade_pdf_essay.py
#
# ESSAY pipeline (CSS English Essay) - STRICT RANGE MARKING:
#   - Structure assumption:
#       (1) Outline section first (expected)
#       (2) Essay body is mostly paragraphs; headings/markers may appear
#   - Marking is VERY strict:
#       - Even a very strong essay should land around 38-40/100 max
#   - DO NOT output exact marks, output mark ranges (e.g., "6-8").
#
# Outputs:
#   - JSON: structure + grading + annotations
#   - PDF: report pages + annotated essay pages
#
# Env (.env):
#   Grok_API=...
#   AZURE_ENDPOINT=...
#   AZURE_KEY=...
#
# Usage:
#   python3 grade_pdf_essay.py --pdf Essay.pdf --output-json essay_result.json --output-pdf essay_annotated.pdf

import argparse
import base64
import io
import json
import os
import re
import time
from typing import Any, Dict, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
import fitz  # PyMuPDF
from docx import Document
from PIL import Image, ImageDraw, ImageFont

try:
    from backend.ocr.annotate_pdf_with_essay_rubric import annotate_pdf_essay_pages
except Exception:
    from annotate_pdf_with_essay_rubric import annotate_pdf_essay_pages


# -----------------------------
# Helpers
# -----------------------------

def clean_json_from_llm(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n-", "", text)
        text = re.sub(r"\n-```$", "", text)
    return text.strip()


def _scrub_ocr_mentions(value: Any) -> Any:
    """
    Remove/replace OCR/legibility/scanning references from strings, recursively.
    """
    patterns = [
        r"\bocr\b",
        r"scann\w*",
        r"legibil\w*",
        r"handwriting",
        r"illegible",
        r"blurred",
        r"smudge\w*",
    ]
    if isinstance(value, str):
        cleaned = value
        for pat in patterns:
            cleaned = re.sub(pat, "content", cleaned, flags=re.IGNORECASE)
        # strip extra spaces created by substitutions
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned
    if isinstance(value, list):
        return [_scrub_ocr_mentions(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_ocr_mentions(v) for k, v in value.items()}
    return value


def _load_docx_text(path: str) -> str:
    doc = Document(path)
    parts: List[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            parts.append(t)
    return "\n".join(parts)


def load_environment() -> Tuple[str, DocumentAnalysisClient]:
    load_dotenv()
    grok_key = os.getenv("Grok_API")
    azure_endpoint = os.getenv("AZURE_ENDPOINT")
    azure_key = os.getenv("AZURE_KEY")
    missing = []
    if not grok_key:
        missing.append("Grok_API")
    if not azure_endpoint:
        missing.append("AZURE_ENDPOINT")
    if not azure_key:
        missing.append("AZURE_KEY")
    if missing:
        raise EnvironmentError(
            f"Missing environment variable(s): {', '.join(missing)}. Please set them in your .env file."
        )
    doc_client = DocumentAnalysisClient(endpoint=azure_endpoint, credential=AzureKeyCredential(azure_key))
    return grok_key, doc_client


def validate_input_paths(pdf_path: str, output_json_path: str, output_pdf_path: str) -> None:
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    with open(pdf_path, "rb") as f:
        if f.read(4) != b"%PDF":
            raise ValueError(f"Not a valid PDF: {pdf_path}")

    for outp in [output_json_path, output_pdf_path]:
        d = os.path.dirname(outp)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            with open(outp, "w", encoding="utf-8") as wf:
                wf.write("")
            os.remove(outp)
        except Exception as e:
            raise ValueError(f"Cannot write to {outp}: {e}")

def parse_json_with_repair(
    grok_api_key: str,
    raw_text: str,
    *,
    debug_tag: str = "grok",
    max_fix_attempts: int = 2,
) -> Dict[str, Any]:
    """
    Try strict JSON parse.
    If fails, ask Grok to output valid JSON only (repair mode).
    Also saves raw + repaired outputs for debugging.
    """
    raw_clean = clean_json_from_llm(raw_text)

    os.makedirs("debug_llm", exist_ok=True)
    raw_path = os.path.join("debug_llm", f"{debug_tag}_raw.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(raw_text or "")

    def _extract_json_candidate(text: str) -> str:
        s = (text or "").strip()
        if not s:
            return s
        if s.startswith("{") and s.endswith("}"):
            return s
        if "{" in s and "}" in s:
            start = s.find("{")
            end = s.rfind("}")
            if end > start:
                return s[start : end + 1]
        if re.search(r'"[^"]+"\s*:', s):
            return "{" + s.strip().strip(",") + "}"
        return s

    # 1) direct parse (with light extraction)
    try:
        candidate = _extract_json_candidate(raw_clean)
        return json.loads(candidate)
    except Exception as e:
        err = str(e)

    # 2) repair loop
    last_text = raw_clean
    for attempt in range(1, max_fix_attempts + 1):
        fix_prompt = {
            "role": "user",
            "content": (
                "You previously produced invalid JSON.\n"
                "Fix it and return VALID JSON ONLY. No markdown, no comments, no extra text.\n\n"
                "Rules:\n"
                "- Use double quotes for all keys and strings.\n"
                "- Escape any inner quotes.\n"
                "- No trailing commas.\n"
                "- Output must be a single JSON object.\n\n"
                "Here is the invalid JSON:\n"
                f"{last_text}"
            ),
        }

        data = _grok_chat(
            grok_api_key,
            messages=[{"role": "system", "content": "Return valid JSON only."}, fix_prompt],
            temperature=0.0,
        )
        repaired = data["choices"][0]["message"]["content"]
        repaired_clean = clean_json_from_llm(repaired)

        repaired_path = os.path.join("debug_llm", f"{debug_tag}_repaired_attempt{attempt}.txt")
        with open(repaired_path, "w", encoding="utf-8") as f:
            f.write(repaired)

        try:
            candidate = _extract_json_candidate(repaired_clean)
            return json.loads(candidate)
        except Exception as e:
            last_text = repaired_clean
            err = str(e)

    raise ValueError(
        f"Failed to parse Grok JSON after repair attempts. Last error: {err}. "
        f"See {raw_path} and debug_llm/{debug_tag}_repaired_attempt*.txt"
    )


# -----------------------------
# PDF  Images for Grok
# -----------------------------

def pdf_to_page_images_for_grok(
    pdf_path: str,
    max_pages: Optional[int] = None,
    max_dim: int = 800,
    base64_cap: Optional[int] = None,
    output_dir: str = "grok_images_essay",
    max_total_base64_chars: int = 240_000,
) -> List[Dict[str, Any]]:
    """
    Render PDF pages to JPEG and encode them for Grok.
    Automatically downsizes/lowers quality until the combined base64 payload
    stays under `max_total_base64_chars` to avoid Grok API size/context errors.
    """

    os.makedirs(output_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        total_pages = doc.page_count if max_pages is None else min(doc.page_count, max_pages)
        pil_pages: List[Image.Image] = []
        for idx in range(total_pages):
            pix = doc[idx].get_pixmap(dpi=200)
            pil_pages.append(Image.open(io.BytesIO(pix.tobytes("png"))))
    finally:
        doc.close()

    # Start from the requested max_dim, then progressively reduce size/quality if needed.
    dim_candidates_base = [800, 640, 560, 512, 448, 384, 360, 320]
    dim_candidates = [max_dim] + [d for d in dim_candidates_base if d < max_dim]
    dim_candidates = [d for i, d in enumerate(dim_candidates) if d not in dim_candidates[:i]]
    quality_candidates = [65, 55, 45, 40, 35]

    def _encode_pages(dim: int, quality: int, save_files: bool) -> Tuple[List[Dict[str, Any]], int]:
        encoded_pages: List[Dict[str, Any]] = []
        total_chars = 0
        for idx, pil_img in enumerate(pil_pages):
            img = pil_img.copy()
            img.thumbnail((dim, dim))

            if img.mode in ("RGBA", "LA", "P"):
                rgb = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                rgb.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = rgb
            elif img.mode != "RGB":
                img = img.convert("RGB")

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)

            encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
            truncated = False
            if base64_cap is not None and len(encoded) > base64_cap:
                encoded = encoded[:base64_cap]
                truncated = True

            total_chars += len(encoded)
            file_path = None
            if save_files:
                file_path = os.path.join(output_dir, f"page_{idx+1:03d}.jpg")
                with open(file_path, "wb") as f:
                    f.write(buffer.getvalue())

            encoded_pages.append(
                {"page": idx + 1, "image_base64": encoded, "file_path": file_path, "truncated": truncated}
            )
        return encoded_pages, total_chars

    chosen: Optional[Tuple[List[Dict[str, Any]], int, int, int]] = None
    for dim in dim_candidates:
        for quality in quality_candidates:
            pages_tmp, total_chars = _encode_pages(dim, quality, save_files=False)
            chosen = (pages_tmp, total_chars, dim, quality)
            if max_total_base64_chars and total_chars > max_total_base64_chars:
                continue
            final_pages, final_total = _encode_pages(dim, quality, save_files=True)
            print(
                f"Saved {len(final_pages)} page images to '{output_dir}/' "
                f"(dim={dim}, quality={quality}, total_base64_chars={final_total})"
            )
            return final_pages

    # Fallback to the smallest attempted settings if nothing met the budget.
    if chosen:
        pages_tmp, total_chars, dim, quality = chosen
        final_pages, final_total = _encode_pages(dim, quality, save_files=True)
        print(
            f"Saved {len(final_pages)} page images to '{output_dir}/' "
            f"(dim={dim}, quality={quality}, total_base64_chars={final_total}) [fallback]"
        )
        return final_pages

    return []


def get_report_page_size(
    pdf_path: str,
    dpi: int = 200,
    margin_ratio: float = 0.40,
    min_height: int = 3500,
    fallback: Tuple[int, int] = (2977, 4211),
) -> Tuple[int, int]:
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count == 0:
            return fallback
        pix = doc[0].get_pixmap(dpi=dpi)
        orig_w, orig_h = pix.width, pix.height
        margin = int(orig_w * margin_ratio)
        return (orig_w + 2 * margin, max(orig_h, min_height))
    except Exception:
        return fallback
    finally:
        doc.close()


# -----------------------------
# OCR (Azure Document Intelligence)
# -----------------------------

def _bbox_to_tuples(bbox) -> List[Tuple[int, int]]:
    return [(v.x, v.y) for v in bbox.vertices]


def _paragraph_text(paragraph) -> str:
    words = []
    for word in paragraph.words:
        symbols = "".join(symbol.text for symbol in word.symbols)
        words.append(symbols)
    return " ".join(words).strip()


def _is_noise_text(text: str, bbox: List[Tuple[int, int]], page_w: int, page_h: int) -> bool:
    if not text:
        return True
    if len(text.strip()) <= 2:
        return True
    # If Azure doesn't provide a polygon, keep the text (we can't judge size)
    if not bbox:
        return False
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    if not xs or not ys:
        return False
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    if page_w and page_h:
        rel_w = w / max(1e-6, page_w)
        rel_h = h / max(1e-6, page_h)
        if rel_w < 0.002 or rel_h < 0.002:
            return True
    else:
        if w < 2 or h < 2:
            return True
    return False


def run_ocr_on_pdf(
    doc_client: DocumentAnalysisClient,
    pdf_path: str,
    *,
    workers: int = 3,
    debug_pages_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run Azure OCR page by page to avoid document size limits.
    Each page is rendered to JPEG, optionally resized/compressed on retry if Azure rejects size.
    Runs pages in parallel (workers>1). Saves per-page debug JSON with bboxes if debug_pages_dir is provided.
    """
    def _analyze_image_bytes(img_bytes: bytes) -> Any:
        poller = doc_client.begin_analyze_document("prebuilt-read", document=img_bytes)
        return poller.result()

    def _encode_page_img(pil_img: Image.Image, scale: float, quality: int) -> bytes:
        img = pil_img.copy()
        if scale != 1.0:
            new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            img = img.resize(new_size, Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()

    doc = fitz.open(pdf_path)
    try:
        pil_pages: List[Tuple[int, Image.Image]] = []
        for idx in range(doc.page_count):
            page = doc[idx]
            pix = page.get_pixmap(dpi=180)
            pil_pages.append((idx + 1, Image.open(io.BytesIO(pix.tobytes("png")))))
    finally:
        doc.close()

    if debug_pages_dir:
        os.makedirs(debug_pages_dir, exist_ok=True)

    def _process_page(page_number: int, pil_img: Image.Image) -> Dict[str, Any]:
        result = None
        attempts = [(1.0, 75), (0.85, 70), (0.7, 60)]
        last_err: Optional[Exception] = None
        for scale, quality in attempts:
            try:
                img_bytes = _encode_page_img(pil_img, scale=scale, quality=quality)
                result = _analyze_image_bytes(img_bytes)
                used = {"scale": scale, "quality": quality}
                break
            except HttpResponseError as e:
                last_err = e
                if "InvalidContentLength" in str(e):
                    continue
                raise
        if result is None:
            raise RuntimeError(f"OCR failed on page {page_number}: {last_err}")

        page_w = float(result.pages[0].width or 0.0) if result.pages else float(pil_img.width)
        page_h = float(result.pages[0].height or 0.0) if result.pages else float(pil_img.height)
        page_lines: List[Dict[str, Any]] = []

        for p in result.pages:
            page_words = list(p.words or [])
            for line in p.lines or []:
                text = (line.content or "").strip()
                line_bbox = []
                if line.polygon:
                    line_bbox = [(int(pt.x), int(pt.y)) for pt in line.polygon]
                if _is_noise_text(text, line_bbox, page_w, page_h):
                    continue

                matched_words = []
                if not line.spans:
                    page_lines.append({"text": text, "bbox": line_bbox, "words": []})
                    continue

                for word in page_words:
                    wsp = getattr(word, "span", None)
                    if not wsp:
                        continue
                    for lsp in line.spans:
                        l_start = lsp.offset
                        l_end = l_start + lsp.length
                        w_start = wsp.offset
                        w_end = w_start + wsp.length
                        if w_start >= l_start and w_end <= l_end:
                            w_bbox = []
                            if word.polygon:
                                w_bbox = [(int(pt.x), int(pt.y)) for pt in word.polygon]
                            if _is_noise_text(word.content, w_bbox, page_w, page_h):
                                continue
                            matched_words.append({"text": word.content, "bbox": w_bbox})
                            break
                    else:
                        continue
                    break
                else:
                    for word in page_words:
                        w_bbox = [(int(pt.x), int(pt.y)) for pt in word.polygon] if word.polygon else []
                        if _is_noise_text(word.content, w_bbox, page_w, page_h):
                            continue
                        matched_words.append({"text": word.content, "bbox": w_bbox})

                if matched_words:
                    page_lines.append({"text": text, "bbox": line_bbox, "words": matched_words})

        page_text = " ".join([(ln.content or "").strip() for ln in (p.lines or []) if (ln.content or "").strip()])
        debug_payload = {
            "page_number": page_number,
            "page_width": page_w,
            "page_height": page_h,
            "lines": page_lines,
            "ocr_full_text_page": page_text,
            "attempt": used if result else {},
        }

        return debug_payload

    pages_output: List[Dict[str, Any]] = []
    full_text_parts: List[str] = []

    worker_count = max(1, int(workers or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        futures = {ex.submit(_process_page, num, img): num for num, img in pil_pages}
        for future in as_completed(futures):
            page_number = futures[future]
            data = future.result()
            pages_output.append({"page_number": data["page_number"], "lines": data["lines"]})
            full_text_parts.append(data["ocr_full_text_page"])
            if debug_pages_dir:
                out_path = os.path.join(debug_pages_dir, f"page_{page_number:03d}.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

    pages_output.sort(key=lambda x: x.get("page_number", 0))
    return {"pages": pages_output, "full_text": "\n".join([t for t in full_text_parts if t]).strip()}


# -----------------------------
# Load Rubrics + Report Format
# -----------------------------

def load_essay_rubric_text(path: str) -> str:
    return _load_docx_text(path)


def load_annotations_rubric_text(path: str) -> str:
    return _load_docx_text(path)


def load_report_format_text(path: str) -> str:
    return _load_docx_text(path)


# -----------------------------
# Grok Calls
# -----------------------------

def _grok_chat(
    grok_api_key: str,
    messages: List[Dict[str, str]],
    model: str = "grok-4-fast-reasoning",
    temperature: float = 0.15,
    max_tokens: Optional[int] = None,
    timeout: int = 180,
    max_retries: int = 3,
    backoff_base: float = 1.6,
    backoff_max: float = 20.0,
) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {grok_api_key}"}
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    last_err: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=(30, timeout),
            )
            if resp.status_code >= 300:
                err = RuntimeError(f"Grok API error {resp.status_code}: {resp.text}")
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    last_err = err
                    time.sleep(min(backoff_max, backoff_base ** attempt))
                    continue
                raise err
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            if attempt >= max_retries:
                raise
            time.sleep(min(backoff_max, backoff_base ** attempt))

    raise RuntimeError(f"Grok request failed after retries: {last_err}")



def call_grok_for_essay_structure_paragraphs_only(
    grok_api_key: str,
    ocr_data: Dict[str, Any],
    page_images: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Essay structure for this pipeline:
      - Outline first (expected)
      - Then essay as paragraphs; headings or section markers may appear

    Output schema:
    {
      "topic": "string",
      "outline": {
        "present": true/false,
        "pages": [1],
        "quality": "Weak|Average|Good|Excellent",
        "issues": ["..."],
        "strengths": ["..."]
      },
      "paragraph_map": [
        {"page": 1, "role_guess": "outline|intro|body|conclusion|mixed", "notes": "short"}
      ],
      "overall_flow_comment": "short"
    }
    """
    system = {
        "role": "system",
        "content": (
            "You are an expert CSS English Essay examiner.\n"
            "Essay may include headings or section markers. Do not invent headings; only report if visible.\n"
            "First part is Outline, then Intro/Body/Conclusion as paragraph blocks.\n"
            "Primary truth = page images. OCR is only helper; ignore OCR errors and never mention them.\n"
            "When returning the topic/title, use the exact wording written in the answer—no rephrasing or additions.\n"
            "Return JSON only."
        ),
    }

    # lightweight OCR summary
    sanitized_pages = []
    for p in ocr_data.get("pages", []):
        lines = []
        for line in p.get("lines", []):
            lines.append((line.get("text") or ""))
        sanitized_pages.append({"page_number": p.get("page_number"), "lines_preview": lines})

    user_payload = {
        "task": (
            "Detect topic/title, identify outline pages first, and map each page's role "
            "(outline/intro/body/conclusion/mixed) for the essay."
        ),
        "rules": [
            "Do NOT invent headings or sections; only report if visible.",
            "Outline is typically a numbered/roman list or bullet plan early (often page 1).",
            "If outline is missing or weak, say so strongly.",
            "role_guess is best-effort: outline, intro, body, conclusion, mixed.",
            "Ignore OCR errors; do not mention OCR quality, legibility, scanning, handwriting, blurring, or smudging anywhere.",
            "Topic must be verbatim as written in the essay; never expand or paraphrase.",
            "If parts are unreadable, say 'content unclear' without blaming OCR/scan/handwriting.",
        ],
        "ocr_pages_preview": sanitized_pages,
        "ocr_full_text": (ocr_data.get("full_text") or ""),
        "page_images": page_images,
        "output_schema": {
            "topic": "string",
            "outline": {
                "present": True,
                "pages": [1],
                "quality": "Weak",
                "issues": ["..."],
                "strengths": ["..."],
            },
            "paragraph_map": [{"page": 1, "role_guess": "outline", "notes": "short"}],
            "overall_flow_comment": "short",
        },
    }

    data = _grok_chat(
        grok_api_key,
        messages=[system, {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
        temperature=0.1,
    )
    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(clean_json_from_llm(content))
    return _scrub_ocr_mentions(parsed)


def call_grok_for_essay_grading_strict_range(
    grok_api_key: str,
    essay_rubric_text: str,
    report_format_text: str,
    ocr_data: Dict[str, Any],
    structure: Dict[str, Any],
    page_images: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    STRICT range grading:
      - DO NOT output exact marks
      - output "marks_awarded_range": "x-y"
      - keep total ranges very conservative (exceptional rarely above 38-40)
    """

    system = {
        "role": "system",
        "content": (
            "You are a strict CSS English Essay examiner (FPSC style). "
            "You MUST be extremely strict. Even exceptional essays rarely exceed about 38-40/100. "
            "Do NOT treat this as a hard cap; just be conservative. "
            "Return VALID JSON only; no markdown."
        ),
    }

    schema_hint = {
        "topic": "",
        "total_marks": 100,
        "overall_rating": "Weak",
        "criteria": [
            {
                "id": "outline_topic_interpretation",
                "criterion": "Essay Outline & Topic Interpretation/Clarity",
                "marks_allocated": 40,
                "marks_awarded_range": "0-0",
                "rating": "Weak",
                "key_comments": "string",
            },
            {
                "id": "introduction",
                "criterion": "Introduction",
                "marks_allocated": 15,
                "marks_awarded_range": "0-0",
                "rating": "Weak",
                "key_comments": "string",
            },
            {
                "id": "relevance_focus",
                "criterion": "Relevance & Focus (Adherence to Topic)",
                "marks_allocated": 5,
                "marks_awarded_range": "0-0",
                "rating": "Weak",
                "key_comments": "string",
            },
            {
                "id": "content_depth_originality",
                "criterion": "Content Depth & Originality",
                "marks_allocated": 10,
                "marks_awarded_range": "0-0",
                "rating": "Weak",
                "key_comments": "string",
            },
            {
                "id": "argumentation_critical_analysis",
                "criterion": "Argumentation & Critical Analysis",
                "marks_allocated": 10,
                "marks_awarded_range": "0-0",
                "rating": "Weak",
                "key_comments": "string",
            },
            {
                "id": "organization_coherence_transitions",
                "criterion": "Organization, Coherence & Transitions",
                "marks_allocated": 5,
                "marks_awarded_range": "0-0",
                "rating": "Weak",
                "key_comments": "string",
            },
            {
                "id": "expression_grammar_vocab_style",
                "criterion": "Expression, Grammar, Vocabulary & Style",
                "marks_allocated": 10,
                "marks_awarded_range": "0-0",
                "rating": "Weak",
                "key_comments": "string",
            },
            {
                "id": "conclusion_overall_impression",
                "criterion": "Conclusion & Overall Impression",
                "marks_allocated": 5,
                "marks_awarded_range": "0-0",
                "rating": "Weak",
                "key_comments": "string",
            },
        ],
        "total_awarded_range": "0-0",
        "reasons_for_low_score": ["..."],
        "suggested_improvements_for_higher_score_70_plus": ["..."],
        "overall_remarks": "string",
    }

    instructions = (
        "Grade the essay STRICTLY per the provided CSS English Essay rubric.\n"
        "CRITICAL RULES:\n"
        "1) DO NOT output exact marks. Output ranges like \"6-8\".\n"
        "2) Be VERY strict: even the best essay should rarely exceed ~38-40/100.\n"
        "   This is a guideline, not a hard cap.\n"
        "3) Follow these criteria allocations exactly:\n"
        "   - Outline & Topic Interpretation = 40\n"
        "   - Introduction = 15\n"
        "   - Relevance & Focus = 5\n"
        "   - Content Depth & Originality = 10\n"
        "   - Argumentation & Critical Analysis = 10\n"
        "   - Organization/Coherence/Transitions = 5\n"
        "   - Expression/Grammar/Vocabulary/Style = 10\n"
        "   - Conclusion & Overall Impression = 5\n"
        "4) Overall rating MUST be one of: Excellent, Good, Average, Weak.\n"
        "5) Headings or section markers may appear; do NOT assume they are absent.\n"
        "   Judge structure based on outline quality, paragraph flow, and any visible headings.\n"
        "6) total_awarded_range must be consistent with the sum of all ranges and kept conservative.\n"
        "7) Ignore OCR errors completely; do NOT mention OCR quality, misreads, legibility, handwriting, blurring, smudging, or scanning issues in any field.\n"
        "8) Topic must be verbatim from the essay as written; do not rephrase, shorten, or expand it.\n"
        "9) Do NOT call the essay garbage, OCR output, or scanning errors. Evaluate the submission as-is; if writing is incoherent, critique clarity/relevance/logic instead of blaming OCR.\n"
        "Return JSON only matching schema."
    )

    payload = {
        "essay_rubric_text": (essay_rubric_text or ""),
        "report_format_text": (report_format_text or ""),
        "structure_detected": structure,
        "ocr_full_text": (ocr_data.get("full_text") or ""),
        "page_images": page_images,
        "output_schema": schema_hint,
    }

    def _is_valid_grading(data: Dict[str, Any]) -> bool:
        criteria = data.get("criteria")
        if not isinstance(criteria, list) or len(criteria) < 6:
            return False
        if not isinstance(data.get("total_awarded_range"), str):
            return False
        if data.get("topic") is None:
            return False
        if data.get("overall_rating") not in ("Excellent", "Good", "Average", "Weak"):
            return False
        return True

    last_err: Optional[Exception] = None
    for _ in range(2):
        data = _grok_chat(
            grok_api_key,
            messages=[system, {"role": "user", "content": instructions + "\n\nDATA:\n" + json.dumps(payload, ensure_ascii=False)}],
            temperature=0.12,
        )
        content = data["choices"][0]["message"]["content"]
        parsed = parse_json_with_repair(grok_api_key, content, debug_tag="essay_grading")
        if _is_valid_grading(parsed):
            return parsed
        last_err = ValueError("Invalid grading JSON: missing required fields")

    raise ValueError(f"Grok grading output invalid after retries: {last_err}")



def _compact_ocr_page(page: Dict[str, Any]) -> Dict[str, Any]:
    lines = []
    for line in page.get("lines", []):
        line_text = (line.get("text") or "")
        words = [w.get("text", "") for w in (line.get("words") or [])]
        lines.append({"text": line_text, "words": words})
    return {"page_number": page.get("page_number"), "lines": lines}


def _load_partial_annotations(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_partial_annotations(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def call_grok_for_essay_annotations(
    grok_api_key: str,
    annotations_rubric_text: str,
    ocr_data: Dict[str, Any],
    structure: Dict[str, Any],
    grading: Dict[str, Any],
    page_images: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Returns:
    {
      "page_suggestions":[{"page":1,"suggestions":["..."]}],
      "annotations":[ ... ]
    }
    """
    system = {
        "role": "system",
        "content": (
            "You generate pinpoint annotations for handwritten CSS essays.\n"
            "Primary truth = page images; OCR is helper. Ignore OCR errors and never mention them.\n"
            "Return JSON only."
        ),
    }

    schema_hint = {
        "page": 1,
        "page_suggestions": ["..."],
        "annotations": [
            {
                "type": "grammar_language",
                "rubric_point": "Grammar & Language",
                "page": 1,
                "target_word_or_sentence": "string",
                "context_before": "string",
                "context_after": "string",
                "correction": "string",
                "comment": "string",
            }
        ],
    }

    instructions = (
        "Using the ANNOTATIONS RUBRIC, generate actionable annotations for ONE PAGE only.\n"
        "Rules:\n"
        "- Keep annotations SPECIFIC and LOCATABLE.\n"
        "- Prefer 2-5 annotations per page.\n"
        "- target_word_or_sentence must be a short snippet that likely appears in OCR.\n"
        "- Use these types exactly:\n"
        "  outline_quality, introduction_quality, paragraph_flow, factual_accuracy,\n"
        "  grammar_language, repetitiveness, argumentation_depth,\n"
        "  organization_coherence, conclusion_quality, relevance_focus.\n"
        "- Headings or section markers may appear. Do not assume they are absent.\n"
        "- Also create page_suggestions: 2-4 short bullets for this page only.\n"
        "- Ignore OCR errors; never mention OCR quality, legibility, handwriting, blurring, smudging, or scanning issues anywhere.\n"
        "Return JSON only matching the schema."
    )

    os.makedirs("debug_llm", exist_ok=True)
    partial_path = os.path.join("debug_llm", "essay_annotations_partial.json")
    partial = _load_partial_annotations(partial_path)
    annotations: List[Dict[str, Any]] = partial.get("annotations") or []
    page_suggestions: List[Dict[str, Any]] = partial.get("page_suggestions") or []
    errors: List[Dict[str, Any]] = partial.get("errors") or []
    completed_pages = set(partial.get("completed_pages") or [])

    image_by_page = {p.get("page"): p for p in page_images}
    ocr_pages = ocr_data.get("pages", [])

    grading_summary = {
        "overall_rating": grading.get("overall_rating"),
        "total_awarded_range": grading.get("total_awarded_range"),
        "criteria": grading.get("criteria", []),
    }
    structure_summary = {
        "outline": structure.get("outline"),
        "paragraph_map": structure.get("paragraph_map", []),
    }

    for page in ocr_pages:
        page_num = page.get("page_number")
        if not isinstance(page_num, int):
            continue
        if page_num in completed_pages:
            continue

        payload = {
            "annotations_rubric_text": (annotations_rubric_text or ""),
            "grading_summary": grading_summary,
            "structure_detected": structure_summary,
            "ocr_page": _compact_ocr_page(page),
            "ocr_full_text": (ocr_data.get("full_text") or ""),
            "page_image": image_by_page.get(page_num),
            "output_schema": schema_hint,
        }

        try:
            data = _grok_chat(
                grok_api_key,
                messages=[system, {"role": "user", "content": instructions + "\n\nDATA:\n" + json.dumps(payload, ensure_ascii=False)}],
                temperature=0.12,
                timeout=200,
                max_retries=4,
            )
            content = data["choices"][0]["message"]["content"]
            parsed = parse_json_with_repair(grok_api_key, content, debug_tag=f"essay_annotations_p{page_num}")
            if not isinstance(parsed, dict):
                raise ValueError("Annotation JSON is not an object")
            if not isinstance(parsed.get("annotations"), list):
                raise ValueError("Annotation JSON missing annotations list")
            if not isinstance(parsed.get("page_suggestions"), list):
                raise ValueError("Annotation JSON missing page_suggestions list")
        except Exception as e:
            errors.append({"page": page_num, "error": str(e)})
            _save_partial_annotations(partial_path, {
                "annotations": annotations,
                "page_suggestions": page_suggestions,
                "errors": errors,
                "completed_pages": sorted(completed_pages),
            })
            continue

        # Light cleanup per page to keep output consistent
        ann = parsed.get("annotations") or []
        cleaned = []
        for a in ann:
            if not isinstance(a, dict):
                continue
            if not isinstance(a.get("page"), int):
                a["page"] = page_num
            for k in ["type", "rubric_point", "target_word_or_sentence", "context_before", "context_after", "correction", "comment"]:
                if k not in a:
                    a[k] = ""
            cleaned.append(a)

        sugg = parsed.get("page_suggestions") or []
        if not isinstance(sugg, list):
            sugg = []

        annotations.extend(cleaned)
        page_suggestions.append({"page": page_num, "suggestions": sugg})
        completed_pages.add(page_num)

        _save_partial_annotations(partial_path, {
            "annotations": annotations,
            "page_suggestions": page_suggestions,
            "errors": errors,
            "completed_pages": sorted(completed_pages),
        })

    if not annotations and errors:
        raise RuntimeError(f"All annotation requests failed. See {partial_path} for details.")

    return {"annotations": annotations, "page_suggestions": page_suggestions, "errors": errors}



# -----------------------------
# Report Rendering (range-based)
# -----------------------------

def _iter_font_candidates() -> List[str]:
    return [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "DejaVuSans.ttf",
        "arial.ttf",
    ]


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    for fp in _iter_font_candidates():
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return [""]
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_essay_report_pages_range(
    grading: Dict[str, Any],
    page_size: Tuple[int, int] = (2977, 4211),
) -> List[Image.Image]:
    """
    Same table layout as before, but marks column shows RANGE strings.
    Text scales down (10% steps) until everything fits on one page.
    """
    W, H = page_size
    margin = int(W * 0.06)
    base_sizes = {"title": 86, "header": 54, "cell": 42, "small": 40}

    def _scaled_font(size: int, scale: float) -> ImageFont.FreeTypeFont:
        return _get_font(max(8, int(size * scale)))

    def _render(scale: float) -> Tuple[bool, Optional[Image.Image]]:
        title_font = _scaled_font(base_sizes["title"], scale)
        header_font = _scaled_font(base_sizes["header"], scale)
        cell_font = _scaled_font(base_sizes["cell"], scale)
        small_font = _scaled_font(base_sizes["small"], scale)

        col_criterion = int(W * 0.33)
        col_alloc = int(W * 0.12)
        col_award = int(W * 0.14)
        col_rating = int(W * 0.12)
        col_comments = W - margin * 2 - (col_criterion + col_alloc + col_award + col_rating)

        img = Image.new("RGB", (W, H), "white")
        draw = ImageDraw.Draw(img)
        y = margin

        def ensure_space(px_needed: int) -> bool:
            return y + px_needed <= H - margin

        topic = grading.get("topic") or ""
        total_range = grading.get("total_awarded_range") or "0-0"

        if not ensure_space(int(220 * scale)):
            return False, None
        draw.text((margin, y), "Essay Evaluation Report", font=title_font, fill=(0, 0, 0))
        y += int(110 * scale)

        # Wrap topic to keep it on-page
        topic_lines = _wrap_text(draw, f"Topic: {topic}", header_font, W - 2 * margin)
        for ln in topic_lines:
            if not ensure_space(int(70 * scale)):
                return False, None
            draw.text((margin, y), ln, font=header_font, fill=(0, 0, 0))
            y += int(70 * scale)

        draw.text((margin, y), f"Total Marks (Range): {total_range}/100", font=header_font, fill=(0, 0, 0))
        y += int(70 * scale)

        y += int(15 * scale)
        table_x = margin
        table_w = W - 2 * margin
        row_h_base = max(40, int(72 * scale))

        headers = ["Criterion", "Total Marks", "Marks Range", "Rating", "Key Comments"]
        if not ensure_space(row_h_base + int(20 * scale)):
            return False, None
        draw.rectangle([table_x, y, table_x + table_w, y + row_h_base], outline=(0, 0, 0), width=3)

        x = table_x
        splits = [col_criterion, col_alloc, col_award, col_rating, col_comments]
        for i, htxt in enumerate(headers):
            wcol = splits[i]
            draw.text((x + int(10 * scale), y + int(12 * scale)), htxt, font=header_font, fill=(0, 0, 0))
            x += wcol
            if i < len(headers) - 1:
                draw.line([x, y, x, y + row_h_base], fill=(0, 0, 0), width=3)
        y += row_h_base

        crit_list = grading.get("criteria") or []
        for c in crit_list:
            crit = c.get("criterion", "")
            alloc = str(c.get("marks_allocated", ""))
            award_range = str(c.get("marks_awarded_range", "0-0"))
            rating = str(c.get("rating", ""))
            comments = str(c.get("key_comments", ""))

            tmp_img = Image.new("RGB", (10, 10), "white")
            tmp_draw = ImageDraw.Draw(tmp_img)
            comment_lines = _wrap_text(tmp_draw, comments, cell_font, col_comments - int(20 * scale))
            crit_lines = _wrap_text(tmp_draw, crit, cell_font, col_criterion - int(20 * scale))
            lines_needed = max(len(comment_lines), len(crit_lines), 1)
            row_h = max(row_h_base, int(lines_needed * 54 * scale))

            if not ensure_space(row_h + int(10 * scale)):
                return False, None
            draw.rectangle([table_x, y, table_x + table_w, y + row_h], outline=(0, 0, 0), width=2)

            x = table_x
            yy = y + int(12 * scale)
            for ln in crit_lines:
                draw.text((x + int(10 * scale), yy), ln, font=cell_font, fill=(0, 0, 0))
                yy += int(50 * scale)
            x += col_criterion
            draw.line([x, y, x, y + row_h], fill=(0, 0, 0), width=2)

            draw.text((x + int(10 * scale), y + int(12 * scale)), alloc, font=cell_font, fill=(0, 0, 0))
            x += col_alloc
            draw.line([x, y, x, y + row_h], fill=(0, 0, 0), width=2)

            draw.text((x + int(10 * scale), y + int(12 * scale)), award_range, font=cell_font, fill=(0, 0, 0))
            x += col_award
            draw.line([x, y, x, y + row_h], fill=(0, 0, 0), width=2)

            draw.text((x + int(10 * scale), y + int(12 * scale)), rating, font=cell_font, fill=(0, 0, 0))
            x += col_rating
            draw.line([x, y, x, y + row_h], fill=(0, 0, 0), width=2)

            yy = y + int(12 * scale)
            for ln in comment_lines:
                draw.text((x + int(10 * scale), yy), ln, font=cell_font, fill=(0, 0, 0))
                yy += int(50 * scale)

            y += row_h

        if not ensure_space(int(120 * scale)):
            return False, None
        y += int(20 * scale)
        draw.text((margin, y), f"Overall Rating: {grading.get('overall_rating','')}", font=header_font, fill=(0, 0, 0))
        y += int(90 * scale)

        def draw_bullets(title: str, bullets: List[str]) -> bool:
            nonlocal y
            if not ensure_space(int(90 * scale)):
                return False
            draw.text((margin, y), title, font=header_font, fill=(0, 0, 0))
            y += int(60 * scale)
            if not bullets:
                bullets = ["(Not provided)"]
            tmp_img = Image.new("RGB", (10, 10), "white")
            tmp_draw = ImageDraw.Draw(tmp_img)
            for b in bullets:
                wrapped = _wrap_text(tmp_draw, str(b), small_font, W - 2 * margin - int(40 * scale))
                for j, ln in enumerate(wrapped):
                    if not ensure_space(int(55 * scale)):
                        return False
                    prefix = "- " if j == 0 else "  "
                    draw.text((margin + int(25 * scale), y), prefix + ln, font=small_font, fill=(0, 0, 0))
                    y += int(50 * scale)
                if not ensure_space(int(10 * scale)):
                    return False
                y += int(10 * scale)
            return True

        if not draw_bullets("Reasons for Low Score", grading.get("reasons_for_low_score") or []):
            return False, None
        if not draw_bullets("Suggested Improvements for Higher Score (70+)", grading.get("suggested_improvements_for_higher_score_70_plus") or []):
            return False, None

        if not ensure_space(int(140 * scale)):
            return False, None
        draw.text((margin, y), "Overall Remarks:", font=header_font, fill=(0, 0, 0))
        y += int(60 * scale)
        remarks = str(grading.get("overall_remarks", "") or "")
        tmp_img = Image.new("RGB", (10, 10), "white")
        tmp_draw = ImageDraw.Draw(tmp_img)
        rlines = _wrap_text(tmp_draw, remarks, small_font, W - 2 * margin)
        for ln in rlines:
            if not ensure_space(int(55 * scale)):
                return False, None
            draw.text((margin, y), ln, font=small_font, fill=(0, 0, 0))
            y += int(50 * scale)

        return True, img

    scale = 1.0
    min_scale = 0.3
    while scale >= min_scale:
        fits, image = _render(scale)
        if fits and image:
            return [image]
        scale *= 0.9

    raise RuntimeError("Report content too long to fit on one page even after scaling.")



# -----------------------------
# Merge pages into final PDF
# -----------------------------

def pil_images_to_pdf_bytes(pages: List[Image.Image]) -> bytes:
    out = io.BytesIO()
    if not pages:
        return b""
    pages_rgb = [p.convert("RGB") for p in pages]
    pages_rgb[0].save(out, format="PDF", save_all=True, append_images=pages_rgb[1:])
    return out.getvalue()


def merge_report_and_annotated_answer(
    report_pages: List[Image.Image],
    annotated_pages: List[Image.Image],
    output_pdf_path: str,
) -> None:
    report_pdf = pil_images_to_pdf_bytes(report_pages)
    answer_pdf = pil_images_to_pdf_bytes(annotated_pages)

    out_doc = fitz.open()
    if report_pdf:
        rdoc = fitz.open("pdf", report_pdf)
        out_doc.insert_pdf(rdoc)
        rdoc.close()

    if answer_pdf:
        adoc = fitz.open("pdf", answer_pdf)
        out_doc.insert_pdf(adoc)
        adoc.close()

    out_doc.save(output_pdf_path)
    out_doc.close()


# -----------------------------
# Main
# -----------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True, help="Input essay PDF path")
    parser.add_argument("--output-json", default="essay_result.json")
    parser.add_argument("--output-pdf", default="essay_annotated.pdf")
    parser.add_argument("--essay-rubric-docx", default="CSS English Essay Evaluation Rubric Based on FPSC Examiners.docx")
    parser.add_argument("--annotations-rubric-docx", default="ANNOTATIONS RUBRIC FOR ESSAY.docx")
    parser.add_argument("--report-format-docx", default="Report Format.docx")
    parser.add_argument("--ocr-workers", type=int, default=3, help="Parallel Azure OCR workers (pages in flight)")
    parser.add_argument(
        "--debug-ocr-pages-dir",
        default="debug_llm/ocr_pages",
        help="Directory to save per-page OCR debug JSON with bounding boxes (set empty to disable)",
    )
    parser.add_argument(
        "--debug-structure-json",
        default="debug_llm/structure_raw.json",
        help="Optional path to save raw structure result",
    )
    parser.add_argument(
        "--debug-ocr-json",
        default="debug_llm/ocr_full.json",
        help="Optional path to save full OCR output for debugging",
    )
    args = parser.parse_args()

    validate_input_paths(args.pdf, args.output_json, args.output_pdf)

    grok_key, doc_client = load_environment()

    essay_rubric_text = load_essay_rubric_text(args.essay_rubric_docx)
    annotations_rubric_text = load_annotations_rubric_text(args.annotations_rubric_docx)
    report_format_text = load_report_format_text(args.report_format_docx)

    print("Running OCR (Azure Document Intelligence)...")
    ocr_data = run_ocr_on_pdf(
        doc_client,
        args.pdf,
        workers=args.ocr_workers,
        debug_pages_dir=args.debug_ocr_pages_dir or None,
    )
    print("OCR done.")
    if args.debug_ocr_json:
        os.makedirs(os.path.dirname(args.debug_ocr_json), exist_ok=True)
        with open(args.debug_ocr_json, "w", encoding="utf-8") as f:
            f.write(ocr_data.get("full_text", ""))
        print(f"OCR full text saved to {args.debug_ocr_json}")

    page_images = pdf_to_page_images_for_grok(args.pdf)

    print("Calling Grok for structure detection (outline first)...")
    structure = call_grok_for_essay_structure_paragraphs_only(grok_key, ocr_data, page_images)
    print("Structure detected.")
    if args.debug_structure_json:
        os.makedirs(os.path.dirname(args.debug_structure_json), exist_ok=True)
        with open(args.debug_structure_json, "w", encoding="utf-8") as f:
            json.dump(structure, f, ensure_ascii=False, indent=2)
        print(f"Structure saved to {args.debug_structure_json}")

    print("Calling Grok for STRICT range grading...")
    grading = call_grok_for_essay_grading_strict_range(
        grok_key,
        essay_rubric_text=essay_rubric_text,
        report_format_text=report_format_text,
        ocr_data=ocr_data,
        structure=structure,
        page_images=page_images,
    )
    print("Grading done.")

    '''
    print("Calling Grok for annotations...")
    ann_pack = call_grok_for_essay_annotations(
        grok_key,
        annotations_rubric_text=annotations_rubric_text,
        ocr_data=ocr_data,
        structure=structure,
        grading=grading,
        page_images=page_images,
    )'''
    ann_pack = {"annotations": [], "page_suggestions": [], "errors": []}
    
    annotations = ann_pack.get("annotations") or []
    page_suggestions = ann_pack.get("page_suggestions") or []
    ann_errors = ann_pack.get("errors") or []
    print(f"Annotations: {len(annotations)}")

    output = {
        "structure": structure,
        "grading": grading,
        "annotations": annotations,
        "page_suggestions": page_suggestions,
        "annotation_errors": ann_errors,
    }
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"Saved JSON  {args.output_json}")
    
    page_size = get_report_page_size(args.pdf)
    report_pages = render_essay_report_pages_range(grading, page_size=page_size)

    annotated_pages = annotate_pdf_essay_pages(
        pdf_path=args.pdf,
        ocr_data=ocr_data,
        structure=structure,
        grading=grading,
        annotations=annotations,
        page_suggestions=page_suggestions,
    )

    merge_report_and_annotated_answer(report_pages, annotated_pages, args.output_pdf)
    print(f"Saved annotated PDF  {args.output_pdf}")


if __name__ == "__main__":
    main()
