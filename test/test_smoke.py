"""
Quick smoke test - verify system starts without errors.
"""

print("=" * 60)
print("SMOKE TEST - Quick System Check")
print("=" * 60)

errors = []

# Test 1: Imports
print("\n1. Testing imports...")
try:
    from src.config import PATTERNS, STRATEGY, TUESDAY
    from src.strategy import RenkoStrategy
    from src.main import TRADE_HEADERS
    print("   ✅ All imports successful")
except Exception as e:
    print(f"   ❌ Import failed: {e}")
    errors.append(f"Import: {e}")

# Test 2: Config values
print("\n2. Testing config values...")
try:
    assert PATTERNS['enabled']
    assert STRATEGY['renko_brick_pct'] == 0.0004
    assert TUESDAY == 1
    print("   ✅ Config values correct")
except Exception as e:
    print(f"   ❌ Config error: {e}")
    errors.append(f"Config: {e}")

# Test 3: Strategy initialization
print("\n3. Testing strategy initialization...")
try:
    strategy = RenkoStrategy(STRATEGY)
    assert strategy.position == 0
    assert strategy._brick_pct == 0.0004
    print("   ✅ Strategy initialized")
except Exception as e:
    print(f"   ❌ Strategy error: {e}")
    errors.append(f"Strategy: {e}")

# Test 4: Trade headers
print("\n4. Testing trade headers...")
try:
    assert 'pattern_name' in TRADE_HEADERS
    assert 'pattern_type' in TRADE_HEADERS
    assert 'exit_reason' in TRADE_HEADERS
    print("   ✅ Trade headers updated")
except Exception as e:
    print(f"   ❌ Headers error: {e}")
    errors.append(f"Headers: {e}")

# Summary
print("\n" + "=" * 60)
if not errors:
    print("✅ SMOKE TEST PASSED - System ready")
else:
    print(f"❌ SMOKE TEST FAILED - {len(errors)} errors:")
    for err in errors:
        print(f"   - {err}")
print("=" * 60)