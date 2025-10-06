from telegram import Update

def format_time(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    if hours > 0:
        return f"{hours}ч {minutes}м {seconds}с"
    elif minutes > 0:
        return f"{minutes}м {seconds}с"
    else:
        return f"{seconds}с"

def get_source_name(source_type: str, length: int = None) -> str:
    names = {
        "imgur5": "Imgur (5 симв.)",
        "imgur7": "Imgur (7 симв.)",
        "prnt": "Prnt.sc",
        "pastenow": "Paste.pics",
        "freeimage": "Freeimage",
        "kappa": "Kappa.lol",
        "all": "Все источники"
    }
    return names.get(source_type, source_type)

def user_info(update: Update) -> str:
    user = update.effective_user
    if user and user.username:
        return f"@{user.username} (ID: {user.id})"
    return f"ID: {user.id}" if user else "Unknown user"