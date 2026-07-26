#!/usr/bin/env python
"""
Simple script to verify intelligence router file syntax
"""
import ast
import sys

print("Checking intelligence router file syntax...")

try:
    with open('backend/routes/intelligence.py', 'r', encoding='utf-8') as f:
        code = f.read()
    
    # Parse the file to check for syntax errors
    ast.parse(code)
    print("✅ File syntax is valid")
    
    # Check for router definition
    if 'router = APIRouter()' in code:
        print("✅ Router is defined")
    else:
        print("❌ Router not found")
    
    # Count route decorators
    route_count = code.count('@router.get(') + code.count('@router.post(') + code.count('@router.put(') + code.count('@router.delete(')
    print(f"✅ Found {route_count} route definitions")
    
    # Check for intelligence endpoints
    endpoints = [
        '/api/identities/{identity_id}/related',
        '/api/identities/{identity_id}/temporal-patterns',
        '/api/identities/{identity_id}/cross-camera',
        '/api/identities/{identity_id}/map'
    ]
    
    print("\nChecking for intelligence endpoints:")
    for endpoint in endpoints:
        if endpoint in code:
            print(f"  ✅ {endpoint}")
        else:
            print(f"  ❌ {endpoint}")
    
    print("\n" + "=" * 60)
    print("File check complete!")
    print("=" * 60)
    print("\n⚠️  IMPORTANT: Restart your FastAPI server for changes to take effect!")
    print("\nTo restart:")
    print("  1. Stop the server (Ctrl+C)")
    print("  2. Start it again:")
    print("     python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000")
    print("\nAfter restart, check server logs for:")
    print("  ✅ Intelligence router imported successfully")
    print("  ✅ Intelligence router registered")
    print("  📍 GET /api/identities/{identity_id}/related")
    print("  ... (more routes)")
    
except SyntaxError as e:
    print(f"❌ Syntax error: {e}")
except Exception as e:
    print(f"❌ Error: {e}")

