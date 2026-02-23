#!/usr/bin/env python3
"""
Script to create a read-only database user for Nepal Government Modernization database.

This script creates a PostgreSQL user with read-only access to all tables in the
court cases database. The user can SELECT from all tables but cannot INSERT, UPDATE,
or DELETE data.

Usage:
    poetry run python etc/create_ro_user.py

Requirements:
    - DATABASE_URL environment variable must be set with admin credentials (or in .env file)
    - PostgreSQL database must be accessible
    - User running script must have CREATE USER privileges
"""

import os
import sys
import re
import getpass
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def validate_username(username):
    """
    Validate PostgreSQL username.

    Rules:
    - Must be 1-63 characters
    - Must start with a letter or underscore
    - Can contain letters, numbers, underscores, and dollar signs
    - Cannot be a PostgreSQL reserved word

    Args:
        username: Proposed username

    Returns:
        tuple: (is_valid, error_message)
    """
    if not username:
        return False, "Username cannot be empty"

    if len(username) > 63:
        return False, "Username must be 63 characters or less"

    if not re.match(r"^[a-zA-Z_]", username):
        return False, "Username must start with a letter or underscore"

    if not re.match(r"^[a-zA-Z0-9_$]+$", username):
        return (
            False,
            "Username can only contain letters, numbers, underscores, and dollar signs",
        )

    # Check for common reserved words
    reserved_words = {
        "user",
        "admin",
        "root",
        "postgres",
        "public",
        "select",
        "insert",
        "update",
        "delete",
        "create",
        "drop",
        "alter",
        "grant",
        "revoke",
    }
    if username.lower() in reserved_words:
        return False, f"'{username}' is a reserved word and cannot be used as username"

    return True, None


def validate_password(password):
    """
    Validate password strength.

    Rules:
    - Minimum 12 characters
    - Must contain at least one uppercase letter
    - Must contain at least one lowercase letter
    - Must contain at least one digit
    - Must contain at least one special character

    Args:
        password: Proposed password

    Returns:
        tuple: (is_valid, error_message)
    """
    if not password:
        return False, "Password cannot be empty"

    if len(password) < 12:
        return False, "Password must be at least 12 characters long"

    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"

    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"

    if not re.search(r"\d", password):
        return False, "Password must contain at least one digit"

    if not re.search(r'[!@#$%^&*()_+\-=\[\]{};:\'",.<>?/\\|`~]', password):
        return (
            False,
            "Password must contain at least one special character (!@#$%^&*()_+-=[]{}...)",
        )

    return True, None


def get_database_name(database_url):
    """
    Extract database name from connection URL.

    Args:
        database_url: PostgreSQL connection string

    Returns:
        str: Database name
    """
    # Parse URL to get database name
    # Format: postgresql://user:pass@host:port/dbname
    parts = database_url.split("/")
    if len(parts) < 4:
        raise ValueError("Invalid DATABASE_URL format")

    db_name = parts[-1].split("?")[0]  # Remove query parameters if any
    return db_name


def user_exists(engine, username):
    """
    Check if a PostgreSQL user already exists.

    Args:
        engine: SQLAlchemy engine
        username: Username to check

    Returns:
        bool: True if user exists, False otherwise
    """
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :username"),
            {"username": username},
        )
        return result.fetchone() is not None


def create_readonly_user(engine, username, password, database_name):
    """
    Create a read-only PostgreSQL user.

    This function:
    1. Creates the user with the specified password
    2. Grants CONNECT privilege on the database
    3. Grants USAGE privilege on the public schema
    4. Grants SELECT privilege on all existing tables
    5. Sets default privileges for future tables

    Args:
        engine: SQLAlchemy engine with admin credentials
        username: Username for the new read-only user
        password: Password for the new user
        database_name: Name of the database to grant access to
    """
    print(f"\n🔧 Creating read-only user '{username}'...")

    with engine.connect() as conn:
        # Use autocommit mode for DDL statements
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")

        # 1. Create user
        print("  ✓ Creating user...")
        conn.execute(
            text(f"CREATE USER {username} WITH PASSWORD :password"),
            {"password": password},
        )

        # 2. Grant CONNECT privilege on database
        print(f"  ✓ Granting CONNECT on database '{database_name}'...")
        conn.execute(text(f"GRANT CONNECT ON DATABASE {database_name} TO {username}"))

        # 3. Grant USAGE on public schema
        print("  ✓ Granting USAGE on schema 'public'...")
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {username}"))

        # 4. Grant SELECT on all existing tables
        print("  ✓ Granting SELECT on all existing tables...")
        conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {username}"))

        # 5. Grant SELECT on all existing sequences (for id columns)
        print("  ✓ Granting SELECT on all sequences...")
        conn.execute(
            text(f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {username}")
        )

        # 6. Set default privileges for future tables
        print("  ✓ Setting default privileges for future tables...")
        conn.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {username}"
            )
        )

        # 7. Set default privileges for future sequences
        print("  ✓ Setting default privileges for future sequences...")
        conn.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO {username}"
            )
        )

    print(f"\n✅ Read-only user '{username}' created successfully!")
    print("\nConnection string for this user:")
    print(f"  postgresql://{username}:<password>@<host>:<port>/{database_name}")


def main():
    """Main script execution."""
    print("=" * 70)
    print("  Nepal Government Modernization - Create Read-Only Database User")
    print("=" * 70)

    load_dotenv()

    # Check for DATABASE_URL
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("\n❌ ERROR: DATABASE_URL environment variable not set")
        print("\nPlease set DATABASE_URL with admin credentials:")
        print("  export DATABASE_URL='postgresql://admin:password@host:5432/dbname'")
        print("\nOr create a .env file with:")
        print("  DATABASE_URL=postgresql://admin:password@host:5432/dbname")
        sys.exit(1)

    # Extract database name
    try:
        database_name = get_database_name(database_url)
        print(f"\n📊 Database: {database_name}")
    except ValueError as e:
        print(f"\n❌ ERROR: {e}")
        sys.exit(1)

    # Connect to database
    print("\n🔌 Connecting to database...")
    try:
        engine = create_engine(database_url, echo=False)
        # Test connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("  ✓ Connected successfully")
    except Exception as e:
        print("\n❌ ERROR: Failed to connect to database")
        print(f"  {e}")
        sys.exit(1)

    # Get username
    print("\n" + "=" * 70)
    print("Username Requirements:")
    print("  • 1-63 characters")
    print("  • Must start with a letter or underscore")
    print("  • Can contain letters, numbers, underscores, and dollar signs")
    print("  • Cannot be a PostgreSQL reserved word")
    print("=" * 70)

    while True:
        username = input("\nEnter username for read-only user: ").strip()

        is_valid, error = validate_username(username)
        if not is_valid:
            print(f"❌ Invalid username: {error}")
            continue

        # Check if user already exists
        if user_exists(engine, username):
            print(f"❌ User '{username}' already exists")
            retry = input("Try a different username? (y/n): ").strip().lower()
            if retry != "y":
                print("\n👋 Exiting...")
                sys.exit(0)
            continue

        break

    # Get password
    print("\n" + "=" * 70)
    print("Password Requirements:")
    print("  • Minimum 12 characters")
    print("  • At least one uppercase letter")
    print("  • At least one lowercase letter")
    print("  • At least one digit")
    print("  • At least one special character (!@#$%^&*()_+-=[]{}...)")
    print("=" * 70)

    while True:
        password = getpass.getpass("\nEnter password: ")

        is_valid, error = validate_password(password)
        if not is_valid:
            print(f"❌ Invalid password: {error}")
            continue

        password_confirm = getpass.getpass("Confirm password: ")

        if password != password_confirm:
            print("❌ Passwords do not match")
            continue

        break

    # Confirm creation
    print("\n" + "=" * 70)
    print("Ready to create read-only user:")
    print(f"  Username: {username}")
    print(f"  Database: {database_name}")
    print("  Privileges: SELECT on all tables (read-only)")
    print("=" * 70)

    confirm = input("\nProceed with user creation? (yes/no): ").strip().lower()
    if confirm not in ["yes", "y"]:
        print("\n👋 User creation cancelled")
        sys.exit(0)

    # Create user
    try:
        create_readonly_user(engine, username, password, database_name)
    except Exception as e:
        print("\n❌ ERROR: Failed to create user")
        print(f"  {e}")
        sys.exit(1)
    finally:
        engine.dispose()

    print("\n" + "=" * 70)
    print("✅ Done!")
    print("=" * 70)


if __name__ == "__main__":
    main()
