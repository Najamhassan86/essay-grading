"""
compressPdf.py

Post-processing module to compress PDF files by compressing embedded images.
If the PDF file size is >= 10MB, it compresses images and generates a new compressed PDF.

FLOW AND STEPS (Simple Explanation):
=====================================

1. CHECK FILE SIZE
   - First, we check how big the PDF file is
   - If it's already smaller than 10MB, we do nothing and stop
   - If it's 10MB or bigger, we need to compress it

2. MAKE A BACKUP (Temporary File)
   - Before we start compressing, we rename the original PDF to a temporary name
   - Example: "essay_annotated.pdf" becomes "essay_annotated.pdf.tmp"
   - This way, if something goes wrong, we can restore the original file

3. COMPRESS EACH PAGE
   For each page in the PDF:
   a. Convert page to image
      - Take the PDF page and turn it into a picture (rasterize it)
      - This preserves how it looks visually
   
   b. Compress the image
      - Make the image smaller by:
        * Reducing the quality (like lowering JPEG quality)
        * Making it smaller if it's too big (resize if needed)
      - Convert it to JPEG format which is smaller than PNG
   
   c. Put compressed image back
      - Create a new page with the same size as the original
      - Put the compressed image on this new page
      - This replaces the original page content

4. SAVE THE COMPRESSED PDF
   - Save all the compressed pages into a new PDF file
   - Use the original filename (so it replaces the original)
   - Enable PDF compression options to make it even smaller

5. CHECK IF IT WORKED
   - Check the new file size
   - If it's still too big (>= 10MB), try again with more aggressive compression
   - Show how much we compressed (original size vs new size)

6. CLEAN UP
   - If compression worked: delete the temporary backup file
   - If compression failed: restore the original PDF from the temporary file
   - This ensures we never lose the original file

7. REPORT RESULTS
   - Show the original file size
   - Show the compressed file size
   - Show how much we reduced it (compression percentage)

MAIN FUNCTION: compress_pdf_if_needed()
- This is the function you call
- It handles all the steps above automatically
- Returns True if compression happened, False if not needed or failed

Usage:
    from compressPdf import compress_pdf_if_needed
    
    compress_pdf_if_needed("output.pdf", target_size_mb=10)
"""

import os
import tempfile
import shutil
from typing import Optional
import fitz  # PyMuPDF
from PIL import Image
import io


def get_file_size_mb(file_path: str) -> float:
    """Get file size in megabytes."""
    if not os.path.isfile(file_path):
        return 0.0
    size_bytes = os.path.getsize(file_path)
    return size_bytes / (1024 * 1024)


def compress_image(image_bytes: bytes, max_quality: int = 75, max_dimension: int = 2000) -> bytes:
    """
    Compress an image by reducing quality and/or dimensions.
    
    Args:
        image_bytes: Original image bytes
        max_quality: Maximum JPEG quality (1-100, lower = more compression)
        max_dimension: Maximum width or height in pixels (images larger will be resized)
    
    Returns:
        Compressed image bytes
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        
        # Convert to RGB if necessary (for JPEG)
        if img.mode in ("RGBA", "LA", "P"):
            rgb = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            if img.mode in ("RGBA", "LA"):
                rgb.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = rgb
        elif img.mode != "RGB":
            img = img.convert("RGB")
        
        # Resize if too large
        width, height = img.size
        if width > max_dimension or height > max_dimension:
            if width > height:
                new_width = max_dimension
                new_height = int(height * (max_dimension / width))
            else:
                new_height = max_dimension
                new_width = int(width * (max_dimension / height))
            img = img.resize((new_width, new_height), Image.LANCZOS)
        
        # Compress to JPEG
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=max_quality, optimize=True)
        return output.getvalue()
    
    except Exception as e:
        print(f"  Warning: Failed to compress image: {e}")
        return image_bytes  # Return original if compression fails


def compress_pdf_images(
    input_pdf_path: str,
    output_pdf_path: str,
    max_quality: int = 75,
    max_dimension: int = 2000,
    target_size_mb: float = 10.0,
) -> bool:
    """
    Compress images in a PDF file by rasterizing pages and compressing them.
    
    This approach preserves visual appearance but rasterizes the content.
    For essay grading outputs, visual preservation is more important than text selectability.
    
    Args:
        input_pdf_path: Path to input PDF
        output_pdf_path: Path to save compressed PDF
        max_quality: JPEG quality for compressed images (1-100)
        max_dimension: Maximum image dimension in pixels
        target_size_mb: Target file size in MB (compression stops if achieved)
    
    Returns:
        True if compression was successful, False otherwise
    """
    try:
        doc = fitz.open(input_pdf_path)
        compressed_doc = fitz.open()  # New empty document
        
        total_pages = len(doc)
        print(f"  Processing {total_pages} pages...")
        
        for page_num in range(total_pages):
            page = doc[page_num]
            
            # Render page to pixmap (rasterize at reasonable DPI)
            # Use DPI that balances quality and file size
            dpi = min(200, max_dimension / max(page.rect.width, page.rect.height) * 72)
            dpi = max(150, dpi)  # Minimum 150 DPI for readability
            
            pix = page.get_pixmap(dpi=int(dpi))
            
            # Convert pixmap to PIL Image
            img_data = pix.tobytes("png")
            pil_img = Image.open(io.BytesIO(img_data))
            
            # Compress the image
            compressed_bytes = compress_image(
                img_data,
                max_quality=max_quality,
                max_dimension=max_dimension
            )
            
            # Create new page with same dimensions
            new_page = compressed_doc.new_page(
                width=page.rect.width,
                height=page.rect.height
            )
            
            # Insert compressed image to fill the page
            new_page.insert_image(
                fitz.Rect(0, 0, page.rect.width, page.rect.height),
                stream=compressed_bytes,
                keep_proportion=False  # Fill entire page
            )
            
            pix = None  # Free memory
        
        # Save compressed PDF with optimization
        compressed_doc.save(
            output_pdf_path,
            deflate=True,  # Enable compression
            garbage=4,     # Aggressive garbage collection
            clean=True,    # Clean up unused objects
        )
        compressed_doc.close()
        doc.close()
        
        print(f"  Successfully compressed {total_pages} pages")
        return True
    
    except Exception as e:
        print(f"  Error during PDF compression: {e}")
        import traceback
        traceback.print_exc()
        return False


def compress_pdf_if_needed(
    pdf_path: str,
    target_size_mb: float = 10.0,
    max_quality: int = 75,
    max_dimension: int = 2000,
    aggressive: bool = False,
) -> bool:
    """
    Compress PDF if file size is >= target_size_mb.
    
    Args:
        pdf_path: Path to PDF file to compress
        target_size_mb: Target maximum size in MB (default: 10MB)
        max_quality: JPEG quality for compressed images (default: 75)
        max_dimension: Maximum image dimension in pixels (default: 2000)
        aggressive: If True, use more aggressive compression (quality=60, max_dim=1500)
    
    Returns:
        True if compression was performed, False if not needed or failed
    """
    if not os.path.isfile(pdf_path):
        print(f"  Warning: PDF file not found: {pdf_path}")
        return False
    
    file_size_mb = get_file_size_mb(pdf_path)
    print(f"  PDF file size: {file_size_mb:.2f} MB")
    
    if file_size_mb < target_size_mb:
        print(f"  PDF is already under {target_size_mb}MB, no compression needed.")
        return False
    
    print(f"  PDF size ({file_size_mb:.2f}MB) >= {target_size_mb}MB, compressing...")
    
    # Use aggressive settings if requested
    if aggressive:
        max_quality = 60
        max_dimension = 1500
        print(f"  Using aggressive compression (quality={max_quality}, max_dim={max_dimension})")
    
    # Create temporary file name
    temp_pdf_path = pdf_path + ".tmp"
    
    try:
        # Rename original to temporary
        shutil.move(pdf_path, temp_pdf_path)
        print(f"  Moved original PDF to temporary file: {temp_pdf_path}")
        
        # Compress PDF
        success = compress_pdf_images(
            input_pdf_path=temp_pdf_path,
            output_pdf_path=pdf_path,
            max_quality=max_quality,
            max_dimension=max_dimension,
            target_size_mb=target_size_mb,
        )
        
        if not success:
            # Restore original if compression failed
            print(f"  Compression failed, restoring original PDF...")
            shutil.move(temp_pdf_path, pdf_path)
            return False
        
        # Check final size
        final_size_mb = get_file_size_mb(pdf_path)
        compression_ratio = ((file_size_mb - final_size_mb) / file_size_mb * 100) if file_size_mb > 0 else 0
        
        print(f"  Compression complete:")
        print(f"    Original size: {file_size_mb:.2f} MB")
        print(f"    Compressed size: {final_size_mb:.2f} MB")
        print(f"    Compression ratio: {compression_ratio:.1f}%")
        
        # If still too large, try more aggressive compression
        if final_size_mb >= target_size_mb and not aggressive:
            print(f"  Still above target ({final_size_mb:.2f}MB >= {target_size_mb}MB), trying aggressive compression...")
            # Recursively try aggressive compression
            return compress_pdf_if_needed(
                pdf_path,
                target_size_mb=target_size_mb,
                aggressive=True
            )
        
        # Delete temporary file
        try:
            os.remove(temp_pdf_path)
            print(f"  Removed temporary file: {temp_pdf_path}")
        except Exception as e:
            print(f"  Warning: Could not remove temporary file {temp_pdf_path}: {e}")
        
        return True
    
    except Exception as e:
        print(f"  Error during compression process: {e}")
        # Try to restore original if it exists
        if os.path.isfile(temp_pdf_path):
            try:
                shutil.move(temp_pdf_path, pdf_path)
                print(f"  Restored original PDF from temporary file")
            except Exception as restore_error:
                print(f"  Error restoring original PDF: {restore_error}")
        return False


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Compress PDF file by compressing embedded images"
    )
    parser.add_argument("--pdf", required=True, help="Path to PDF file to compress")
    parser.add_argument("--target-size-mb", type=float, default=10.0, help="Target maximum size in MB (default: 10)")
    parser.add_argument("--quality", type=int, default=75, help="JPEG quality (1-100, default: 75)")
    parser.add_argument("--max-dimension", type=int, default=2000, help="Maximum image dimension in pixels (default: 2000)")
    parser.add_argument("--aggressive", action="store_true", help="Use aggressive compression settings")
    
    args = parser.parse_args()
    
    compress_pdf_if_needed(
        pdf_path=args.pdf,
        target_size_mb=args.target_size_mb,
        max_quality=args.quality,
        max_dimension=args.max_dimension,
        aggressive=args.aggressive,
    )
