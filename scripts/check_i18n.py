#!/usr/bin/env python3
"""
OrchisX i18n Parity Checker
Validates that all data-i18n, data-i18n-placeholder, and t('...') references
in static/index.html exist across all 5 supported languages (TR, EN, ES, DE, FR).
"""

import sys
import re
from pathlib import Path

def main():
    html_path = Path(__file__).resolve().parent.parent / "static" / "index.html"
    if not html_path.exists():
        print(f"Error: {html_path} not found.")
        sys.exit(1)

    content = html_path.read_text(encoding="utf-8")

    # 1. Extract all referenced keys
    data_i18n_keys = set(re.findall(r'data-i18n=["\']([a-zA-Z0-9_]+)["\']', content))
    placeholder_keys = set(re.findall(r'data-i18n-placeholder=["\']([a-zA-Z0-9_]+)["\']', content))
    # Extract t('key') or t("key") calls (including ${t('key')})
    t_call_keys = set(re.findall(r'\bt\(\s*["\']([a-zA-Z0-9_]+)["\']\s*\)', content))

    all_referenced_keys = sorted(data_i18n_keys | placeholder_keys | t_call_keys)

    print(f"=== OrchisX i18n Parity Audit ===")
    print(f"Total Unique Keys Referenced in DOM & JS: {len(all_referenced_keys)}")
    print(f"  - data-i18n attributes:        {len(data_i18n_keys)}")
    print(f"  - data-i18n-placeholder:      {len(placeholder_keys)}")
    print(f"  - t('...') function calls:     {len(t_call_keys)}")
    print("=" * 35)

    # 2. Extract language dictionaries from 'const translations = { ... };'
    match = re.search(r'const translations\s*=\s*(\{.*?\n\s*\});\n\n\s*function t\(', content, re.DOTALL)
    if not match:
        print("Error: Could not locate 'translations' dictionary in static/index.html")
        sys.exit(1)

    translations_block = match.group(1)
    languages = ["tr", "en", "es", "de", "fr"]
    lang_names = {
        "tr": "Turkish (TR)",
        "en": "English (EN)",
        "es": "Spanish (ES)",
        "de": "German (DE)",
        "fr": "French (FR)"
    }

    all_passed = True

    for lang in languages:
        lang_pattern = rf'{lang}:\s*\{{(.*?)\n\s*\}}[,\n]'
        l_match = re.search(lang_pattern, translations_block, re.DOTALL)
        if not l_match:
            print(f"❌ [{lang_names[lang]}] Dictionary block NOT FOUND!")
            all_passed = False
            continue

        dict_body = l_match.group(1)
        # Extract dictionary keys
        defined_keys = set(re.findall(r'([a-zA-Z0-9_]+)\s*:', dict_body))

        missing_keys = set(all_referenced_keys) - defined_keys

        if missing_keys:
            print(f"❌ [{lang_names[lang]}] Missing {len(missing_keys)} key(s):")
            for k in sorted(missing_keys):
                print(f"     - {k}")
            all_passed = False
        else:
            print(f"✅ [{lang_names[lang]}] 100% Complete ({len(defined_keys)} defined keys, 0 missing)")

    print("=" * 35)
    if all_passed:
        print("🎉 Result: SUCCESS! All 5 languages have 100% translation coverage.")
        sys.exit(0)
    else:
        print("⚠️ Result: FAILED! One or more languages have missing translation keys.")
        sys.exit(1)

if __name__ == "__main__":
    main()
