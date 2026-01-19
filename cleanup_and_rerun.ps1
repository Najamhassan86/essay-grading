#!/usr/bin/env pwsh
# cleanup_and_rerun.ps1
# Clean up old annotation cache and regenerate with new Grok prompt

Write-Host "🧹 Cleaning up old annotation cache..." -ForegroundColor Yellow

# Remove partial progress (forces fresh Grok calls)
if (Test-Path "debug_llm/essay_annotations_partial.json") {
    Remove-Item "debug_llm/essay_annotations_partial.json" -Force
    Write-Host "   ✓ Deleted essay_annotations_partial.json" -ForegroundColor Green
}

# Remove old result (optional, but recommended for first test)
if (Test-Path "essay_result.json") {
    Remove-Item "essay_result.json" -Force
    Write-Host "   ✓ Deleted essay_result.json" -ForegroundColor Green
}

Write-Host ""
Write-Host "📝 Running annotation generation with NEW Grok prompt..." -ForegroundColor Cyan
Write-Host "   (This will call Grok for each page with anchor_quote requirement)" -ForegroundColor Cyan
Write-Host ""

# Run the actual command
python grade_pdf_essay.py --pdf Essay2.pdf --output-json essay_result.json --output-pdf essay_annotated.pdf

Write-Host ""
Write-Host "✅ Complete! Check essay_result.json for anchor_quote fields" -ForegroundColor Green
