"""Проверки REST-сценария. НИ ОДНОГО реального запроса в VK/Jira."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest


@pytest.fixture
def service(tmp_path, monkeypatch):
    import importlib
    import sys

    # Важно: задать отдельную БД ДО импорта main (у main есть init_db()).
    isolated_db = tmp_path / "test-equipment.db"
    monkeypatch.setenv("EQUIPMENT_DB_PATH", str(isolated_db))
    main = importlib.reload(sys.modules["main"]) if "main" in sys.modules else importlib.import_module("main")
    monkeypatch.setattr(main, "DB_PATH", isolated_db)
    main.init_db()
    monkeypatch.setenv("JIRA_MODE", "demo")
    monkeypatch.setenv("SERVICE_API_KEY", "")
    monkeypatch.setattr(
        main,
        "create_incident",
        lambda machine_id, started_at, event_id: {
            "key": f"DEMO-{event_id}",
            "mode": "demo",
        },
    )
    monkeypatch.setattr(main, "send_vk_message", lambda message, random_id: 12345)
    return main


def send(service, machine_id, status, stopped_at=""):
    return service.receive_event(
        service.MachineEvent(
            machine_id=machine_id, status=status, stopped_at=stopped_at
        )
    )


def db_count(service, table):
    with sqlite3.connect(service.DB_PATH) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_full_cycle_does_not_duplicate_notifications(service):
    statuses = ["RUNNING", "STOPPED", "STOPPED", "RUNNING"]
    response = [send(service, "CNC-01", s) for s in statuses]
    assert [r["notification_needed"] for r in response] == [False, True, False, False]
    assert response[1]["jira_incident"]["status"] == "demo"
    assert response[1]["vk_notification"]["status"] == "sent"
    assert response[2]["jira_incident"]["status"] == "not_required"
    assert response[3]["duration_seconds"] >= 0
    assert db_count(service, "events") == 4
    assert db_count(service, "downtimes") == 1
    assert db_count(service, "vk_notifications") == 1
    assert db_count(service, "jira_incidents") == 1
    assert service.get_machines() == [{"machine_id": "CNC-01", "status": "RUNNING"}]


def test_new_stop_after_resume_creates_second_incident(service):
    started_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    send(service, "LATHE-01", "STOPPED", started_at)
    send(service, "LATHE-01", "RUNNING")
    send(service, "LATHE-01", "STOPPED", started_at)
    assert db_count(service, "jira_incidents") == 2
    assert db_count(service, "vk_notifications") == 2
    assert db_count(service, "downtimes") == 2
    assert [row["jira_key"] for row in service.get_downtimes()] == ["DEMO-3", "DEMO-1"]


def test_existing_downtime_keeps_delivery_statuses_after_schema_update(service):
    send(service, "LEGACY-01", "STOPPED")
    with sqlite3.connect(service.DB_PATH) as db:
        db.execute("ALTER TABLE downtimes DROP COLUMN event_id")

    service.init_db()

    downtime, = service.get_downtimes()
    assert downtime["ongoing"] is True
    assert downtime["jira_key"] == "DEMO-1"
    assert downtime["vk_status"] == "sent"


def test_two_machines_are_independent(service):
    assert send(service, "A", "STOPPED")["notification_needed"]
    assert send(service, "B", "STOPPED")["notification_needed"]
    assert not send(service, "A", "STOPPED")["notification_needed"]
    assert db_count(service, "jira_incidents") == 2


def test_source_timestamp_used_for_start(service):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    send(service, "CNC-02", "STOPPED", stopped_at=stamp)
    downtime = service.get_downtimes()[0]
    assert downtime["ongoing"] is True
    assert 299 <= downtime["duration_seconds"] < 320


def test_bad_machine_id_is_rejected(service):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        send(service, "  ", "STOPPED")
    assert exc.value.status_code == 422
    assert db_count(service, "events") == 0


def test_jira_failure_does_not_rollback_event_or_vk(service, monkeypatch):
    calls = []
    def fail_jira(**kwargs):
        calls.append(kwargs)
        raise ConnectionError("network timed out")
    monkeypatch.setattr(service, "create_incident", fail_jira)
    result = send(service, "PRESS-01", "STOPPED")
    assert result["jira_incident"]["status"] == "uncertain"
    assert result["vk_notification"]["status"] == "sent"
    assert db_count(service, "events") == 1
    assert db_count(service, "jira_incidents") == 1
    assert db_count(service, "vk_notifications") == 1
    event_id = service.get_incidents()[0]["event_id"]
    assert service.deliver_jira_incident(event_id)["status"] == "uncertain"
    assert len(calls) == 1  # НЕ дублируем потенциально созданную задачу Jira


def test_jira_network_call_claimed_before_request(service, monkeypatch):
    calls = []
    def mock_jira(**kwargs):
        calls.append(kwargs)
        # Возможный второй вызов во время первого запроса не создаёт дубль.
        concurrent = service.deliver_jira_incident(kwargs["event_id"])
        assert concurrent["status"] == "sending"
        return {"key": "SCRUM-TEST", "mode": "live"}
    monkeypatch.setattr(service, "create_incident", mock_jira)
    result = send(service, "CNC-03", "STOPPED")
    assert result["jira_incident"]["status"] == "created"
    assert result["jira_incident"]["key"] == "SCRUM-TEST"
    assert len(calls) == 1


def test_vk_failure_does_not_cancel_jira(service, monkeypatch):
    def fail_vk(message, random_id):
        raise ConnectionError("VK unavailable")
    monkeypatch.setattr(service, "send_vk_message", fail_vk)
    result = send(service, "CNC-04", "STOPPED")
    assert result["jira_incident"]["status"] == "demo"
    assert result["vk_notification"]["status"] == "failed"
    assert db_count(service, "events") == 1
    assert db_count(service, "vk_notifications") == 1


def test_vk_retry_preserves_random_id(service, monkeypatch):
    used_ids = []
    def fail_vk(message, random_id):
        used_ids.append(random_id)
        if len(used_ids) == 1:
            raise ConnectionError("temporarily unavailable")
        return 123
    monkeypatch.setattr(service, "send_vk_message", fail_vk)
    assert send(service, "CNC-05", "STOPPED")["vk_notification"]["status"] == "failed"
    result = service.retry_notifications()
    assert result[0]["status"] == "sent"
    assert used_ids[0] == used_ids[1]


def test_invalid_status_via_http(service):
    async def scenario():
        transport = httpx.ASGITransport(app=service.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/events", json={"machine_id": "A", "status": "BROKEN"})
            assert r.status_code == 422
            r = await client.post("/api/events", json={"machine_id": "  ", "status": "STOPPED"})
            assert r.status_code == 422
            r = await client.post("/api/events", json={"machine_id": "CNC", "status": "STOPPED", "stopped_at": "yesterday"})
            assert r.status_code == 422
            r = await client.get("/health")
            assert r.status_code == 200
            assert r.json()["database"] == "ok"
    asyncio.run(scenario())


def test_api_key_blocks_post_without_header(service, monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "test-only-key")
    async def scenario():
        transport = httpx.ASGITransport(app=service.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {"machine_id": "CNC", "status": "STOPPED"}
            assert (await client.post("/api/events", json=body)).status_code == 401
            assert (await client.post("/api/notifications/retry")).status_code == 401
            assert (await client.post("/api/events", json=body, headers={"X-API-Key": "test-only-key"})).status_code == 200
            assert (await client.get("/api/incidents")).status_code == 200
    asyncio.run(scenario())


def test_http_full_rest_flow(service):
    async def scenario():
        transport = httpx.ASGITransport(app=service.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            steps = [
                ("RUNNING", False),
                ("STOPPED", True),
                ("STOPPED", False),
                ("RUNNING", False),
                ("STOPPED", True),
            ]
            for status, should_notify in steps:
                response = await client.post(
                    "/api/events", json={"machine_id": "CNC-06", "status": status}
                )
                assert response.status_code == 200
                result = response.json()
                assert result["notification_needed"] is should_notify
                assert result["jira_incident"]["status"] == ("demo" if should_notify else "not_required")
                assert result["vk_notification"]["status"] == ("sent" if should_notify else "not_required")
            assert len((await client.get("/api/events")).json()) == 5
            assert len((await client.get("/api/machines")).json()) == 1
            assert len((await client.get("/api/incidents")).json()) == 2
            assert len((await client.get("/api/notifications")).json()) == 2
            active, downtime = (await client.get("/api/downtimes")).json()
            assert active["ongoing"] is True
            assert active["jira_key"] == "DEMO-5"
            assert downtime["machine_id"] == "CNC-06"
            assert downtime["ongoing"] is False
            assert downtime["duration_seconds"] >= 0
            assert downtime["jira_status"] == "demo"
            assert downtime["jira_key"] == "DEMO-2"
            assert downtime["vk_status"] == "sent"
            assert 'id="downtimes"' in (await client.get("/dashboard")).text
    asyncio.run(scenario())
