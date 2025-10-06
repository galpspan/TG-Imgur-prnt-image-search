import asyncio

from logger import Logger
from bot import ImageBot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

def main():
    logger = Logger()
    bot = ImageBot()
    try:
        with open("token.txt", "r") as f:
            token = f.read().strip()
    except FileNotFoundError:
        logger.error("Создайте файл token.txt с токеном бота")
        return
    except Exception as e:
        logger.error(f"Ошибка чтения token.txt: {str(e)}")
        return

    if not token:
        logger.error("Токен бота не найден в token.txt")
        return

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("getimg", bot.get_imgur_images))
    application.add_handler(CommandHandler("getprnt", bot.get_prnt_images))
    application.add_handler(CommandHandler("getpastenow", bot.get_pastenow_images))
    application.add_handler(CommandHandler("getfreeimage", bot.get_freeimage_images))
    application.add_handler(CommandHandler("getkappa", bot.get_kappa_images))
    application.add_handler(CommandHandler("getall", bot.search_all_sources))
    application.add_handler(CommandHandler("stop", bot.stop))
    application.add_handler(CommandHandler("repeat", bot.repeat_last_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    logger.info("Бот запущен")
    print("Бот запущен. Нажмите Ctrl+C для остановки")

    try:
        application.run_polling()
    except KeyboardInterrupt:
        logger.info("Получен сигнал KeyboardInterrupt")
    except Exception as e:
        logger.error(f"Ошибка при работе бота: {str(e)}")
    finally:
        logger.info("Завершение работы бота")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            loop.run_until_complete(bot.cleanup())
        except RuntimeError as e:
            if "Event loop is closed" in str(e):
                pass
        except Exception as e:
            pass


if __name__ == "__main__":
    main()