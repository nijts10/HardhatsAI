"""cmd_report's validation logic is pulled into a pure helper specifically
so it's testable without a live DB connection (cmd_* functions otherwise
call load_config()/db.connect() straight from real env config -- not
something a unit test should ever trigger)."""
from ingest.cli import _run_brand_mismatch_error


def test_no_error_when_run_belongs_to_the_given_brand():
    brand = {"id": "brand-a"}
    run = {"brand_id": "brand-a"}
    assert _run_brand_mismatch_error(run, brand, "run-1", "acme") is None


def test_error_when_run_belongs_to_a_different_brand():
    brand = {"id": "brand-a"}
    run = {"brand_id": "brand-b"}
    error = _run_brand_mismatch_error(run, brand, "run-1", "acme")
    assert error is not None
    assert "run-1" in error
    assert "brand-b" in error


def test_error_when_run_does_not_exist():
    brand = {"id": "brand-a"}
    error = _run_brand_mismatch_error(None, brand, "missing-run", "acme")
    assert error is not None
    assert "missing-run" in error


def test_uuid_objects_compare_correctly_not_just_identical_strings():
    # brand_id columns come back from psycopg as uuid.UUID objects, not
    # str -- the comparison must not rely on object identity/exact type.
    import uuid
    shared = uuid.uuid4()
    brand = {"id": shared}
    run = {"brand_id": shared}
    assert _run_brand_mismatch_error(run, brand, "run-1", "acme") is None
