#!/usr/bin/env python3
"""
Generate API Access Token Utility
==================================
Simple utility to generate access tokens for API usage.

Usage:
    python utils/generate_token.py <username> <password>
    
    Or set environment variables:
    export API_USERNAME="admin"
    export API_PASSWORD="password"
    python utils/generate_token.py

Output:
    Prints the access token to stdout (suitable for scripts)
"""

import sys
import os
import asyncio

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from backend.auth.auth_service import AuthService
from db_connection import get_db


async def generate_token(username: str, password: str):
    """
    Generate and return access token for the given user.
    
    Args:
        username: Username for authentication
        password: Password for authentication
    
    Returns:
        str: JWT access token
    
    Raises:
        SystemExit: If authentication fails or user is inactive
    """
    try:
        async for db in get_db():
            # Authenticate user
            user = await AuthService.authenticate_user(username, password, db)
            if not user:
                print("ERROR: Invalid username or password", file=sys.stderr)
                sys.exit(1)
            
            # Check if user is active
            if not user.is_active:
                print("ERROR: User account is inactive", file=sys.stderr)
                if hasattr(user, 'blocked_reason') and user.blocked_reason:
                    print(f"ERROR: Blocked reason: {user.blocked_reason}", file=sys.stderr)
                sys.exit(1)
            
            # Generate token
            token = AuthService.create_access_token(
                data={
                    "sub": str(user.id),
                    "username": user.username,
                    "role": user.role
                }
            )
            
            # Print token to stdout (for use in scripts)
            print(token)
            return token
            
    except Exception as e:
        print(f"ERROR: Failed to generate token: {str(e)}", file=sys.stderr)
        sys.exit(1)


def main():
    """Main entry point"""
    # Get credentials from command line or environment
    if len(sys.argv) == 3:
        username = sys.argv[1]
        password = sys.argv[2]
    elif len(sys.argv) == 1:
        # Try environment variables
        username = os.getenv("API_USERNAME")
        password = os.getenv("API_PASSWORD")
        
        if not username or not password:
            print("Usage: python utils/generate_token.py <username> <password>", file=sys.stderr)
            print("   Or set API_USERNAME and API_PASSWORD environment variables", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: python utils/generate_token.py <username> <password>", file=sys.stderr)
        print("   Or set API_USERNAME and API_PASSWORD environment variables", file=sys.stderr)
        sys.exit(1)
    
    # Generate token
    token = asyncio.run(generate_token(username, password))
    
    # Exit successfully
    sys.exit(0)


if __name__ == "__main__":
    main()

