"""Applies the real migrations from visibility-platform/supabase/migrations
against a scratch Postgres database once per session, then hands each test
a connection inside a transaction that's rolled back afterwards."""
from __future__ import annotations

import pathlib

import psycopg
import pytest
from psycopg.rows import dict_row

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]  # visibility-platform/
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
SEED_FILE = REPO_ROOT / "supabase" / "seed.sql"

ADMIN_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
TEST_DB = "visibility_pytest"
TEST_DSN = f"postgresql://postgres:postgres@127.0.0.1:5432/{TEST_DB}"


@pytest.fixture(scope="session")
def database_url() -> str:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin_conn:
        admin_conn.execute(f'drop database if exists "{TEST_DB}"')
        admin_conn.execute(f'create database "{TEST_DB}"')

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("create schema if not exists auth")
        conn.execute("create extension if not exists pgcrypto")
        conn.execute(
            "create table if not exists auth.users "
            "(id uuid primary key default gen_random_uuid(), "
            "raw_user_meta_data jsonb not null default '{}'::jsonb)"
        )
        conn.execute("create or replace function auth.uid() returns uuid language sql stable as $$ select null::uuid $$")
        for role in ("authenticated", "anon", "service_role"):
            conn.execute(f"do $$ begin create role {role}; exception when duplicate_object then null; end $$")

        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute(migration.read_text())
        conn.execute(SEED_FILE.read_text())

        # Supabase grants these by default on a real project; a bare
        # Postgres test instance needs it done explicitly, and only after
        # all tables/functions exist.
        conn.execute("grant usage on schema public to anon, authenticated, service_role")
        conn.execute("grant select, insert, update, delete on all tables in schema public to anon, authenticated, service_role")
        conn.execute("grant execute on all functions in schema public to anon, authenticated, service_role")

    return TEST_DSN


@pytest.fixture()
def conn(database_url: str):
    connection = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()
