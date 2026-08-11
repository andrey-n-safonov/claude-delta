"""
One-off maintenance script: consume a Delta Chat secure-join QR/invite
link (from the peer's own app — Settings -> Invite via QR/link) on the
bot account, establishing the verified key-contact the bridge needs for
create_session_group()/peer_contact() to work.

Needed once per bot mailbox — after a fresh mailbox swap (see INSTALL.md/
Delta Bridge creds.md), the bot has no key-contact for the peer yet and
`/delta-chat on` fails with "нет известного key-contact". Run this with
the daemon STOPPED (same account-exclusivity rule as smoke_test.py).

Usage:
    cd ~/work/claude-delta && source .venv/bin/activate
    python scripts/securejoin.py 'https://i.delta.chat/#FINGERPRINT&v=3&i=...&s=...&a=peer%40example.org&n=Name'
"""
import os
import sys
import threading
from urllib.parse import parse_qs

from deltachat_rpc_client import DeltaChat, Rpc

ACCOUNTS_DIR = os.environ.get("DELTA_ACCOUNTS_DIR", "./accounts")
TIMEOUT_SEC = 60


def parse_peer_addr(qr: str) -> str | None:
    """Both link forms (`https://i.delta.chat/#...` and `OPENPGP4FPR:...#...`)
    carry the peer's address as the `a=` param after the `#` — no need to
    ask the user for DELTA_PEER_ADDR separately, it's already in the link
    they send for step 5 of INSTALL.md."""
    if "#" not in qr:
        return None
    values = parse_qs(qr.split("#", 1)[1]).get("a")
    return values[0] if values else None


def main():
    if len(sys.argv) != 2:
        print("usage: securejoin.py <qr-or-link>", file=sys.stderr)
        return 1
    qr = sys.argv[1]

    parsed_peer = parse_peer_addr(qr)
    env_peer = os.environ.get("DELTA_PEER_ADDR")
    if parsed_peer:
        print("peer-адрес из ссылки:", parsed_peer)
    if env_peer and parsed_peer and env_peer != parsed_peer:
        print(
            f"ВНИМАНИЕ: DELTA_PEER_ADDR={env_peer} не совпадает с адресом "
            f"из ссылки ({parsed_peer}) — проверь daemon.env",
            file=sys.stderr,
        )
    peer_addr = env_peer or parsed_peer

    with Rpc(accounts_dir=ACCOUNTS_DIR) as rpc:
        deltachat = DeltaChat(rpc)
        accounts = deltachat.get_all_accounts()
        if not accounts:
            print("нет сконфигурированного аккаунта — сначала настроить бота", file=sys.stderr)
            return 1
        account = accounts[0]
        if not account.is_configured():
            print("аккаунт не сконфигурирован", file=sys.stderr)
            return 1

        print("аккаунт:", account.get_config("addr"))
        print("проверка QR:", account.check_qr(qr))

        account.start_io()
        try:
            chat = account.secure_join(qr)
            print("secure_join запущен, chat_id:", chat.id)

            done = threading.Event()

            def waiter():
                try:
                    account.wait_for_securejoin_joiner_success()
                finally:
                    done.set()

            t = threading.Thread(target=waiter, daemon=True)
            t.start()
            print(f"жду подтверждения (до {TIMEOUT_SEC}с)...")
            if done.wait(TIMEOUT_SEC):
                print("secure-join завершён успешно (progress=1000)")
            else:
                print(
                    "таймаут ожидания финального подтверждения — "
                    "проверяю контакт напрямую (Autocrypt-обмен мог пройти "
                    "и без формального завершения хендшейка, см. design.md)",
                    file=sys.stderr,
                )
        finally:
            account.stop_io()

        peer_addr = os.environ.get("DELTA_PEER_ADDR")
        if peer_addr:
            contact = account.get_contact_by_addr(peer_addr)
            print(f"get_contact_by_addr({peer_addr}):", contact)
        else:
            print("DELTA_PEER_ADDR не задан в окружении — контакт не проверен", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
