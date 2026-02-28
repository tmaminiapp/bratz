import asyncio
import json
import logging
import os
import threading
import http
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# Загружаем переменные из .env
load_dotenv()

# === РЕЖИМ РАБОТЫ ===
# Определяем автоматически: local если запущено на localhost, иначе production
IS_LOCAL = os.getenv('ENVIRONMENT', 'production') == 'local'

# === КОНФИГУРАЦИЯ ИЗ .env ===
TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', str(ADMIN_ID)).split(',')]
PORT = int(os.getenv('PORT', 8000))  # Меняем на 8000 как у вас
FIREBASE_KEY_PATH = os.getenv('FIREBASE_CRED_PATH', 'bratz.json')

# === URL В ЗАВИСИМОСТИ ОТ РЕЖИМА ===
if IS_LOCAL:
    # Локально: отдаем файлы с компьютера
    WEBAPP_URL = f'http://localhost:{PORT}'
    STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
    print(f"\n🌍 ЛОКАЛЬНЫЙ РЕЖИМ РАЗРАБОТКИ")
    print(f"📂 Статические файлы из: {STATIC_DIR}")
    print(f"🔗 Откройте в браузере: http://localhost:{PORT}")
else:
    # На сервере: GitHub Pages
    WEBAPP_URL = 'https://tmaminiapp.github.io/bratz/'
    STATIC_DIR = None
    print(f"\n🌍 ПРОДАКШН РЕЖИМ")
    print(f"🔗 Сайт на GitHub: {WEBAPP_URL}")

# === НАСТРОЙКИ ЛОГИРОВАНИЯ ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Глобальные переменные
db_fs = None
application = None


# === ЧАСТЬ 1: HTTP СЕРВЕР ДЛЯ КОНФИГУРАЦИИ ===
class ConfigRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Добавляем CORS заголовки
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Cache-Control')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        try:
            # В ЛОКАЛЬНОМ режиме отдаем файлы с диска
            if IS_LOCAL:
                if self.path == '/config.json':
                    self._serve_file('config.json', 'application/json')
                elif self.path == '/' or self.path == '/index.html':
                    self._serve_file('index.html', 'text/html')
                else:
                    # Пробуем отдать другие файлы (css, js, изображения)
                    file_path = self.path.lstrip('/')
                    if os.path.exists(file_path) and os.path.isfile(file_path):
                        # Определяем тип по расширению
                        if file_path.endswith('.css'):
                            self._serve_file(file_path, 'text/css')
                        elif file_path.endswith('.js'):
                            self._serve_file(file_path, 'application/javascript')
                        elif file_path.endswith('.png'):
                            self._serve_file(file_path, 'image/png')
                        elif file_path.endswith('.jpg') or file_path.endswith('.jpeg'):
                            self._serve_file(file_path, 'image/jpeg')
                        elif file_path.endswith('.svg'):
                            self._serve_file(file_path, 'image/svg+xml')
                        else:
                            self._serve_file(file_path, 'text/plain')
                    else:
                        self.send_error(404, f"File {self.path} not found")
            else:
                # В ПРОДАКШН режиме отдаем только config.json
                if self.path == '/config.json':
                    self._serve_file('config.json', 'application/json')
                else:
                    self.send_error(404, "Not found")

        except Exception as e:
            logging.error(f"❌ Ошибка при обработке запроса {self.path}: {e}")
            self.send_error(500, f"Internal error: {e}")

    def _serve_file(self, file_path, content_type):
        """Вспомогательный метод для отправки файлов"""
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            if IS_LOCAL:
                print(f"✅ Отправлен файл: {file_path} ({len(content)} байт)")
        except FileNotFoundError:
            self.send_error(404, f"{file_path} not found")
        except Exception as e:
            self.send_error(500, f"Error reading {file_path}: {e}")

    def log_message(self, format, *args):
        # Подавляем стандартные логи HTTP сервера
        pass


def run_http_server():
    """Запускает HTTP сервер в отдельном потоке"""
    server = HTTPServer(('0.0.0.0', PORT), ConfigRequestHandler)
    print(f"✅ HTTP сервер запущен на порту {PORT}")
    server.serve_forever()


# === ЧАСТЬ 2: FIREBASE ИНИЦИАЛИЗАЦИЯ ===
def init_firebase():
    global db_fs
    try:
        # Проверяем существование файла с ключами
        if not os.path.exists(FIREBASE_KEY_PATH):
            print(f"⚠️ Файл {FIREBASE_KEY_PATH} не найден, Firebase не будет доступен")
            return False

        cred = credentials.Certificate(FIREBASE_KEY_PATH)
        firebase_admin.initialize_app(cred)
        db_fs = firestore.client()
        print("✅ Firebase успешно подключен")
        return True
    except Exception as e:
        print(f"❌ Ошибка Firebase: {e}")
        return False


# === ЧАСТЬ 3: СЛУШАТЕЛЬ ИЗМЕНЕНИЙ В ЗАКАЗАХ ===
def setup_firebase_listener(loop, app):
    global db_fs
    if db_fs is None:
        print("❌ Firebase не инициализирован, слушатель не запущен")
        return

    def on_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            try:
                if change.type.name == 'MODIFIED':
                    order_data = change.document.to_dict()
                    status = order_data.get('status')
                    order_id = order_data.get('order_id')
                    client_id = order_data.get('user', {}).get('id')

                    if client_id:
                        if status == 'Отправлен':
                            msg = (f"📦 <b>Ваш заказ #{order_id} отправлен!</b>\n"
                                   f"Скоро он будет у вас. Спасибо за покупку! ✨")
                        elif status == 'Доставлен':
                            msg = (f"✅ <b>Ваш заказ #{order_id} доставлен!</b>\n"
                                   f"Надеемся, вам всё понравилось. Будем рады вашему отзыву! ✨")
                        else:
                            return

                        # Отправка сообщения клиенту
                        asyncio.run_coroutine_threadsafe(
                            app.bot.send_message(chat_id=client_id, text=msg, parse_mode='HTML'),
                            loop
                        )
                        print(f"📩 Уведомление ({status}) отправлено клиенту {client_id}")
            except Exception as e:
                logging.error(f"Ошибка в on_snapshot: {e}")

    try:
        db_fs.collection('orders').on_snapshot(on_snapshot)
        print("👂 Слушатель Firebase запущен")
    except Exception as e:
        print(f"❌ Ошибка при запуске слушателя Firebase: {e}")


# === ЧАСТЬ 4: ОБРАБОТЧИКИ БОТА ===
async def web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_json = update.effective_message.web_app_data.data
        data = json.loads(raw_json)
        user_id = update.effective_user.id

        # Извлекаем все данные
        order_id = data.get('order_id', '???')
        name = data.get('customer_name') or data.get('name') or 'Не указано'
        phone = data.get('customer_phone') or data.get('phone') or 'Не указано'
        address = data.get('address') or data.get('customer_address') or 'Не указан'
        delivery = data.get('delivery') or data.get('delivery_type') or 'Не выбрана'
        total = data.get('order_total') or data.get('total') or 0

        # Получаем состав заказа
        items_list = data.get('items_text')
        if not items_list and 'items' in data:
            items = data.get('items', [])
            items_list = "\n".join(
                [f"▫️ {i.get('title')} ({i.get('size') or i.get('selSize') or '-'}) — {i.get('price')} ₽"
                 for i in items]
            )

        if not items_list:
            items_list = "Состав не указан"

        # 1. Сохраняем в Firebase для админки
        if db_fs:
            try:
                order_entry = {
                    **data,
                    'status': 'Новый',
                    'user': {'id': user_id},
                    'createdAt': firestore.SERVER_TIMESTAMP
                }
                db_fs.collection("orders").add(order_entry)
                print(f"💾 Заказ #{order_id} сохранен в Firebase")
            except Exception as e:
                logging.error(f"Ошибка сохранения заказа в Firebase: {e}")

        # 2. Формируем сообщение для админа
        admin_message = (
            f"🛍 <b>НОВЫЙ ЗАКАЗ #{order_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Клиент:</b> {name}\n"
            f"📞 <b>Телефон:</b> <code>{phone}</code>\n"
            f"🚚 <b>Доставка:</b> {delivery}\n"
            f"📍 <b>Адрес:</b> {address}\n\n"
            f"📋 <b>СОСТАВ ЗАКАЗА:</b>\n{items_list}\n\n"
            f"💰 <b>ИТОГО: {total} ₽</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 <a href='tg://user?id={user_id}'>Связаться с клиентом</a>"
        )

        # Отправляем сообщение админу
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_message,
            parse_mode='HTML',
            disable_web_page_preview=True
        )

        await update.message.reply_text(f"✅ Заказ #{order_id} принят!")

    except json.JSONDecodeError as e:
        logging.error(f"Ошибка парсинга JSON: {e}")
        await update.message.reply_text("❌ Ошибка формата данных")
    except Exception as e:
        logging.error(f"❌ Ошибка в web_app_data: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке заказа")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[KeyboardButton("🛍 Открыть Магазин", web_app=WebAppInfo(url=WEBAPP_URL))]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    # В локальном режиме показываем URL для отладки
    if IS_LOCAL:
        await update.message.reply_text(
            f"👋 Добро пожаловать в магазин BRATZ!\n\n"
            f"⚠️ ЛОКАЛЬНЫЙ РЕЖИМ\n"
            f"🔗 Откройте сайт в браузере:\n"
            f"http://localhost:{PORT}\n\n"
            f"Или нажмите кнопку ниже, чтобы открыть в Telegram WebApp.",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "Добро пожаловать в магазин BRATZ! 👋\n"
            "Нажмите кнопку ниже, чтобы открыть каталог.",
            reply_markup=reply_markup
        )


# === ОСНОВНАЯ ФУНКЦИЯ ===
def main():
    global application

    print("\n" + "=" * 50)
    print("🚀 ЗАПУСК БОТА BRATZ")
    print("=" * 50)

    # 1. Инициализируем Firebase
    if not init_firebase():
        print("⚠️ Продолжаем без Firebase (заказы не будут сохраняться)")

    # 2. Запускаем HTTP сервер в отдельном потоке
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    # 3. Создаем и настраиваем бота
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data))

    # 4. Запускаем слушатель Firebase (если Firebase работает)
    if db_fs:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        listener_thread = threading.Thread(
            target=setup_firebase_listener,
            args=(loop, application),
            daemon=True
        )
        listener_thread.start()
        print("👂 Firebase слушатель запущен")

    print("\n" + "=" * 50)
    print("✅ БОТ ГОТОВ К РАБОТЕ!")
    if IS_LOCAL:
        print(f"📂 Локальный режим: http://localhost:{PORT}")
        print(f"⚠️ Не забудьте переключить .env на production для сервера")
    else:
        print(f"🌍 Продакшн режим: {WEBAPP_URL}")
    print("=" * 50 + "\n")

    # 5. Запускаем бота (блокирующий вызов)
    application.run_polling()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        sys.exit(1)