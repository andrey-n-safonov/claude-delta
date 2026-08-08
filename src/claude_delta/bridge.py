"""
Тонкая обёртка над deltachat_rpc_client: всё общение с Delta Chat идёт
только отсюда, и только из демона (daemon.py) — единственного процесса,
которому разрешено держать открытым account db.
"""
import os

from deltachat_rpc_client import Account, DeltaChat, Rpc


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
        return self

    def __exit__(self, *exc):
        if self._account is not None:
            self._account.stop_io()
        if self._rpc is not None:
            self._rpc.close()

    @property
    def account(self) -> Account:
        return self._account

    def peer_contact(self):
        """Возвращает уже известный key-contact пользователя.

        Групповые чаты с шифрованием требуют key-contact (с обменянным
        публичным ключом) — create_contact() по одному адресу создаёт
        обычный address-contact без ключа и для encrypted-групп не
        годится ("Only key-contacts can be added to encrypted chats").
        Ключ уже есть после ручного secure-join/переписки, ищем его.
        """
        contact = self._account.get_contact_by_addr(self.peer_addr)
        if contact is None:
            raise RuntimeError(
                f"нет известного key-contact для {self.peer_addr} — "
                "нужен предварительный обмен сообщением/secure-join"
            )
        return contact

    def create_session_group(self, name: str) -> int:
        """Создаёт групповой чат под сессию с уже известным контактом-пользователем."""
        chat = self._account.create_group(name)
        chat.add_contact(self.peer_contact())
        return chat.id

    def send_text(self, chat_id: int, text: str) -> int:
        chat = self._account.get_chat_by_id(chat_id)
        msg = chat.send_text(text)
        return msg.id

    def fetch_new_messages(self, chat_id: int):
        """Все непрочитанные входящие в чате, помечает их прочитанными (MDN).

        Возвращает словари {id, text, file, file_mime, view_type} — для
        голосовых/аудио text обычно пуст, а file указывает на локальный
        путь к скачанному вложению (распознаётся отдельно, см. stt.py).
        """
        chat = self._account.get_chat_by_id(chat_id)
        result = []
        for m in chat.get_messages():
            snap = m.get_snapshot()
            if snap.state == 10:  # DC_STATE_IN_FRESH
                result.append({
                    "id": m.id,
                    "text": snap.text,
                    "file": snap.file,
                    "file_mime": snap.file_mime,
                    "view_type": snap.view_type,
                })
                m.mark_seen()
        return result
