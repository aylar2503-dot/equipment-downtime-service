import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))


class VKNotificationError(Exception):
    pass


def send_vk_message(message: str, random_id: int) -> int:
    token = os.getenv("VK_TOKEN", "").strip()
    user_id = os.getenv("VK_USER_ID", "").strip()

    if not token:
        raise VKNotificationError("VK_TOKEN не указан в файле .env")

    if not user_id.isdigit() or int(user_id) <= 0:
        raise VKNotificationError(
            "VK_USER_ID должен быть положительным числом"
        )

    try:
        response = requests.post(
            "https://api.vk.ru/method/messages.send",
            data={
                "access_token": token,
                "v": "5.199",
                "user_id": int(user_id),
                "random_id": random_id,
                "message": message,
            },
            timeout=10,
        )

        response.raise_for_status()
        result = response.json()

    except (requests.RequestException, ValueError) as exc:
        raise VKNotificationError(
            "Не удалось связаться с VK API"
        ) from exc

    if "error" in result:
        error = result["error"]
        code = error.get("error_code", "?")
        description = error.get("error_msg", "Ошибка VK API")

        raise VKNotificationError(
            f"VK API {code}: {description}"
        )

    if not isinstance(result.get("response"), int):
        raise VKNotificationError(
            "VK API вернул неожиданный ответ"
        )

    return result["response"]