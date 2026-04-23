import asyncio
import logging
import os
import aiohttp
import base64
from groq import AsyncGroq
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

user_histories: dict[int, list] = {}
user_modes: dict[int, str] = {}
user_names: dict[int, str] = {}
MAX_HISTORY = 20

# ── ХАРАКТЕР LUNAAI ───────────────────────────────────────────────────────────

BASE_CHARACTER = """Ты — Luna, умный и харизматичный ИИ-ассистент от команды Luna.

Твой характер:
- Ты дружелюбная, умная и немного с юмором
- Называешь себя Luna (не ChatGPT, не ИИ, не ассистент)
- Если спросят кто ты — говоришь что ты Luna, созданная командой Luna
- Используешь эмодзи уместно но не переусердствуешь
- Отвечаешь на языке пользователя
- Помнишь имя пользователя если он его назвал
- Иногда добавляешь фирменную фразу в конце: "С уважением, Luna 🌙"
- Никогда не говоришь что ты Groq, LLaMA или OpenAI

Ты часть экосистемы Luna — бесплатных Telegram ботов для людей."""

MODES = {
    "default": {
        "name": "🌙 Luna",
        "emoji": "🌙",
        "prompt": BASE_CHARACTER + "\nОбщайся естественно и помогай с любыми вопросами.",
        "desc": "Умная и дружелюбная Luna"
    },
    "coder": {
        "name": "💻 Программист",
        "emoji": "💻",
        "prompt": BASE_CHARACTER + "\nСейчас ты в режиме программиста. Ты эксперт в Python, JavaScript, и других языках. Пиши чистый код с комментариями. Объясняй каждый шаг.",
        "desc": "Помощь с кодом и программированием"
    },
    "psychologist": {
        "name": "🧠 Психолог",
        "emoji": "🧠",
        "prompt": BASE_CHARACTER + "\nСейчас ты в режиме психолога. Ты поддерживающий, эмпатичный слушатель. Помогаешь разобраться в чувствах, даёшь советы по управлению стрессом и тревогой. Никогда не осуждаешь. Задаёшь уточняющие вопросы.",
        "desc": "Поддержка и психологическая помощь"
    },
    "business": {
        "name": "💼 Бизнес",
        "emoji": "💼",
        "prompt": BASE_CHARACTER + "\nСейчас ты в режиме бизнес-советника. Ты эксперт в маркетинге, продажах, стартапах и предпринимательстве. Даёшь конкретные практичные советы. Мыслишь как успешный предприниматель.",
        "desc": "Советы по бизнесу и маркетингу"
    },
    "creative": {
        "name": "🎨 Творчество",
        "emoji": "🎨",
        "prompt": BASE_CHARACTER + "\nСейчас ты в творческом режиме. Ты талантливый писатель, поэт и генератор идей. Пишешь красиво, образно, с душой. Помогаешь с текстами, постами, историями, стихами.",
        "desc": "Тексты, посты, идеи, истории"
    },
    "teacher": {
        "name": "📚 Учитель",
        "emoji": "📚",
        "prompt": BASE_CHARACTER + "\nСейчас ты в режиме учителя. Объясняешь любые темы просто и понятно, с примерами и аналогиями. Терпелив, не осуждаешь за незнание. Проверяешь понимание вопросами.",
        "desc": "Объяснения и обучение"
    },
    "friend": {
        "name": "😊 Друг",
        "emoji": "😊",
        "prompt": BASE_CHARACTER + "\nСейчас ты в режиме лучшего друга. Общайся неформально, с юмором, на ты. Поддерживай, шути, болтай о жизни. Как будто переписываешься с близким другом.",
        "desc": "Неформальное общение и поддержка"
    },
    "translator": {
        "name": "🌍 Переводчик",
        "emoji": "🌍",
        "prompt": BASE_CHARACTER + "\nСейчас ты в режиме переводчика. Переводишь текст на любой язык. Если язык не указан — переводишь на русский. Также объясняешь культурные нюансы если нужно.",
        "desc": "Перевод на любой язык"
    },
}

WELCOME = (
    "🌙 <b>Привет! Я Luna — твой умный ИИ-ассистент!</b>\n\n"
    "Я умею:\n"
    "💬 Отвечать на любые вопросы\n"
    "🎨 Генерировать картинки по описанию\n"
    "💻 Помогать с кодом\n"
    "🧠 Поддерживать и советовать\n"
    "✍️ Писать тексты и посты\n"
    "🌍 Переводить\n\n"
    "Просто напиши мне что-нибудь!\n\n"
    "Команды:\n"
    "/mode — сменить режим работы\n"
    "/img описание — создать картинку\n"
    "/clear — очистить историю\n"
    "/help — помощь\n\n"
    "🌙 @LunaaHubb"
)


def get_mode_kb(current: str = "default") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, mode in MODES.items():
        check = "✓ " if key == current else ""
        b.row(InlineKeyboardButton(
            text=f"{check}{mode['name']} — {mode['desc']}",
            callback_data=f"mode_{key}"
        ))
    return b.as_markup()


async def ask_groq(uid: int, user_message: str) -> str:
    if uid not in user_histories:
        user_histories[uid] = []

    mode_key = user_modes.get(uid, "default")
    system = MODES[mode_key]["prompt"]

    # Добавляем имя пользователя в контекст
    name = user_names.get(uid, "")
    if name:
        system += f"\nИмя пользователя: {name}. Обращайся по имени иногда."

    user_histories[uid].append({"role": "user", "content": user_message})
    if len(user_histories[uid]) > MAX_HISTORY:
        user_histories[uid] = user_histories[uid][-MAX_HISTORY:]

    messages = [{"role": "system", "content": system}] + user_histories[uid]

    response = await groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=1024,
        temperature=0.8,
    )

    reply = response.choices[0].message.content
    user_histories[uid].append({"role": "assistant", "content": reply})
    return reply


async def generate_image(prompt: str) -> bytes | None:
    url = "https://api.together.xyz/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {TOGETHER_API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "model": "black-forest-labs/FLUX.1-schnell-Free",
        "prompt": prompt,
        "width": 1024,
        "height": 1024,
        "steps": 4,
        "n": 1,
        "response_format": "b64_json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                logging.error(f"Image API error: {resp.status} {await resp.text()}")
                return None
            result = await resp.json()
            b64 = result["data"][0]["b64_json"]
            return base64.b64decode(b64)


@dp.message(CommandStart())
async def cmd_start(msg: Message):
    uid = msg.from_user.id
    name = msg.from_user.first_name or ""
    user_names[uid] = name
    await msg.answer(WELCOME, parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "🌙 <b>Luna — помощь</b>\n\n"
        "💬 Просто напиши сообщение — отвечу!\n\n"
        "/mode — выбрать режим работы\n"
        "/img &lt;описание&gt; — создать картинку\n"
        "Пример: <code>/img закат над горами в стиле аниме</code>\n\n"
        "/clear — очистить историю разговора\n\n"
        "🎨 <b>Режимы:</b>\n" +
        "\n".join(f"{m['emoji']} {m['name']} — {m['desc']}" for m in MODES.values()),
        parse_mode="HTML"
    )


@dp.message(Command("clear"))
async def cmd_clear(msg: Message):
    user_histories[msg.from_user.id] = []
    await msg.answer("🗑️ История очищена! Начнём заново 🌙")


@dp.message(Command("mode"))
async def cmd_mode(msg: Message):
    uid = msg.from_user.id
    current = user_modes.get(uid, "default")
    await msg.answer(
        "🎭 <b>Выбери режим работы</b>\n\nКаждый режим — это другой характер и специализация:",
        parse_mode="HTML",
        reply_markup=get_mode_kb(current)
    )


@dp.callback_query(F.data.startswith("mode_"))
async def cb_mode(call: CallbackQuery):
    mode_key = call.data[5:]
    if mode_key not in MODES:
        await call.answer("Режим не найден!")
        return
    uid = call.from_user.id
    user_modes[uid] = mode_key
    user_histories[uid] = []
    mode = MODES[mode_key]
    await call.message.edit_text(
        f"{mode['emoji']} <b>Режим: {mode['name']}</b>\n\n"
        f"{mode['desc']}\n\n"
        f"История очищена. Поехали! 🌙",
        parse_mode="HTML"
    )
    await call.answer(f"✅ {mode['name']}")


@dp.message(Command("img"))
async def cmd_img(msg: Message):
    args = msg.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await msg.answer(
            "🎨 Напиши описание картинки:\n\n"
            "<code>/img закат над горами</code>\n"
            "<code>/img космический корабль в стиле аниме</code>\n"
            "<code>/img портрет девушки акварель</code>",
            parse_mode="HTML"
        )
        return

    prompt = args[1].strip()
    wait = await msg.answer("🎨 Генерирую картинку... Это займёт 10-20 секунд ⏳")

    try:
        image_bytes = await generate_image(prompt)
        if not image_bytes:
            await wait.edit_text("❌ Не удалось создать картинку. Попробуй другое описание.")
            return

        await wait.delete()
        await msg.answer_photo(
            BufferedInputFile(image_bytes, filename="luna_art.jpg"),
            caption=f"🎨 <b>{prompt}</b>\n\n🌙 Создано Luna AI",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Image error: {e}")
        await wait.edit_text("❌ Ошибка при генерации. Попробуй позже.")


@dp.message(F.text)
async def handle_message(msg: Message):
    text = msg.text.strip()
    uid = msg.from_user.id

    # Сохраняем имя
    if msg.from_user.first_name:
        user_names[uid] = msg.from_user.first_name

    # Если просит картинку без команды
    lower = text.lower()
    if any(w in lower for w in ["нарисуй", "создай картинку", "сгенерируй картинку", "draw", "generate image"]):
        wait = await msg.answer("🎨 Генерирую картинку... ⏳")
        try:
            image_bytes = await generate_image(text)
            if image_bytes:
                await wait.delete()
                await msg.answer_photo(
                    BufferedInputFile(image_bytes, filename="luna_art.jpg"),
                    caption=f"🎨 <b>{text[:100]}</b>\n\n🌙 Создано Luna AI",
                    parse_mode="HTML"
                )
                return
        except Exception as e:
            logging.error(f"Auto image error: {e}")
        await wait.delete()

    wait = await msg.answer("🌙 Думаю...")

    try:
        reply = await ask_groq(uid, text)

        if len(reply) > 4000:
            parts = [reply[i:i+4000] for i in range(0, len(reply), 4000)]
            await wait.delete()
            for part in parts:
                await msg.answer(part)
        else:
            await wait.edit_text(reply)

    except Exception as e:
        logging.error(f"Groq error: {e}")
        await wait.edit_text("❌ Что-то пошло не так. Попробуй ещё раз или /clear")


async def main():
    logging.info("LunaAI запущен!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
