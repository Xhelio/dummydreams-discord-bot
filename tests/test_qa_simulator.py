import db
import scripts.qa_simulator as qa_simulator


def test_gift_simulator_uses_real_transaction_and_deduplicates(monkeypatch):
    def unexpected_connection():
        raise AssertionError("simulator must replace the configured database")

    monkeypatch.setattr(db, "get_db_connection", unexpected_connection)

    result = qa_simulator.run_gift_simulation()

    assert result["passed"] is True
    assert result["external_services_used"] is False
    assert result["database_totals"] == {
        "gift_logs": 1,
        "session_coins": 25,
        "all_time_coins": 25,
        "daily_coins": 25,
    }


def test_gift_simulator_rolls_back_failed_transaction():
    result = qa_simulator.run_gift_simulation(force_failure=True)

    assert result["passed"] is True
    assert result["first_result"]["status"] == "failed"
    assert result["database_totals"] == {
        "gift_logs": 0,
        "session_coins": 0,
        "all_time_coins": 0,
        "daily_coins": 0,
    }


def test_simulator_restores_database_dependencies_after_run(monkeypatch):
    original_connection = db.get_db_connection
    original_host_lookup = db.get_scheduled_host_name

    qa_simulator.run_gift_simulation()

    assert db.get_db_connection is original_connection
    assert db.get_scheduled_host_name is original_host_lookup
