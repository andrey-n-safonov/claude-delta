"""
One-off smoke test: bot login to Delta Chat, account configuration check.
Not part of the daemon — just to confirm the setup works before writing
the permanent bridge.
"""
import os

from deltachat_rpc_client import DeltaChat, Rpc

ADDR = os.environ["DELTA_ADDR"]
PASSWORD = os.environ["DELTA_PASSWORD"]


def main():
    with Rpc(accounts_dir="./accounts") as rpc:
        deltachat = DeltaChat(rpc)
        system_info = deltachat.get_system_info()
        print("core version:", system_info["deltachat_core_version"])

        accounts = deltachat.get_all_accounts()
        if accounts:
            account = accounts[0]
            print("используем существующий аккаунт id:", account.id)
        else:
            account = deltachat.add_account()
            print("создан новый аккаунт id:", account.id)

        if not account.is_configured():
            print("настраиваю аккаунт...")
            account.set_config("addr", ADDR)
            account.set_config("mail_pw", PASSWORD)
            account.configure()
            print("настройка завершена")
        else:
            print("аккаунт уже настроен")

        me = account.get_config("addr")
        print("аккаунт активен как:", me)


if __name__ == "__main__":
    main()
