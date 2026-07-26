#!/usr/bin/env python
"""
Quick script to verify intelligence router can be imported and has routes
"""
import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

print("=" * 60)
print("Intelligence Router Verification")
print("=" * 60)

try:
    print("\n1. Testing imports...")
    from backend.routes.intelligence import router
    print("   ✅ Router imported successfully")
    
    print(f"\n2. Router has {len(router.routes)} routes:")
    for i, route in enumerate(router.routes, 1):
        if hasattr(route, 'path') and hasattr(route, 'methods'):
            methods = ', '.join(route.methods) if route.methods else 'N/A'
            print(f"   {i}. {methods:8} {route.path}")
    
    print("\n3. Checking for intelligence endpoints...")
    intelligence_paths = [
        '/api/identities/{identity_id}/related',
        '/api/identities/{identity_id}/temporal-patterns',
        '/api/identities/{identity_id}/cross-camera',
        '/api/identities/{identity_id}/map'
    ]
    
    found_paths = [r.path for r in router.routes if hasattr(r, 'path')]
    for path in intelligence_paths:
        # Check if path pattern matches (accounting for variable names)
        pattern_match = any(
            path.replace('{identity_id}', '{') in r.path.replace('{identity_id}', '{') 
            for r in router.routes if hasattr(r, 'path')
        )
        if pattern_match:
            print(f"   ✅ Found: {path}")
        else:
            print(f"   ❌ Missing: {path}")
    
    print("\n" + "=" * 60)
    print("✅ Router verification complete!")
    print("=" * 60)
    print("\nIf all routes are found, restart your FastAPI server:")
    print("  python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000")
    print("\nThen check server logs for:")
    print("  ✅ Intelligence router imported successfully")
    print("  ✅ Intelligence router registered")
    
except ImportError as e:
    print(f"\n❌ Import Error: {e}")
    print("\nThis usually means:")
    print("  1. Missing dependencies")
    print("  2. Import error in dependencies")
    print("  3. Python path issues")
    import traceback
    traceback.print_exc()
    
except Exception as e:
    print(f"\n❌ Error: {e}")
    import traceback
    traceback.print_exc()

