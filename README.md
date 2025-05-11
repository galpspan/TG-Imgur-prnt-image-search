# Telegram Image Search Bot 🤖🖼️  

**Бот для поиска случайных изображений** с Imgur и prnt.sc. Находит рабочие ссылки на картинки, проверяет их доступность и отправляет в чат.  
Подсмотрел у https://github.com/VladislavMilev/prnt.sc_parser

## 🔧 Установка и настройка  

### Требования  
- Python 3.7+  
- Аккаунт в Telegram  
- Созданный бот через [@BotFather](https://t.me/BotFather)  

### 1. Установка зависимостей  
```bash
pip install python-telegram-bot requests beautifulsoup4
```  
*Рекомендуемые версии:*  
```bash
pip install python-telegram-bot==20.3 requests==2.31.0 beautifulsoup4==4.12.2
```  

### 2. Настройка бота  
1. Получите токен бота у [@BotFather](https://t.me/BotFather)  
2. В файле `bot.py` замените строку:  
```python
application = Application.builder().token("YOUR_BOT_TOKEN").build()
```  
на ваш токен (в кавычках).  

### 3. Запуск  
```bash
python bot.py
```  

## 🚀 Как использовать  
Отправьте боту команду `/start` для списка команд:  

```
🔍 Доступные команды:  
/getimg <5|7> <1-25> - поиск на Imgur  
/getprnt <1-25> - поиск на prnt.sc (код из 6 символов)  
/stop - отменить текущий поиск  

Примеры:  
/getimg 5 10 → 10 случайных изображений с 5-символьным кодом  
/getprnt 5 → 5 изображений с prnt.sc  
```  

## ⚠️ Важно  
- Для prnt.sc длина кода всегда **6 символов**.  
- Бот проверяет каждое изображение перед отправкой.  
- Поддерживаются **GIF, JPG, PNG**.  

## 📝 Логи  
Журнал работы сохраняется в `image_bot.log`.  

## ⏹ Остановка  
- **Отмена поиска:** команда `/stop`  
- **Выключение бота:** `Ctrl+C` в терминале  
