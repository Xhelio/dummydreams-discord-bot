import sqlite3

import db

# Keep pruning atomic while limiting the remote SQL round-trips.


class _RecordingConnection:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.execute_count = 0

    def execute(self, sql, parameters=()):
        self.execute_count += 1
        return self.connection.execute(sql, parameters)

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        return self.connection.close()


def test_activity_log_prune_uses_one_delete_and_preserves_category_limits(
    tmp_path, monkeypatch
):
    path = tmp_path / "activity-prune.sqlite3"
    setup = sqlite3.connect(path)
    try:
        setup.execute(
            "CREATE TABLE activity_logs (log_id INTEGER PRIMARY KEY, category TEXT)"
        )
        setup.executemany(
            "INSERT INTO activity_logs (log_id, category) VALUES (?, ?)",
            [(index, "operational") for index in range(1, 206)]
            + [(index, "technical") for index in range(206, 413)]
            + [(index, "other") for index in range(413, 416)],
        )
        setup.commit()
    finally:
        setup.close()

    connection = _RecordingConnection(path)
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)

    db.prune_activity_logs(max_operational=200, max_technical=201)

    assert connection.execute_count == 1

    check = sqlite3.connect(path)
    try:
        counts = dict(
            check.execute(
                "SELECT category, COUNT(*) FROM activity_logs GROUP BY category"
            ).fetchall()
        )
        operational_range = check.execute(
            "SELECT MIN(log_id), MAX(log_id) FROM activity_logs "
            "WHERE category = 'operational'"
        ).fetchone()
        technical_range = check.execute(
            "SELECT MIN(log_id), MAX(log_id) FROM activity_logs "
            "WHERE category = 'technical'"
        ).fetchone()
    finally:
        check.close()

    assert counts == {"operational": 200, "other": 3, "technical": 201}
    assert operational_range == (6, 205)
    assert technical_range == (212, 412)
