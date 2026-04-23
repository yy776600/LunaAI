import asyncio
import logging
import os
from groq import AsyncGroq
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# История сообщений для каждого пользователя
user_histories: dict[int, list] = {}
MAX_HISTORY = 20

SYSTEM_PROMPT = """Ты — LunaAI, умный и дружелюбный ИИ-ассистент от команды Luna.
Ты помогаешь с любыми вопросами: объясняешь, пишешь код, помогаешь с текстами, отвечаешь на вопросы.
Отвечай на том языке на котором пишет пользователь.
Будь кратким но информативным. Используй эмодзи уместно.
Ты часть экосистемы Luna — набора бесплатных Telegram ботов."""

WELCOME = (
    "🤖 <b>LunaAI</b> — твой умный помощник!\n\n"
    "Я могу:\n"
    "💬 Отвечать на любые вопросы\n"
    "✍️ Писать тексты и посты\n"
    "💻 Помогать с кодом\n"
    "🌍 Переводить\n"
    "🧠 Объяснять сложные темы\n"
    "🎨 Генерировать идеи\n\n"
    "Просто напиши мне что-нибудь!\n\n"
    "/clear — очистить историю\n"
    "/mode — сменить режим\n\n"
    "🌙 @LunaaHubb"
)

MODES = {
    "default": {
        "name": "🤖 Обычный",
        "prompt": SYSTEM_PROMPT
    },
    "coder": {
        "name": "💻 Программист",
        "prompt": SYSTEM_PROMPT + "\nТы эксперт-программист. Пиши чистый код с комментариями. Предпочитай Python."
    },
    "writer": {
        "name": "✍️ Писатель",
        "prompt": SYSTEM_PROMPT + "\nТы талантливый писатель и копирайтер. Пиши красиво, живо и убедительно."
    },
    "teacher": {
        "name": "📚 Учитель",
        "prompt": SYSTEM_PROMPT + "\nТы терпеливый учитель. Объясняй просто, с примерами, пошагово."
    },
    "friend": {
        "name": "😊 Друг",
        "prompt": SYSTEM_PROMPT + "\nТы лучший друг — общайся неформально, с юмором, поддерживай."
    },
}

user_modes: dict[int, str] = {}


def get_mode_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, mode in MODES.items():
        b.row(InlineKeyboardButton(text=mode["name"], callback_data=f"mode_{key}"))
    return b.as_markup()


async def ask_groq(uid: int, user_message: str) -> str:
    if uid not in user_histories:
        user_histories[uid] = []

    mode_key = user_modes.get(uid, "default")
    system = MODES[mode_key]["prompt"]

    user_histories[uid].append({"role": "user", "content": user_message})

    # Ограничиваем историю
    if len(user_histories[uid]) > MAX_HISTORY:
        user_histories[uid] = user_histories[uid][-MAX_HISTORY:]

    messages = [{"role": "system", "content": system}] + user_histories[uid]

    response = await groq_client.chat.completions.create(
        model="llama3-70b-8192",
        messages=messages,
        max_tokens=1024,
        temperature=0.7,
    )

    reply = response.choices[0].message.content
    user_histories[uid].append({"role": "assistant", "content": reply})

    return reply


@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(WELCOME, parse_mode="HTML")


@dp.message(Command("clear"))
async def cmd_clear(msg: Message):
    uid = msg.from_user.id
    user_histories[uid] = []
    await msg.answer("🗑️ История очищена! Начнём с чистого листа.")


@dp.message(Command("mode"))
async def cmd_mode(msg: Message):
    uid = msg.from_user.id
    current = user_modes.get(uid, "default")
    current_name = MODES[current]["name"]
    await msg.answer(
        f"🎭 <b>Режим работы</b>\n\nТекущий: <b>{current_name}</b>\n\nВыбери режим:",
        parse_mode="HTML",
        reply_markup=get_mode_kb()
    )


@dp.callback_query(F.data.startswith("mode_"))
async def cb_mode(call: CallbackQuery):
    mode_key = call.data[5:]
    if mode_key not in MODES:
        await call.answer("Режим не найден!")
        return
    uid = call.from_user.id
    user_modes[uid] = mode_key
    user_histories[uid] = []  # сбрасываем историю при смене режима
    mode_name = MODES[mode_key]["name"]
    await call.message.edit_text(
        f"✅ Режим изменён на <b>{mode_name}</b>\n\nИстория очищена. Начнём!",
        parse_mode="HTML"
    )
    await call.answer()


@dp.message(F.text | F.caption)
async def handle_message(msg: Message):
    text = msg.text or msg.caption
    if not text:
        return

    uid = msg.from_user.id
    wait = await msg.answer("🤔 Думаю...")

    try:
        reply = await ask_groq(uid, text)

        # Разбиваем длинные сообщения
        if len(reply) > 4000:
            parts = [reply[i:i+4000] for i in range(0, len(reply), 4000)]
            await wait.delete()
            for part in parts:
                await msg.answer(part)
        else:
            await wait.edit_text(reply)

    except Exception as e:
        logging.error(f"Groq error: {e}")
        await wait.edit_text("❌ Ошибка. Попробуй ещё раз или напиши /clear")


async def main():
    logging.info("LunaAI запущен!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
