"""REST API мониторинга оборудования и регистрации инцидентов."""

import hmac
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from jira_integration import JiraIntegrationError, create_incident
from vk_notifier import VKNotificationError, send_vk_message


app = FastAPI(
    title="Мониторинг оборудования",
    description="Учёт простоев оборудования, уведомления VK и инциденты Jira",
    version="2.0.0",
)

DB_PATH = Path(os.getenv("EQUIPMENT_DB_PATH") or Path(__file__).with_name("equipment.db")).expanduser()


def require_api_key(x_api_key: str | None = Header(default=None)):
    """Защита операций записи, когда SERVICE_API_KEY задан в .env/окружении."""
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
    expected = os.getenv("SERVICE_API_KEY", "")
    if expected and not hmac.compare_digest(x_api_key or "", expected):
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий X-API-Key")




def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Создать отсутствующие таблицы, сохранив существующие данные."""

    with closing(get_connection()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS machines (
                machine_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id TEXT NOT NULL,
                status TEXT NOT NULL,
                stopped_at TEXT,
                notification_needed INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS downtimes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_seconds INTEGER
            )
        """)

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_downtime_per_machine
            ON downtimes(machine_id)
            WHERE ended_at IS NULL
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS vk_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL UNIQUE,
                random_id INTEGER NOT NULL UNIQUE,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                vk_message_id INTEGER,
                error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS jira_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL UNIQUE,
                machine_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                jira_key TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


init_db()


class MachineEvent(BaseModel):
    machine_id: str = Field(min_length=1, max_length=50)
    status: Literal["RUNNING", "STOPPED"]
    stopped_at: str = Field(default="", max_length=64)


def deliver_vk_notification(notification_id: int):
    """Отправить уведомление VK и сохранить результат в SQLite."""

    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM vk_notifications WHERE id = ?",
            (notification_id,),
        ).fetchone()

    if row is None:
        raise ValueError("Уведомление VK не найдено")

    if row["status"] == "sent":
        return {"status": "sent", "error": None}

    try:
        vk_message_id = send_vk_message(
            row["message"],
            row["random_id"],
        )

    except VKNotificationError as exc:
        error = str(exc)

    except Exception:
        # Ошибка VK не должна препятствовать созданию инцидента.
        error = "Непредвиденная ошибка отправки VK"

    else:
        with closing(get_connection()) as conn, conn:
            conn.execute(
                """
                UPDATE vk_notifications
                SET status = 'sent',
                    vk_message_id = ?,
                    error = NULL,
                    sent_at = ?
                WHERE id = ?
                """,
                (
                    vk_message_id,
                    datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    notification_id,
                ),
            )

        return {
            "status": "sent",
            "error": None,
        }

    with closing(get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE vk_notifications
            SET status = 'failed',
                error = ?
            WHERE id = ?
            """,
            (error, notification_id),
        )

    return {
        "status": "failed",
        "error": error,
    }


def deliver_jira_incident(event_id: int):
    """Доставить событие ровно одним потоком; спорные ответы не повторять.

    Состояние sending сохраняется ДО отправки. Если процесс погибнет
    после запроса, задача может существовать в Jira — нужно проверить её.
    """
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jira_incidents WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Запись об инциденте Jira не найдена")
        if row["status"] != "pending":
            return {
                "status": row["status"],
                "key": row["jira_key"],
                "error": row["error"],
            }
        conn.execute(
            "UPDATE jira_incidents SET status = 'sending' WHERE event_id = ?",
            (event_id,),
        )

    try:
        result = create_incident(
            machine_id=row["machine_id"],
            started_at=row["started_at"],
            event_id=row["event_id"],
        )
        issue_key = result["key"]
        mode = result["mode"]
        if mode not in ("demo", "live") or not issue_key:
            raise JiraIntegrationError("Неожиданный ответ модуля Jira")
    except JiraIntegrationError:
        # Даже при ошибке нельзя гарантировать, что Jira не создала задачу.
        error = "Jira не подтвердила результат. Проверьте Jira перед повтором."
    except Exception:
        error = "Неопределённый результат Jira. Возможна созданная задача."
    else:
        status = "demo" if mode == "demo" else "created"
        with closing(get_connection()) as conn, conn:
            conn.execute(
                """
                UPDATE jira_incidents
                SET status = ?, jira_key = ?, error = NULL
                WHERE event_id = ? AND status = 'sending'
                """,
                (status, issue_key, event_id),
            )
        return {"status": status, "key": issue_key, "error": None}

    with closing(get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jira_incidents
            SET status = 'uncertain', error = ?
            WHERE event_id = ? AND status = 'sending'
            """,
            (error, event_id),
        )
    return {"status": "uncertain", "key": None, "error": error}


@app.get("/")
def root():
    return {
        "message": "Сервис мониторинга работает",
        "status": "online",
    }


@app.post("/api/events", dependencies=[Depends(require_api_key)])
def receive_event(event: MachineEvent):

    machine_id = event.machine_id.strip()

    if not machine_id:
        raise HTTPException(
            status_code=422,
            detail="Укажите идентификатор станка",
        )

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    # Для STOPPED принимаем время от оборудования в ISO 8601 с часовым поясом.
    started_at = now_iso
    if event.status == "STOPPED" and event.stopped_at:
        try:
            source_time = datetime.fromisoformat(
                event.stopped_at.replace("Z", "+00:00")
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="Некорректный stopped_at; нужен ISO 8601")
        if source_time.tzinfo is None:
            raise HTTPException(status_code=422, detail="Укажите часовой пояс в stopped_at")
        if source_time > now + timedelta(minutes=5):
            raise HTTPException(status_code=422, detail="stopped_at находится в будущем")
        started_at = source_time.astimezone(timezone.utc).isoformat(timespec="seconds")

    duration_seconds = None
    notification_id = None
    incident_event_id = None

    conn = get_connection()

    try:
        # Защита от одновременных изменений одного станка.
        conn.execute("BEGIN IMMEDIATE")

        old = conn.execute(
            """
            SELECT status
            FROM machines
            WHERE machine_id = ?
            """,
            (machine_id,),
        ).fetchone()

        previous_status = old["status"] if old else None

        if event.status == "STOPPED":

            notification_needed = previous_status != "STOPPED"

            if notification_needed:
                message = "ВНИМАНИЕ! Станок остановился!"

                conn.execute(
                    """
                    INSERT INTO downtimes (
                        machine_id,
                        started_at
                    )
                    VALUES (?, ?)
                    """,
                    (machine_id, started_at),
                )

            else:
                message = (
                    "Повторное сообщение. "
                    "Уведомление не требуется."
                )

        else:
            message = "Станок работает нормально"
            notification_needed = False

            active = conn.execute(
                """
                SELECT id, started_at
                FROM downtimes
                WHERE machine_id = ?
                  AND ended_at IS NULL
                """,
                (machine_id,),
            ).fetchone()

            if active:
                start = datetime.fromisoformat(
                    active["started_at"]
                )

                duration_seconds = max(
                    0,
                    int((now - start).total_seconds()),
                )

                conn.execute(
                    """
                    UPDATE downtimes
                    SET ended_at = ?,
                        duration_seconds = ?
                    WHERE id = ?
                    """,
                    (
                        now_iso,
                        duration_seconds,
                        active["id"],
                    ),
                )

        # Сохраняем текущее состояние оборудования.
        conn.execute(
            """
            INSERT INTO machines (
                machine_id,
                status
            )
            VALUES (?, ?)
            ON CONFLICT(machine_id)
            DO UPDATE SET status = excluded.status
            """,
            (machine_id, event.status),
        )

        # Сохраняем полученное событие.
        cursor = conn.execute(
            """
            INSERT INTO events (
                machine_id,
                status,
                stopped_at,
                notification_needed
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                machine_id,
                event.status,
                event.stopped_at,
                int(notification_needed),
            ),
        )

        event_id = cursor.lastrowid

        # Для новой остановки создаём запись Jira.
        if notification_needed:

            incident_event_id = event_id

            conn.execute(
                """
                INSERT INTO jira_incidents (
                    event_id,
                    machine_id,
                    started_at,
                    status
                )
                VALUES (?, ?, ?, 'pending')
                """,
                (
                    event_id,
                    machine_id,
                    started_at,
                ),
            )

            # Формируем VK-уведомление.
            text_message = (
                "ОСТАНОВКА ОБОРУДОВАНИЯ\n"
                f"Станок: {machine_id}\n"
                "Статус: STOPPED\n"
                f"Время регистрации: "
                f"{now.strftime('%d.%m.%Y %H:%M:%S')} UTC\n"
                "Требуется проверка оборудования."
            )

            vk_cursor = conn.execute(
                """
                INSERT INTO vk_notifications (
                    event_id,
                    random_id,
                    message,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (
                    event_id,
                    secrets.randbelow(2_147_483_647) + 1,
                    text_message,
                    now_iso,
                ),
            )

            notification_id = vk_cursor.lastrowid

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    # Отправляем уведомления после сохранения события.
    jira_incident = {
        "status": "not_required",
        "key": None,
        "error": None,
    }

    vk_notification = {
        "status": "not_required",
        "error": None,
    }

    if incident_event_id is not None:
        jira_incident = deliver_jira_incident(
            incident_event_id
        )

    if notification_id is not None:
        vk_notification = deliver_vk_notification(
            notification_id
        )

    return {
        "message": message,
        "machine_id": machine_id,
        "status": event.status,
        "notification_needed": notification_needed,
        "duration_seconds": duration_seconds,
        "vk_notification": vk_notification,
        "jira_incident": jira_incident,
    }


@app.get("/api/events")
def get_events():

    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM events
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

    result = [dict(row) for row in rows]

    for item in result:
        item["notification_needed"] = bool(
            item["notification_needed"]
        )

    return result


@app.get("/api/machines")
def get_machines():

    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT machine_id, status
            FROM machines
            ORDER BY machine_id
            """
        ).fetchall()

    return [dict(row) for row in rows]


@app.get("/api/downtimes")
def get_downtimes():

    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM downtimes
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

    now = datetime.now(timezone.utc)
    result = []

    for row in rows:
        item = dict(row)

        item["ongoing"] = item["ended_at"] is None

        if item["ongoing"]:
            start = datetime.fromisoformat(
                item["started_at"]
            )

            item["duration_seconds"] = max(
                0,
                int((now - start).total_seconds()),
            )

        result.append(item)

    return result


@app.get("/api/notifications")
def get_notifications():

    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT id,
                   event_id,
                   status,
                   error,
                   created_at,
                   sent_at
            FROM vk_notifications
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

    return [dict(row) for row in rows]


@app.get("/api/incidents")
def get_incidents():
    """История инцидентов Jira."""

    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT id,
                   event_id,
                   machine_id,
                   started_at,
                   jira_key,
                   status,
                   error,
                   created_at
            FROM jira_incidents
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

    return [dict(row) for row in rows]


@app.post("/api/notifications/retry", dependencies=[Depends(require_api_key)])
def retry_notifications():
    # Перед публикацией API в интернете
    # этот маршрут необходимо защитить.

    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM vk_notifications
            WHERE status IN ('failed', 'pending')
            ORDER BY id
            LIMIT 20
            """
        ).fetchall()

    return [
        {
            "id": row["id"],
            **deliver_vk_notification(row["id"]),
        }
        for row in rows
    ]


@app.get("/health")
def health():
    """Проверка локальной базы без обращений во внешние сервисы."""
    with closing(get_connection()) as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "database": "ok"}


@app.get("/dashboard", include_in_schema=False)
def dashboard():

    dashboard_path = Path(__file__).with_name(
        "dashboard.html"
    )

    if not dashboard_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Файл dashboard.html не найден",
        )

    return FileResponse(
        dashboard_path,
        media_type="text/html",
    )