import logging

class Logger:
    async def handle(self):
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        user_info_filter = UserInfoFilter()

        file_handler = logging.FileHandler("image_bot.log", encoding='utf-8')
        file_handler.addFilter(user_info_filter)

        stream_handler = logging.StreamHandler()
        stream_handler.addFilter(user_info_filter)

        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s [User: %(user_info)s]"
        )
        file_handler.setFormatter(formatter)
        stream_handler.setFormatter(formatter)

        root_logger.addHandler(file_handler)
        root_logger.addHandler(stream_handler)
        root_logger.setLevel(logging.INFO)

        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

        logger = logging.getLogger(__name__)

        return logger

class UserInfoFilter(logging.Filter):
        def filter(self, record):
            if not hasattr(record, 'user_info'):
                record.user_info = "System"
            return True