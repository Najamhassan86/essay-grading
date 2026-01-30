# grade_pdf_answer.py
#
# Simplified pipeline:
#   1) Google Vision OCR for text + bounding boxes.
#   2) Grok Prompt 1: Headings & structure detection (with PDF images).
#   3) Grok Prompt 2: Subject-wise marking (with PDF images + subject rubric DOCX).
#   4) Grok Prompt 3: Refined rubric annotations (with PDF images + refined rubric DOCX).
#   5) Render subject-wise report pages.
#   6) Render refined-rubric summary page.
#   7) Annotate answer pages according to simplified rules.
#   8) Merge all pages into final PDF.

# -------- 1. Standard library (alphabetical) --------
import argparse
import base64
import datetime
import gc
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

# -------- 2. Third-party (alphabetical by package) --------
import cv2
import fitz  # PyMuPDF
import numpy as np
import requests
from docx import Document
from dotenv import load_dotenv
from google.api_core.client_options import ClientOptions
from google.cloud import vision
from PIL import Image, ImageDraw, ImageFont
from PyPDF2 import PdfReader, PdfWriter

# -------- 3. Local (try/except for package vs standalone) --------
try:
    from backend.ocr.annotate_pdf_with_rubric import annotate_pdf_answer_pages
    from backend.ocr.grok_client import (
        GROK_MODEL,
        GROK_REQUEST_TIMEOUT,
        GrokAPIError,
        call_grok_api,
    )
    from backend.ocr.ocr_vision import run_ocr_on_pdf
    from backend.ocr.progress_tracker import OCRProgressTracker
except ImportError:
    from annotate_pdf_with_rubric import annotate_pdf_answer_pages
    from grok_client import (
        GROK_MODEL,
        GROK_REQUEST_TIMEOUT,
        GrokAPIError,
        call_grok_api,
    )
    from ocr_vision import run_ocr_on_pdf
    from progress_tracker import OCRProgressTracker


# -----------------------------
# UTILS & ENV
# -----------------------------

# -----------------------------
# CONSTANTS
# -----------------------------
# GROK_CHAT_URL, GROK_MODEL, GROK_REQUEST_TIMEOUT -> backend.ocr.grok_client
MAX_MARKS_CAP = 14
DEFAULT_MAX_MARKS = 20


def _get_ocr_config() -> Dict[str, Any]:
    """
    Read OCR-related settings from environment variables.
    Used by grade_pdf_answer to pass config into run_ocr_on_pdf.
    All OCR_* env vars and their defaults are documented here.
    """
    def _truthy(s: str) -> bool:
        return (s or "").strip().lower() in ("true", "1", "yes", "on")

    return {
        "per_page_timeout": float(os.getenv("OCR_PER_PAGE_TIMEOUT", "120.0")),
        "overall_timeout": float(os.getenv("OCR_OVERALL_TIMEOUT", "600.0")),
        "max_retries": int(os.getenv("OCR_MAX_RETRIES", "3")),
        "retry_base_delay": float(os.getenv("OCR_RETRY_BASE_DELAY", "1.0")),
        "retry_max_delay": float(os.getenv("OCR_RETRY_MAX_DELAY", "60.0")),
        "retry_jitter_range": float(os.getenv("OCR_RETRY_JITTER_RANGE", "0.2")),
        "rate_limit_base_delay": float(os.getenv("OCR_RATE_LIMIT_BASE_DELAY", "5.0")),
        "rate_limit_max_delay": float(os.getenv("OCR_RATE_LIMIT_MAX_DELAY", "300.0")),
        "concurrent_pages": int(os.getenv("OCR_CONCURRENT_PAGES", "2")),
        "batch_size": int(os.getenv("OCR_BATCH_SIZE", "5")),
        "batch_failure_threshold": float(os.getenv("OCR_BATCH_FAILURE_THRESHOLD", "0.5")),
        "adaptive_concurrency_enabled": _truthy(os.getenv("OCR_ADAPTIVE_CONCURRENCY_ENABLED", "true")),
        "adaptive_min_concurrency": int(os.getenv("OCR_ADAPTIVE_MIN_CONCURRENCY", "1")),
        "adaptive_max_concurrency": int(os.getenv("OCR_ADAPTIVE_MAX_CONCURRENCY", "4")),
        "adaptive_latency_threshold_ms": float(os.getenv("OCR_ADAPTIVE_LATENCY_THRESHOLD_MS", "90000.0")),
        "adaptive_stable_batches": int(os.getenv("OCR_ADAPTIVE_STABLE_BATCHES", "2")),
        "image_optimization_enabled": _truthy(os.getenv("OCR_IMAGE_OPTIMIZATION_ENABLED", "true")),
        "image_max_dimension": int(os.getenv("OCR_IMAGE_MAX_DIMENSION", "2048")),
        "image_min_dimension_for_optimization": int(os.getenv("OCR_IMAGE_MIN_DIMENSION_FOR_OPTIMIZATION", "1500")),
    }


def debug_dump_sections(
    sections: List[Dict[str, Any]],
    output_path: str = "debug_sections.json",
    log_path: Optional[str] = None,
) -> None:
    """
    Save detected headings/sections to a JSON file and print a clean summary.
    Also logs to a text file in the logs folder.

    Each section includes:
      - title
      - level (1 = main heading, 2 = subheading)
      - page_numbers
      - first 200 chars of content (for quick checking)
      - rephrased_heading
    """
    light_sections = []
    for idx, sec in enumerate(sections):
        light_sections.append(
            {
                "index": idx,
                "title": sec.get("title"),
                "exact_ocr_heading": sec.get("exact_ocr_heading"),
                "level": sec.get("level"),
                "page_numbers": sec.get("page_numbers"),
                "content_preview": (sec.get("content") or "")[:200],
                "rephrased_heading": sec.get("rephrased_heading"),  # Add rephrased heading field
            }
        )

    # Always save, even if empty (for debugging purposes)
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(light_sections, f, ensure_ascii=False, indent=2)
        
        # Format output for terminal
        output_lines = []
        output_lines.append("\n" + "=" * 70)
        output_lines.append("DETECTED HEADINGS / SECTIONS (from Grok)")
        output_lines.append("=" * 70)
        
        if not light_sections:
            output_lines.append("⚠️  WARNING: No sections detected! This may cause issues with report generation.")
        else:
            output_lines.append(f"\nTotal sections detected: {len(light_sections)}\n")
            for sec in light_sections:
                output_lines.append(f"Section {sec['index'] + 1}:")
                output_lines.append(f"  Title: {sec['title']}")
                output_lines.append(f"  Exact OCR Heading: {sec['exact_ocr_heading']}")
                output_lines.append(f"  Level: {sec['level']} {'(Main)' if sec['level'] == 1 else '(Sub)'}")
                output_lines.append(f"  Pages: {sec['page_numbers']}")
                if sec.get("rephrased_heading"):
                    rephrased_heading = sec["rephrased_heading"]
                    # Truncate long values for display
                    if len(rephrased_heading) > 150:
                        rephrased_heading = rephrased_heading[:150] + "..."
                    output_lines.append(f"  Rephrased Heading: {rephrased_heading}")
                if sec.get('content_preview'):
                    preview = sec['content_preview']
                    if len(preview) > 100:
                        preview = preview[:100] + "..."
                    output_lines.append(f"  Content Preview: {preview}")
                output_lines.append("")
        
        output_lines.append("=" * 70 + "\n")
        
        # Print to terminal
        for line in output_lines:
            print(line)
        
        # Write to log file if log_path is provided
        if log_path:
            try:
                log_dir = os.path.dirname(log_path) if log_path else None
                if log_dir:
                    sections_log_path = os.path.join(log_dir, "sections_log.txt")
                    # Use datetime.datetime.now() since datetime is imported as a module
                    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with open(sections_log_path, "a", encoding="utf-8") as f:
                        f.write(f"\n{'='*70}\n")
                        f.write(f"Section Detection Log - {timestamp}\n")
                        f.write(f"{'='*70}\n")
                        f.write(f"Total sections: {len(light_sections)}\n")
                        f.write(f"JSON saved to: {output_path}\n\n")
                        for line in output_lines:
                            f.write(line + "\n")
                        f.write(f"\n{'='*70}\n\n")
            except Exception as log_err:
                print(f"WARNING: Failed to write to sections log: {log_err}")
        
    except Exception as e:
        error_msg = f"ERROR: Failed to save debug sections to {output_path}: {e}\n  Sections count: {len(light_sections)}"
        print(error_msg)
        if log_path:
            try:
                log_dir = os.path.dirname(log_path) if log_path else None
                if log_dir:
                    sections_log_path = os.path.join(log_dir, "sections_log.txt")
                    with open(sections_log_path, "a", encoding="utf-8") as f:
                        f.write(f"{error_msg}\n")
            except Exception:
                pass


def _append_log(log_path: Optional[str], level: str, message: str) -> None:
    if not log_path:
        return
    try:
        timestamp = datetime.datetime.utcnow().isoformat() + "Z"
        line = f"{timestamp} [{level}] {message}\n"
        
        # Determine log directory
        log_dir = os.path.dirname(log_path)
        
        # Check if this is an OCR-related log message
        # OCR logs include: upload, steps, timing reports, completion, OCR events
        is_ocr_log = (
            "upload_start" in message or
            "start pdf=" in message or
            " step=" in message or  # Note: space before step= to avoid false matches
            "TIMING_REPORT" in message or
            ("completed" in message and "request=" in message) or
            "report_generated" in message or
            "ocr_" in message or
            "Step " in message  # Timing report steps (e.g., "Step 1: Convert PDF")
        )
        
        # Write to main log file
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
        
        # Write OCR logs to separate OCR log file
        if is_ocr_log:
            ocr_log_path = os.path.join(log_dir, "ocr_log.txt")
            with open(ocr_log_path, "a", encoding="utf-8") as f:
                f.write(line)
        
        # Write errors to separate error log file
        if level == "ERROR":
            error_log_path = os.path.join(log_dir, "errors_log.txt")
            with open(error_log_path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        # Never fail the pipeline due to logging issues.
        pass


def _format_time(seconds: float) -> str:
    """Format time in seconds to 'X min Y sec' format."""
    total_seconds = int(seconds)
    minutes = total_seconds // 60
    secs = total_seconds % 60
    if minutes > 0:
        return f"{minutes} min {secs} sec"
    else:
        return f"{secs} sec"






# -----------------------------
# JSON SCHEMA VALIDATION
# -----------------------------


def validate_refined_summary(summary_list: List[Dict[str, Any]]) -> bool:
    """Validate refined_rubric_summary schema."""
    REQUIRED_KEYS = ["id", "name", "rating", "comment"]
    VALID_RATINGS = {"weak", "average", "good", "excellent"}
    VALID_IDS = {"length_completeness"}  # Only Length & Completeness is required now

    for idx, item in enumerate(summary_list):
        # Check required keys
        missing = set(REQUIRED_KEYS) - set(item.keys())
        if missing:
            raise ValueError(f"refined_rubric_summary[{idx}] missing fields: {missing}")

        # Validate rating
        if item["rating"].lower() not in VALID_RATINGS:
            print(f"WARNING: Invalid rating in summary[{idx}]: {item['rating']} (expected: weak/average/good/excellent)")

        # Validate ID (warn but don't fail)
        if item["id"] not in VALID_IDS:
            print(f"WARNING: Unexpected summary ID in summary[{idx}]: {item['id']}")

    return True


def validate_annotation(annotation: Dict[str, Any], idx: int = 0) -> bool:
    """Validate single annotation schema."""
    REQUIRED_KEYS = ["type", "rubric_point", "page",
                     "target_word_or_sentence", "context_before",
                     "context_after", "correction", "comment"]

    missing = set(REQUIRED_KEYS) - set(annotation.keys())
    if missing:
        print(f"WARNING: Annotation[{idx}] missing fields: {missing}")
        return False

    # Validate page number
    if not isinstance(annotation["page"], int) or annotation["page"] < 1:
        print(f"WARNING: Invalid page number in annotation[{idx}]: {annotation['page']}")
        return False

    return True


def validate_input_paths(pdf_path: str, output_json_path: str, output_pdf_path: str) -> bool:
    """Validate all input/output paths before processing."""
    # Check PDF exists and is readable
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    try:
        with open(pdf_path, 'rb') as f:
            # Check file is not empty and starts with PDF header
            header = f.read(4)
            if header != b'%PDF':
                raise ValueError(f"File is not a valid PDF: {pdf_path}")
    except Exception as e:
        raise ValueError(f"Cannot read PDF {pdf_path}: {e}")

    # Check output paths are writable
    for path in [output_json_path, output_pdf_path]:
        try:
            dirname = os.path.dirname(path)
            if dirname and not os.path.exists(dirname):
                os.makedirs(dirname, exist_ok=True)
            # Test write access
            with open(path, 'w') as f:
                f.write("")
            os.remove(path)
        except Exception as e:
            raise ValueError(f"Cannot write to {path}: {e}")

    return True


def load_environment() -> Tuple[str, vision.ImageAnnotatorClient]:
    """
    Load environment variables for Grok and Google Vision.

    .env must contain:
      Grok_API=YOUR_GROK_KEY
      Google_cloud_key=YOUR_GOOGLE_CLOUD_API_KEY
    """
    load_dotenv()
    grok_key = os.getenv("Grok_API")
    google_key = os.getenv("Google_cloud_key")
    missing = []
    if not grok_key:
        missing.append("Grok_API")
    if not google_key:
        missing.append("Google_cloud_key")
    if missing:
        raise EnvironmentError(
            f"Missing environment variable(s): {', '.join(missing)}. "
            "Please set them in your .env file."
        )

    # Validate API key formats
    if len(grok_key) < 20:
        raise ValueError(
            f"Invalid Grok_API key format: key is too short ({len(grok_key)} characters). "
            "Expected at least 20 characters."
        )
    if len(google_key) < 20:
        raise ValueError(
            f"Invalid Google_cloud_key format: key is too short ({len(google_key)} characters). "
            "Expected at least 20 characters."
        )

    client_options = ClientOptions(api_key=google_key)
    vision_client = vision.ImageAnnotatorClient(client_options=client_options)
    return grok_key, vision_client


# -----------------------------
# PDF → PAGE IMAGES (for Grok)
# -----------------------------


def pdf_to_page_images_for_grok(
    pdf_path: str,
    max_pages: int = 20,
    max_dim: int = 400,
    base64_cap: int = 12000,
    output_dir: str = "grok_images",
) -> List[Dict[str, Any]]:
    """
    Convert up to `max_pages` pages of the PDF into resized JPEG images (for Grok).
    Optimized to reduce token usage: lower DPI, smaller size, JPEG compression.
    Saves images to output_dir for inspection/debugging.
    Returns a list: [{"page": 1, "image_base64": "...", "file_path": "...", "truncated": bool}, ...]
    """
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    doc = fitz.open(pdf_path)
    try:
        page_images: List[Dict[str, Any]] = []
        
        # Process pages one at a time to avoid memory accumulation
        for idx, page in enumerate(doc):
            if idx >= max_pages:
                break
            
            # Load page image
            pix = None
            pil_img = None
            resized = None
            rgb_img = None
            
            try:
                # Check page size first to estimate memory requirements
                # Get page rect in points (1/72 inch), then calculate pixels at target DPI
                page_rect = page.rect
                page_width_pts = page_rect.width
                page_height_pts = page_rect.height
                
                # Calculate expected pixmap size at 200 DPI
                # 1 point = 1/72 inch, so at 200 DPI: pixels = points * (200/72)
                expected_width = int(page_width_pts * (200 / 72))
                expected_height = int(page_height_pts * (200 / 72))
                expected_pixels = expected_width * expected_height
                expected_mb = (expected_pixels * 4) / (1024 * 1024)  # RGBA = 4 bytes per pixel
                
                # Progressive DPI reduction if page is too large
                # Start with 200 DPI, reduce if needed
                target_dpi = 200
                max_safe_mb = 50  # ~50MB per pixmap is reasonable
                
                if expected_mb > max_safe_mb:
                    # Calculate safe DPI: reduce proportionally
                    safe_dpi = int(200 * (max_safe_mb / expected_mb) ** 0.5)
                    # Don't go below 100 DPI (too low quality)
                    target_dpi = max(100, safe_dpi)
                    print(f"WARNING: Page {idx + 1} is very large ({expected_width}x{expected_height} at 200 DPI, "
                          f"~{expected_mb:.1f}MB). Using {target_dpi} DPI instead to prevent MemoryError.")
                
                # Try to get pixmap with progressive DPI reduction
                dpi_options = [target_dpi, 150, 100, 75] if target_dpi < 200 else [200, 150, 100, 75]
                pix = None
                last_error = None
                
                for attempt_dpi in dpi_options:
                    try:
                        pix = page.get_pixmap(dpi=attempt_dpi)
                        if attempt_dpi < 200:
                            print(f"Page {idx + 1}: Successfully created pixmap at {attempt_dpi} DPI (reduced from 200 DPI)")
                        break
                    except Exception as e:
                        last_error = e
                        if attempt_dpi == dpi_options[-1]:
                            # Last attempt failed, re-raise
                            raise RuntimeError(
                                f"Failed to create pixmap for page {idx + 1} even at {attempt_dpi} DPI. "
                                f"Page size: {page_width_pts:.1f}x{page_height_pts:.1f} points. "
                                f"Error: {e}"
                            ) from e
                        # Try next lower DPI
                        continue
                
                if pix is None:
                    raise RuntimeError(f"Failed to create pixmap for page {idx + 1}: {last_error}")
                
                img_bytes = pix.tobytes("png")
                pil_img = Image.open(io.BytesIO(img_bytes))
                
                # Process immediately - don't accumulate in memory
                # Reduced max dimension from 1200 to 800 (44% fewer pixels)
                resized = pil_img.copy()
                resized.thumbnail((max_dim, max_dim))

                # Convert to RGB if necessary (JPEG doesn't support transparency)
                if resized.mode in ('RGBA', 'LA', 'P'):
                    rgb_img = Image.new('RGB', resized.size, (255, 255, 255))
                    if resized.mode == 'P':
                        resized = resized.convert('RGBA')
                    rgb_img.paste(resized, mask=resized.split()[-1] if resized.mode in ('RGBA', 'LA') else None)
                    resized = rgb_img
                elif resized.mode != 'RGB':
                    resized = resized.convert('RGB')

                buffer = io.BytesIO()
                # Changed from PNG to JPEG with 60% quality for reduced payload size
                resized.save(buffer, format="JPEG", quality=60, optimize=True)

                # Save image to disk
                file_path = os.path.join(output_dir, f"page_{idx + 1:03d}.jpg")
                resized.save(file_path, format="JPEG", quality=60, optimize=True)
                
                encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
                truncated = False
                if len(encoded) > base64_cap:
                    encoded = encoded[:base64_cap]
                    truncated = True
                page_images.append(
                    {
                        "page": idx + 1,
                        "image_base64": encoded,
                        "file_path": file_path,
                        "truncated": truncated
                    }
                )
            finally:
                # Explicitly delete and cleanup after each page
                if rgb_img is not None:
                    del rgb_img
                if resized is not None:
                    del resized
                if pil_img is not None:
                    pil_img.close()
                    del pil_img
                if pix is not None:
                    del pix
                # Force garbage collection after each page
                gc.collect()
        
        print(f"Saved {len(page_images)} page images to '{output_dir}/'")
        return page_images
    finally:
        doc.close()  # Always close the document to release file handle


def get_report_page_size(
    pdf_path: str,
    dpi: int = 200,
    margin_ratio: float = 0.40,
    min_height: int = 3000,
    max_width: int = 6000,
    max_height: int = 12000,
    max_pixels: int = 50000000,  # ~50MP limit (e.g., 5000x10000)
    fallback: Tuple[int, int] = (2977, 4211),
) -> Tuple[int, int]:
    """
    Match report page size to annotated answer pages:
    annotated width = orig_w + 2 * (margin_ratio * orig_w), height = orig_h.
    
    This ensures that report pages have the same dimensions as the annotated
    answer pages, creating a consistent document layout.
    
    For very large PDFs, both width and height are capped to prevent
    MemoryError when creating images. The function also checks total pixel count.
    
    Args:
        pdf_path: Path to the PDF file
        dpi: DPI for page size calculation (default: 200, matches OCR processing)
        margin_ratio: Ratio of margin to page width (default: 0.40 = 40%)
        min_height: Minimum page height in pixels (default: 3500)
        max_width: Maximum page width in pixels (default: 6000, ~30 inches at 200 DPI)
        max_height: Maximum page height in pixels (default: 12000, ~60 inches at 200 DPI)
        max_pixels: Maximum total pixels (width * height) to prevent MemoryError (default: 50M)
        fallback: Fallback page size if calculation fails (default: A4 at 200 DPI)
    
    Returns:
        Tuple of (width, height) in pixels at the specified DPI
    
    Example:
        >>> size = get_report_page_size("answer.pdf")
        >>> print(size)  # (4167, 4211) - width with margins, height with minimum
    """
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count == 0:
            return fallback
        
        # Check page size first to estimate memory requirements
        page = doc[0]
        page_rect = page.rect
        page_width_pts = page_rect.width
        page_height_pts = page_rect.height
        
        # Calculate expected pixmap size at target DPI
        expected_width = int(page_width_pts * (dpi / 72))
        expected_height = int(page_height_pts * (dpi / 72))
        expected_pixels = expected_width * expected_height
        expected_mb = (expected_pixels * 4) / (1024 * 1024)  # RGBA = 4 bytes per pixel
        
        # Progressive DPI reduction if page is too large
        target_dpi = dpi
        max_safe_mb = 50  # ~50MB per pixmap is reasonable
        
        if expected_mb > max_safe_mb:
            # Calculate safe DPI: reduce proportionally
            safe_dpi = int(dpi * (max_safe_mb / expected_mb) ** 0.5)
            # Don't go below 100 DPI (too low quality)
            target_dpi = max(100, safe_dpi)
            print(f"WARNING: First page is very large ({expected_width}x{expected_height} at {dpi} DPI, "
                  f"~{expected_mb:.1f}MB). Using {target_dpi} DPI instead to prevent MemoryError.")
        
        # Try to get pixmap with progressive DPI reduction
        dpi_options = [target_dpi, 150, 100, 75] if target_dpi < dpi else [dpi, 150, 100, 75]
        pix = None
        last_error = None
        
        for attempt_dpi in dpi_options:
            try:
                pix = page.get_pixmap(dpi=attempt_dpi)
                if attempt_dpi < dpi:
                    print(f"Successfully created pixmap at {attempt_dpi} DPI (reduced from {dpi} DPI)")
                break
            except Exception as e:
                last_error = e
                if attempt_dpi == dpi_options[-1]:
                    # Last attempt failed, return fallback
                    print(f"WARNING: Failed to create pixmap even at {attempt_dpi} DPI. Using fallback size. Error: {e}")
                    return fallback
                # Try next lower DPI
                continue
        
        if pix is None:
            print(f"WARNING: Failed to create pixmap. Using fallback size. Error: {last_error}")
            return fallback
        
        orig_w, orig_h = pix.width, pix.height
        
        # Clean up pixmap immediately after extracting dimensions
        del pix
        gc.collect()
        
        # Calculate margin and total width
        margin = int(orig_w * margin_ratio)
        total_width = orig_w + 2 * margin
        
        # Cap width at max_width to prevent extremely wide pages
        if total_width > max_width:
            new_orig_w = int(max_width / (1 + 2 * margin_ratio))
            new_margin = int(new_orig_w * margin_ratio)
            total_width = new_orig_w + 2 * new_margin
        
        # Calculate height with minimum
        total_height = max(orig_h, min_height)
        
        # Cap height at max_height to prevent extremely tall pages
        if total_height > max_height:
            total_height = max_height
        
        # Check total pixel count to prevent MemoryError
        # Estimate memory: width * height * 3 bytes (RGB) * safety_factor
        total_pixels = total_width * total_height
        if total_pixels > max_pixels:
            # Scale down proportionally to fit within max_pixels
            scale = (max_pixels / total_pixels) ** 0.5  # Square root to scale both dimensions equally
            total_width = int(total_width * scale)
            total_height = int(total_height * scale)
            print(f"WARNING: Calculated page size ({total_width}x{total_height}) exceeds pixel limit. "
                  f"Scaled down to ({total_width}x{total_height}) to prevent MemoryError.")
        
        # Final safety check: ensure dimensions are reasonable
        if total_width > max_width or total_height > max_height:
            print(f"WARNING: Page size ({total_width}x{total_height}) still exceeds limits. Using fallback size.")
            return fallback
        
        return (total_width, total_height)
    except Exception as e:
        print(f"WARNING: Error calculating report page size: {e}. Using fallback size.")
        return fallback
    finally:
        doc.close()


# OCR WITH GOOGLE VISION -> backend.ocr.ocr_vision (run_ocr_on_pdf and helpers)

# -----------------------------
# RUBRIC DOCX HELPERS
# -----------------------------


def _load_docx_text(path: str) -> str:
    try:
        doc = Document(path)
    except Exception as exc:
        return f"[Error reading DOCX at {path}: {exc}]"
    parts: List[str] = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            parts.append(t)
    return "\n".join(parts)


def _normalize_subject_key(value: str) -> str:
    """
    Convert any subject identifier (dropdown id, folder name, filename) into a
    comparable slug so "Political Science Rubric" and "political-science" match.
    """
    if not value:
        return ""
    key = value.lower().strip()
    key = re.sub(r"[\s_]+", "-", key)
    key = re.sub(r"[^a-z0-9-]", "", key)
    key = re.sub(r"-{2,}", "-", key)
    return key.strip("-")


def _subject_key_variants(value: str) -> Set[str]:
    """
    Generate slug variants for matching; we also accept subjects without the
    trailing "-rubric" suffix because some folders include that word.
    """
    base = _normalize_subject_key(value)
    variants: Set[str] = set()
    if base:
        variants.add(base)
        if base.endswith("-rubric"):
            trimmed = base[: -len("-rubric")].strip("-")
            if trimmed:
                variants.add(trimmed)
    return variants


def _subject_keys_match(candidate: str, target_variants: Set[str]) -> bool:
    candidate_variants = _subject_key_variants(candidate)
    return any(var in target_variants for var in candidate_variants)


def find_subject_rubric_path(subject: str) -> Optional[str]:
    """
    Search under ./20marks_Rubrics for a .docx whose folder or filename matches
    the provided subject id (case/spacing insensitive).
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    rubrics_root = os.path.join(base_dir, "20marks_Rubrics")
    if not os.path.isdir(rubrics_root):
        return None

    target_variants = _subject_key_variants(subject)
    if not target_variants:
        return None

    # Prefer matches where the directory name (subject folder) aligns with the
    # requested id; fall back to filename matches if needed.
    for entry in sorted(os.listdir(rubrics_root)):
        entry_path = os.path.join(rubrics_root, entry)
        if not os.path.isdir(entry_path):
            continue

        docx_files = sorted(
            f for f in os.listdir(entry_path) if f.lower().endswith(".docx")
        )
        if not docx_files:
            continue

        dir_matches = _subject_keys_match(entry, target_variants)
        for fname in docx_files:
            stem = os.path.splitext(fname)[0]
            if dir_matches or _subject_keys_match(stem, target_variants):
                return os.path.join(entry_path, fname)

    # Handle DOCX files that might live directly under the root.
    for fname in sorted(os.listdir(rubrics_root)):
        if not fname.lower().endswith(".docx"):
            continue
        stem = os.path.splitext(fname)[0]
        if _subject_keys_match(stem, target_variants):
            return os.path.join(rubrics_root, fname)

    return None


def load_subject_rubric_text(subject: str) -> Tuple[str, Optional[str]]:
    docx_path = find_subject_rubric_path(subject)
    if not docx_path:
        print(f"WARNING: No subject rubric DOCX found for '{subject}'.")
        return "", None
    text = _load_docx_text(docx_path)
    return text, docx_path


def load_refined_rubric_text() -> Tuple[str, Optional[str]]:
    """
    Load the refined generic rubric DOCX (REFINED RUBRIC (1).docx).
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    refined_path = os.path.join(base_dir, "REFINED RUBRIC.docx")
    if not os.path.isfile(refined_path):
        print("WARNING: Refined rubric DOCX not found.")
        return "", None
    text = _load_docx_text(refined_path)
    return text, refined_path


# GROK API: call_grok_api, GrokAPIError, clean_json_from_llm, repair_json -> backend.ocr.grok_client

# -----------------------------
# GROK CALL 1: SECTION DETECTION
# -----------------------------

def call_grok_for_section_detection(
    grok_api_key: str,
    ocr_data: Dict[str, Any],
    page_images: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Use Grok to detect headings, subheadings and content sections
    from the full OCR text + ALL page images.

    Returns a list of sections in the shape:
      {
        "title": str,
        "level": int,            # 1 = main heading, 2 = subheading
        "page_numbers": [int],
        "content": str,
        "line_indices": []       # kept for compatibility (not used)
      }

    IMPORTANT:
      - Grok is explicitly told to trust the PAGE IMAGES as the primary source
        and only use OCR as a helper.
    """
    system_msg = {
        "role": "system",
        "content": (
            "You are an expert at visually and logically segmenting handwritten exam answers.\n\n"
            "INPUT DATA YOU RECEIVE:\n"
            "- OCR text (approximate, may contain errors)\n"
            "- Per-page OCR text lines (text only, no bounding boxes or coordinates)\n"
            "- Base64-encoded page images of the handwritten script\n\n"
            "PRIMARY RULE:\n"
            "- The PAGE IMAGES are the primary source of truth\n"
            "- OCR text is only a helper for searching or clarifying words\n"
            "- If OCR and image disagree, ALWAYS trust the handwritten image\n\n"
            "YOUR TASK:\n"
            "Segment the answer into logical sections with this structure:\n"
            "Introduction → Main body sections (with optional subsections) → Conclusion\n\n"
            "STRICT REQUIREMENTS FOR SECTION DETECTION:\n\n"
            "1) INTRODUCTION (REQUIRED - MUST BE FIRST):\n"
            "   - If there is an explicit heading like 'Introduction', use that exact text as the title\n"
            "   - If no such heading exists, identify the first paragraph(s) that introduce the topic\n"
            "   - Set title to 'Introduction' for implicit introductions\n"
            "   - For 'exact_ocr_heading': copy the EXACT OCR text of the heading line (or first line if implicit)\n"
            "   - This MUST be the first section in your output\n\n"
            "2) BODY SECTIONS (THOROUGH BUT NON-REPETITIVE DETECTION):\n"
            "   - Be THOROUGH: Identify ALL headings that mark distinct topics or arguments\n"
            "   - Look for these VISUAL CUES in the images (be comprehensive):\n"
            "     • Larger or bolder handwriting (even moderately larger counts)\n"
            "     • Underlined words or phrases (full or partial underlines)\n"
            "     • Extra spacing above and/or below text\n"
            "     • Numbered headings: 1., 2., 3., or i., ii., iii., or (a), (b), (c)\n"
            "     • Short phrases at the start of a line that label content below (even 2+ sentences)\n"
            "     • Words written in ALL CAPS or with emphasis\n"
            "     • Headings that appear centered or indented differently\n"
            "   - For each heading found:\n"
            "     • Create a section with that heading as the title\n"
            "     • Set 'level' = 1 for main topics, 'level' = 2 for subtopics under the previous main heading\n"
            "     • For 'exact_ocr_heading': copy the EXACT OCR text of that heading line (word-for-word, with any typos)\n"
            "     • Include ALL page numbers where this section's content appears\n"
            "   - CRITICAL: AVOID DUPLICATES:\n"
            "     • Before adding a new section, check ALL existing sections\n"
            "     • If a heading is IDENTICAL (exact same text) to an existing one, SKIP it\n"
            "     • If headings are VERY SIMILAR (differ only by 1-2 words, same core meaning), use only the FIRST occurrence\n"
            "     • Examples of duplicates to skip:\n"
            "       - 'Introduction' appears twice → use only first\n"
            "       - 'Montesquieu Theory' and 'Montesquieu's Theory' → use only first\n"
            "       - 'Separation of Powers' and 'Separation of Power' → use only first\n"
            "     • If headings are DISTINCT (different topics), include BOTH even if they share some words\n"
            "     • Example of distinct headings (include both):\n"
            "       - 'Political Context' and 'Economic Context' → both are valid\n"
            "       - 'Montesquieu's Theory' and 'Locke's Theory' → both are valid\n"
            "   - EXPECTED RANGE (guidelines, not strict limits):\n"
            "     • For a 3-5 page answer: Expect 4-8 body sections (excluding intro/conclusion)\n"
            "     • For a 6-10 page answer: Expect 6-12 body sections\n"
            "     • If you find fewer than 3 body sections, you may be missing headings - look more carefully\n"
            "     • If you find more than 15 sections total, reconsider if some are too minor\n"
            "   - If NO clear headings exist between intro and conclusion:\n"
            "     • Create ONE body section titled 'Main Body' or 'Body' with all content\n"
            "     • Do NOT force micro-sections where none exist visually\n\n"
            "3) CONCLUSION (REQUIRED - MUST BE LAST):\n"
            "   - If there is an explicit heading like 'Conclusion' or 'In conclusion', use that text\n"
            "   - If no such heading, identify the final paragraph(s) that summarize/wrap up the answer\n"
            "   - Set title to 'Conclusion' for implicit conclusions\n"
            "   - For 'exact_ocr_heading': copy the EXACT OCR text of the heading line (or last paragraph's first line if implicit)\n"
            "   - This MUST be the last section in your output\n\n"
            "SECTION DEFINITIONS:\n"
            "- A section is a continuous block of content belonging together under one heading/topic\n"
            "- Sections must appear in reading order (top to bottom, page by page)\n"
            "- Sections must NOT overlap - each line belongs to only one section\n"
            "- If you cannot confidently assign some lines, leave them out rather than forcing them\n\n"
            "CONTENT_TEXT REQUIREMENTS:\n"
            "- For each section, provide a concise summary of the student's actual content\n"
            "- Use your own words but stay faithful to what is written\n"
            "- Do NOT invent arguments or facts not present in the script\n"
            "- If handwriting is unclear, infer only what is reasonably supported\n\n"
            "HEADING REPHRASE REQUIREMENT:\n"
            "For each heading/subheading (excluding 'Introduction' and 'Conclusion'), provide:\n"
            "  - 'rephrased_heading': a clear, CSS-style academic heading that functions as a one-line analytical summary of the paragraph.\n"
            "Rules for rephrased_heading:\n"
            "  - Must be fully self-explanatory and understandable without reading the paragraph.\n"
            "  - Should convey the complete essence of the argument made in the paragraph, not merely the topic.\n" 
            "  - Must explicitly reflect cause-and-effect relationships where applicable (i.e., the measure/action and the problem or outcome it addresses).\n"
            "  - All key variables, dimensions, or constraints mentioned in the question statement must be directly addressed or clearly implied in the heading.\n"
            "  - Headings should analytically link solutions, factors, or mechanisms to the specific challenges or objectives identified in the question.\n"
            "  - May be written as a concise sentence if required to preserve analytical clarity.\n"
            "  - Must be directly aligned with the question statement and demonstrate clear relevance.\n"
            "  - Prefer analytical, declarative phrasing over generic or thematic labels.\n"
            "  - Avoid vague constructions such as 'Role of', 'Overview of', 'Impact of', or 'Issues', unless analytically qualified.\n"
            "  - Preserve the original meaning while improving clarity, specificity, and examiner readability.\n"
            "  - Length should be concise but flexible (ideally 8–18 words) to fully capture causal linkage and argument depth.\n"
            "  - If the original heading already meets these standards, rephrased_heading may remain unchanged.\n"
            "For 'Introduction' and 'Conclusion', set rephrased_heading to an empty string.\n\n"
            "Objective:\n"
           "  - Ensure each heading encapsulates a complete causal argument that addresses all variables of the question, enabling an examiner to understand both the problem and the proposed mechanism or outcome at a glance.\n\n"
            "OUTPUT FORMAT (CRITICAL):\n"
            "- Return ONLY valid JSON with structure: {\"sections\": [...]}\n"
            "- NO markdown formatting, NO code blocks, NO explanations\n"
            "- Each section object must have these EXACT fields:\n"
            "  • 'title': Clean, readable heading text\n"
            "  • 'exact_ocr_heading': EXACT OCR text with any typos/errors (used for precise location matching)\n"
            "  • 'level': integer (1 for main, 2 for sub)\n"
            "  • 'page_numbers': array of integers\n"
            "  • 'content_text': string summary\n"
            "  • 'rephrased_heading': improved heading suggestion aligned with rubric (empty for intro/conclusion)\n"
            "- NO extra fields, NO top-level keys besides 'sections'\n\n"
            "CONSISTENCY AND QUALITY:\n"
            "- For the same input, produce consistent segmentation\n"
            "- Avoid random or arbitrary splits\n"
            "- BALANCE: Be thorough in finding headings - it's better to detect more headings than miss important ones\n"
            "- QUALITY: Each heading should mark a distinct topic or argument\n"
            "- DEDUPLICATION: Avoid exact duplicates and very similar headings (same core meaning)\n"
            "- If uncertain whether something is a heading, err on the side of INCLUDING it (you can always merge later)\n"
            "- However, if headings are clearly duplicates (same text or very similar meaning), use only the first occurrence\n"
        ),
    }


    # Sanitize OCR pages: remove any bounding-box or coordinate data before sending
    raw_pages = ocr_data.get("pages", [])
    sanitized_pages = []
    for p in raw_pages:
        lines = []
        for line in p.get("lines", []):
            # Keep only textual content (line text + list of word texts) — drop bbox info
            line_text = line.get("text", "")
            words = [w.get("text", "") for w in line.get("words", [])]
            lines.append({"text": line_text, "words": words})
        sanitized_pages.append({"page_number": p.get("page_number"), "lines": lines})

    user_payload = {
        "task": "Segment this handwritten exam answer into logical sections. Use the page images as primary source and OCR as helper.",
        "ocr_full_text": ocr_data.get("full_text", ""),
        "ocr_pages": sanitized_pages,
        "page_images_base64_png": page_images,
        "output_schema": {
            "sections": [
                {
                    "title": "string",
                    "exact_ocr_heading": "exact text from OCR",
                    "level": 1,
                    "page_numbers": [1],
                    "content_text": "string",
                    "rephrased_heading": "Improved heading suggestion aligned with rubric",
                }
            ]
        },
    }

    user_msg = {
        "role": "user",
        "content": json.dumps(user_payload, ensure_ascii=False),
    }

    payload = {
        "model": GROK_MODEL,
        "messages": [system_msg, user_msg],
        "temperature": 0.1,
        "max_tokens": 4000,  # Sufficient for structure detection
    }

    try:
        parsed, token_usage = call_grok_api(
            grok_api_key,
            payload,
            max_retries=3,
            timeout=120,
            retry_backoff=True,
            use_repair=False,
        )
    except GrokAPIError as e:
        return [
            {
                "title": "Section Detection Error",
                "level": 1,
                "page_numbers": [1],
                "content_text": f"Grok section detection failed: {e}\nRaw: {(e.raw_content or '')[:400]}",
            }
        ], e.token_usage

    # If we get here, parsing succeeded

    raw_sections = parsed.get("sections", []) or []
    
    # Log raw sections before deduplication
    print(f"DEBUG: Raw sections from Grok: {len(raw_sections)}")
    if raw_sections:
        for idx, sec in enumerate(raw_sections):
            print(f"  Raw[{idx}]: '{sec.get('title', 'NO TITLE')}' on pages {sec.get('page_numbers', [])}")

    
    # NEW: Add deduplication logic to prevent repeated headings
    sections: List[Dict[str, Any]] = []
    seen_titles: Set[str] = set()  # Track titles to prevent duplicates
    seen_exact_headings: Set[str] = set()  # Track exact OCR headings
    
    def normalize_for_comparison(text: str) -> str:
        """Normalize text for duplicate detection."""
        if not text:
            return ""
        # Lowercase, remove extra spaces, remove common variations
        normalized = text.lower().strip()
        # Remove common prefixes/suffixes that don't change meaning
        normalized = re.sub(r"^(the|a|an)\s+", "", normalized)
        normalized = re.sub(r"'s$", "", normalized)  # "Montesquieu's" vs "Montesquieu"
        normalized = re.sub(r"\s+", " ", normalized)  # Normalize whitespace
        return normalized
    
    
    
    def _strip_heading_prefix(text: str) -> str:
        """
        Remove leading bullet/number prefixes like '1.', '1)', '(1)', 'a)', '(a)', 'Q1.' etc.
        IMPORTANT: Do NOT eat the first character of normal words like 'Introduction'
        or 'Judicial'. We only strip if the bullet/number is a separate token
        followed by space.
        """
        if not text:
            return ""
        t = text.strip()
        # Patterns where the bullet token is clearly separated from the heading text.
        # NOTE: Some OCR outputs lose the digit/letter before ')' so we can end up with
        # headings like ') Leadership Vacuum ...'. We treat a lone leading ')' + space
        # as a bullet as well.
        patterns = [
            r"^[\(\[]\s*[0-9A-Za-z]{1,3}[\)\].-]\s+",  # (1) Heading, (a) Heading, [1] Heading
            r"^[0-9]{1,3}[\).]\s+",                    # 1. Heading, 2) Heading
            r"^[0-9]{1,3}\s+",                        # 1 Heading
            r"^[A-Za-z]\s+",                          # a Heading
            r"^[\)\]]\s+",                            # ) Heading, ] Heading (OCR leftovers)
        ]
        for pat in patterns:
            new_t = re.sub(pat, "", t)
            if new_t != t:
                t = new_t
                break
        return t.strip()

    # NOTE: Question prefix stripping is implemented in the report renderer
    # (`_render_subject_report_with_scale`) because it's only a display concern.

    # NEW: Enhanced deduplication with cross-page duplicate detection
    for sec in raw_sections:
        raw_title = (sec.get("title") or "UNSPECIFIED").strip()
        raw_exact_heading = (sec.get("exact_ocr_heading") or raw_title).strip()

        # Remove leading bullets/numbering from headings
        title = _strip_heading_prefix(raw_title) or raw_title
        exact_ocr_heading = _strip_heading_prefix(raw_exact_heading) or raw_exact_heading
        level = sec.get("level") or 1
        pages = sec.get("page_numbers") or []
        content_text = sec.get("content_text") or sec.get("content") or ""
        rephrased_heading = (sec.get("rephrased_heading") or "").strip()
        comment = rephrased_heading

        # Normalize for comparison
        title_normalized = normalize_for_comparison(title)
        exact_normalized = normalize_for_comparison(exact_ocr_heading)
        
        # Skip Introduction/Conclusion duplicates (always keep first)
        # Use normalized version for consistency
        title_lower = title.lower().strip()
        if title_lower in ("introduction", "conclusion"):
            # Check both normalized and lowercase to catch all variations
            if title_normalized in seen_titles or title_lower in seen_titles:
                print(f"WARNING: Skipping duplicate {title_lower} section")
                continue
            # Add both normalized and lowercase for intro/conclusion
            seen_titles.add(title_normalized)
            seen_titles.add(title_lower)
            # IMPORTANT: Add section immediately and continue to avoid duplicate check later
            sections.append(
                {
                    "title": title,
                    "title_raw": raw_title,
                    "exact_ocr_heading": exact_ocr_heading,
                    "exact_ocr_heading_raw": raw_exact_heading,
                    "level": int(level) if isinstance(level, (int, float)) else 1,
                    "page_numbers": sorted(
                        set(int(p) for p in pages if isinstance(p, (int, float)))
                    ),
                    "content": content_text,
                    "comment": comment,
                    "rephrased_heading": rephrased_heading,
                    "line_indices": [],
                }
            )
            continue  # Skip all other duplicate checks for intro/conclusion
        
        # Check for cross-page duplicates: if a previous section ends on page N and this one starts on N+1 with same heading
        is_cross_page_duplicate = False
        if pages:
            current_first_page = min(int(p) for p in pages if isinstance(p, (int, float)))
            for existing_sec in sections:
                existing_pages = existing_sec.get("page_numbers", [])
                if not existing_pages:
                    continue
                existing_last_page = max(int(p) for p in existing_pages if isinstance(p, (int, float)))
                
                # Check if this section starts right after an existing section ends
                if current_first_page == existing_last_page + 1:
                    existing_title = existing_sec.get("title", "").strip()
                    existing_title_normalized = normalize_for_comparison(existing_title)
                    
                    # If headings match (normalized), merge them
                    if title_normalized == existing_title_normalized and title_normalized:
                        print(f"WARNING: Merging cross-page duplicate heading '{title}' (page {existing_last_page} -> {current_first_page})")
                        # Merge: update existing section's page_numbers to include new pages
                        merged_pages = sorted(set(existing_pages + pages))
                        existing_sec["page_numbers"] = merged_pages
                        # Merge content if needed (keep longer or combine)
                        existing_content = existing_sec.get("content", "")
                        if len(content_text) > len(existing_content):
                            existing_sec["content"] = content_text
                        is_cross_page_duplicate = True
                        break
        
        if is_cross_page_duplicate:
            continue
        
        # Check for duplicate titles (normalized comparison)
        if title_normalized in seen_titles:
            print(f"WARNING: Skipping duplicate heading (normalized): '{title}' (already seen)")
            continue
        
        # Check for duplicate exact OCR headings (exact match)
        if exact_normalized in seen_exact_headings and exact_normalized:
            print(f"WARNING: Skipping duplicate exact OCR heading: '{exact_ocr_heading}'")
            continue
        
        # Check for very similar headings (fuzzy match)
        is_duplicate = False
        for seen_title in seen_titles:
            seen_normalized = normalize_for_comparison(seen_title)
            if title_normalized and seen_normalized:
                # If normalized versions are very similar (differ by 1-2 words), skip
                title_words = set(title_normalized.split())
                seen_words = set(seen_normalized.split())
                if len(title_words) > 0 and len(seen_words) > 0:
                    overlap_ratio = len(title_words & seen_words) / max(len(title_words), len(seen_words))
                    # If 80%+ overlap and both are short (likely duplicates), skip
                    if overlap_ratio >= 0.8 and len(title_words) <= 5 and len(seen_words) <= 5:
                        print(f"WARNING: Skipping very similar heading: '{title}' (similar to '{seen_title}')")
                        is_duplicate = True
                        break
        
        if is_duplicate:
            continue

        # Add to seen sets
        seen_titles.add(title_normalized)
        if exact_normalized:
            seen_exact_headings.add(exact_normalized)

        sections.append(
            {
                "title": title,
                "title_raw": raw_title,
                "exact_ocr_heading": exact_ocr_heading,  # Store cleaned OCR text
                "exact_ocr_heading_raw": raw_exact_heading,  # Preserve original for reference/annotation
                "level": int(level) if isinstance(level, (int, float)) else 1,
                "page_numbers": sorted(
                    set(int(p) for p in pages if isinstance(p, (int, float)))
                ),
                "content": content_text,
                "comment": comment,  # Backward-compatible display for heading annotations
                "rephrased_heading": rephrased_heading,
                "line_indices": [],  # not used downstream, kept for compatibility
            }
        )

    print(f"Detected {len(sections)} sections after deduplication (from {len(raw_sections)} raw sections)")
    if len(sections) == 0 and len(raw_sections) > 0:
        print(f"⚠️  WARNING: All {len(raw_sections)} raw sections were filtered out during deduplication!")
        print("  This may indicate the deduplication logic is too aggressive.")
        print("  Raw sections that were filtered:")
        for idx, sec in enumerate(raw_sections):
            print(f"    [{idx}] '{sec.get('title', 'NO TITLE')}' on pages {sec.get('page_numbers', [])}")
    
    return sections, token_usage


# -----------------------------
# GROK CALL 2: SUBJECT-WISE MARKING
# -----------------------------


def build_grok_payload_for_grading(
    subject: str,
    subject_rubric_text: str,
    ocr_data: Dict[str, Any],
    sections: List[Dict[str, Any]],
    page_images: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Build payload for Grok subject-wise grading.

    We send:
      - subject,
      - subject rubric text (DOCX),
      - OCR full text,
      - sections structure,
      - page images.
    """
    schema_hint = {
        "subject": subject,
        "max_marks": DEFAULT_MAX_MARKS,
        "total_marks_awarded": 0,
        "question_statement": "",
        "criteria": [
            {
                "id": "knowledge_accuracy",
                "name": "Knowledge & Accuracy",
                "max": 8,
                "awarded": 5,
                "remark": "One-liner critical feedback against this criterion",
            }
        ],
        "high_scoring_outline": {
            "title": "High-Scoring Ideal Outline",
            "outline_points": [
                {
                    "heading": "Section Title",
                    "summary": "2-3 sentence overview of what an excellent response covers in this section.",
                    "key_points": [
                        "Key argument or piece of evidence 1",
                        "Key argument or piece of evidence 2"
                    ],
                }
            ],
        },
    }


    # NEW: Stricter marking + hard consistency requirement between criteria sum and total_marks_awarded.
    instructions = (
        "You are an experienced strict CSS examiner. "
        "Using ONLY the provided subject-wise rubric text, you must grade the student's answer with STRICT but FAIR marking.\n\n"
        "CRITICAL: OCR ERROR HANDLING - ABSOLUTE REQUIREMENT \n"
        "- The page_images_base64_png are the PRIMARY and ULTIMATE source of truth - these show the actual handwritten student answers\n"
        "- OCR text (ocr_full_text) is ONLY an approximate transcription helper and contains many errors\n"
        "- OCR transcription errors are NOT student errors - they are technical limitations of the OCR system\n"
        "- DO NOT mention OCR-induced typos, spelling errors, or transcription mistakes in remarks for ANY criterion\n"
        "- Examples of OCR errors to IGNORE: 'af' for 'of', 'Jhe' for 'The', 'rn' for 'm', 'cl' for 'd', etc.\n"
        "- When evaluating 'Language, Expression, and Scholarly Tone' or any language-related criterion:\n"
        "  * ONLY evaluate the actual handwritten content visible in the page images\n"
        "  * If you see apparent typos in OCR text, check the page images first\n"
        "  * If the handwriting in the image is correct, do NOT list it as a weakness\n"
        "  * ONLY mention actual weaknesses in the student's writing (poor grammar structure, informal tone, incorrect terminology usage)\n"
        "- OCR errors should NEVER appear in remarks for any criterion\n"
        "- If you cannot determine the actual text from the page image, do NOT assume it's an error - skip that evaluation\n"
        "- Remember: You are grading the student's handwriting, NOT the OCR transcription quality\n\n"
        "CRITICAL MARKING RULES (MUST FOLLOW):\n"
        "1. Maximum marks awarded: 14 out of 20 (HARD CAP - NEVER EXCEED)\n"
        "2. Average/acceptable answers: Score LESS than 10 marks (typically 6–9 marks)\n"
        "   - Most answers fall into this category\n"
        "   - If answer covers basics but has gaps or lacks depth: 6-9 marks\n"
        "3. Only exceptional answers: Approach 14 marks (typically 12–14 marks)\n"
        "   - ONLY give 12-14 marks if answer is comprehensive, accurate, well-analyzed, AND has minimal issues\n"
        "   - If answer has significant gaps or weaknesses: DO NOT give 12-14 marks\n"
        "   - Default to lower marks (6-9) unless answer is truly exceptional\n"
        "4. Weak answers: Score 5 marks or less\n"
        "   - Major gaps, incorrect information, or minimal coverage\n"
        "5. STRICT CONSISTENCY REQUIREMENT: The sum of all criteria[i].awarded MUST EXACTLY EQUAL total_marks_awarded\n"
        "   - Calculate: total_marks_awarded = sum of all criteria[i].awarded\n"
        "   - If the sum exceeds {MAX_MARKS_CAP}, proportionally reduce each criterion's awarded marks until the sum is {MAX_MARKS_CAP}\n"
        "   - Example: If criteria sum to 16, multiply each by (14/16) and round appropriately\n"
        "   - NEVER return a total_marks_awarded that differs from the sum of criteria marks\n\n"
        "FAIR MARKING DECISION PROCESS:\n"
        "Step 1: Identify key strengths and weaknesses in the answer (be thorough)\n"
        "Step 2: If significant weaknesses exist, start with marks in the 6-9 range (average)\n"
        "Step 3: Only increase marks if answer is truly exceptional:\n"
        "   - Comprehensive coverage of all key points\n"
        "   - Accurate information with minimal errors\n"
        "   - Well-analyzed with depth and critical thinking\n"
        "   - Minimal weaknesses (weaknesses should be minor, not significant)\n"
        "Step 4: Be fair - if strengths significantly outweigh weaknesses, award higher marks\n"
        "Step 5: Be strict - if weaknesses are significant, do NOT award 12-{MAX_MARKS_CAP} marks\n"
        "Step 7: Default to conservative marking - it's better to give 8 marks than 14 marks unless truly exceptional\n\n"
        "STRICT MARKING GUIDELINES:\n"
        "- Award marks conservatively – partial credit should be rare\n"
        "- If content is partially correct but incomplete: award roughly 30–50% of max marks for that criterion\n"
        "- If content is incorrect or missing: award 0 marks for that criterion\n"
        "- If content is correct but lacks depth/analysis: award roughly 50–70% of max marks\n"
        "- Only award 80–100% of max marks if content is comprehensive, accurate, and well-analyzed\n"
        "- Derive criteria and marks from the rubric text – do not invent new criteria\n"
        "- IMPORTANT: For each criterion, provide a single one-liner critical remark that explains the key feedback\n"
        "  * The remark should be honest and reflect why marks were awarded or deducted\n"
        "  * If marks are high, the remark should highlight strengths\n"
        "  * If marks are low, the remark should clearly state the key weakness or gap\n"
        "  * Ensure marks and remark are consistent - if remark identifies weaknesses, marks should reflect them\n\n"
        "Required fields:\n"
        "  - subject\n"
        "  - max_marks: always {DEFAULT_MAX_MARKS}\n"
        "  - total_marks_awarded: MUST equal sum of all criteria[i].awarded, capped at {MAX_MARKS_CAP} maximum\n"
        "  - question_statement: the exam question as written by the student (verbatim, not paraphrased)\n"
        "    * MUST copy the exact question text from the page images/OCR, including punctuation and question marks\n"
        "    * DO NOT prefix with phrases like 'The question asks' or 'The question requires'\n"
        "    * If multiple question lines exist, include the full combined question statement\n"
        "  - criteria[]: each criterion with id, name, max, awarded, remark\n"
        "    * IMPORTANT: After assigning awarded marks to each criterion, verify that sum(criteria[i].awarded) == total_marks_awarded <= {MAX_MARKS_CAP}\n"
        "    * remark: A single one-liner critical feedback sentence for this criterion\n"
        "    * The remark should be concise (one sentence) and provide key feedback about why marks were awarded/deducted\n"
        "    * Examples: 'Lacks depth in analyzing historical context' or 'Demonstrates strong knowledge with accurate facts and references' or 'Missing key theoretical framework'\n"
        "    * The remark should be critical and specific - focus on the most important aspect that influenced the marks\n"
        "    * DO NOT use multiple sentences - keep it to one clear, concise sentence\n"
    )

    content_payload = {
        "subject": subject,
        "rubric_text": subject_rubric_text,
        # Send full OCR text (no truncation)
        "ocr_full_text": ocr_data.get("full_text", ""),
        "sections": sections,
        "page_images_base64_png": page_images,
        "output_schema": schema_hint,
    }

    return {
        "model": GROK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert CSS examiner. "
                    "You produce detailed, rubric-based marking reports and respond in JSON only."
                ),
            },
            {
                "role": "user",
                "content": instructions
                + "\n\nDATA:\n"
                + json.dumps(content_payload, ensure_ascii=False),
            },
        ],
        "temperature": 0.15,
        "max_tokens": 8000,  # Increased to allow longer responses
    }


def call_grok_for_grading(
    grok_api_key: str,
    subject: str,
    subject_rubric_text: str,
    ocr_data: Dict[str, Any],
    sections: List[Dict[str, Any]],
    page_images: List[Dict[str, Any]],
    max_retries: int = 3,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    payload = build_grok_payload_for_grading(
        subject, subject_rubric_text, ocr_data, sections, page_images
    )
    return call_grok_api(
        grok_api_key,
        payload,
        max_retries=max_retries,
        timeout=GROK_REQUEST_TIMEOUT,
        retry_backoff=False,
        use_repair=True,
        error_file_prefix="grok_error_response",
    )


def validate_and_adjust_grading_result(grading_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Post-process grading result to enforce strict marking guidelines while maintaining fairness.
    Ensures consistency between criteria marks and total marks awarded.
    """
    total = grading_result.get("total_marks_awarded", 0)
    criteria = grading_result.get("criteria", [])
    
    if not criteria:
        return grading_result
    
    # Calculate sum of criteria marks
    criteria_sum = sum(c.get("awarded", 0) for c in criteria)
    
    # Check consistency between total and criteria sum
    if abs(total - criteria_sum) > 0.1:  # Allow small rounding differences
        print(f"WARNING: total_marks_awarded ({total}) doesn't match criteria sum ({criteria_sum:.1f})")
        # Fix it
        grading_result["total_marks_awarded"] = criteria_sum
        total = criteria_sum
    
    # Enforce hard cap of 14
    if total > MAX_MARKS_CAP:
        print(f"WARNING: Total marks ({total:.1f}) exceeds cap ({MAX_MARKS_CAP}), reducing proportionally...")
        scale_factor = MAX_MARKS_CAP / total
        for crit in criteria:
            crit["awarded"] = round(crit.get("awarded", 0) * scale_factor, 1)
        grading_result["total_marks_awarded"] = MAX_MARKS_CAP
        total = MAX_MARKS_CAP
    
    return grading_result


# -----------------------------
# GROK CALL 3: REFINED RUBRIC ANNOTATIONS
# -----------------------------


def call_grok_for_refined_rubric_annotations(
    grok_api_key: str,
    refined_rubric_text: str,
    ocr_data: Dict[str, Any],
    sections: List[Dict[str, Any]],
    page_images: List[Dict[str, Any]],
    max_retries: int = 3,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    Ask Grok to apply the refined generic rubric and produce:
      - annotations[] (for drawing boxes / comments),
      - refined_rubric_summary[] (one entry per rubric point).

    Output shape:

      {
        "annotations": [
          { ... see annotate_pdf_with_rubric.py docstring ... }
        ],
        "refined_rubric_summary": [
          {
            "id": "introduction_quality",
            "name": "Introduction Quality",
            "rating": "weak/average/good/excellent",
            "comment": "..."
          },
          ...
        ]
      }
    """
    system_msg = {
        "role": "system",
        "content": (
            "You are an expert examiner and annotation assistant.\n"
            "You apply a refined generic rubric to handwritten exam answers and output:\n"
            "- concrete annotations (where to draw boxes and what feedback to write), and\n"
            "- a short summary for each rubric point.\n\n"
            "PRIMARY INPUTS YOU RECEIVE:\n"
            "- refined_rubric_text: the generic rubric you must apply;\n"
            "- ocr_full_text + ocr_pages: approximate OCR text and per-page structure;\n"
            "- sections: JSON from a previous step that segments the answer into logical sections;\n"
            "- page_images_base64_png: base64 PNG page images (the handwritten script).\n\n"
            "GROUND TRUTH PRIORITY:\n"
            "- The PAGE IMAGES are the ultimate source of truth.\n"
            "- OCR is only a helper for locating text; if OCR and image disagree, trust the image.\n"
            "- The SECTIONS JSON gives you stable titles and page ranges. Use it whenever you need\n"
            "  to refer to headings or sections (via section_id or target_section_id).\n\n"
            "YOUR GOAL:\n"
            "- Produce a JSON object with:\n"
            "    annotations[]: ALL important issues and observations you can detect,\n"
            "    refined_rubric_summary[]: one item per rubric point with rating + comment.\n"
            "- Be especially thorough for:\n"
            "    * incorrect or weak headings (missing clarity / relevance), and\n"
            "    * factual inaccuracies (wrong dates, wrong names, wrong causal claims, etc.).\n"
            "- It is acceptable to be conservative for spelling/grammar due to OCR noise.\n\n"
            "STRICT OUTPUT FORMAT (IMPORTANT):\n"
            "- Return ONLY valid JSON (no markdown, no commentary).\n"
            "- Top-level keys allowed: 'annotations' and 'refined_rubric_summary'.\n"
            "- annotations[] entries can have additional fields, but MUST include the fields\n"
            "  required in the provided output_schema examples for each type.\n"
            "- The JSON must be parseable by a strict JSON parser.\n"
        ),
    }

    instructions = (
        "Use the refined generic rubric text to evaluate this answer strictly. "
        "You must obey these annotation rules:\n\n"
        "⚠️ ABSOLUTE REQUIREMENT - INTRODUCTION COMMENT:\n"
        "- THE VERY FIRST ANNOTATION IN YOUR OUTPUT MUST BE type='introduction_comment'\n"
        "- THIS IS NON-NEGOTIABLE - EVERY ANSWER MUST HAVE EXACTLY ONE INTRODUCTION COMMENT\n"
        "- PLACE IT AS THE FIRST ITEM IN THE annotations[] ARRAY\n"
        "- IF YOU DO NOT INCLUDE THIS, YOUR RESPONSE WILL BE REJECTED\n\n"
        "CRITICAL REQUIREMENT FOR ALL ANNOTATIONS:\n"
        "- For ALL annotation types, you MUST copy text EXACTLY as it appears in the OCR text.\n"
        "- NEVER paraphrase, reword, or correct the text when filling target_word_or_sentence.\n"
        "- ALWAYS provide context_before (3-5 words immediately before) and context_after (3-5 words immediately after).\n"
        "- Copy these contexts EXACTLY from the OCR text with original spelling and punctuation.\n\n"
        "ALL ANNOTATIONS MUST HAVE THIS UNIFIED SCHEMA:\n"
        "  type: string (introduction_comment/heading_issue/factual_error/grammar_language/repetition)\n"
        "  rubric_point: string (e.g., 'introduction_quality', 'headings_subheadings', 'factual_accuracy', 'grammar_language')\n"
        "  page: integer (page number where the annotation appears)\n"
        "  target_word_or_sentence: string (EXACT text from OCR - the word, phrase, or sentence being annotated)\n"
        "  context_before: string (EXACT 3-5 words from OCR that appear immediately before the target)\n"
        "  context_after: string (EXACT 3-5 words from OCR that appear immediately after the target)\n"
        "  correction: string (the correct version, or suggestion for improvement)\n"
        "  comment: string (explanation of the issue)\n"
        "  sentiment: string (optional, for heading_issue: 'positive' or 'negative')\n\n"
        "1) Introduction:\n"
        "   - MANDATORY: You MUST ALWAYS create exactly ONE annotation of type 'introduction_comment'.\n"
        "   - This annotation is REQUIRED for every answer, regardless of quality.\n"
        "   - Decide if introduction is weak/average/good/excellent and be strict about it.\n"
        "   - Create type 'introduction_comment' with:\n"
        "       rubric_point = 'introduction_quality',\n"
        "       page = first page where introduction appears,\n"
        "       target_word_or_sentence = EXACT first sentence or opening phrase from OCR,\n"
        "       context_before = '' (empty for first sentence),\n"
        "       context_after = EXACT next 3-5 words from OCR after the target,\n"
        "       correction = '' (not applicable for introduction comments),\n"
        "       comment = a detailed 3–5 sentence evaluation of ONLY the introduction using the refined generic rubric.\n"
        "   - DO NOT SKIP THIS ANNOTATION. It is MANDATORY.\n\n"
        "2) Headings and subheadings:\n"
        "   - MANDATORY: You MUST evaluate EVERY SINGLE heading and subheading detected in the sections[] array.\n"
        "   - For EACH heading/subheading found, you MUST create ONE 'heading_issue' annotation.\n"
        "   - DO NOT skip any headings. If you detect 5 headings, you MUST create 5 heading_issue annotations.\n"
        "   - IMPORTANT: DO NOT evaluate spelling, grammar, or OCR errors in headings. These are handled separately.\n"
        "   - ONLY evaluate heading CONTENT: relevance, clarity, self-explanatory nature.\n"
        "   - For CORRECT headings (self-explanatory, relevant, clear), add annotation type 'heading_issue' with:\n"
        "       rubric_point = 'headings_subheadings',\n"
        "       page = page number of that heading,\n"
        "       target_word_or_sentence = EXACT heading text from OCR,\n"
        "       context_before = EXACT 3-5 words from OCR before the heading,\n"
        "       context_after = EXACT 3-5 words from OCR after the heading,\n"
        "       sentiment = 'positive',\n"
        "       correction = a refined/rephrased heading suggestion (even for positive headings),\n"
        "       comment = POSITIVE: short explanation (1-2 sentences) of why the heading is good.\n"
        "   - For INCORRECT/PROBLEMATIC headings, create type 'heading_issue' with:\n"
        "       rubric_point = 'headings_subheadings',\n"
        "       page = page number of that heading,\n"
        "       target_word_or_sentence = EXACT heading text from OCR (even if misspelled),\n"
        "       context_before = EXACT 3-5 words from OCR before the heading,\n"
        "       context_after = EXACT 3-5 words from OCR after the heading,\n"
        "       sentiment = 'negative',\n"
        "       correction = a better alternate heading that would be more self-explanatory and relevant,\n"
        "       comment = NEGATIVE: short explanation (1-2 sentences) of the issue (NEVER mention spelling/grammar/OCR errors).\n"
        "   - For ALL heading_issue annotations, correction MUST be a concise, academic rephrasing that preserves meaning,\n"
        "     stays tied to the question focus, keeps any time range or key term, and uses <= 12 words unless the original is longer.\n"
        "   - NEVER leave correction empty for heading_issue; even positive headings need a refined rephrase.\n"
        "   - Focus ONLY on: not being self-explanatory, not directly relevant, vague, unclear, irrelevant.\n"
        "   - IGNORE: spelling mistakes, grammar errors, OCR misreads in headings.\n\n"
        "3) Factual inaccuracies:\n"
        "   - CRITICAL: Do NOT create annotations for CORRECT facts. ONLY annotate ACTUAL ERRORS.\n"
        "   - Do NOT mark spelling mistakes as factual errors.\n"
        "   - If a date/fact is correct, DO NOT create any annotation for it.\n"
        "   - Only flag actual factual mistakes (wrong dates, wrong facts, incorrect information).\n"
        "   - Keep target_word_or_sentence VERY SHORT (1-10 words max) containing only the error.\n"
        "   - For each ACTUAL factual mistake, create type 'factual_error' with:\n"
        "       rubric_point = 'factual_accuracy',\n"
        "       page = page where the error appears,\n"
        "       target_word_or_sentence = EXACT SHORT PHRASE containing the WRONG fact (e.g., '1944' when it should be '1945'),\n"
        "       context_before = EXACT 3-5 words immediately before the error in OCR,\n"
        "       context_after = EXACT 3-5 words immediately after the error in OCR,\n"
        "       correction = the CORRECT fact (e.g., '1945'),\n"
        "       comment = short explanation (e.g., 'Year should be 1945 not 1944').\n"
        "   - NEVER copy full sentences - only the specific phrase with the error.\n"
        "   - DO NOT create factual_error if target_word_or_sentence = correction (that means it's correct!)\n"
        "   - Examples:\n"
        "       * WRONG: target='1944', correction='1944', comment='correct' (DO NOT DO THIS!)\n"
        "       * WRONG: target='1707', correction='1707', comment='Year is correct' (DO NOT DO THIS!)\n"
        "       * CORRECT: target='1944', correction='1945', comment='Year should be 1945 not 1944'\n"
        "       * CORRECT: target='World War I', correction='World War II', comment='Should be WWII not WWI'\n\n"
        "4) Spelling only (no grammar):\n"
        "   - Focus ONLY on clear spelling mistakes (wrongly spelled words).\n"
        "   - Do NOT correct grammar, sentence structure, style, or phrasing.\n"
        "   - For each spelling issue, create type 'grammar_language' with:\n"
        "       rubric_point = 'grammar_language',\n"
        "       page = page where the misspelled word appears,\n"
        "       target_word_or_sentence = EXACT misspelled word or very short span (1-3 words) from OCR,\n"
        "       context_before = EXACT 3-5 words from OCR immediately before the misspelled word,\n"
        "       context_after = EXACT 3-5 words from OCR immediately after the misspelled word,\n"
        "       correction = the correctly spelled word or very short corrected phrase,\n"
        "       comment = brief note like 'spelling error'.\n"
        "   - Always cross-check using BOTH OCR text AND the page image:\n"
        "       * Use ocr_full_text to locate the word,\n"
        "       * Then visually verify the spelling directly on the page image.\n"
        "       * If OCR and the image disagree, TRUST THE IMAGE and do NOT flag a spelling error.\n"
        "   - Do not send entire paragraphs or long sentences as target_word_or_sentence.\n"
        "   - If the same misspelling occurs multiple times on the same page, create a SEPARATE annotation for EACH occurrence,\n"
        "     with different context_before and context_after for each instance.\n\n"
        "5) Repetition:\n"
        "   - If content is repeated across pages, create type 'repetition' with:\n"
        "       rubric_point = 'repetitiveness',\n"
        "       page = the page where the repeated content appears again,\n"
        "       target_word_or_sentence = EXACT repeated phrase or sentence from OCR,\n"
        "       context_before = EXACT 3-5 words from OCR before the repeated text,\n"
        "       context_after = EXACT 3-5 words from OCR after the repeated text,\n"
        "       correction = suggestion like 'Remove repetition' or 'Already mentioned on page X',\n"
        "       comment = note indicating where it was first mentioned.\n\n"
        "Additionally, build refined_rubric_summary[]:\n"
        "   - ONLY include this 1 rubric point:\n"
        "     1. length_completeness (name: 'Length & Completeness')\n"
        "   - Each entry: id, name, rating (weak/average/good/excellent), comment (2-3 sentences, detailed evaluation of length and completeness).\n"
        "   - The comment should be comprehensive and explain how well the answer covers the question requirements, including:\n"
        "     * Whether the answer is of adequate length for the question\n"
        "     * Whether all parts of the question are addressed\n"
        "     * Whether the coverage is thorough or superficial\n"
        "   - Do NOT include argumentation_quality, presentation, or contemporary_relevance.\n"
    )


    # Sanitize OCR pages (remove bounding boxes) and send full OCR text
    raw_pages = ocr_data.get("pages", [])
    sanitized_pages = []
    for p in raw_pages:
        lines = []
        for line in p.get("lines", []):
            line_text = line.get("text", "")
            words = [w.get("text", "") for w in line.get("words", [])]
            lines.append({"text": line_text, "words": words})
        sanitized_pages.append({"page_number": p.get("page_number"), "lines": lines})

    user_payload = {
        "refined_rubric_text": refined_rubric_text,
        "ocr_full_text": ocr_data.get("full_text", ""),
        "ocr_pages": sanitized_pages,
        "sections": sections,
        "page_images_base64_png": page_images,
        "output_schema": {
            "annotations": [
                {
                    "type": "introduction_comment",
                    "rubric_point": "introduction_quality",
                    "page": 1,
                    "target_word_or_sentence": "First sentence of introduction (EXACT from OCR)",
                    "context_before": "",
                    "context_after": "Next 3-5 words after (EXACT from OCR)",
                    "correction": "",
                    "comment": "Detailed evaluation of introduction quality (3-5 sentences)",
                },
                {
                    "type": "heading_issue",
                    "rubric_point": "headings_subheadings",
                    "page": 1,
                    "target_word_or_sentence": "EXACT heading text from OCR",
                    "context_before": "EXACT 3-5 words before",
                    "context_after": "EXACT 3-5 words after",
                    "correction": "Refined/rephrased heading suggestion",
                    "comment": "POSITIVE/NEGATIVE: concise quality evaluation",
                    "sentiment": "positive/negative",
                },
                {
                    "type": "factual_error",
                    "rubric_point": "factual_accuracy",
                    "page": 2,
                    "target_word_or_sentence": "SHORT PHRASE with error (EXACT from OCR)",
                    "context_before": "EXACT 3-5 words before",
                    "context_after": "EXACT 3-5 words after",
                    "correction": "Correct fact",
                    "comment": "Explanation of error",
                }
            ],
            "refined_rubric_summary": [
                {
                    "id": "length_completeness",
                    "name": "Length & Completeness",
                    "rating": "weak/average/good/excellent",
                    "comment": "Detailed 2-3 sentence evaluation of how comprehensively the answer covers the question requirements, including length adequacy and completeness of coverage.",
                }
            ],
        },
    }

    user_msg = {
        "role": "user",
        "content": instructions + "\n\nDATA:\n" + json.dumps(user_payload, ensure_ascii=False),
    }

    payload = {
        "model": GROK_MODEL,
        "messages": [system_msg, user_msg],
        "temperature": 0.1,
        "max_tokens": 6000,  # Increased for refined rubric annotations
    }

    _REFINED_FALLBACK_ANNOT = {
        "type": "introduction_comment",
        "rubric_point": "introduction_quality",
        "page": 1,
        "target_word_or_sentence": "",
        "context_before": "",
        "context_after": "",
        "correction": "",
        "comment": "",
    }

    try:
        parsed, token_usage = call_grok_api(
            grok_api_key,
            payload,
            max_retries=max_retries,
            timeout=GROK_REQUEST_TIMEOUT,
            retry_backoff=False,
            use_repair=True,
            error_file_prefix="grok_refined_error",
        )
    except GrokAPIError as e:
        fallback = {
            "annotations": [{**_REFINED_FALLBACK_ANNOT, "comment": str(e)}],
            "refined_rubric_summary": [],
        }
        return fallback, (e.token_usage if e.token_usage else {"input_tokens": 0, "output_tokens": 0})

    # Success! Normalize and validate
    parsed.setdefault("annotations", [])
    parsed.setdefault("refined_rubric_summary", [])

    # ENFORCE: Ensure introduction_comment always exists as first annotation
    annotations = parsed.get("annotations", [])
    has_intro = any(a.get("type") == "introduction_comment" for a in annotations)

    if not has_intro:
        # Inject introduction_comment as first annotation
        intro_text = ocr_data.get("full_text", "")[:200].split('\n')[0] if ocr_data.get("full_text") else "Introduction"
        intro_annotation = {
            "type": "introduction_comment",
            "rubric_point": "introduction_quality",
            "page": 1,
            "target_word_or_sentence": intro_text,
            "context_before": "",
            "context_after": "",
            "correction": "",
            "comment": "Introduction evaluation (auto-generated due to missing annotation)"
        }
        parsed["annotations"].insert(0, intro_annotation)
        print("⚠️  Warning: Introduction comment was missing. Auto-injected.")

    return parsed, token_usage


# -----------------------------
# GROK CALL 4: PAGE-WISE IMPROVEMENT SUGGESTIONS
# -----------------------------


def call_grok_for_page_wise_suggestions(
    grok_api_key: str,
    subject: str,
    subject_rubric_text: str,
    ocr_data: Dict[str, Any],
    sections: List[Dict[str, Any]],) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    Ask Grok to provide specific, actionable improvement suggestions for each page.

    Output shape:
      {
        "page_suggestions": [
          {
            "page": 1,
            "suggestions": [
              "Add comparison with Kant's categorical imperative",
              "Include the 1945 UN Charter establishment date",
              "Reference Rawls' theory of justice for stronger argumentation",
              "Add empirical evidence from 2020 Climate Summit"
            ]
          },
          ...
        ]
      }
    """
    system_msg = {
        "role": "system",
        "content": (
            "You are an expert CSS examiner focused on helping students improve their answers.\n"
            "You receive:\n"
            "- The subject rubric (detailed criteria)\n"
            "- OCR text from student's answer (page-wise)\n"
            "- Section structure (headings detected)\n\n"
            "YOUR GOAL:\n"
            "For each page, provide 3-6 specific, actionable suggestions for improvement.\n"
            "Focus on VALUE ADDITIONS that would strengthen the answer.\n\n"
            "TYPES OF SUGGESTIONS TO PROVIDE:\n"
            "1. Theoretical additions: 'Add comparison with X philosopher/theorist'\n"
            "2. Factual additions: 'Include the date: [specific event] occurred in [year]'\n"
            "3. Evidence additions: 'Add empirical data from [specific study/report]'\n"
            "4. Comparative analysis: 'Compare with [country/era/policy]'\n"
            "5. Critical perspectives: 'Include critique from [scholar/school of thought]'\n"
            "6. Contemporary relevance: 'Link to recent event: [specific event in year]'\n\n"
            "STRICT OUTPUT FORMAT:\n"
            "- Return ONLY valid JSON\n"
            "- Top-level key: 'page_suggestions' with array of page objects\n"
            "- Each page object has: 'page' (integer) and 'suggestions' (array of strings)\n"
            "- Each suggestion must be a single, specific, actionable statement\n"
            "- No markdown, no commentary, just JSON\n"
        ),
    }

    instructions = (
        "Analyze this student's answer page by page.\n"
        "For each page, identify 3-6 specific additions that would improve the answer quality.\n\n"
        "REQUIREMENTS:\n"
        "1. Suggestions must be SPECIFIC, not vague\n"
        "   ❌ Bad: 'Add more theories'\n"
        "   ✅ Good: 'Add Foucault's concept of biopower (1976)'\n\n"
        "2. Focus on what to ADD, not what's wrong\n"
        "   ❌ Bad: 'This argument is weak'\n"
        "   ✅ Good: 'Strengthen argument by adding Weber's bureaucracy theory'\n\n"
        "3. Include specific names, dates, events, theories\n"
        "   ❌ Bad: 'Reference a philosopher'\n"
        "   ✅ Good: 'Reference Mill's harm principle (On Liberty, 1859)'\n\n"
        "4. Suggestions should align with the subject rubric criteria\n\n"
        "5. Each page should have 3-6 suggestions maximum\n\n"
        "OUTPUT:\n"
        "Return JSON with this exact structure:\n"
        "{\n"
        "  \"page_suggestions\": [\n"
        "    {\n"
        "      \"page\": 1,\n"
        "      \"suggestions\": [\n"
        "        \"Add comparison with Locke's social contract theory (Two Treatises, 1689)\",\n"
        "        \"Include the Magna Carta signing date (1215) as historical precedent\",\n"
        "        \"Reference the 1948 Universal Declaration of Human Rights\"\n"
        "      ]\n"
        "    },\n"
        "    {\n"
        "      \"page\": 2,\n"
        "      \"suggestions\": [\n"
        "        \"Add Amartya Sen's capability approach for economic analysis\",\n"
        "        \"Include World Bank poverty data (2021 report)\",\n"
        "        \"Compare with China's economic reforms post-1978\"\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n"
    )

    # Simplified page data (text only, no images or bounding boxes to reduce tokens)
    ocr_pages_minimal = [
        {
            "page": p.get("page", idx + 1),
            "text": p.get("text", "")[:300000]  # Limit per-page text
        }
        for idx, p in enumerate(ocr_data.get("pages", []))
    ]

    user_payload = {
        "subject": subject,
        "rubric_text": subject_rubric_text,
        "ocr_full_text": ocr_data.get("full_text", "")[:15000],
        "ocr_pages": ocr_pages_minimal,
        "sections": sections,
        "output_schema": {
            "page_suggestions": [
                {
                    "page": 1,
                    "suggestions": ["string", "string"]
                }
            ]
        },
    }

    user_msg = {
        "role": "user",
        "content": instructions + "\n\nDATA:\n" + json.dumps(user_payload, ensure_ascii=False),
    }

    payload = {
        "model": GROK_MODEL,
        "messages": [system_msg, user_msg],
        "temperature": 0.2,
        "max_tokens": 4000,  # Sufficient for page suggestions
    }

    try:
        return call_grok_api(
            grok_api_key,
            payload,
            max_retries=3,
            timeout=GROK_REQUEST_TIMEOUT,
            retry_backoff=True,
            use_repair=False,
        )
    except GrokAPIError:
        return {"page_suggestions": []}, {"input_tokens": 0, "output_tokens": 0}


# -----------------------------
# GROK CALL 5: MARK DEDUCTION ANALYSIS
# -----------------------------


def call_grok_for_mark_deduction_analysis(
    grok_api_key: str,
    grading_result: Dict[str, Any],
    subject_rubric_text: str,
    ocr_data: Dict[str, Any],
    sections: List[Dict[str, Any]],
    max_retries: int = 3,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    Analyze the marks breakdown and explain why marks were lost overall.
    Result is merged into grading_result for report rendering; optionally saved to
    Tests folder as JSON when SAVE_TEST_FILES is enabled.
    
    IMPORTANT: Analyze each criterion internally to inform the overall analysis,
    but DO NOT output individual criterion breakdowns. Instead, provide an
    aggregated overall analysis.
    
    Output shape:
    {
        "total_marks_analysis": {
            "total_awarded": 8.5,
            "total_possible": DEFAULT_MAX_MARKS,
            "total_lost": 11.5,
            "percentage_awarded": 42.5,
            "overall_summary": "Brief explanation of overall performance"
        },
        "overall_why_marks_lost": [
            "WHY: [Direct reason]. WHERE: [Which criteria]. IMPACT: [Marks lost]."
        ],
        "overall_what_was_missing": [
            "[Missing element]: [Specific detail]"
        ],
        "overall_how_to_improve": [
            "HOW: [Specific action]. WHY: [Reason]. WHERE: [Which criteria]."
        ],
        "priority_improvements": [
            {
                "priority": 1,
                "area": "Overall area name (e.g., 'Critical Analysis')",
                "reason": "Why this is a priority overall",
                "quick_wins": ["Action 1", "Action 2"]
            },
            ...
        ]
    }
    """
    system_msg = {
        "role": "system",
        "content": (
            "You are a STRICT CSS examiner evaluating student answers.\n"
            "Your task is to evaluate the answer, outline reasons for low score, and suggest improvements.\n\n"
            "You will receive:\n"
            "- The grading result (with criteria remarks)\n"
            "- The subject rubric text (for reference on what was expected)\n"
            "- OCR text and sections structure (for context)\n\n"
            "Your analysis must be:\n"
            "- STRICT: Apply rigorous standards - identify all weaknesses clearly\n"
            "- CRITICAL: Find ALL problems, gaps, and missing elements\n"
            "- SPECIFIC: Explain exactly what was wrong and what was missing\n"
            "- SIMPLE: Use clear, easy-to-understand language\n"
            "- THOROUGH: Leave no weakness unexamined\n"
            "- HELPFUL: Provide clear suggestions for improvement\n"
        ),
    }

    instructions = (
        "You have THREE SIMPLE TASKS:\n\n"
        "1. EVALUATE this answer based on the rubric.\n"
        "   - Look at the answer and see what's wrong\n"
        "   - Be STRICT - identify all weaknesses\n\n"
        "2. OUTLINE reasons for low score.\n"
        "   - Explain why the score is low in simple, clear sentences\n"
        "   - Write 4-6 reasons, each 1-2 sentences\n"
        "   - Use simple language - just explain what was wrong\n"
        "   - Example: 'The answer lacks critical analysis. It only describes events without evaluating them.'\n\n"
        "3. SUGGEST improvements.\n"
        "   - Tell the student what was missing and how to fix it\n"
        "   - Write 5-7 suggestions, each 1-2 sentences\n"
        "   - Use simple language - explain what to do and why it helps\n"
        "   - Example: 'Add counterarguments with evidence in each section. This will turn description into critical analysis.'\n\n"
        "IMPORTANT RULES:\n"
        "- Write in SIMPLE, CLEAR language - no complex terms\n"
        "- Keep each item SHORT (1-2 sentences)\n"
        "- Make it EASY TO UNDERSTAND - the student should know exactly what was wrong\n"
        "- Be STRICT but FAIR - point out all weaknesses clearly\n"
        "- Do NOT mention specific criteria names or mark counts - just explain what was wrong\n\n"
        "OUTPUT FORMAT:\n"
        "- Return ONLY valid JSON (no markdown, no code blocks)\n"
        "- Follow the exact schema provided in output_schema\n"
        "- total_marks_analysis.overall_summary: 1-2 sentences explaining overall performance\n"
        "- overall_why_marks_lost: 4-6 simple reasons for low score (1-2 sentences each)\n"
        "- overall_what_was_missing: 6-8 items showing what was missing (1 sentence each)\n"
        "- overall_how_to_improve: 5-7 suggestions for improvement (1-2 sentences each)\n"
        "- priority_improvements: Top 3 priority areas with simple explanations\n"
    )

    # Prepare data payload
    criteria_data = []
    for crit in grading_result.get("criteria", []):
        criteria_data.append({
            "id": crit.get("id", ""),
            "name": crit.get("name", ""),
            "max": crit.get("max", 0),
            "awarded": crit.get("awarded", 0),
            "remark": crit.get("remark", ""),
        })

    user_payload = {
        "grading_result": {
            "subject": grading_result.get("subject", ""),
            "total_marks_awarded": grading_result.get("total_marks_awarded", 0),
            "max_marks": grading_result.get("max_marks", DEFAULT_MAX_MARKS),
            "criteria": criteria_data,
        },
        "subject_rubric_text": subject_rubric_text[:5000],  # Limit to avoid token overflow
        "ocr_full_text": ocr_data.get("full_text", "")[:10000],  # Limit for context
        "sections": sections,
        "output_schema": {
            "total_marks_analysis": {
                "total_awarded": 0,
                "total_possible": DEFAULT_MAX_MARKS,
                "total_lost": 0,
                "percentage_awarded": 0,
                "overall_summary": "Brief explanation"
            },
            "overall_why_marks_lost": [
                "Simple 1-2 sentence explanation of why marks were lost, written in clear language."
            ],
            "overall_what_was_missing": [
                "Simple 1 sentence explaining what was missing, written in clear language."
            ],
            "overall_how_to_improve": [
                "Simple 1-2 sentence suggestion for improvement, written in clear language."
            ],
            "priority_improvements": [
                {
                    "priority": 1,
                    "area": "Overall area name (e.g., 'Critical Analysis')",
                    "reason": "Why this is a priority overall",
                    "quick_wins": ["Action 1", "Action 2"]
                }
            ]
        },
    }

    user_msg = {
        "role": "user",
        "content": instructions + "\n\nDATA:\n" + json.dumps(user_payload, ensure_ascii=False),
    }

    payload = {
        "model": GROK_MODEL,
        "messages": [system_msg, user_msg],
        "temperature": 0.2,
        "max_tokens": 6000,
    }

    return call_grok_api(
        grok_api_key,
        payload,
        max_retries=max_retries,
        timeout=GROK_REQUEST_TIMEOUT,
        retry_backoff=True,
        use_repair=True,
    )


def _should_save_test_files() -> bool:
    """
    Check if test files should be saved based on environment variable.
    Defaults to False (production-safe).
    Set SAVE_TEST_FILES=true in local development.
    """
    save_test_files = os.getenv("SAVE_TEST_FILES", "false").lower()
    return save_test_files in ("true", "1", "yes")


def _get_tests_dir() -> Optional[str]:
    """
    Return Tests directory path if SAVE_TEST_FILES is enabled, else None.
    Creates the directory if it does not exist.
    """
    if not _should_save_test_files():
        return None
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "Tests")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_subject_for_filename(subject: str) -> str:
    """Sanitize subject string for use in filenames."""
    return re.sub(r'[^\w\s-]', '', (subject or "").strip()).replace(' ', '_')


def save_mark_deduction_analysis_to_tests(
    analysis_result: Dict[str, Any],
    request_id: str,
    subject: str,
) -> Optional[str]:
    """
    Save mark deduction analysis to Tests folder as JSON.
    Only saves if SAVE_TEST_FILES environment variable is set to true.
    
    Args:
        analysis_result: The mark deduction analysis result from Grok
        request_id: The request ID for filename
        subject: The subject name for filename
    
    Returns:
        Path to the saved file, or None if saving is disabled
    """
    tests_dir = _get_tests_dir()
    if tests_dir is None:
        return None
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_subject = _safe_subject_for_filename(subject)
    filename = f"mark_deduction_analysis_{safe_subject}_{request_id}_{timestamp}.json"
    filepath = os.path.join(tests_dir, filename)
    
    # Save the analysis
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(analysis_result, f, ensure_ascii=False, indent=2)
    
    print(f"Mark deduction analysis saved to: {filepath}")
    return filepath


def save_annotations_to_tests(
    annotations: List[Dict[str, Any]],
    request_id: str,
    subject: str,
) -> Optional[str]:
    """
    Save annotations to Tests folder as JSON.
    Only saves if SAVE_TEST_FILES environment variable is set to true.
    
    Args:
        annotations: The list of validated annotations
        request_id: The request ID for filename
        subject: The subject name for filename
    
    Returns:
        Path to the saved file, or None if saving is disabled
    """
    tests_dir = _get_tests_dir()
    if tests_dir is None:
        return None
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_subject = _safe_subject_for_filename(subject)
    filename = f"annotations_{safe_subject}_{request_id}_{timestamp}.json"
    filepath = os.path.join(tests_dir, filename)
    
    # Prepare the data structure
    annotations_data = {
        "request_id": request_id,
        "subject": subject,
        "timestamp": timestamp,
        "total_annotations": len(annotations),
        "annotations": annotations,
    }
    
    # Save the annotations
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(annotations_data, f, ensure_ascii=False, indent=2)
    
    print(f"Annotations saved to: {filepath}")
    return filepath


def save_unfiltered_ocr_text(
    ocr_data: Dict[str, Any],
    request_id: str,
    subject: str,
) -> Optional[str]:
    """
    Save unfiltered OCR text to Tests folder as JSON and plain text.
    This is saved BEFORE any filtering or processing happens.
    Only saves if SAVE_TEST_FILES environment variable is set to true.
    
    Args:
        ocr_data: The raw OCR data dictionary
        request_id: The request ID for filename
        subject: The subject name for filename
    
    Returns:
        Path to the saved JSON file, or None if saving is disabled
    """
    tests_dir = _get_tests_dir()
    if tests_dir is None:
        return None
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_subject = _safe_subject_for_filename(subject)
    
    # Save as JSON (full OCR data structure)
    json_filename = f"ocr_unfiltered_{safe_subject}_{request_id}_{timestamp}.json"
    json_filepath = os.path.join(tests_dir, json_filename)
    
    # Extract full text from all pages
    full_text_pages = []
    full_text_combined = []
    
    pages = ocr_data.get("pages", [])
    for page_idx, page in enumerate(pages):
        page_num = page.get("page_number", page_idx + 1)
        
        # Try multiple ways to extract text from OCR data structure
        page_text = page.get("full_text", "")
        
        # If full_text is empty, extract from lines array (common OCR structure)
        if not page_text:
            lines = page.get("lines", [])
            if lines:
                # Extract text from each line
                line_texts = []
                for line in lines:
                    line_text = line.get("text", "") or line.get("line_text", "")
                    if line_text:
                        line_texts.append(line_text)
                page_text = "\n".join(line_texts)
        
        # If still empty, try blocks
        if not page_text:
            blocks = page.get("blocks", [])
            if blocks:
                block_texts = []
                for block in blocks:
                    block_text = block.get("text", "")
                    if block_text:
                        block_texts.append(block_text)
                page_text = "\n".join(block_texts)
        
        # If still empty, try paragraphs
        if not page_text:
            paragraphs = page.get("paragraphs", [])
            if paragraphs:
                para_texts = []
                for para in paragraphs:
                    para_text = para.get("text", "")
                    if para_text:
                        para_texts.append(para_text)
                page_text = "\n".join(para_texts)
        
        full_text_pages.append({
            "page_number": page_num,
            "text": page_text,
            "line_count": len(page_text.splitlines()) if page_text else 0,
            "char_count": len(page_text) if page_text else 0
        })
        if page_text:
            full_text_combined.append(f"=== PAGE {page_num} ===\n{page_text}\n")
        else:
            full_text_combined.append(f"=== PAGE {page_num} ===\n[No text extracted from OCR data]\n")
    
    # Prepare the data structure
    ocr_save_data = {
        "request_id": request_id,
        "subject": subject,
        "timestamp": timestamp,
        "total_pages": len(pages),
        "full_ocr_data": ocr_data,  # Complete unfiltered OCR data
        "extracted_text": {
            "by_page": full_text_pages,
            "combined": "\n".join(full_text_combined)
        }
    }
    
    # Save as JSON
    with open(json_filepath, "w", encoding="utf-8") as f:
        json.dump(ocr_save_data, f, ensure_ascii=False, indent=2)
    
    # Also save as plain text for easy reading
    txt_filename = f"ocr_unfiltered_{safe_subject}_{request_id}_{timestamp}.txt"
    txt_filepath = os.path.join(tests_dir, txt_filename)
    with open(txt_filepath, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"UNFILTERED OCR TEXT - {subject}\n")
        f.write(f"Request ID: {request_id}\n")
        f.write(f"Timestamp: {timestamp}\n")
        f.write(f"Total Pages: {len(pages)}\n")
        f.write("=" * 80 + "\n\n")
        f.write("\n".join(full_text_combined))
    
    print(f"Unfiltered OCR text saved to:")
    print(f"  JSON: {json_filepath}")
    print(f"  TXT: {txt_filepath}")
    return json_filepath


# -----------------------------
# REPORT RENDERING (SUBJECT MARKING)
# -----------------------------


_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_FONTS_DIR = os.path.join(_MODULE_DIR, "fonts")
_FONT_CANDIDATES = [
    os.environ.get("OCR_FONT_PATH"),
    os.path.join(_FONTS_DIR, "ReportFont.ttf"),
    os.path.join(_FONTS_DIR, "NotoSans-Regular.ttf"),
    os.path.join(_FONTS_DIR, "DejaVuSans.ttf"),
    "arial.ttf",
    "Arial.ttf",
    "LiberationSans-Regular.ttf",
    "DejaVuSans.ttf",
]


def _iter_font_candidates() -> List[str]:
    seen: Set[str] = set()
    ordered: List[str] = []
    for candidate in _FONT_CANDIDATES:
        if not candidate:
            continue
        norm = os.path.normpath(candidate) if os.path.isabs(candidate) else candidate
        if norm in seen:
            continue
        seen.add(norm)
        ordered.append(candidate)
    return ordered


@lru_cache(maxsize=32)
def _get_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in _iter_font_candidates():
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> List[str]:
    words = text.split()
    lines: List[str] = []
    current = ""
    for w in words:
        trial = (current + " " + w).strip()
        if not trial:
            continue
        width = draw.textlength(trial, font=font)
        if width <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _strip_question_prefix(text: str) -> str:
    """Remove leading question numbering like 'Question #1', 'Q1)' for display."""
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"^(question\s*#?\s*\d+[:.)-]?\s*)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(q\s*#?\s*\d+[:.)-]?\s*)", "", t, flags=re.IGNORECASE)
    return t.strip()


def _calculate_rating(awarded: float, max_marks: float) -> str:
    """Calculate rating based on awarded/max ratio."""
    if max_marks == 0:
        return "N/A"
    ratio = awarded / max_marks
    if ratio >= 0.85:
        return "Excellent"
    elif ratio >= 0.70:
        return "Good"
    elif ratio >= 0.50:
        return "Average"
    else:
        return "Weak"


def _combine_strengths_weaknesses(strengths: List[str], weaknesses: List[str]) -> str:
    """Combine strengths and weaknesses into a single comment string."""
    parts = []
    if strengths:
        strengths_text = ". ".join(strengths)
        if not strengths_text.endswith("."):
            strengths_text += "."
        parts.append(f"Strengths: {strengths_text}")
    if weaknesses:
        weaknesses_text = ". ".join(weaknesses)
        if not weaknesses_text.endswith("."):
            weaknesses_text += "."
        parts.append(f"Weaknesses: {weaknesses_text}")
    if not parts:
        return "No specific feedback provided."
    return " ".join(parts)

def render_subject_report_pages(
    grading_result: Dict[str, Any],
    page_size: Tuple[int, int] = (2977, 4211),  # 200 DPI: Width x Height
    refined_summary: Optional[List[Dict[str, Any]]] = None,  # ADD THIS: refined rubric summary (for Length & Completeness)
) -> List[Image.Image]:
    """
    Render the subject-wise marking report on a single page.
    Dynamically adjusts page height to fit content perfectly:
      - Starts with base height (1.2x original)
      - Incrementally increases height until content fits
      - Falls back to font scaling only if height reaches maximum limit
      - SUBJECT NAME (at top)
      - TOTAL MARKS
      - QUESTION STATEMENT
      - CRITERIA breakdown (with remarks)
      (Note: high-scoring outline is on a separate dedicated page)
    """
    W, H = page_size
    # Start from the base size returned by get_report_page_size().
    # Then grow height gradually until everything fits on a SINGLE page.
    base_height_multiplier = 1.0
    max_height_multiplier = 5.0  # safety upper bound; actual cap enforced in renderer based on memory
    height_increment = 0.05  # smaller step for a tighter fit
    
    # Try dynamic height adjustment first
    current_height_multiplier = base_height_multiplier
    font_scale = 1.0  # Keep font size constant initially
    attempt = 0
    last_pages: List[Image.Image] = []
    
    while current_height_multiplier <= max_height_multiplier:
        adjusted_page_size = (W, int(H * current_height_multiplier))
        pages, overflowed = _render_subject_report_with_scale(
            grading_result, adjusted_page_size, font_scale, refined_summary
        )
        last_pages = pages
        
        if not overflowed:
            if attempt > 0:
                print(
                    f"Subject report fit on one page with height multiplier "
                    f"{current_height_multiplier:.2f}x (height: {adjusted_page_size[1]}px)"
                )
            return pages
        
        attempt += 1
        print(
            f"Subject report overflowed (attempt {attempt}), "
            f"increasing height to {current_height_multiplier + height_increment:.2f}x..."
        )
        current_height_multiplier += height_increment
    
    # If height adjustment didn't work, fall back to font scaling (last resort)
    print(
        f"WARNING: Subject report still overflows at max height ({max_height_multiplier:.2f}x). "
        "Falling back to font scaling..."
    )
    
    # Use the maximum height we tried, but now scale fonts
    max_height_page_size = (W, int(H * max_height_multiplier))
    font_scale = 1.0
    min_scale = 0.5
    
    while font_scale >= min_scale:
        pages, overflowed = _render_subject_report_with_scale(
            grading_result, max_height_page_size, font_scale, refined_summary
        )
        last_pages = pages
        
        if not overflowed:
            print(
                f"Subject report fit after reducing font to {font_scale:.1%} "
                f"at max height ({max_height_multiplier:.2f}x)"
            )
            return pages
        
        font_scale *= 0.9  # Reduce by 10% each iteration
    
    print(
        f"WARNING: Subject report still overflows after all adjustments "
        f"(height: {max_height_multiplier:.2f}x, font: {min_scale:.1%})"
    )
    return last_pages


# --- _draw_* helpers for _render_subject_report_with_scale (5.3) ---


def _draw_subject_header(
    draw: ImageDraw.ImageDraw,
    y: int,
    W: int,
    H: int,
    margin: int,
    grading_result: Dict[str, Any],
    subject_font: ImageFont.FreeTypeFont,
    total_heading_font: ImageFont.FreeTypeFont,
    ensure_space: Any,
    draw_bold_text: Any,
) -> Tuple[int, bool]:
    """Draw subject name and total marks line. Returns (new_y, overflowed)."""
    subject = grading_result.get("subject", "")
    line_spacing = 1.3
    if subject:
        if not ensure_space(y, subject_font, 2):
            return y, True
        display_subject = str(subject).strip().replace("-", " ").title()
        draw_bold_text(f"Subject: {display_subject}", subject_font, (margin, y), "black")
        line_h_subj = subject_font.getbbox("Ag")[3] - subject_font.getbbox("Ag")[1]
        y += int(line_h_subj * line_spacing * 1.2)
    total = grading_result.get("total_marks_awarded", 0)
    maximum = grading_result.get("max_marks", DEFAULT_MAX_MARKS)
    if not ensure_space(y, total_heading_font, 1):
        return y, True
    total_label = "Total Marks Obtained:"
    total_value = f"{total} / {maximum}"
    draw_bold_text(total_label, total_heading_font, (margin, y), "#B22222")
    label_w = int(draw.textlength(total_label + " ", font=total_heading_font))
    draw_bold_text(total_value, total_heading_font, (margin + label_w, y), "#B22222")
    line_h = total_heading_font.getbbox("Ag")[3] - total_heading_font.getbbox("Ag")[1]
    y += int(line_h * line_spacing * 1.4)
    return y, False


def _draw_question_section(
    draw: ImageDraw.ImageDraw,
    y: int,
    W: int,
    H: int,
    margin: int,
    max_text_width: int,
    question: str,
    section_heading_font: ImageFont.FreeTypeFont,
    question_text_font: ImageFont.FreeTypeFont,
    ensure_space: Any,
    draw_bold_text: Any,
) -> Tuple[int, bool]:
    """Draw 'Question Statement:' heading and wrapped question text. Returns (new_y, overflowed)."""
    if not ensure_space(y, section_heading_font, 2):
        return y, True
    draw_bold_text("Question Statement:", section_heading_font, (margin, y), "black")
    line_h_section = section_heading_font.getbbox("Ag")[3] - section_heading_font.getbbox("Ag")[1]
    y += int(line_h_section * 1.3)
    if not ensure_space(y, question_text_font, 2):
        return y, True
    wrapped_question = _wrap_text(draw, question, question_text_font, max_text_width) or [""]
    line_hq = question_text_font.getbbox("Ag")[3] - question_text_font.getbbox("Ag")[1]
    line_spacing = 1.3
    for line in wrapped_question:
        if not ensure_space(y, question_text_font, 1):
            return y, True
        draw.text((margin, y), line, font=question_text_font, fill="black")
        y += int(line_hq * line_spacing)
    y += int(line_hq * 0.6)
    return y, False


def _draw_key_gaps_section(
    draw: ImageDraw.ImageDraw,
    y: int,
    W: int,
    H: int,
    margin: int,
    max_text_width: int,
    grading_result: Dict[str, Any],
    section_heading_font: ImageFont.FreeTypeFont,
    body_font: ImageFont.FreeTypeFont,
    bullet_font: ImageFont.FreeTypeFont,
    ensure_space_or_new_page: Any,
    draw_bold_text: Any,
) -> Tuple[int, bool]:
    """Draw 'Key Gaps in the Answer' and bullet list. Returns (new_y, overflowed)."""
    line_spacing = 1.3
    key_gaps = grading_result.get("overall_what_was_missing", []) or grading_result.get("key_gaps_in_answer", [])
    if isinstance(key_gaps, str):
        key_gaps = [key_gaps]
    if not key_gaps:
        key_gaps = ["No key gaps provided."]
    if not ensure_space_or_new_page(y, section_heading_font, 2):
        return y, True
    draw_bold_text("Key Gaps in the Answer", section_heading_font, (margin, y), "black")
    line_h_section = section_heading_font.getbbox("Ag")[3] - section_heading_font.getbbox("Ag")[1]
    y += int(line_h_section * line_spacing)
    y += int((body_font.getbbox("Ag")[3] - body_font.getbbox("Ag")[1]) * 0.2)
    bullet = "•"
    bullet_gap = int(0.02 * W)
    text_x = margin + bullet_gap
    text_width = max_text_width - bullet_gap
    line_hb = bullet_font.getbbox("Ag")[3] - bullet_font.getbbox("Ag")[1]
    overflowed = False
    for idx, raw in enumerate(key_gaps[:6]):
        text = str(raw).strip()
        if not text:
            continue
        wrapped = _wrap_text(draw, text, bullet_font, text_width) or [""]
        if not ensure_space_or_new_page(y, bullet_font, len(wrapped)):
            overflowed = True
            break
        draw.text((margin, y), bullet, font=bullet_font, fill="black")
        for line in wrapped:
            draw.text((text_x, y), line, font=bullet_font, fill="black")
            y += int(line_hb * line_spacing)
        y += int(line_hb * 0.2)
    y += int((body_font.getbbox("Ag")[3] - body_font.getbbox("Ag")[1]) * 0.6)
    return y, overflowed


def _draw_how_to_improve_section(
    draw: ImageDraw.ImageDraw,
    y: int,
    W: int,
    H: int,
    margin: int,
    max_text_width: int,
    grading_result: Dict[str, Any],
    section_heading_font: ImageFont.FreeTypeFont,
    body_font: ImageFont.FreeTypeFont,
    bullet_font: ImageFont.FreeTypeFont,
    ensure_space_or_new_page: Any,
    draw_bold_text: Any,
) -> Tuple[int, bool]:
    """Draw 'How to Improve' and bullet list. Returns (new_y, overflowed)."""
    line_spacing = 1.3
    how_to_improve = grading_result.get("overall_how_to_improve", []) or grading_result.get("how_to_improve", [])
    if isinstance(how_to_improve, str):
        how_to_improve = [how_to_improve]
    if not how_to_improve:
        how_to_improve = ["No improvement suggestions provided."]
    if not ensure_space_or_new_page(y, section_heading_font, 2):
        return y, True
    draw_bold_text("How to Improve", section_heading_font, (margin, y), "black")
    line_h_section = section_heading_font.getbbox("Ag")[3] - section_heading_font.getbbox("Ag")[1]
    y += int(line_h_section * line_spacing)
    y += int((body_font.getbbox("Ag")[3] - body_font.getbbox("Ag")[1]) * 0.2)
    bullet = "•"
    bullet_gap = int(0.02 * W)
    text_x = margin + bullet_gap
    text_width = max_text_width - bullet_gap
    line_hb = bullet_font.getbbox("Ag")[3] - bullet_font.getbbox("Ag")[1]
    overflowed = False
    for idx, raw in enumerate(how_to_improve[:6]):
        text = str(raw).strip()
        if not text:
            continue
        wrapped = _wrap_text(draw, text, bullet_font, text_width) or [""]
        if not ensure_space_or_new_page(y, bullet_font, len(wrapped)):
            overflowed = True
            break
        draw.text((margin, y), bullet, font=bullet_font, fill="black")
        for line in wrapped:
            draw.text((text_x, y), line, font=bullet_font, fill="black")
            y += int(line_hb * line_spacing)
        y += int(line_hb * 0.2)
    return y, overflowed


def _draw_criteria_table(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    y: int,
    W: int,
    H: int,
    margin: int,
    font_scale: float,
    grading_result: Dict[str, Any],
    section_heading_font: ImageFont.FreeTypeFont,
    body_font: ImageFont.FreeTypeFont,
    ensure_space: Any,
    new_page: Any,
    pages: List[Image.Image],
) -> Tuple[Image.Image, ImageDraw.ImageDraw, int, bool]:
    """Draw Marks Breakdown: heading, optional table or 'No criteria breakdown available.'
    May call new_page; returns (img, draw, new_y, overflowed).
    """

    def _bold(
        d: ImageDraw.ImageDraw,
        txt: str,
        font: ImageFont.FreeTypeFont,
        pos: Tuple[int, int],
        fill: str = "black",
    ) -> None:
        for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            d.text((pos[0] + dx, pos[1] + dy), txt, font=font, fill=fill)

    def _as_int_if_whole(v: float) -> str:
        try:
            fv = float(v)
        except Exception:
            return str(v)
        return str(int(fv)) if fv.is_integer() else f"{fv:.1f}"

    def _first_sentence_one_liner(text: str) -> str:
        t = (text or "").strip()
        t = re.sub(r"^(Strengths|Weaknesses):\s*", "", t, flags=re.IGNORECASE).strip()
        parts = re.split(r"[.!?]+", t)
        first = (parts[0].strip() if parts and parts[0].strip() else t).strip()
        if first and not first.endswith((".", "!", "?")):
            first += "."
        return first

    def _draw_centered_multiline(
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        lines: List[str],
        font: ImageFont.FreeTypeFont,
        *,
        fill: str = "black",
        highlight_line_idxs: Optional[set] = None,
        highlight_fill: Tuple[int, int, int] = (255, 255, 0),
        pad_px: int = 2,
        vpad_px: int = 4,
        line_adv_px: Optional[int] = None,
        highlight_pad_y_px: int = 1,
        bold: bool = False,
    ) -> None:
        highlight_line_idxs = highlight_line_idxs or set()
        line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        line_adv = line_adv_px if line_adv_px is not None else max(1, int(line_h * 1.50))
        inner_y0 = y0 + max(0, vpad_px)
        inner_y1 = y1 - max(0, vpad_px)
        inner_h = max(0, inner_y1 - inner_y0)
        total_h = len(lines) * line_adv
        start_y = inner_y0 + max(0, (inner_h - total_h) // 2)
        for i, line in enumerate(lines):
            tw = int(draw.textlength(line, font=font))
            tx = x0 + max(0, (x1 - x0 - tw) // 2)
            ty = start_y + i * line_adv
            if i in highlight_line_idxs:
                slot_y0 = ty
                slot_y1 = ty + line_adv
                rect_y0 = max(inner_y0, slot_y0 + 1)
                rect_y1 = min(inner_y1, slot_y0 + line_h + max(0, highlight_pad_y_px))
                rect_y1 = min(rect_y1, slot_y1 - 3)
                draw.rectangle(
                    [(tx - pad_px, rect_y0), (tx + tw + pad_px, rect_y1)],
                    fill=highlight_fill,
                )
            if bold:
                for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                    draw.text((tx + dx, ty + dy), line, font=font, fill=fill)
            else:
                draw.text((tx, ty), line, font=font, fill=fill)

    def _measure_wrapped_lines(font: ImageFont.FreeTypeFont, text: str, max_w: int) -> List[str]:
        return _wrap_text(draw, text, font, max_w)

    criteria_list = grading_result.get("criteria", []) or []
    if not criteria_list:
        if not ensure_space(y, section_heading_font, 2):
            return (img, draw, y, True)
        _bold(draw, "Marks Breakdown", section_heading_font, (margin, y), "black")
        heading_line_h = section_heading_font.getbbox("Ag")[3] - section_heading_font.getbbox("Ag")[1]
        y += int(heading_line_h * 1.4)

        if not ensure_space(y, body_font, 2):
            return (img, draw, y, True)
        draw.text((margin, y), "No criteria breakdown available.", font=body_font, fill="gray")
        y += int((body_font.getbbox("Ag")[3] - body_font.getbbox("Ag")[1]) * 2.0)
        return (img, draw, y, False)

    # --- Layout constants to match screenshot ---
    table_left = margin + int(0.02 * W)
    table_right = W - margin - int(0.02 * W)
    table_width = table_right - table_left

    col_category_w = int(table_width * 0.28)
    col_alloc_w = int(table_width * 0.18)
    col_obt_w = int(table_width * 0.18)
    col_remarks_w = table_width - col_category_w - col_alloc_w - col_obt_w

    x_cat0 = table_left
    x_cat1 = x_cat0 + col_category_w
    x_alloc1 = x_cat1 + col_alloc_w
    x_obt1 = x_alloc1 + col_obt_w
    x_rem1 = table_right

    grey_row = (200, 200, 200)
    white = (255, 255, 255)
    outer_w = max(2, int(2 * font_scale))
    inner_w = 1

    rows: List[Dict[str, str]] = []
    total_rows = max(6, len(criteria_list))
    for i in range(total_rows):
        crit = criteria_list[i] if i < len(criteria_list) else {}
        name = str(crit.get("name", "") or "").strip()
        max_marks = crit.get("max", "") if i < len(criteria_list) else ""
        awarded = crit.get("awarded", "") if i < len(criteria_list) else ""
        remark = crit.get("remark", "") if i < len(criteria_list) else ""

        if not remark and i < len(criteria_list) and float(awarded or 0) < float(max_marks or 0):
            remark = "Missing required content for this criterion."

        category_text = f"{i+1}. {name}".strip() if name else f"{i+1}."
        rows.append(
            {
                "category": category_text,
                "allocated": _as_int_if_whole(max_marks) if max_marks != "" else "",
                "obtained": _as_int_if_whole(awarded) if awarded != "" else "",
                "remarks": remark,
            }
        )

    heading_line_h = section_heading_font.getbbox("Ag")[3] - section_heading_font.getbbox("Ag")[1]
    gap_after_heading = max(int(0.70 * heading_line_h), int(28 * font_scale))

    preferred_body_px = max(int(56 * font_scale), 32)
    preferred_header_px = max(int(46 * font_scale), 28)

    def _table_dims_for(body_px: int, header_px: int) -> Tuple[int, int, int, int]:
        body_f = _get_font(body_px)
        head_f = _get_font(header_px)
        line_h = body_f.getbbox("Ag")[3] - body_f.getbbox("Ag")[1]
        pad = max(4, int(0.30 * line_h))
        row_h = max(int(line_h * 2.15), 40)
        header_h = max(int((head_f.getbbox("Ag")[3] - head_f.getbbox("Ag")[1]) * 2.15), 66)
        needed = (heading_line_h + gap_after_heading) + header_h + (len(rows) * row_h) + outer_w
        return row_h, header_h, pad, needed

    _pref_row_h, _pref_header_h, _pref_pad, _pref_needed = _table_dims_for(preferred_body_px, preferred_header_px)
    if y + _pref_needed > H - margin:
        pages.append(img)
        img, draw, y = new_page()

    if not ensure_space(y, section_heading_font, 2):
        return (img, draw, y, True)
    _bold(draw, "Marks Breakdown", section_heading_font, (margin, y), "black")
    y += int(heading_line_h + gap_after_heading)

    available_h = (H - margin) - y
    base_body_px = preferred_body_px
    base_header_px = preferred_header_px
    min_px = max(int(24 * font_scale), 24)

    chosen_body_px = base_body_px
    chosen_header_px = base_header_px
    chosen_pad = max(6, int(8 * font_scale))
    chosen_row_heights: List[int] = []
    chosen_header_h = 0
    chosen_body_line_h = 0
    chosen_line_adv = 0
    chosen_row_lines: List[Dict[str, List[str]]] = []

    def _layout_for(
        body_px: int, header_px: int
    ) -> Tuple[bool, int, int, int, int, List[int], List[Dict[str, List[str]]]]:
        body_f = _get_font(body_px)
        head_f = _get_font(header_px)
        line_h = body_f.getbbox("Ag")[3] - body_f.getbbox("Ag")[1]
        line_adv = max(1, int(line_h * 1.10))
        pad = max(6, int(0.35 * line_h))

        header_line_h = head_f.getbbox("Ag")[3] - head_f.getbbox("Ag")[1]
        header_line_adv = max(1, int(header_line_h * 1.50))
        header_vpad = max(12, int(header_line_h * 0.65))
        header_lines_count = 3
        header_h = max(108, (header_lines_count * header_line_adv) + 2 * header_vpad)

        row_heights = []
        row_lines = []
        for r in rows:
            cat_lines = _measure_wrapped_lines(body_f, r["category"], col_category_w - 2 * pad)
            rem_lines = _measure_wrapped_lines(body_f, r["remarks"], col_remarks_w - 2 * pad) if r["remarks"] else [""]
            needed_lines = max(len(cat_lines), len(rem_lines), 1)
            row_h = max(40, (needed_lines * line_adv) + 2 * pad)
            row_heights.append(row_h)
            row_lines.append({"cat": cat_lines, "rem": rem_lines})

        needed_total = header_h + sum(row_heights) + outer_w
        return (needed_total <= available_h, line_h, line_adv, pad, header_h, row_heights, row_lines)

    for px in range(base_body_px, min_px - 1, -1):
        header_px = max(min_px, int(px * 0.85))
        ok, line_h, line_adv, pad, header_h, row_heights, row_lines = _layout_for(px, header_px)
        if ok:
            chosen_body_px = px
            chosen_header_px = header_px
            chosen_pad = pad
            chosen_header_h = header_h
            chosen_row_heights = row_heights
            chosen_row_lines = row_lines
            chosen_body_line_h = line_h
            chosen_line_adv = line_adv
            break

    body_f = _get_font(chosen_body_px)
    head_f = _get_font(chosen_header_px)
    body_line_h = chosen_body_line_h or (body_f.getbbox("Ag")[3] - body_f.getbbox("Ag")[1])

    table_top = y
    header_y0 = table_top
    header_y1 = header_y0 + chosen_header_h
    table_bottom = header_y1 + sum(chosen_row_heights)

    draw.rectangle([(table_left, header_y0), (table_right, header_y1)], fill=white)

    header_line_h = head_f.getbbox("Ag")[3] - head_f.getbbox("Ag")[1]
    header_line_adv = max(1, int(header_line_h * 1.50))
    header_vpad = max(12, int(header_line_h * 0.65))
    _draw_centered_multiline(
        x_cat0, header_y0, x_cat1, header_y1, ["Category"],
        head_f, pad_px=max(2, int(2 * font_scale)), vpad_px=header_vpad,
        line_adv_px=header_line_adv, bold=True,
    )
    _draw_centered_multiline(
        x_cat1, header_y0, x_alloc1, header_y1, ["Allocated Marks"],
        head_f, pad_px=max(2, int(2 * font_scale)), vpad_px=header_vpad,
        line_adv_px=header_line_adv, bold=True,
    )
    _draw_centered_multiline(
        x_alloc1, header_y0, x_obt1, header_y1, ["Obtained Marks"],
        head_f, vpad_px=header_vpad, line_adv_px=header_line_adv,
        pad_px=max(2, int(2 * font_scale)), bold=True,
    )
    _draw_centered_multiline(
        x_obt1, header_y0, x_rem1, header_y1, ["Remarks"],
        head_f, vpad_px=header_vpad, line_adv_px=header_line_adv,
        pad_px=max(2, int(2 * font_scale)), bold=True,
    )

    y_cursor = header_y1
    for i, r in enumerate(rows):
        ry0 = y_cursor
        ry1 = ry0 + chosen_row_heights[i]
        fill = grey_row if (i % 2 == 0) else white
        draw.rectangle([(table_left, ry0), (table_right, ry1)], fill=fill)

        cat_lines = chosen_row_lines[i]["cat"]
        tx = x_cat0 + chosen_pad
        ty = ry0 + chosen_pad
        for line in cat_lines:
            draw.text((tx, ty), line, font=body_f, fill="black")
            ty += chosen_line_adv

        def _draw_centered_cell_text(x0: int, x1: int, text: str) -> None:
            tw = int(draw.textlength(text, font=body_f))
            txc = x0 + max(0, (x1 - x0 - tw) // 2)
            tyc = ry0 + max(0, ((ry1 - ry0) - body_line_h) // 2)
            draw.text((txc, tyc), text, font=body_f, fill="black")

        _draw_centered_cell_text(x_cat1, x_alloc1, r["allocated"])
        _draw_centered_cell_text(x_alloc1, x_obt1, r["obtained"])

        rem_lines = chosen_row_lines[i]["rem"]
        tx = x_obt1 + chosen_pad
        ty = ry0 + chosen_pad
        for line in rem_lines:
            draw.text((tx, ty), line, font=body_f, fill="black")
            ty += chosen_line_adv

        y_cursor = ry1

    for x in (x_cat1, x_alloc1, x_obt1):
        draw.line([(x, table_top), (x, table_bottom)], fill="black", width=inner_w)
    draw.line([(table_left, header_y1), (table_right, header_y1)], fill="black", width=inner_w)
    yy = header_y1
    for i in range(1, len(rows)):
        yy += chosen_row_heights[i - 1]
        draw.line([(table_left, yy), (table_right, yy)], fill="black", width=inner_w)

    draw.rectangle([(table_left, table_top), (table_right, table_bottom)], outline="black", width=outer_w)

    y = table_bottom + max(int(0.30 * body_line_h), int(18 * font_scale))
    return (img, draw, y, False)


def _render_subject_report_with_scale(
    grading_result: Dict[str, Any],
    page_size: Tuple[int, int],
    font_scale: float = 1.0,
    refined_summary: Optional[List[Dict[str, Any]]] = None,  # ADD THIS: refined rubric summary (for Length & Completeness)
) -> Tuple[List[Image.Image], bool]:
    """
    Internal helper to render subject report with a given font scale.
    Returns the rendered pages and a flag indicating whether content overflowed.
    """
    W, H = page_size

    # Safety check: prevent MemoryError from extremely large images.
    # IMPORTANT: do NOT change width here (caller expects width to remain stable).
    # Instead, cap height based on a memory budget for a single RGB image.
    max_safe_mb = 200  # ~200MB per image is reasonable
    max_h_by_memory = int((max_safe_mb * 1024 * 1024) / max(1, W * 3))
    if H > max_h_by_memory:
        print(
            f"WARNING: Requested report height {H}px exceeds safe height {max_h_by_memory}px "
            f"for width {W}px. Capping height to avoid MemoryError."
        )
        H = max_h_by_memory

    # Additional safety: cap absolute height (still width-preserving)
    max_height = 30000
    if H > max_height:
        print(f"WARNING: Report height ({H}px) exceeds hard cap ({max_height}px). Capping.")
        H = max_height
    
    margin = int(W * 0.07)
    line_spacing = 1.3

    # Base font sizes (scaled by font_scale parameter) tuned to match report screenshot.
    subject_font = _get_font(int(62 * font_scale))
    section_heading_font = _get_font(int(60 * font_scale))
    question_text_font = _get_font(int(54 * font_scale))
    body_font = _get_font(int(46 * font_scale))
    total_heading_font = _get_font(int(68 * font_scale))
    bullet_font = _get_font(int(50 * font_scale))

    pages: List[Image.Image] = []
    overflowed = False
    # IMPORTANT: The subject report must always be a SINGLE page.
    # If content doesn't fit, we signal overflow so the caller can increase height.
    max_report_pages = 1

    def new_page() -> Tuple[Image.Image, ImageDraw.ImageDraw, int]:
        try:
            img = Image.new("RGB", (W, H), "white")
            draw = ImageDraw.Draw(img)
            return img, draw, margin
        except MemoryError as e:
            # If still fails, use fallback size
            print(f"ERROR: MemoryError creating page image ({W}x{H}). Using fallback size.")
            fallback_w, fallback_h = 2977, 4211  # A4 at 200 DPI
            img = Image.new("RGB", (fallback_w, fallback_h), "white")
            draw = ImageDraw.Draw(img)
            return img, draw, int(fallback_w * 0.07)

    img, draw, y = new_page()

    def ensure_space(current_y: int, font_obj: ImageFont.FreeTypeFont, needed_lines: int) -> bool:
        nonlocal overflowed
        line_h = font_obj.getbbox("Ag")[3] - font_obj.getbbox("Ag")[1]
        if current_y + line_h * needed_lines * line_spacing > H - margin:
            overflowed = True
            return False
        return True

    def ensure_space_or_new_page(current_y: int, font_obj: ImageFont.FreeTypeFont, needed_lines: int) -> bool:
        nonlocal img, draw, y, overflowed, pages
        line_h = font_obj.getbbox("Ag")[3] - font_obj.getbbox("Ag")[1]
        if current_y + line_h * needed_lines * line_spacing <= H - margin:
            return True
        overflowed = True
        return False

    def draw_bold_text(
        text: str,
        font_obj: ImageFont.FreeTypeFont,
        position: Tuple[int, int],
        fill: str,
    ) -> None:
        offsets = [(0, 0), (1, 0), (0, 1), (1, 1)]
        for dx, dy in offsets:
            draw.text((position[0] + dx, position[1] + dy), text, font=font_obj, fill=fill)

    max_text_width = W - 2 * margin

    question = _strip_question_prefix(grading_result.get("question_statement") or "") or "No question statement provided."

    y, over = _draw_subject_header(draw, y, W, H, margin, grading_result, subject_font, total_heading_font, ensure_space, draw_bold_text)
    if over:
        return [img], True

    y, over = _draw_question_section(draw, y, W, H, margin, max_text_width, question, section_heading_font, question_text_font, ensure_space, draw_bold_text)
    if over:
        return [img], True

    img, draw, y, over = _draw_criteria_table(img, draw, y, W, H, margin, font_scale, grading_result, section_heading_font, body_font, ensure_space, new_page, pages)
    if over:
        return [img], True

    y, over = _draw_key_gaps_section(draw, y, W, H, margin, max_text_width, grading_result, section_heading_font, body_font, bullet_font, ensure_space_or_new_page, draw_bold_text)
    overflowed = overflowed or over

    y, over = _draw_how_to_improve_section(draw, y, W, H, margin, max_text_width, grading_result, section_heading_font, body_font, bullet_font, ensure_space_or_new_page, draw_bold_text)
    overflowed = overflowed or over

    pages.append(img)
    return pages, overflowed




# -----------------------------
# REFINED RUBRIC SUMMARY PAGE
# -----------------------------


def render_refined_rubric_summary_page(
    refined_summary: List[Dict[str, Any]],
    page_size: Tuple[int, int] = (2480, 3508),
) -> Image.Image:
    """
    Render a single page summarizing each refined rubric point:

      - Introduction Quality – rating + comment
      - Headings & Subheadings – rating + comment
      - etc.
    """
    W, H = page_size
    margin = int(W * 0.07)
    line_spacing = 1.4

    title_font = _get_font(72)
    h2_font = _get_font(56)
    body_font = _get_font(44)

    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    y = margin

    max_text_width = W - 2 * margin

    # Title
    draw.text((margin, y), "Refined Rubric Feedback", font=title_font, fill="black")
    y += int((title_font.getbbox("Ag")[3] - title_font.getbbox("Ag")[1]) * line_spacing * 1.5)

    # For each rubric point
    for item in refined_summary:
        rid = item.get("id", "")
        name = item.get("name", rid)
        rating = (item.get("rating") or "").capitalize()
        comment = item.get("comment") or ""

        header = f"{name} — {rating}"
        header_height = h2_font.getbbox("Ag")[3] - h2_font.getbbox("Ag")[1]
        if y + header_height * 4 > H - margin:
            # if page overflow ever needed, here we could create more pages
            # but for now, just stop rendering
            break

        draw.text((margin, y), header, font=h2_font, fill="black")
        y += int(header_height * line_spacing)

        line_hb = body_font.getbbox("Ag")[3] - body_font.getbbox("Ag")[1]
        for line in _wrap_text(draw, comment, body_font, max_text_width):
            draw.text((margin, y), line, font=body_font, fill="black")
            y += int(line_hb * line_spacing)

        y += int(line_hb * 0.8)

    return img


# -----------------------------------------
# PAGE: HIGH-SCORING IDEAL OUTLINE
# -----------------------------------------

def render_high_scoring_outline_page(
    high_scoring_outline: Dict[str, Any],
    page_size: Tuple[int, int] = (2480, 3508),
) -> Image.Image:
    """
    Render a dedicated page for the "High-Scoring Ideal Outline" with structured headings and detailed content.

    The outline supports:
    - Section headings with descriptions (Format: "Heading: Description")
    - Regular bullet points for additional content
    - Proper indentation and visual hierarchy
    - Multi-line text wrapping with appropriate spacing
    """
    W, H = page_size
    margin = int(W * 0.07)
    line_spacing = 1.5

    title_font = _get_font(72)
    h3_font = _get_font(52)
    body_font = _get_font(44)

    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    y = margin

    max_text_width = W - 2 * margin

    # Title (always use fixed title, never extend it)
    outline_title = "High-Scoring Ideal Outline"
    draw.text((margin, y), outline_title, font=title_font, fill="black")
    line_h_title = title_font.getbbox("Ag")[3] - title_font.getbbox("Ag")[1]
    y += int(line_h_title * line_spacing * 1.5)

    outline_points = high_scoring_outline.get("outline_points", [])

    structured_points: List[Dict[str, Any]] = []
    for raw_point in outline_points:
        heading = ""
        summary = ""
        key_points: List[str] = []
        if isinstance(raw_point, dict):
            heading = str(
                raw_point.get("heading")
                or raw_point.get("title")
                or raw_point.get("section")
                or ""
            ).strip()
            summary = str(
                raw_point.get("summary")
                or raw_point.get("description")
                or raw_point.get("overview")
                or ""
            ).strip()
            raw_key_points = (
                raw_point.get("key_points")
                or raw_point.get("bullets")
                or raw_point.get("points")
                or []
            )
            if isinstance(raw_key_points, str):
                if raw_key_points.strip():
                    key_points = [raw_key_points.strip()]
            else:
                key_points = [str(p).strip() for p in raw_key_points if p]
        else:
            text = str(raw_point).strip()
            if ":" in text:
                parts = text.split(":", 1)
                heading = parts[0].strip()
                summary = parts[1].strip()
            else:
                summary = text
        structured_points.append(
            {"heading": heading, "summary": summary, "key_points": key_points}
        )

    structured_points = [
        pt for pt in structured_points if pt["heading"] or pt["summary"] or pt["key_points"]
    ]

    if not structured_points:
        line_hb = body_font.getbbox("Ag")[3] - body_font.getbbox("Ag")[1]
        draw.text((margin, y), "No outline provided", font=body_font, fill="gray")
        return img

    line_hb = body_font.getbbox("Ag")[3] - body_font.getbbox("Ag")[1]
    h2_font = _get_font(56)
    line_h2 = h2_font.getbbox("Ag")[3] - h2_font.getbbox("Ag")[1]

    for point in structured_points:
        heading_text = point.get("heading", "")
        summary_text = point.get("summary", "")
        bullet_items = point.get("key_points", [])

        if heading_text:
            if y + line_h2 * 2 > H - margin:
                break
            draw.text((margin, y), heading_text, font=h2_font, fill="darkblue")
            y += int(line_h2 * line_spacing)

        if summary_text:
            wrapped_lines = _wrap_text(
                draw, summary_text, body_font, max_text_width - int(0.05 * W)
            )
            for line in wrapped_lines:
                if y + line_hb > H - margin:
                    break
                draw.text((margin + int(0.03 * W), y), line, font=body_font, fill="black")
                y += int(line_hb * line_spacing)

        if bullet_items:
            for bullet in bullet_items:
                bullet_lines = _wrap_text(
                    draw, str(bullet).strip(), body_font, max_text_width - int(0.08 * W)
                )
                for idx, line in enumerate(bullet_lines):
                    if y + line_hb > H - margin:
                        break
                    if idx == 0:
                        draw.text(
                            (margin + int(0.04 * W), y), f"• {line}", font=body_font, fill="black"
                        )
                    else:
                        draw.text(
                            (margin + int(0.08 * W), y), line, font=body_font, fill="black"
                        )
                    y += int(line_hb * line_spacing)

        y += int(line_hb * 0.7)

    return img


# -----------------------------
# MAIN PIPELINE
# -----------------------------


def grade_pdf_answer(
    pdf_path: str,
    subject: str,
    output_json_path: str,
    output_pdf_path: str,
    user_id: Optional[str] = None,
    log_path: Optional[str] = None,
    request_id: Optional[str] = None,
    progress_tracker: Optional[Any] = None,
) -> None:
    start_ts = time.perf_counter()
    request_id = request_id or uuid.uuid4().hex[:8]
    _append_log(
        log_path,
        "INFO",
        f"request={request_id} start pdf={os.path.basename(pdf_path)} subject={subject}",
    )

    # Initialize progress tracker if not provided
    if progress_tracker is None:
        logs_dir = os.path.dirname(log_path) if log_path else None
        progress_tracker = OCRProgressTracker(logs_dir=logs_dir)
    
    # Dictionary to store step timings
    step_timings: Dict[str, float] = {}
    
    # Total steps in pipeline
    TOTAL_STEPS = 12

    # Validate all inputs before processing
    print("Validating inputs...")
    validate_input_paths(pdf_path, output_json_path, output_pdf_path)

    # Validate subject name
    if not subject or len(subject.strip()) == 0:
        raise ValueError("Subject name cannot be empty")

    # Create unique output directory per request to prevent concurrent process conflicts
    output_dir = os.path.join(tempfile.gettempdir(), f"grok_images_{request_id}")
    os.makedirs(output_dir, exist_ok=True)
    print(f"Created unique temp directory: {output_dir}")

    try:
        grok_key, vision_client = load_environment()

        print("Step 1: Converting PDF pages to images (for Grok)...")
        progress_tracker.update_progress(
            request_id=request_id,
            step="Converting PDF to images",
            step_number=1,
            total_steps=TOTAL_STEPS,
            progress_percent=5.0,
            message="Converting PDF pages to images...",
        )
        step_start = time.perf_counter()
        page_images = pdf_to_page_images_for_grok(pdf_path, output_dir=output_dir)
        step_duration = time.perf_counter() - step_start
        step_timings["Step 1: Convert PDF to images"] = step_duration
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=1 name=convert_pdf_images duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")

        print("Step 2: Running OCR on PDF (Google Vision)...")
        progress_tracker.update_progress(
            request_id=request_id,
            step="OCR Processing",
            step_number=2,
            total_steps=TOTAL_STEPS,
            progress_percent=15.0,
            message="Running OCR on PDF pages...",
            details={"pages_completed": 0, "total_pages": len(page_images)},
        )
        step_start = time.perf_counter()
        ocr_config = _get_ocr_config()
        ocr_data = run_ocr_on_pdf(
            vision_client=vision_client,
            pdf_path=pdf_path,
            log_path=log_path,
            request_id=request_id,
            progress_tracker=progress_tracker,
            append_log=_append_log,
            **ocr_config,
        )
        step_duration = time.perf_counter() - step_start
        step_timings["Step 2: Google Vision OCR"] = step_duration
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=2 name=google_vision_ocr duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")
        
        # Update progress after OCR completes
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="OCR Processing",
                step_number=2,
                total_steps=TOTAL_STEPS,
                progress_percent=45.0,
                message="OCR processing complete",
            )

        print("Step 3: Detecting sections/headings with Grok...")
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Detecting sections",
                step_number=3,
                total_steps=TOTAL_STEPS,
                progress_percent=50.0,
                message="Detecting sections and headings...",
            )
        step_start = time.perf_counter()
        sections, section_token_usage = call_grok_for_section_detection(
            grok_api_key=grok_key,
            ocr_data=ocr_data,
            page_images=page_images,
        )
        step_duration = time.perf_counter() - step_start
        step_timings["Step 3: Grok section detection"] = step_duration
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=3 name=grok_section_detection duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")

        # Debug dump (only if DEBUG_SECTIONS environment variable is set)
        # Disabled by default in production to prevent file accumulation and OOM errors
        if os.getenv("DEBUG_SECTIONS", "").lower() in ("true", "1", "yes"):
            debug_dump_sections(sections, output_path="debug_sections.json", log_path=log_path)

        # Track total token usage
        total_input_tokens = section_token_usage.get("input_tokens", 0)
        total_output_tokens = section_token_usage.get("output_tokens", 0)

        print("Step 4: Loading subject-wise rubric DOCX...")
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Loading rubric",
                step_number=4,
                total_steps=TOTAL_STEPS,
                progress_percent=55.0,
                message="Loading subject rubric...",
            )
        step_start = time.perf_counter()
        subject_rubric_text, subject_rubric_path = load_subject_rubric_text(subject)
        step_duration = time.perf_counter() - step_start
        step_timings["Step 4: Load subject rubric"] = step_duration
        if subject_rubric_path:
            print(f"Using subject rubric file: {subject_rubric_path}")
        else:
            print("Warning: No subject rubric file found; grading will be weaker.")
            _append_log(
                log_path,
                "WARNING",
                f"request={request_id} missing_subject_rubric subject={subject}",
            )
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=4 name=load_subject_rubric duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")

        print("Step 5: Calling Grok for subject-wise grading...")
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Subject grading",
                step_number=5,
                total_steps=TOTAL_STEPS,
                progress_percent=60.0,
                message="Grading with subject rubric...",
            )
        step_start = time.perf_counter()
        grading_result, grading_token_usage = call_grok_for_grading(
            grok_api_key=grok_key,
            subject=subject,
            subject_rubric_text=subject_rubric_text,
            ocr_data=ocr_data,
            sections=sections,
            page_images=page_images,
        )
        step_duration = time.perf_counter() - step_start
        step_timings["Step 5: Grok subject grading"] = step_duration
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=5 name=grok_subject_grading duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")

        # Accumulate token usage
        total_input_tokens += grading_token_usage.get("input_tokens", 0)
        total_output_tokens += grading_token_usage.get("output_tokens", 0)

        grading_result.setdefault("subject", subject)
        if not grading_result.get("max_marks"):
            grading_result["max_marks"] = DEFAULT_MAX_MARKS

        # Validate and adjust marks to ensure fairness (only adjusts if there's clear mismatch)
        grading_result = validate_and_adjust_grading_result(grading_result)

        # Save unfiltered OCR text BEFORE any processing/filtering
        try:
            ocr_filepath = save_unfiltered_ocr_text(
                ocr_data=ocr_data,
                request_id=request_id,
                subject=subject,
            )
            if ocr_filepath:
                _append_log(
                    log_path,
                    "INFO",
                    f"request={request_id} unfiltered_ocr_saved file={ocr_filepath}",
                )
        except Exception as e:
            print(f"  ⚠ Warning: Failed to save unfiltered OCR text: {e}")
            _append_log(
                log_path,
                "WARNING",
                f"request={request_id} unfiltered_ocr_save_failed error={e}",
            )

        print("Step 6: Analyzing mark deductions...")
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Mark deduction analysis",
                step_number=6,
                total_steps=TOTAL_STEPS,
                progress_percent=62.0,
                message="Analyzing why marks were lost...",
            )
        step_start = time.perf_counter()
        try:
            mark_deduction_analysis, mark_analysis_token_usage = call_grok_for_mark_deduction_analysis(
                grok_api_key=grok_key,
                grading_result=grading_result,
                subject_rubric_text=subject_rubric_text,
                ocr_data=ocr_data,
                sections=sections,
            )
            
            # Merge mark deduction analysis into grading_result for report rendering.
            grading_result["overall_why_marks_lost"] = mark_deduction_analysis.get("overall_why_marks_lost", [])
            grading_result["overall_what_was_missing"] = mark_deduction_analysis.get("overall_what_was_missing", [])
            grading_result["overall_how_to_improve"] = mark_deduction_analysis.get("overall_how_to_improve", [])
            grading_result["priority_improvements"] = mark_deduction_analysis.get("priority_improvements", [])
            
            # Save to Tests folder (only if SAVE_TEST_FILES is enabled)
            analysis_filepath = save_mark_deduction_analysis_to_tests(
                analysis_result=mark_deduction_analysis,
                request_id=request_id,
                subject=subject,
            )
            
            step_duration = time.perf_counter() - step_start
            step_timings["Step 6: Mark deduction analysis"] = step_duration
            _append_log(
                log_path,
                "INFO",
                f"request={request_id} step=6 name=mark_deduction_analysis duration_ms={int(step_duration * 1000)} file={analysis_filepath}",
            )
            print(f"  ✓ Completed in {_format_time(step_duration)}")
            if analysis_filepath:
                print(f"  ✓ Analysis saved to: {analysis_filepath}")
            
            # Accumulate token usage
            total_input_tokens += mark_analysis_token_usage.get("input_tokens", 0)
            total_output_tokens += mark_analysis_token_usage.get("output_tokens", 0)
            
        except Exception as e:
            # Don't fail the pipeline; use empty lists so report still renders.
            grading_result["overall_why_marks_lost"] = []
            grading_result["overall_what_was_missing"] = []
            grading_result["overall_how_to_improve"] = []
            grading_result["priority_improvements"] = []
            print(f"  ⚠ Warning: Mark deduction analysis failed: {e}")
            _append_log(
                log_path,
                "WARNING",
                f"request={request_id} mark_deduction_analysis_failed error={e}",
            )

        # Token usage and grading JSON save (after mark deduction so JSON includes it)
        grading_result["token_usage"] = {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
        }
        print(f"Token usage: Input: {total_input_tokens}, Output: {total_output_tokens}")

        print("Saving grading JSON...")
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(grading_result, f, ensure_ascii=False, indent=2)
        print(f"Grading JSON saved to {output_json_path}")
        
        # Also save grading JSON to logs folder for easy access
        try:
            if log_path:
                logs_dir = os.path.dirname(log_path)
            else:
                current_file_dir = os.path.dirname(os.path.abspath(__file__))
                project_root = os.path.abspath(os.path.join(current_file_dir, "..", ".."))
                logs_dir = os.path.join(project_root, "logs")
            
            os.makedirs(logs_dir, exist_ok=True)
            logs_grading_json_path = os.path.join(logs_dir, f"grading_{request_id}.json")
            with open(logs_grading_json_path, "w", encoding="utf-8") as f:
                json.dump(grading_result, f, ensure_ascii=False, indent=2)
            print(f"Grading JSON also saved to logs folder: {logs_grading_json_path}")
            _append_log(
                log_path,
                "INFO",
                f"request={request_id} grading_json_saved_to_logs file={logs_grading_json_path}",
            )
        except Exception as e:
            print(f"  ⚠ Warning: Failed to save grading JSON to logs folder: {e}")
            _append_log(
                log_path,
                "WARNING",
                f"request={request_id} grading_json_logs_save_failed error={e}",
            )

        print("Step 7: Rendering subject-wise report pages...")
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Rendering report",
                step_number=7,
                total_steps=TOTAL_STEPS,
                progress_percent=70.0,
                message="Rendering subject report pages...",
            )
        step_start = time.perf_counter()
        report_page_size = get_report_page_size(pdf_path)
        # render_subject_report_pages now dynamically adjusts height to fit content perfectly
        # It starts with 1.2x height and increases incrementally until content fits
        # Initial render without refined_summary (will be added later)
        subject_report_pages = render_subject_report_pages(
            grading_result,
            page_size=report_page_size,  # Base size - function will adjust height dynamically
            refined_summary=None,  # Will be updated after refined_summary is generated
        )
        step_duration = time.perf_counter() - step_start
        step_timings["Step 7: Render subject report pages"] = step_duration
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=7 name=render_subject_report_pages duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")

        print("Step 8: Loading refined rubric DOCX...")
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Loading refined rubric",
                step_number=8,
                total_steps=TOTAL_STEPS,
                progress_percent=75.0,
                message="Loading refined rubric...",
            )
        step_start = time.perf_counter()
        refined_rubric_text, refined_rubric_path = load_refined_rubric_text()
        step_duration = time.perf_counter() - step_start
        step_timings["Step 8: Load refined rubric"] = step_duration
        if refined_rubric_path:
            print(f"Using refined rubric file: {refined_rubric_path}")
        else:
            print("Warning: No refined rubric file found; annotations will be weaker.")
            _append_log(
                log_path,
                "WARNING",
                f"request={request_id} missing_refined_rubric",
            )
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=8 name=load_refined_rubric duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")

        print("Step 9: Calling Grok for refined rubric annotations...")
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Refined annotations",
                step_number=9,
                total_steps=TOTAL_STEPS,
                progress_percent=80.0,
                message="Generating refined annotations...",
            )
        step_start = time.perf_counter()
        refined_result, refined_token_usage = call_grok_for_refined_rubric_annotations(
            grok_api_key=grok_key,
            refined_rubric_text=refined_rubric_text,
            ocr_data=ocr_data,
            sections=sections,
            page_images=page_images,
        )
        step_duration = time.perf_counter() - step_start
        step_timings["Step 9: Grok refined annotations"] = step_duration
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=9 name=grok_refined_annotations duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")

        # Accumulate token usage
        total_input_tokens += refined_token_usage.get("input_tokens", 0)
        total_output_tokens += refined_token_usage.get("output_tokens", 0)

        annotations = refined_result.get("annotations", []) or []
        refined_summary = refined_result.get("refined_rubric_summary", []) or []

        def _normalize_heading_key(text: str) -> str:
            return re.sub(r"\s+", " ", (text or "").strip().lower())
        
        def _is_intro_or_conclusion_heading(text: str) -> bool:
            """Check if a heading text is Introduction or Conclusion (or common variations)."""
            if not text:
                return False
            normalized = _normalize_heading_key(text)
            # Check for common variations including ones with missing first letter due to prefix stripping
            intro_patterns = ["introduction", "ntroduction", "intro"]
            conclusion_patterns = ["conclusion", "onclusion", "conclude"]
            return any(pattern in normalized for pattern in intro_patterns) or \
                   any(pattern in normalized for pattern in conclusion_patterns)

        heading_corrections: Dict[str, str] = {}
        for sec in sections:
            exact_heading = sec.get("exact_ocr_heading") or ""
            title = sec.get("title") or ""
            if exact_heading and title:
                heading_corrections[_normalize_heading_key(exact_heading)] = title

        for ann in annotations:
            if (ann.get("type") or "").lower() != "heading_issue":
                continue
            if (ann.get("correction") or "").strip():
                continue
            target_heading = ann.get("target_word_or_sentence") or ""
            normalized_target = _normalize_heading_key(target_heading)
            fallback = heading_corrections.get(normalized_target)
            if fallback:
                ann["correction"] = fallback

        # Re-render subject report pages with refined_summary (Length & Completeness)
        # This ensures Length & Completeness appears at the end of the report page
        # Height will be dynamically adjusted again to fit the additional content
        if refined_summary:
            print("Re-rendering subject report pages with Length & Completeness...")
            subject_report_pages = render_subject_report_pages(
                grading_result,
                page_size=report_page_size,  # Base size - function will adjust height dynamically
                refined_summary=refined_summary,
            )

        # Validate refined summary schema
        if refined_summary:
            try:
                validate_refined_summary(refined_summary)
                print(f"Validated {len(refined_summary)} refined rubric summary items")
            except ValueError as e:
                print(f"WARNING: Refined summary validation failed: {e}")
                _append_log(
                    log_path,
                    "WARNING",
                    f"request={request_id} refined_summary_validation_failed error={e}",
                )

        # Filter annotations to only show specific rubric points:
        # 1. Introduction (introduction_quality)
        # 2. Heading & Subheadings (headings_subheadings)
        # 4. Factual Accuracy (factual_accuracy)
        # Grammar & Language (grammar_language) - shown as boxes only (no side comments)
        allowed_rubric_points = {
            "introduction_quality",
            "headings_subheadings",
            "factual_accuracy",
            "grammar_language",  # Allowed but only shows box on text (no side comment)
        }
        
        filtered_annotations = []
        for ann in annotations:
            rubric_point = (ann.get("rubric_point") or "").lower().strip()
            ann_type = (ann.get("type") or "").lower()
            
            # Only keep annotations with allowed rubric points
            if rubric_point not in allowed_rubric_points:
                continue
            
            # Also filter out Introduction/Conclusion heading annotations (they're extra and not needed)
            if ann_type == "heading_issue":
                target_heading = ann.get("target_word_or_sentence") or ""
                correction = ann.get("correction") or ""
                # Skip if target heading or correction is Introduction/Conclusion
                if _is_intro_or_conclusion_heading(target_heading) or \
                   _is_intro_or_conclusion_heading(correction):
                    continue
            
            filtered_annotations.append(ann)
        annotations = filtered_annotations
        
        # Validate annotations schema
        valid_annotations = []
        for idx, ann in enumerate(annotations):
            if validate_annotation(ann, idx):
                valid_annotations.append(ann)
        if len(valid_annotations) < len(annotations):
            skipped_count = len(annotations) - len(valid_annotations)
            print(f"WARNING: {skipped_count} annotations failed validation and were skipped")
            _append_log(
                log_path,
                "WARNING",
                f"request={request_id} annotation_validation_skipped count={skipped_count}",
            )
        annotations = valid_annotations
        
        # Save annotations to Tests folder (only if SAVE_TEST_FILES is enabled)
        if annotations:
            try:
                annotations_filepath = save_annotations_to_tests(
                    annotations=annotations,
                    request_id=request_id,
                    subject=subject,
                )
                if annotations_filepath:
                    _append_log(
                        log_path,
                        "INFO",
                        f"request={request_id} annotations_saved file={annotations_filepath} count={len(annotations)}",
                    )
            except Exception as e:
                print(f"  ⚠ Warning: Failed to save annotations: {e}")
                _append_log(
                    log_path,
                    "WARNING",
                    f"request={request_id} annotations_save_failed error={e}",
                )

        print("Step 10: Calling Grok for page-wise improvement suggestions...")
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Page suggestions",
                step_number=10,
                total_steps=TOTAL_STEPS,
                progress_percent=85.0,
                message="Generating page-wise suggestions...",
            )
        step_start = time.perf_counter()
        page_suggestions_result, page_suggestions_token_usage = call_grok_for_page_wise_suggestions(
            grok_api_key=grok_key,
            subject=subject,
            subject_rubric_text=subject_rubric_text,
            ocr_data=ocr_data,
            sections=sections,
        )
        step_duration = time.perf_counter() - step_start
        step_timings["Step 10: Grok page suggestions"] = step_duration
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=10 name=grok_page_suggestions duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")

        # Accumulate token usage
        total_input_tokens += page_suggestions_token_usage.get("input_tokens", 0)
        total_output_tokens += page_suggestions_token_usage.get("output_tokens", 0)

        page_suggestions = page_suggestions_result.get("page_suggestions", []) or []

        print("Step 11: Annotating answer pages with improvement suggestions...")
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Annotating pages",
                step_number=11,
                total_steps=TOTAL_STEPS,
                progress_percent=90.0,
                message="Annotating answer pages...",
            )
        step_start = time.perf_counter()
        annotated_answer_pages = annotate_pdf_answer_pages(
            pdf_path=pdf_path,
            ocr_data=ocr_data,
            sections=sections,
            annotations=annotations,
            page_suggestions=page_suggestions,
            log_path=log_path,
            request_id=request_id,
        )
        step_duration = time.perf_counter() - step_start
        step_timings["Step 11: Annotate answer pages"] = step_duration
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=11 name=annotate_answer_pages duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")

        #print("Step 11: Rendering refined rubric summary page...")
        #refined_summary_page = render_refined_rubric_summary_page(refined_summary)
        # Assemble final PDF incrementally to avoid memory accumulation:
        #   1) Subject report pages (includes marks table)
        #   2) Annotated answer pages (with left-side improvement suggestions)
        #   3) Refined rubric summary page

        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Writing PDF",
                step_number=12,
                total_steps=TOTAL_STEPS,
                progress_percent=95.0,
                message="Writing final PDF...",
            )
        step_start = time.perf_counter()
        print(f"Step 12: Writing final PDF incrementally to {output_pdf_path} ...")
        
        # Create PDF writer for incremental writing
        pdf_writer = PdfWriter()
        
        # Helper function to convert PIL Image to PDF bytes and add to writer
        def add_image_to_pdf(img: Image.Image) -> None:
            """Convert PIL Image to PDF bytes and add to PDF writer incrementally."""
            buffer = io.BytesIO()
            img.save(buffer, format="PDF", resolution=300.0)
            buffer.seek(0)
            pdf_reader = PdfReader(buffer)
            for page in pdf_reader.pages:
                pdf_writer.add_page(page)
        
        # Add subject report pages (small, single page, acceptable to keep in memory)
        for page in subject_report_pages:
            add_image_to_pdf(page)
        
        # Add annotated answer pages incrementally (don't accumulate in memory)
        for page in annotated_answer_pages:
            add_image_to_pdf(page)
        
        # Write final PDF
        with open(output_pdf_path, "wb") as output_file:
            pdf_writer.write(output_file)
        
        step_duration = time.perf_counter() - step_start
        step_timings["Step 12: Write final PDF"] = step_duration
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} step=12 name=merge_and_write_pdf duration_ms={int(step_duration * 1000)}",
        )
        print(f"  ✓ Completed in {_format_time(step_duration)}")
        
        # Update progress to complete
        if progress_tracker:
            progress_tracker.update_progress(
                request_id=request_id,
                step="Complete",
                step_number=12,
                total_steps=TOTAL_STEPS,
                progress_percent=100.0,
                message="✅ Evaluation complete!",
            )
        
        # Calculate total time
        total_duration = time.perf_counter() - start_ts
        
        # Print comprehensive timing report
        print("\n" + "="*70)
        print("TIMING REPORT")
        print("="*70)
        for step_name, duration in step_timings.items():
            print(f"  {step_name:.<50} {_format_time(duration)}")
        print("-"*70)
        print(f"  {'TOTAL TIME':.<50} {_format_time(total_duration)}")
        print("="*70 + "\n")
        
        # Log the timing report
        timing_report_lines = [
            f"request={request_id} TIMING_REPORT_START",
        ]
        for step_name, duration in step_timings.items():
            timing_report_lines.append(
                f"request={request_id} {step_name} duration={_format_time(duration)} duration_sec={duration:.2f}"
            )
        timing_report_lines.append(
            f"request={request_id} TOTAL_TIME duration={_format_time(total_duration)} duration_sec={total_duration:.2f}"
        )
        timing_report_lines.append(f"request={request_id} TIMING_REPORT_END")
        
        for line in timing_report_lines:
            _append_log(log_path, "INFO", line)
        
        _append_log(
            log_path,
            "INFO",
            f"request={request_id} completed total_duration_ms={int(total_duration * 1000)}",
        )
    except Exception as exc:
        _append_log(
            log_path,
            "ERROR",
            f"request={request_id} error={exc} traceback={traceback.format_exc().strip()}",
        )
        raise

    finally:
        # Clean up unique temp directory
        if os.path.exists(output_dir):
            try:
                shutil.rmtree(output_dir)
                print(f"Cleaned up temp directory: {output_dir}")
            except Exception as e:
                print(f"WARNING: Failed to remove temp directory {output_dir}: {e}")
                _append_log(
                    log_path,
                    "WARNING",
                    f"request={request_id} temp_dir_cleanup_failed error={e}",
                )

# -----------------------------
# CLI
# -----------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Grade a handwritten PDF answer using Grok + Google Vision OCR, "
        "generate a subject-wise report, annotate the answer pages, "
        "and add a refined-rubric summary page."
    )
    parser.add_argument("--pdf_path", required=True, help="Path to the PDF file to grade.")
    parser.add_argument("--subject", required=True, help="Subject name, e.g., 'British History'.")
    parser.add_argument(
        "--output_json",
        default="grading_result.json",
        help="Path to write the grading JSON output.",
    )
    parser.add_argument(
        "--output_pdf",
        default="result.pdf",
        help="Path to write the final PDF (report + annotated pages + rubric summary).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        args = parse_args()
        grade_pdf_answer(
            pdf_path=args.pdf_path,
            subject=args.subject,
            output_json_path=args.output_json,
            output_pdf_path=args.output_pdf,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
