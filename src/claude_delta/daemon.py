"""
Демон-мост: единственный процесс, держащий открытым соединение с Delta
Chat (deltachat-rpc-server). Обслуживает очередь запросов из sqlite
(store.py) — создание сессионных чатов, отправка, приём.

Конфигурация — переменные окружения:
  DELTA_ADDR, DELTA_PASSWORD  — ящик бота
  DELTA_PEER_ADDR             — адрес пользователя (личный аккаунт)
  DELTA_ACCOUNTS_DIR          — куда deltachat-core кладёт свою базу
  DELTA_STORE_DB              — путь к drop-box sqlite (store.py)

Цикл: каждую итерацию (несколько секунд) обрабатывает pending
session_requests и pending outbox, затем проверяет новые сообщения во
всех armed-сессиях. Раз в FALLBACK_RESTART_SEC демон форсирует
stop_io()/start_io() — страховка на случай, если внутренний
IMAP-планировщик ядра тихо застрял (см. docs/design.md в vault).
"""
import logging
import os
import signal
import sys
import time

from . import store, stt
from .bridge import Bridge

VOICE_VIEW_TYPES = {"Voice", "Audio"}

LOOP_INTERVAL_SEC = 5
FALLBACK_RESTART_SEC = 10 * 60  # 10 минут, см. design.md

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("claude_delta.daemon")

_running = True


def _handle_signal(signum, frame):
    global _running
    log.info("получен сигнал %s, завершаюсь", signum)
    _running = False


def _process_session_requests(bridge: Bridge, db_path: str):
    for req in store.pending_session_requests(db_path):
        session_id, name = req["session_id"], req["name"]
        try:
            chat_id = bridge.create_session_group(name)
            store.fulfill_session_request(db_path, session_id, chat_id)
            log.info("сессия %s: создан чат id=%s", session_id, chat_id)
        except Exception as e:
            store.fail_session_request(db_path, session_id, repr(e))
            log.exception("сессия %s: ошибка создания чата", session_id)


def _process_outbox(bridge: Bridge, db_path: str):
    for item in store.pending_outbox(db_path):
        try:
            bridge.send_text(item["chat_id"], item["text"])
            store.mark_outbox_sent(db_path, item["id"])
            log.info("outbox #%s отправлен в чат %s", item["id"], item["chat_id"])
        except Exception as e:
            store.mark_outbox_error(db_path, item["id"], repr(e))
            log.exception("outbox #%s: ошибка отправки", item["id"])


def _process_inbox(bridge: Bridge, db_path: str):
    for sess in store.armed_sessions(db_path):
        processed_ids = []
        for msg in bridge.fetch_new_messages(sess["chat_id"]):
            text = msg["text"]
            if msg["view_type"] in VOICE_VIEW_TYPES and msg["file"]:
                try:
                    transcript = stt.transcribe(msg["file"])
                    text = f"[голосовое] {transcript}"
                except Exception:
                    log.exception("сессия %s: ошибка распознавания msg_id=%s", sess["session_id"], msg["id"])
                    text = "[голосовое — распознать не удалось]"
            store.store_inbox_message(db_path, sess["chat_id"], msg["id"], text)
            log.info("сессия %s: новое сообщение (msg_id=%s) %r", sess["session_id"], msg["id"], text[:60])
            processed_ids.append(msg["id"])
        # Удаляем только после того, как всё (включая STT) обработано —
        # см. комментарий в Bridge.delete_processed.
        try:
            bridge.delete_processed(processed_ids)
        except Exception:
            log.exception("сессия %s: ошибка удаления обработанных сообщений", sess["session_id"])


def run():
    addr = os.environ["DELTA_ADDR"]
    password = os.environ["DELTA_PASSWORD"]
    peer_addr = os.environ["DELTA_PEER_ADDR"]
    accounts_dir = os.environ.get("DELTA_ACCOUNTS_DIR", "./accounts")
    db_path = os.environ.get("DELTA_STORE_DB", "./bridge.sqlite3")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("старт демона, ящик=%s, peer=%s", addr, peer_addr)

    last_restart = time.time()
    with Bridge(accounts_dir, addr, password, peer_addr) as bridge:
        while _running:
            try:
                _process_session_requests(bridge, db_path)
                _process_outbox(bridge, db_path)
                _process_inbox(bridge, db_path)
            except Exception:
                log.exception("ошибка в цикле демона")

            if time.time() - last_restart > FALLBACK_RESTART_SEC:
                log.info("периодический перезапуск IO (fallback, см. design.md)")
                bridge.account.stop_io()
                bridge.account.start_io()
                last_restart = time.time()

            time.sleep(LOOP_INTERVAL_SEC)

    log.info("демон остановлен")


if __name__ == "__main__":
    sys.exit(run())
