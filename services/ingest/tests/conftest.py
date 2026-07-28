"""Applies the real migrations from supabase/migrations against a scratch
Postgres database once per test session, then hands each test a connection
inside a transaction that's rolled back afterwards - so tests exercise the
actual schema (constraints, functions, RLS helpers) rather than a mock."""
from __future__ import annotations

import pathlib

import psycopg
import pytest
from psycopg.rows import dict_row

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
SEED_FILE = REPO_ROOT / "supabase" / "seed.sql"

ADMIN_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
TEST_DB = "hardhatsai_pytest"
TEST_DSN = f"postgresql://postgres:postgres@127.0.0.1:5432/{TEST_DB}"


@pytest.fixture(scope="session")
def database_url() -> str:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin_conn:
        admin_conn.execute(f'drop database if exists "{TEST_DB}"')
        admin_conn.execute(f'create database "{TEST_DB}"')

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        # Stand-ins for what Supabase provides that a bare Postgres doesn't:
        # the auth.users table + auth.uid() referenced by RLS, and the three
        # Supabase-managed roles the grants target.
        conn.execute("create schema if not exists auth")
        conn.execute("create extension if not exists pgcrypto")
        conn.execute("create table if not exists auth.users (id uuid primary key default gen_random_uuid())")
        conn.execute("create or replace function auth.uid() returns uuid language sql stable as $$ select null::uuid $$")
        for role in ("authenticated", "anon", "service_role"):
            conn.execute(f"do $$ begin create role {role}; exception when duplicate_object then null; end $$")

        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute(migration.read_text())
        conn.execute(SEED_FILE.read_text())

    return TEST_DSN


@pytest.fixture()
def conn(database_url: str):
    connection = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()
