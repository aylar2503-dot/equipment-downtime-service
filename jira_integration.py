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

    # Настройки Jira
    jira_url = os.getenv("JIRA_URL", "").strip().rstrip("/")
    jira_user = os.getenv("JIRA_USER", "").strip()
    jira_token = os.getenv("JIRA_API_TOKEN", "").strip()
    project_key = os.getenv("JIRA_PROJECT_KEY", "").strip()

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

    # Описание остановки оборудования
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
        # Получаем идентификатор Jira Cloud
        tenant_response = requests.get(
            f"{jira_url}/_edge/tenant_info",
            timeout=10,
        )

        tenant_response.raise_for_status()

        tenant_data = tenant_response.json()
        cloud_id = tenant_data.get("cloudId")

        if not cloud_id:
            raise JiraIntegrationError(
                "Не удалось получить Cloud ID Jira"
            )

        # Адрес API для токена с областями действия
        api_url = (
            f"https://api.atlassian.com/ex/jira/"
            f"{cloud_id}/rest/api/3/issue"
        )

        # Создаём настоящую задачу Jira
        response = requests.post(
            api_url,
            json=payload,
            auth=HTTPBasicAuth(
                jira_user,
                jira_token,
            ),
            headers={
                "Accept": "application/json",
            },
            timeout=10,
        )

        response.raise_for_status()

        result = response.json()

    except requests.HTTPError as exc:
        status_code = (
            exc.response.status_code
            if exc.response is not None
            else "неизвестен"
        )

        raise JiraIntegrationError(
            f"Ошибка Jira HTTP: {status_code}"
        ) from exc

    except requests.RequestException as exc:
        raise JiraIntegrationError(
            "Ошибка соединения с Jira"
        ) from exc

    except ValueError as exc:
        raise JiraIntegrationError(
            "Jira вернула некорректный JSON"
        ) from exc

    # Проверяем номер созданной задачи
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