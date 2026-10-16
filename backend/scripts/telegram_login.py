"""Telegram 首次登录(两阶段,验证码由用户本人输入)。

第一阶段:发送验证码
  TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TELEGRAM_PHONE=+86... \
    .venv/bin/python scripts/telegram_login.py send

第二阶段:输入验证码完成登录(验证码写入 data/.login-code.txt,脚本读取后删除)
  echo "12345" > data/.login-code.txt
  TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TELEGRAM_PHONE=+86... \
    .venv/bin/python scripts/telegram_login.py code

若账号开了两步验证,需要第三阶段(密码同样经 data/.login-code.txt):
  echo "你的密码" > data/.login-code.txt
  ... .venv/bin/python scripts/telegram_login.py password

登录成功后打印账号信息与可见的群/频道(含 ID),用于填写 TELEGRAM_CHATS。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

STATE = Path("data/.login-state.json")
CODE_FILE = Path("data/.login-code.txt")


def read_code() -> str:
    if not CODE_FILE.exists():
        print("ERROR: 请先把验证码写入 data/.login-code.txt", file=sys.stderr)
        sys.exit(1)
    code = CODE_FILE.read_text(encoding="utf-8").strip()
    CODE_FILE.unlink()
    return code


async def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else "send"
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone = os.environ.get("TELEGRAM_PHONE")
    session_path = os.environ.get("TELEGRAM_SESSION_PATH", "data/telegram.session")

    from telethon import TelegramClient, errors

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    if phase == "status":
        if await client.is_user_authorized():
            await _finish(client)
        else:
            print("未登录:请先执行 send 阶段获取验证码。")
    elif phase == "send":
        sent = await client.send_code_request(phone)
        STATE.write_text(json.dumps({"phone": phone, "phone_code_hash": sent.phone_code_hash}))
        print(f"验证码已发送到 {phone}(Telegram 客户端内或短信)。")
        print("把收到的验证码告诉我,我来完成登录。")
    elif phase == "code":
        code = read_code()
        st = json.loads(STATE.read_text(encoding="utf-8"))
        try:
            await client.sign_in(st["phone"], code, phone_code_hash=st["phone_code_hash"])
        except errors.SessionPasswordNeededError:
            STATE.write_text(json.dumps({**st, "needs_2fa": True}))
            print("此账号开启了两步验证:请把两步验证密码发给我继续。")
            await client.disconnect()
            return
        await _finish(client)
    elif phase == "password":
        password = read_code()
        st = json.loads(STATE.read_text(encoding="utf-8"))
        await client.sign_in(st["phone"], password=password)
        await _finish(client)
    else:
        print("用法: telegram_login.py status|send|code|password", file=sys.stderr)
        sys.exit(2)

    await client.disconnect()


async def _finish(client) -> None:
    me = await client.get_me()
    print(f"\n✅ 登录成功: {me.first_name} (@{me.username or '-'}) id={me.id}")
    print("\n账号可见的群/频道(取前 40 个,供填写 TELEGRAM_CHATS):")
    async for d in client.iter_dialogs(limit=40):
        if d.is_group or d.is_channel:
            print(f"  {d.id}\t{d.title}")
    STATE.unlink(missing_ok=True)
    print("\nsession 已保存,后续 Runner 将直接复用,无需重复登录。")


if __name__ == "__main__":
    asyncio.run(main())
