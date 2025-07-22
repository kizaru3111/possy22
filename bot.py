import os
import sys
import json
import mysql.connector
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timedelta
import secrets
import asyncio
import aiohttp
import logging
import tempfile
import pdfplumber
import io
import re
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Конфигурация из .env ---
API_TOKEN = os.getenv('API_TOKEN')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS').split(',')))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
SUSPICIOUS_RECEIPT_LIMIT = int(os.getenv('SUSPICIOUS_RECEIPT_LIMIT', 3))
MAX_PDF_SIZE = int(os.getenv('MAX_PDF_SIZE', 3 * 1024 * 1024))  # 3MB по умолчанию
RECEIPT_MAX_AGE_MINUTES = int(os.getenv('RECEIPT_MAX_AGE_MINUTES', 25))

# Настройки оплаты
PAYMENT_DETAILS = {
    "card_number": os.getenv('CARD_NUMBER'),
    "card_name": os.getenv('CARD_NAME'),
    "recipient_name": os.getenv('RECIPIENT_NAME'),
    "tariff_prices": {
        "2 минуты": 0,
        "1 час": 490,
        "2 часа": 790
    }
}

TARIFFS = {
    "2 минуты": 2,
    "1 час": 60,
    "2 часа": 120
}

# Database Configuration
MYSQL_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME'),
    'pool_name': 'bot_pool',
    'pool_size': 5,
    'connect_timeout': 5
}

# Инициализация бота
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Кэш и состояния
user_cache = {}
user_tariffs = {}
extend_sessions = {}
menu_hint_shown = set()

# --- Классы состояний ---
class AdminStates(StatesGroup):
    waiting_password = State()
    main_menu = State()
    change_card = State()
    change_name = State()
    change_tariff = State()
    block_user = State()
    unblock_user = State()
    view_stats = State()

# --- Вспомогательные функции ---
def get_db():
    """Возвращает соединение с базой данных"""
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {e}")
        return None

async def execute_db(query: str, params=None, fetch_one=False, fetch_all=False, commit=False):
    """Выполняет SQL запрос с обработкой ошибок"""
    conn = None
    cursor = None
    try:
        conn = get_db()
        if not conn:
            return None

        cursor = conn.cursor(dictionary=True, buffered=True)  # Используем буферизованный курсор
        cursor.execute(query, params or ())

        if commit:
            conn.commit()
            result = True
        elif fetch_one:
            result = cursor.fetchone()
        elif fetch_all:
            result = cursor.fetchall()
        else:
            result = True

        return result
    except Exception as e:
        logger.error(f"Ошибка выполнения запроса: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            try:
                # Убеждаемся, что все результаты получены
                while conn.unread_result:
                    cursor = conn.cursor(dictionary=True, buffered=True)
                    cursor.fetchall()
                    cursor.close()
            except Exception:
                pass
            finally:
                conn.close()

async def notify_website(user_id: int, session_id: str):
    """Уведомляет сайт об обновлении сессии"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                'http://localhost:5000/api/session_updated',
                json={'user_id': user_id, 'session_id': session_id},
                timeout=2
            ):
                pass
    except Exception as e:
        logger.error(f"Ошибка уведомления сайта: {e}")

# --- Функции для работы с чеками ---
def extract_text_from_pdf_sync(file_bytes: bytes) -> str:
    """Синхронная версия извлечения текста из PDF"""
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text_parts = []
            for page in pdf.pages:
                if text := page.extract_text():
                    text_parts.append(text)
            return "\n".join(text_parts)
    except Exception as e:
        logger.error(f"Ошибка извлечения текста из PDF: {e}")
        return ""

async def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Асинхронная обертка для извлечения текста"""
    return await asyncio.to_thread(extract_text_from_pdf_sync, file_bytes)

async def validate_receipt(filename: str, text: str, tariff: str) -> tuple[bool, str]:
    """Проверяет чек на соответствие требованиям и возвращает (результат, причина)"""
    # 1. Проверка суммы
    price = PAYMENT_DETAILS["tariff_prices"][tariff]
    if price > 0:
        # Ищем сумму в тексте чека (учитываем разные форматы)
        price_str = str(price)
        if not any(pattern in text for pattern in [
            f"{price_str},00", f"{price_str}.00",
            f"{price_str} KZT", f"{price_str}₸",
            f"{price_str} тг", f"{price_str} тенге"
        ]):
            return False, f"❌ Сумма в чеке не соответствует тарифу ({price}₸)"

    # 2. Проверка получателя
    if PAYMENT_DETAILS["recipient_name"] not in text:
        return False, "❌ Получатель в чеке не соответствует указанному"

    # 3. Проверка времени
    receipt_time = None
    for pattern, dt_format in [
        (r"(\d{2})\.(\d{2})\.(\d{4})\s(\d{2}):(\d{2})", "%d.%m.%Y %H:%M"),
        (r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", "%Y-%m-%dT%H:%M")
    ]:
        if match := re.search(pattern, text):
            try:
                receipt_time = datetime.strptime(match.group(), dt_format)
                break
            except ValueError:
                continue

    if not receipt_time:
        return False, "❌ Не удалось определить время в чеке"

    if (datetime.now() - receipt_time) > timedelta(minutes=RECEIPT_MAX_AGE_MINUTES):
        return False, f"❌ Чек слишком старый (более {RECEIPT_MAX_AGE_MINUTES} минут)"

    # 4. Проверка номера транзакции
    # Ищем номер транзакции из имени файла в тексте чека
    transaction_numbers = re.findall(r'\d+', filename)
    if not transaction_numbers:
        return False, "❌ Не удалось найти номер транзакции в чеке"

    # Берем самое длинное число из имени файла как номер транзакции
    transaction_id = max(transaction_numbers, key=len)

    # Проверяем, что этот номер есть в тексте чека
    if transaction_id not in text:
        return False, "❌ Номер транзакции из имени файла не найден в чеке"

    if await execute_db("SELECT 1 FROM receipt_transactions WHERE receipt_id = %s", (transaction_id,), fetch_one=True):
        return False, "❌ Этот чек уже был использован"

    return True, "✅ Чек успешно прошел проверку"

async def log_suspicious_receipt(user_id: int, username: str, file_name: str) -> int:
    """Логирует подозрительный чек"""
    await execute_db(
        "INSERT INTO suspicious_receipts (user_id, username, file_name) "
        "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE receipt_count = receipt_count + 1",
        (user_id, username, file_name),
        commit=True
    )
    result = await execute_db(
        "SELECT receipt_count FROM suspicious_receipts WHERE user_id = %s AND file_name = %s",
        (user_id, file_name),
        fetch_one=True
    )
    return result['receipt_count'] if result else 1

async def log_transaction(transaction_id: str, user_id: int):
    """Логирует успешную транзакцию"""
    await execute_db(
        "INSERT INTO receipt_transactions (receipt_id, user_id) "
        "VALUES (%s, %s) ON DUPLICATE KEY UPDATE used_at = NOW()",
        (transaction_id, user_id),
        commit=True
    )

async def is_user_blocked(user_id: int) -> bool:
    """Проверяет заблокирован ли пользователь"""
    result = await execute_db(
        "SELECT 1 FROM suspicious_receipts WHERE user_id = %s AND is_blocked = TRUE LIMIT 1",
        (user_id,),
        fetch_one=True
    )
    return result is not None

async def is_new_user(user_id: int) -> bool:
    """Проверяет новый ли пользователь"""
    result = await execute_db(
        "SELECT 1 FROM codes WHERE user_id = %s LIMIT 1",
        (user_id,),
        fetch_one=True
    )
    return result is None

# --- Логирование действий ---
async def log_admin_action(user_id: int, action: str):
    """Логирует действие администратора"""
    await execute_db(
        "INSERT INTO admin_logs (admin_id, action) VALUES (%s, %s)",
        (user_id, action),
        commit=True
    )

async def log_user_activity(user_id: int, action: str):
    """Логирует действия пользователей"""
    await execute_db(
        "INSERT INTO user_activity (user_id, action) VALUES (%s, %s)",
        (user_id, action),
        commit=True
    )

async def update_user_stats(user_id: int):
    """Обновляет статистику пользователя"""
    await execute_db(
        "INSERT INTO user_stats (user_id, first_seen, last_seen) "
        "VALUES (%s, NOW(), NOW()) ON DUPLICATE KEY UPDATE last_seen = NOW()",
        (user_id,),
        commit=True
    )

async def log_payment(user_id: int, amount: int, tariff: str):
    """Логирует платеж"""
    await execute_db(
        "INSERT INTO payments (user_id, amount, tariff) VALUES (%s, %s, %s)",
        (user_id, amount, tariff),
        commit=True
    )

# --- Клавиатуры ---
def get_main_keyboard():
    """Основная клавиатура с меню и сессиями"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💎 Выбрать тариф")],
            [KeyboardButton(text="🕒 Мои сессии")],
            [KeyboardButton(text="📋 Меню")]
        ],
        resize_keyboard=True
    )

async def get_tariff_keyboard(user_id: int):
    """Клавиатура с тарифами"""
    builder = InlineKeyboardBuilder()

    if await is_new_user(user_id):
        builder.button(text="2 минуты (бесплатно)", callback_data="tariff_2 минуты")

    builder.button(text="1 час - 490₸", callback_data="tariff_1 час")
    builder.button(text="2 часа - 790₸", callback_data="tariff_2 часа")

    builder.adjust(1)
    return builder.as_markup()

# --- Основные обработчики ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    if await is_user_blocked(user_id):
        await message.answer("⛔ Ваш доступ к боту заблокирован")
        return

    await update_user_stats(user_id)
    await log_user_activity(user_id, "start")

    welcome_text = """
🔥 Добро пожаловать в наш бот!

[Здесь будет ваше описание товара/услуги]
- Пункт 1
- Пункт 2
- Пункт 3

Нажмите кнопку ниже, чтобы выбрать тариф.
"""

    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.message(F.text == "💎 Выбрать тариф")
async def show_tariffs(message: Message):
    """Показывает доступные тарифы"""
    keyboard = await get_tariff_keyboard(message.from_user.id)
    await message.answer("Выберите подходящий тариф:", reply_markup=keyboard)

@dp.message(F.text == "📋 Меню")
async def handle_menu_button(message: Message):
    """Обработчик кнопки меню"""
    await show_main_menu(message)

async def show_main_menu(message: Message):
    """Показывает главное меню"""
    user_id = message.from_user.id
    if user_id in ADMIN_IDS:
        await show_admin_menu(message)
    else:
        keyboard = await get_tariff_keyboard(user_id)
        await message.answer("Выберите тариф для доступа:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("tariff_"))
async def process_tariff(callback: CallbackQuery):
    """Обработчик выбора тарифа"""
    await callback.answer()
    tariff = callback.data.replace('tariff_', '')
    user_tariffs[callback.from_user.id] = tariff

    if tariff == "2 минуты":
        code, session_id = await generate_code(callback.from_user.id, tariff)
        await callback.message.edit_text(
            f"✅ Ваш бесплатный код доступа: <code>{code}</code>\n"
            f"Тариф: {tariff}\n"
            f"Срок действия: {TARIFFS[tariff]} минут",
            parse_mode="HTML"
        )
        await notify_website(callback.from_user.id, session_id)
        return

    payment_text = (
        f"Тариф: {tariff}\n"
        f"Сумма к оплате: {PAYMENT_DETAILS['tariff_prices'][tariff]}₸\n\n"
        f"Реквизиты для оплаты:\n"
        f"Карта: {PAYMENT_DETAILS['card_number']}\n"
        f"Получатель: {PAYMENT_DETAILS['card_name']}\n\n"
        "После оплаты отправьте чек (PDF) и нажмите /checkpayment"
    )

    await callback.message.edit_text(payment_text, reply_markup=None)

@dp.message(F.document)
async def handle_payment_proof(message: Message):
    """Обработчик чеков"""
    user_id = message.from_user.id
    username = message.from_user.username or "нет_username"

    if await is_user_blocked(user_id):
        await message.answer("⛔ Ваш доступ к боту заблокирован")
        return

    if user_id not in user_tariffs:
        await message.answer("❌ Сначала выберите тариф через меню", reply_markup=get_main_keyboard())
        return

    try:
        if message.document.mime_type != "application/pdf":
            await message.answer("❌ Отправьте чек в формате PDF", reply_markup=get_main_keyboard())
            return

        if message.document.file_size > MAX_PDF_SIZE:
            await message.answer(f"❌ Файл слишком большой. Максимум: {MAX_PDF_SIZE//1024//1024}MB")
            return

        processing_msg = await message.answer("⏳ Обрабатываю чек...")

        file = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(file.file_path)
        text = await extract_text_from_pdf(file_bytes.read())

        is_valid, reason = await validate_receipt(message.document.file_name, text, user_tariffs[user_id])
        if is_valid:
            # Извлекаем самое длинное число из имени файла как номер транзакции
            transaction_id = max(re.findall(r'\d+', message.document.file_name), key=len)
            await log_transaction(transaction_id, user_id)
            await message.answer("✅ Чек принят! Нажмите /checkpayment для кода")
        else:
            count = await log_suspicious_receipt(user_id, username, message.document.file_name)
            if count >= SUSPICIOUS_RECEIPT_LIMIT:
                await message.answer("⛔ Вы заблокированы за подозрительные чеки")
            else:
                error_text = f"{reason}\n\nПожалуйста, убедитесь что:\n"
                error_text += "1. Чек в формате PDF\n"
                error_text += f"2. Сумма соответствует выбранному тарифу ({PAYMENT_DETAILS['tariff_prices'][user_tariffs[user_id]]}₸)\n"
                error_text += f"3. Получатель: {PAYMENT_DETAILS['recipient_name']}\n"
                error_text += f"4. Чек не старше {RECEIPT_MAX_AGE_MINUTES} минут"
                await message.answer(error_text, reply_markup=get_main_keyboard())

        await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)

    except Exception as e:
        logger.error(f"Ошибка обработки чека: {e}")
        await message.answer("⚠️ Ошибка обработки чека. Попробуйте снова")

@dp.message(Command("checkpayment"))
async def cmd_check_payment(message: Message):
    """Обработчик проверки платежа"""
    user_id = message.from_user.id
    if user_id not in user_tariffs:
        await message.answer("❌ Сначала выберите тариф через меню", reply_markup=get_main_keyboard())
        return

    tariff = user_tariffs[user_id]

    # Для бесплатного тарифа сразу выдаем код
    if tariff == "2 минуты":
        code, session_id = await generate_code(user_id, tariff)
        await send_code_message(message, code, tariff)
        return

    # Для платных тарифов проверяем наличие подтвержденного чека
    has_valid_receipt = await execute_db(
        "SELECT 1 FROM receipt_transactions WHERE user_id = %s AND used_at > DATE_SUB(NOW(), INTERVAL 1 HOUR)",
        (user_id,),
        fetch_one=True
    )

    if not has_valid_receipt:
        await message.answer("❌ Чек не найден или просрочен. Отправьте PDF-чек.")
        return

    code, session_id = await generate_code(user_id, tariff)
    amount = PAYMENT_DETAILS["tariff_prices"][tariff]

    if amount > 0:
        await log_payment(user_id, amount, tariff)

    await message.answer(
        f"✅ Ваш код доступа: <code>{code}</code>\n"
        f"Тариф: {tariff}\n"
        f"Срок действия: {TARIFFS[tariff]} минут",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

    user_tariffs.pop(user_id, None)
    await notify_website(user_id, session_id)

async def send_code_message(message: Message, code: str, tariff: str):
    """Отправляет сообщение с кодом доступа"""
    await message.answer(
        f"✅ Ваш код доступа: <code>{code}</code>\n"
        f"Тариф: {tariff}\n"
        f"Срок действия: {TARIFFS[tariff]} минут",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "🕒 Мои сессии")
async def handle_my_sessions(message: Message):
    """Обработчик кнопки моих сессий"""
    user_id = message.from_user.id
    sessions = await execute_db(
        """SELECT 
            code, 
            CASE 
                WHEN expires_at > NOW() THEN 'active'
                ELSE 'expired'
            END as status,
            expires_at,
            duration_minutes
        FROM codes 
        WHERE user_id = %s 
        ORDER BY expires_at DESC""",
        (user_id,),
        fetch_all=True
    )

    if not sessions:
        await message.answer("У вас ещё нет сессий.", reply_markup=get_main_keyboard())
        return

    response = ["Ваши сессии:\n"]
    for session in sessions:
        emoji = "🟢" if session['status'] == 'active' else "🔴"
        response.append(
            f"{emoji} {session['duration_minutes']} мин. "
            f"(до {session['expires_at'].strftime('%d.%m.%Y %H:%M')})"
            f"{' - код: ' + session['code'] if session['status'] == 'active' else ''}"
        )

    await message.answer("\n".join(response), reply_markup=get_main_keyboard())

@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    """Показывает ID пользователя"""
    await message.answer(f"Ваш chat_id: {message.from_user.id}", reply_markup=get_main_keyboard())

# --- Админ-панель ---
@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    """Вход в админ-панель"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет прав администратора")
        return

    await log_admin_action(message.from_user.id, "Вход в админ-панель")
    await show_admin_menu(message)

async def show_admin_menu(message: Message):
    """Меню администратора"""
    markup = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="👥 Пользователи")],
            [KeyboardButton(text="🔒 Блокировки")],
            [KeyboardButton(text="💳 Реквизиты")],
            [KeyboardButton(text="📋 Главное меню")]
        ],
        resize_keyboard=True
    )

    await message.answer("👨‍💻 Панель администратора\nВыберите раздел:", reply_markup=markup)

@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    """Статистика бота"""
    if message.from_user.id not in ADMIN_IDS:
        return

    try:
        stats = await execute_db(
            "SELECT COUNT(*) as total_users, "
            "SUM(CASE WHEN DATE(first_seen) = CURDATE() THEN 1 ELSE 0 END) as today_users "
            "FROM user_stats",
            fetch_one=True
        )

        payments = await execute_db(
            "SELECT COUNT(*) as count, SUM(amount) as total FROM payments WHERE DATE(payment_date) = CURDATE()",
            fetch_one=True
        )

        response = (
            "📊 Статистика:\n"
            f"👥 Всего пользователей: {stats['total_users']}\n"
            f"🆕 Новых сегодня: {stats['today_users']}\n"
            f"💳 Платежей сегодня: {payments['count']} на {payments['total'] or 0}₸"
        )

        await message.answer(response)

    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await message.answer("⚠️ Ошибка получения статистики")

@dp.message(F.text == "📋 Главное меню")
async def admin_back_to_main(message: Message):
    """Возврат из админ-панели в главное меню"""
    await show_main_menu(message)

@dp.message(F.text == "👥 Пользователи")
async def admin_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    stats = await execute_db("SELECT COUNT(*) as total FROM user_stats", fetch_one=True)
    await message.answer(f"Всего пользователей: {stats['total'] if stats else 0}")

@dp.message(F.text == "🔒 Блокировки")
async def admin_blocks(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    blocked = await execute_db("SELECT COUNT(DISTINCT user_id) as blocked FROM suspicious_receipts WHERE is_blocked = TRUE", fetch_one=True)
    await message.answer(f"Заблокировано пользователей: {blocked['blocked'] if blocked else 0}")

@dp.message(F.text == "💳 Реквизиты")
async def admin_card_info(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        f"Текущие реквизиты:\n"
        f"Карта: {PAYMENT_DETAILS['card_number']}\n"
        f"Получатель: {PAYMENT_DETAILS['card_name']}\n"
        f"Имя для проверки: {PAYMENT_DETAILS['recipient_name']}"
    )

# --- Функции генерации кодов ---
async def generate_code(user_id: int, tariff: str) -> tuple[str, str]:
    """Генерирует код доступа"""
    session_id = secrets.token_hex(8)
    code = secrets.token_hex(4)
    duration = TARIFFS[tariff]

    await execute_db(
        "INSERT INTO codes (code, user_id, session_id, duration_minutes, expires_at) "
        "VALUES (%s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL %s MINUTE))",
        (code, user_id, session_id, duration, duration),
        commit=True
    )

    return code, session_id

# --- Запуск бота ---
async def main():
    """Основная функция запуска"""
    logger.info("Бот запускается...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())