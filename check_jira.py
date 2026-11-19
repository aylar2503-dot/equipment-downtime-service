import os
import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

load_dotenv()

url = os.getenv("JIRA_URL", "").rstrip("/")
email = os.getenv("JIRA_USER", "")
token = os.getenv("JIRA_API_TOKEN", "")

if not url.startswith("https://") or not email or not token:
    print("ОШИБКА: проверь настройки .env")
else:
    try:
        response = requests.get(
            url + "/rest/api/3/myself",
            auth=HTTPBasicAuth(email, token),
            timeout=10,
        )
        print("HTTP:", response.status_code)

        if response.status_code == 200:
            print("Jira подключена успешно!")
    except requests.RequestException:
        print("Ошибка соединения с Jira")