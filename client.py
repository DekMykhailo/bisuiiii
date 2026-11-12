#!/usr/bin/env python3
"""Interactive command-line client for the BSUI2 Messenger server."""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from websockets.sync.client import connect


class ApiError(Exception):
    """An HTTP or connection error returned while calling the server."""


class MessengerClient:
    def __init__(self, base_url: str, ssl_context: ssl.SSLContext | None):
        self.base_url = base_url.rstrip("/")
        self.ssl_context = ssl_context
        self.token: str | None = None

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, context=self.ssl_context, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = exc.reason
            if isinstance(detail, dict):
                detail = detail.get("detail", detail)
            raise ApiError(f"HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"Не удалось подключиться к серверу: {exc}") from exc

    def websocket_url(self, room_id: str) -> str:
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        path = f"{parts.path}/ws/{quote(room_id, safe='')}"
        query = urlencode({"token": self.token or ""})
        return urlunsplit((scheme, parts.netloc, path, query, ""))

    def chat(self, room_id: str) -> None:
        history = self.request("GET", f"/get_msg/{quote(room_id, safe='')}?limit=100")
        print("\n--- История ---")
        for item in history.get("messages", []):
            print(f"[{item.get('Timestamp', '?')}] {item.get('who', '?')}: {item.get('message', '')}")
        print("--- Чат подключается; /q — выйти ---")

        kwargs = {"open_timeout": 15}
        if urlsplit(self.base_url).scheme == "https":
            kwargs["ssl"] = self.ssl_context or ssl.create_default_context()

        try:
            with connect(self.websocket_url(room_id), **kwargs) as ws:
                stopped = threading.Event()

                def receive() -> None:
                    try:
                        for raw in ws:
                            data = json.loads(raw)
                            if "error" in data:
                                print(f"Ошибка сервера: {data['error']}")
                            else:
                                print(f"\n[{data.get('Timestamp', '?')}] {data.get('who', '?')}: {data.get('message', '')}")
                            print("> ", end="", flush=True)
                    except Exception as exc:
                        if not stopped.is_set():
                            print(f"\nСоединение закрыто: {exc}")
                    finally:
                        stopped.set()

                reader = threading.Thread(target=receive, daemon=True)
                reader.start()
                while not stopped.is_set():
                    try:
                        text = input("> ").strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if text.lower() == "/q":
                        break
                    if text:
                        ws.send(json.dumps({"message": text}, ensure_ascii=False))
                stopped.set()
        except Exception as exc:
            raise ApiError(f"Не удалось открыть чат: {exc}") from exc


def run() -> int:
    parser = argparse.ArgumentParser(description="Консольный клиент BSUI2 Messenger")
    parser.add_argument("--server", default="http://localhost:2137", help="Адрес сервера, например http://localhost:2137")
    parser.add_argument("--ca-cert", help="Путь к доверенному сертификату сервера (для HTTPS с self-signed сертификатом)")
    parser.add_argument("--insecure", action="store_true", help="Отключить проверку TLS-сертификата (только для тестов)")
    args = parser.parse_args()

    if args.insecure and args.ca_cert:
        parser.error("Не используйте одновременно --insecure и --ca-cert")
    ssl_context = None
    if args.insecure:
        ssl_context = ssl._create_unverified_context()
        print("ВНИМАНИЕ: проверка TLS-сертификата отключена; используйте только для тестов.")
    elif args.ca_cert:
        ssl_context = ssl.create_default_context(cafile=args.ca_cert)

    client = MessengerClient(args.server, ssl_context)
    try:
        print("BSUI2 Messenger — введите 1 для входа, 2 для регистрации")
        action = input("> ").strip()
        login = input("Логин: ").strip()
        password = input("Пароль: ")
        if action == "2":
            client.request("POST", "/register", {"login": login, "password": password})
            print("Аккаунт создан. Выполняю вход…")
        elif action != "1":
            print("Нужно выбрать 1 или 2.")
            return 2
        client.token = client.request("POST", "/login", {"login": login, "password": password})["token"]
        print(f"Вход выполнен как {login}.")

        while True:
            print("\n1 — комнаты  2 — создать комнату  3 — пригласить  4 — открыть чат  0 — выход")
            action = input("> ").strip()
            if action == "0":
                break
            if action == "1":
                rooms = client.request("GET", "/my_rooms").get("rooms", [])
                if not rooms:
                    print("Комнат пока нет.")
                for room in rooms:
                    print(f"{room.get('id')} | {room.get('name')} | участники: {', '.join(room.get('users', []))}")
            elif action == "2":
                name = input("Название комнаты: ").strip()
                result = client.request("POST", "/create", {"name": name})
                print(f"Комната создана, ID: {result['id']}")
            elif action == "3":
                room_id = input("ID комнаты: ").strip()
                users = [user.strip() for user in input("Логины через запятую: ").split(",") if user.strip()]
                client.request("POST", f"/invite/{quote(room_id, safe='')}", {"users": users})
                print("Приглашение отправлено.")
            elif action == "4":
                client.chat(input("ID комнаты: ").strip())
            else:
                print("Неизвестная команда.")
    except (ApiError, KeyError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nВыход.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
