"""
Важные нюансы при такой схеме:
1. Метод client.add_event_handler: Вместо декоратора @client.on здесь используется явное добавление хэндлера, так как мы создаем клиенты динамически в цикле.
 Работает это абсолютно одинаково.
2. Сессии: При первом запуске скрипта в терминале каждый аккаунт по очереди попросит ввести код из SMS (или Telegram-уведомления) и пароль двухфакторки (2FA).
 После этого создадутся файлы .session. При повторных запусках коды уже запрашиваться не будут.
"""

import asyncio
import socks # я бы выбрал http-proxy
from telethon import TelegramClient, events

# Конфигурация ваших 10 аккаунтов
ACCOUNTS = [
    {
        "session": "session_account_1",
        "api_id": 111111,
        "api_hash": "hash_1",
        "proxy": (socks.SOCKS5, "192.168.1.1", 1080, True, "user1", "pass1")
    },
    {
        "session": "session_account_2",
        "api_id": 222222,
        "api_hash": "hash_2",
        "proxy": (socks.SOCKS5, "192.168.1.2", 1080, True, "user2", "pass2")
    },
    # ... и так далее до 10
]

# Общий обработчик для всех постов
async def msg_handler(event):
    try:
        chat = await event.get_chat()
        # event.client позволяет узнать, какой именно аккаунт поймал пост
        me = await event.client.get_me()
        
        print(f"[{me.username}] Новый пост в '{chat.title}':")
        print(event.text)
        print("-" * 30)

        # def fetch_posts():
        #     ...
    except Exception as e:
        print(f"Ошибка при обработке события: {e}")

async def run_account(config):
    """Функция запускает один конкретный аккаунт"""
    client = TelegramClient(
        config["session"],
        config["api_id"],
        config["api_hash"],
        proxy=config["proxy"]
    )
    
    # Регистрируем хэндлер на этот клиент (без указания chats, ловит всё где он состоит)
    client.add_event_handler(msg_handler, events.NewMessage())
    
    try:
        await client.start()
        print(f"Аккаунт {config['session']} успешно запущен и слушает каналы.")
        # Удерживаем соединение для этого клиента
        await client.run_until_disconnected()
    except Exception as e:
        print(f"Ошибка запуска аккаунта {config['session']}: {e}")

async def main():
    # Создаем асинхронные задачи для каждого аккаунта
    tasks = [run_account(acc) for acc in ACCOUNTS]
    # Запускаем их одновременно в одном Event Loop
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    # Запуск общего бесконечного цикла
    asyncio.run(main())


# #####
# async def msg_handler(event):
#     try:
#         # ВСЕГДА оборачиваем логику в try-except, чтобы ошибка не убила хэндлер
#         chat = await event.get_chat()
#         print(f"Поймали пост из {chat.title}: {event.text[:30]}")
#     except Exception as e:
#         print(f"Ошибка внутри обработчика поста: {e}")

# async def run_account(config):
#     while True: # Бесконечный цикл перезапуска при жестких сбоях сети/прокси
#         client = TelegramClient(
#             config["session"],
#             config["api_id"],
#             config["api_hash"],
#             proxy=config["proxy"],
#             connection_retries=5, # Пытаться переподключиться 5 раз при сбое сети
#             retry_delay=5        # Пауза в 5 секунд между попытками
#         )
        
#         client.add_event_handler(msg_handler, events.NewMessage())
        
#         try:
#             await client.start()
#             print(f"[{config['session']}] Соединение установлено. Слушаем...")
#             await client.run_until_disconnected()
#         except (socks.ProxyConnectionError, ConnectionError, asyncio.CancelledError) as e:
#             print(f"[{config['session']}] Проблема с сетью/прокси ({e}). Переподключение через 30 секунд...")
#             await asyncio.sleep(30)
#         except Exception as e:
#             print(f"[{config['session']}] Критическая ошибка: {e}. Перезапуск через 60 секунд...")
#             await asyncio.sleep(60)
#         finally:
#             # Корректно закрываем сессию перед перезапуском
#             await client.disconnect()

