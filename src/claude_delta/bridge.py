"""
Thin wrapper over deltachat_rpc_client: all Delta Chat communication goes
only through here, and only from the daemon (daemon.py) — the only
process allowed to hold the account db open.
"""
import logging
import os
import time

from deltachat_rpc_client import Account, DeltaChat, Rpc

log = logging.getLogger(__name__)

# DC_CONNECTIVITY_* thresholds from deltachat-core (jsonrpc get_connectivity).
_CONNECTIVITY_LABELS = {
    1000: "не подключён",
    2000: "подключается",
    3000: "работает (не все папки синхронизированы)",
    4000: "подключён",
}
_CONNECTIVITY_POLL_TIMEOUT_SEC = 15


class Bridge:
    def __init__(self, accounts_dir: str, bot_addr: str, bot_password: str, peer_addr: str):
        self.accounts_dir = accounts_dir
        self.bot_addr = bot_addr
        self.bot_password = bot_password
        self.peer_addr = peer_addr
        self._rpc = None
        self._dc = None
        self._account = None

    def __enter__(self):
        self._rpc = Rpc(accounts_dir=self.accounts_dir)
        self._rpc.start()
        self._dc = DeltaChat(self._rpc)
        accounts = self._dc.get_all_accounts()
        self._account = accounts[0] if accounts else self._dc.add_account()
        if not self._account.is_configured():
            self._account.set_config("addr", self.bot_addr)
            self._account.set_config("mail_pw", self.bot_password)
            self._account.configure()
        self._account.start_io()
        self._log_connectivity()
        return self

    def _log_connectivity(self):
        """Best-effort: poll get_connectivity() briefly and log the
        result, so the daemon's log answers "did IMAP/SMTP actually come
        up" instead of going silent after "RPC server ready" until the
        first 10-minute fallback-restart log line (see design.md).
        Never raises — a missing/renamed jsonrpc method on some future
        core version shouldn't take the daemon down over a log line.
        """
        deadline = time.time() + _CONNECTIVITY_POLL_TIMEOUT_SEC
        last = None
        try:
            while time.time() < deadline:
                last = self._rpc.get_connectivity(self._account.id)
                if last >= 3000:
                    break
                time.sleep(1)
            label = _CONNECTIVITY_LABELS.get(last, f"код {last}")
            log.info("connectivity после start_io(): %s", label)
        except Exception:
            log.exception("не удалось проверить connectivity (не критично, продолжаю)")

    def __exit__(self, *exc):
        if self._account is not None:
            self._account.stop_io()
        if self._rpc is not None:
            self._rpc.close()

    @property
    def account(self) -> Account:
        return self._account

    def peer_contact(self):
        """Returns the already-known key-contact for the user.

        Encrypted group chats require a key-contact (with an exchanged
        public key) — create_contact() from a bare address creates a
        plain address-contact without a key, which doesn't work for
        encrypted groups ("Only key-contacts can be added to encrypted
        chats"). The key already exists after a prior manual
        secure-join/exchange, we just look it up.
        """
        contact = self._account.get_contact_by_addr(self.peer_addr)
        if contact is None:
            raise RuntimeError(
                f"нет известного key-contact для {self.peer_addr} — "
                "нужен предварительный обмен сообщением/secure-join"
            )
        return contact

    def create_session_group(self, name: str) -> int:
        """Creates a group chat for a session with the already-known user contact."""
        chat = self._account.create_group(name)
        chat.add_contact(self.peer_contact())
        return chat.id

    def send_text(self, chat_id: int, text: str) -> int:
        chat = self._account.get_chat_by_id(chat_id)
        msg = chat.send_text(text)
        return msg.id

    DC_STATE_IN_FRESH = 10

    def fetch_all_fresh_messages(self):
        """All unread incoming messages across *every* chat on the
        account, marks them read (MDN) — one RPC round-trip regardless of
        how many armed sessions exist, via Account.get_fresh_messages().

        Reliability note (2026-08-10): the previous per-chat approach
        called chat.get_messages() (the chat's *entire* history) plus a
        get_snapshot() RPC per message, once per armed session, every
        ~5s poll — a growing, unbounded cost as a chat accumulates
        forwarded prompts (dozens per session in a long run), repeated
        for every armed session in the same single-threaded loop that
        also runs prompt detection and delivery. get_fresh_messages()
        only returns what's actually unread, account-wide.

        Returns dicts {id, chat_id, text, file, file_mime, view_type} —
        for voice/audio messages text is usually empty and file points to
        the locally downloaded attachment (transcribed separately, see
        stt.py). Caller is responsible for routing by chat_id.

        Does not delete messages — the caller must finish post-processing
        first (e.g. transcribe a voice message by reading file), and only
        then call delete_processed(). Otherwise delete_messages() would
        wipe the local blob before STT gets a chance to read it.
        """
        result = []
        for m in self._account.get_fresh_messages():
            snap = m.get_snapshot()
            if snap.state != self.DC_STATE_IN_FRESH:
                continue
            result.append({
                "id": m.id,
                "chat_id": snap.chat_id,
                "text": snap.text,
                "file": snap.file,
                "file_mime": snap.file_mime,
                "view_type": snap.view_type,
            })
            m.mark_seen()
        return result

    def delete_processed(self, msg_ids: list[int]) -> None:
        """Delete messages locally and on the bot's server — a precious
        copy already lives in the drop-box, no need to pile up on the
        server too."""
        if not msg_ids:
            return
        messages = [self._account.get_message_by_id(i) for i in msg_ids]
        self._account.delete_messages(messages)
