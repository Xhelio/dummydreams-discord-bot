from turso_serverless.connection import Connection


def test_installed_turso_driver_exposes_atomic_batch_api():
    assert callable(getattr(Connection, "batch", None))
