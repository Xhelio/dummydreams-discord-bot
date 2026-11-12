"""
Test script for Feature Set B: Dynamic host detection, new accounts, 1-message announcement
"""
import db

# Init DB
db.init_db()
print("✅ DB init OK")

# Check active hosts
hosts = db.get_active_hosts()
print(f"✅ Active hosts ({len(hosts)}): {hosts}")

# Expected accounts (from config/config.json)
expected_accounts = [
    'clarissadreams', 'georgevilora', 'stefannyalisha',
    'juliannedreams', 'dummydreamsentertaiment', 'franklyndreams'
]
for acc in expected_accounts:
    if acc in hosts:
        print(f"  ✅ Found: @{acc}")
    else:
        print(f"  ❌ MISSING: @{acc}")

print()

# Test host detection (current aliases: Luna/Sinta/Zize/Beatrix/Fearless/Yosa/Yuca)
tests = [
    ("ZIZE\u2019s here!", "Zize"),
    ("Zize's here!", "Zize"),
    ("ZIZE here", "Zize"),
    ("Mommy is here", "Sinta"),
    ("Momy is live", "Sinta"),
    ("shinta is streaming", "Sinta"),
    ("SOLEN\u2019s here", None),
    ("Solen's Live", None),
    ("welcome everyone, Julianne is live!", None),
    ("ITS YUCA TIME", "Yuca"),
    ("Yuka is here", None),
    ("hey its IELYA", None),
    ("ILYA here", None),
    ("COLE in the house", None),
    ("Luna's stream", "Luna"),
    ("Beatrix is here", "Beatrix"),
    ("Fearless on cam", "Fearless"),
    ("Yosa is live", "Yosa"),
    ("halo semuanya", None),
    ("welcome to the stream", None),
    ("ZIZE and SOLEN together", "Zize"),  # returns first match
]

print("--- Host Detection Tests ---")
all_pass = True
for text, expected in tests:
    result = db.detect_host_from_welcome_message(text)
    ok = result == expected
    if not ok:
        all_pass = False
    status = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {status}: \"{text}\" -> {result!r} (expected {expected!r})")

print()
if all_pass:
    print("✅ ALL HOST DETECTION TESTS PASSED!")
else:
    print("❌ SOME TESTS FAILED!")

print()

# Test module imports
print("--- Module Import Tests ---")
try:
    import observer
    print("  ✅ observer.py imports OK")
except Exception as e:
    print(f"  ❌ observer.py import FAILED: {e}")

try:
    import routes.admin
    print("  ✅ routes/admin.py imports OK")
except Exception as e:
    print(f"  ❌ routes/admin.py import FAILED: {e}")

try:
    import routes.api
    print("  ✅ routes/api.py imports OK")
except Exception as e:
    print(f"  ❌ routes/api.py import FAILED: {e}")

try:
    import utils
    print("  ✅ utils.py imports OK")
except Exception as e:
    print(f"  ❌ utils.py import FAILED: {e}")

print()
print("=== TEST COMPLETE ===")
