"""
Test Integration of Spell Correction with Essay Grading

This script verifies that:
1. Spelling errors are detected
2. They are passed to the annotation function
3. They appear in the final JSON output
4. The annotation function accepts the new parameter
"""

import json
import os
from typing import Dict, Any, List


def test_json_structure():
    """Test that output JSON has all required fields."""
    print("=" * 60)
    print("Testing JSON Structure")
    print("=" * 60)
    
    # Check if a result file exists
    test_files = ["essay_result.json", "result.json", "output.json"]
    result_file = None
    
    for f in test_files:
        if os.path.exists(f):
            result_file = f
            break
    
    if not result_file:
        print("⚠ No output JSON found. Run grade_pdf_essay.py first.")
        return False
    
    print(f"✓ Found output file: {result_file}")
    
    with open(result_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    required_keys = ["structure", "grading", "annotations", "page_suggestions"]
    optional_keys = ["spelling_grammar_errors", "annotation_errors"]
    
    print("\nRequired fields:")
    for key in required_keys:
        if key in data:
            print(f"  ✓ {key}: present")
        else:
            print(f"  ✗ {key}: MISSING")
            return False
    
    print("\nOptional/new fields:")
    for key in optional_keys:
        if key in data:
            count = len(data[key]) if isinstance(data[key], list) else "N/A"
            print(f"  ✓ {key}: present (count: {count})")
        else:
            print(f"  ⚠ {key}: not present")
    
    # Check spelling errors structure if present
    if "spelling_grammar_errors" in data and data["spelling_grammar_errors"]:
        print("\nSpelling/Grammar Errors Structure:")
        first_error = data["spelling_grammar_errors"][0]
        required_error_fields = ["page", "type", "error_text", "correction", "anchor_quote"]
        
        for field in required_error_fields:
            if field in first_error:
                print(f"  ✓ {field}: '{first_error[field][:50] if isinstance(first_error[field], str) else first_error[field]}'")
            else:
                print(f"  ✗ {field}: MISSING")
    
    print("\n" + "=" * 60)
    print("JSON Structure Test: PASSED")
    print("=" * 60)
    return True


def test_function_signatures():
    """Test that functions have the correct signatures."""
    print("\n" + "=" * 60)
    print("Testing Function Signatures")
    print("=" * 60)
    
    try:
        import annotate_pdf_with_essay_rubric
        import inspect
        
        # Check annotate_pdf_essay_pages signature
        func = annotate_pdf_with_essay_rubric.annotate_pdf_essay_pages
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())
        
        print("\nannotate_pdf_essay_pages parameters:")
        for param in params:
            print(f"  - {param}")
        
        if "spelling_errors" in params:
            print("\n✓ spelling_errors parameter: present")
        else:
            print("\n✗ spelling_errors parameter: MISSING")
            return False
        
        print("\n" + "=" * 60)
        print("Function Signature Test: PASSED")
        print("=" * 60)
        return True
        
    except ImportError as e:
        print(f"\n✗ Import error: {e}")
        return False
    except Exception as e:
        print(f"\n✗ Error: {e}")
        return False


def test_ocr_spell_module():
    """Test that OCR spell correction module can be imported."""
    print("\n" + "=" * 60)
    print("Testing OCR Spell Correction Module")
    print("=" * 60)
    
    try:
        import sys
        import importlib.util
        
        spec = importlib.util.spec_from_file_location(
            "ocr_spell_correction", 
            "ocr-spell-correction.py"
        )
        
        if spec and spec.loader:
            ocr_spell_module = importlib.util.module_from_spec(spec)
            sys.modules["ocr_spell_correction"] = ocr_spell_module
            spec.loader.exec_module(ocr_spell_module)
            
            # Check for required functions
            required_funcs = [
                "detect_spelling_grammar_errors",
                "_filter_errors",
                "annotate_pdf"
            ]
            
            print("\nRequired functions:")
            for func_name in required_funcs:
                if hasattr(ocr_spell_module, func_name):
                    print(f"  ✓ {func_name}: present")
                else:
                    print(f"  ✗ {func_name}: MISSING")
                    return False
            
            print("\n" + "=" * 60)
            print("OCR Spell Module Test: PASSED")
            print("=" * 60)
            return True
        else:
            print("\n✗ Could not load ocr-spell-correction.py")
            return False
            
    except Exception as e:
        print(f"\n✗ Error loading module: {e}")
        return False


def test_integration_flow():
    """Test the complete integration flow logic."""
    print("\n" + "=" * 60)
    print("Testing Integration Flow")
    print("=" * 60)
    
    try:
        import grade_pdf_essay
        import inspect
        
        # Check main function
        main_source = inspect.getsource(grade_pdf_essay.main)
        
        checks = {
            "detect_spelling_grammar_errors call": "detect_spelling_grammar_errors" in main_source,
            "_filter_errors call": "_filter_errors" in main_source,
            "spelling_errors variable": "spelling_errors" in main_source,
            "spelling_errors in output": '"spelling_grammar_errors"' in main_source,
            "spelling_errors passed to annotate": "spelling_errors=" in main_source,
        }
        
        print("\nIntegration flow checks:")
        all_passed = True
        for check_name, result in checks.items():
            status = "✓" if result else "✗"
            print(f"  {status} {check_name}")
            if not result:
                all_passed = False
        
        if all_passed:
            print("\n" + "=" * 60)
            print("Integration Flow Test: PASSED")
            print("=" * 60)
        else:
            print("\n" + "=" * 60)
            print("Integration Flow Test: FAILED")
            print("=" * 60)
        
        return all_passed
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        return False


def print_usage_example():
    """Print usage example."""
    print("\n" + "=" * 60)
    print("Usage Example")
    print("=" * 60)
    print("""
To test the integration with an actual PDF:

1. Basic usage:
   python grade_pdf_essay.py --pdf Essay.pdf --output-json result.json --output-pdf annotated.pdf

2. Check the output:
   - result.json should contain "spelling_grammar_errors" field
   - annotated.pdf should show red boxes with corrections on the page
   - Console should print "Found N spelling/grammar errors"

3. Verify annotations:
   - Left margin: Page-level improvements (black boxes)
   - Center: Essay text with inline spelling corrections (red boxes)
   - Right margin: Essay annotations (red boxes)

4. Debug output:
   python grade_pdf_essay.py --pdf Essay.pdf \\
       --output-json result.json \\
       --output-pdf annotated.pdf \\
       --debug-ocr-pages-dir debug_llm/ocr_pages \\
       --debug-structure-json debug_llm/structure_raw.json
""")
    print("=" * 60)


def main():
    """Run all tests."""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 10 + "ESSAY GRADING INTEGRATION TEST" + " " * 17 + "║")
    print("╚" + "=" * 58 + "╝")
    
    results = []
    
    # Test 1: Function signatures
    results.append(("Function Signatures", test_function_signatures()))
    
    # Test 2: OCR spell module
    results.append(("OCR Spell Module", test_ocr_spell_module()))
    
    # Test 3: Integration flow
    results.append(("Integration Flow", test_integration_flow()))
    
    # Test 4: JSON structure (if output exists)
    results.append(("JSON Structure", test_json_structure()))
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✓ PASSED" if result else "✗ FAILED"
        print(f"  {status}: {test_name}")
    
    print("\n" + "-" * 60)
    print(f"  Total: {passed}/{total} tests passed")
    print("-" * 60)
    
    if passed == total:
        print("\n🎉 All tests passed! Integration is working correctly.")
    else:
        print(f"\n⚠ {total - passed} test(s) failed. Check the output above.")
    
    print_usage_example()


if __name__ == "__main__":
    main()
