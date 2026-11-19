import os

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

load_dotenv()


class JiraIntegrationError(Exception):
    """Ошибка подключения к Jira."""


def create_incident(
    machine_id: str,
    started_at: str,
    event_id: int,
) -> dict:
    """
    Создаёт инцидент при остановке оборудования.

    demo — демонстрация без подключения к Jira.
    live — создание настоящей задачи в Jira Cloud.
    """

    mode = os.getenv("JIRA_MODE", "demo").strip().lower()

    # Демонстрационный режим
    if mode == "demo":
        return {
            "key": f"DEMO-{event_id}",
            "status": "created",
            "mode": "demo",
        }

    if mode != "live":
        raise JiraIntegrationError(
            "JIRA_MODE должен быть demo или live"
        )

    # Настройки реальной Jira
    jira_url = os.getenv("JIRA_URL", "").rstrip("/")
    jira_user = os.getenv("JIRA_USER", "")
    jira_token = os.getenv("JIRA_API_TOKEN", "")
    project_key = os.getenv("JIRA_PROJECT_KEY", "")

    if not all([
        jira_url,
        jira_user,
        jira_token,
        project_key,
    ]):
        raise JiraIntegrationError(
            "Не заполнены настройки Jira в .env"
        )

    if not jira_url.startswith("https://"):
        raise JiraIntegrationError(
            "Для Jira необходимо использовать HTTPS"
        )

    description = (
        f"Оборудование: {machine_id}\n"
        f"Начало простоя: {started_at}\n"
        f"ID события: {event_id}\n"
        "Требуется проверка оборудования."
    )

    # Данные для создания задачи
    payload = {
        "fields": {
            "project": {
                "key": project_key
            },
            "summary": (
                f"Простой оборудования: {machine_id}"
            ),
            "issuetype": {
                "name": "Task"
            },
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": description
                            }
                        ]
                    }
                ]
            }
        }
    }

    try:
        response = requests.post(
            f"{jira_url}/rest/api/3/issue",
            json=payload,
            auth=HTTPBasicAuth(
                jira_user,
                jira_token
            ),
            headers={
                "Accept": "application/json"
            },
            timeout=10,
        )

        response.raise_for_status()
        result = response.json()

    except requests.RequestException as exc:
        raise JiraIntegrationError(
            "Не удалось создать инцидент в Jira"
        ) from exc

    except ValueError as exc:
        raise JiraIntegrationError(
            "Jira вернула некорректный JSON"
        ) from exc

    issue_key = result.get("key")

    if not issue_key:
        raise JiraIntegrationError(
            "Jira не вернула номер задачи"
        )

    return {
        "key": issue_key,
        "status": "created",
        "mode": "live",
        "url": f"{jira_url}/browse/{issue_key}",
    }