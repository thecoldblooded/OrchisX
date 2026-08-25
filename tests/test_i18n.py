import re
from pathlib import Path

def test_i18n_translation_parity():
    """Verify that all data-i18n, placeholder, and t(...) keys are defined in all 5 languages."""
    html_path = Path(__file__).resolve().parent.parent / "static" / "index.html"
    assert html_path.exists(), "static/index.html not found"

    content = html_path.read_text(encoding="utf-8")

    data_i18n_keys = set(re.findall(r'data-i18n=["\']([a-zA-Z0-9_]+)["\']', content))
    placeholder_keys = set(re.findall(r'data-i18n-placeholder=["\']([a-zA-Z0-9_]+)["\']', content))
    t_call_keys = set(re.findall(r'\bt\(\s*["\']([a-zA-Z0-9_]+)["\']\s*\)', content))

    all_referenced_keys = set(data_i18n_keys | placeholder_keys | t_call_keys)
    assert len(all_referenced_keys) > 0

    match = re.search(r'const translations\s*=\s*(\{.*?\n\s*\});\n\n\s*function t\(', content, re.DOTALL)
    assert match is not None, "translations dictionary not found in HTML"

    translations_block = match.group(1)
    languages = ["tr", "en", "es", "de", "fr"]

    for lang in languages:
        lang_pattern = rf'{lang}:\s*\{{(.*?)\n\s*\}}[,\n]'
        l_match = re.search(lang_pattern, translations_block, re.DOTALL)
        assert l_match is not None, f"Language dictionary block for '{lang}' not found"

        dict_body = l_match.group(1)
        defined_keys = set(re.findall(r'([a-zA-Z0-9_]+)\s*:', dict_body))
        missing_keys = all_referenced_keys - defined_keys

        assert len(missing_keys) == 0, f"Language '{lang}' is missing keys: {missing_keys}"
