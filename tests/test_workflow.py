from pathlib import Path

import main


def test_equipment_workflow(tmp_path, monkeypatch):
    # Отдельная тестовая база данных
    monkeypatch.setattr(
        main,
        "DB_PATH",
        tmp_path / "equipment_test.db"
    )

    # Демонстрационный режим Jira
    monkeypatch.setenv("JIRA_MODE", "demo")

    # Имитация отправки VK
    vk_messages = []

    def fake_vk(message, random_id):
        vk_messages.append(message)
        return 12345

    monkeypatch.setattr(
        main,
        "send_vk_message",
        fake_vk
    )

    main.init_db()

    results = []

    for status in [
        "RUNNING",
        "STOPPED",
        "STOPPED",
        "RUNNING",
    ]:
        event = main.MachineEvent(
            machine_id="TEST-001",
            status=status,
        )

        results.append(main.receive_event(event))

    assert [
        result["notification_needed"]
        for result in results
    ] == [False, True, False, False]

    assert results[1]["jira_incident"]["status"] == "demo"
    assert results[1]["vk_notification"]["status"] == "sent"

    assert len(vk_messages) == 1

    incidents = main.get_incidents()

    assert len(incidents) == 1
    assert incidents[0]["status"] == "demo"

    downtimes = main.get_downtimes()

    assert len(downtimes) == 1
    assert downtimes[0]["ongoing"] is False