import asyncio
import logging
import os
import json
import aiohttp
import aiosqlite
import urllib.parse
import google.generativeai as genai
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "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")
DB = "lunaai.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


MAX_HISTORY = 20

# ── БД ────────────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                mode TEXT DEFAULT 'default',
                memory TEXT DEFAULT '{}',
                created_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            );
        """)
        await db.commit()


async def get_user(uid: int, name: str = "") -> dict:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as c:
            row = await c.fetchone()
        if not row:
            await db.execute("INSERT INTO users (user_id, name) VALUES (?,?)", (uid, name))
            await db.commit()
            async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as c:
                row = await c.fetchone()
        return dict(row)


async def update_user(uid: int, **kw):
    if 'memory' in kw and isinstance(kw['memory'], dict):
        kw['memory'] = json.dumps(kw['memory'], ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in kw)
    vals = list(kw.values()) + [uid]
    async with aiosqlite.connect(DB) as db:
        await db.execute(f"UPDATE users SET {sets} WHERE user_id=?", vals)
        await db.commit()


async def get_history(uid: int) -> list:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content FROM history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (uid, MAX_HISTORY)
        ) as c:
            rows = await c.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def add_to_history(uid: int, role: str, content: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO history (user_id, role, content) VALUES (?,?,?)",
            (uid, role, content)
        )
        # Оставляем только последние MAX_HISTORY сообщений
        await db.execute("""
            DELETE FROM history WHERE user_id=? AND id NOT IN (
                SELECT id FROM history WHERE user_id=? ORDER BY created_at DESC LIMIT ?
            )
        """, (uid, uid, MAX_HISTORY))
        await db.commit()


async def clear_history(uid: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM history WHERE user_id=?", (uid,))
        await db.commit()


# ── ПОИСК В ИНТЕРНЕТЕ ─────────────────────────────────────────────────────────

async def web_search(query: str) -> str:
    url = "https://api.tavily.com/search"
    data = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": 3,
        "include_answer": True,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return ""
            result = await resp.json()
    answer = result.get("answer", "")
    results = result.get("results", [])
    text = f"Результат поиска: {answer}\n\n"
    for r in results[:3]:
        text += f"• {r.get('title', '')}: {r.get('content', '')[:300]}\n"
    return text


def needs_search(text: str) -> bool:
    keywords = [
        "сейчас", "сегодня", "новости", "актуально", "последние", "2024", "2025", "2026",
        "курс", "цена", "погода", "когда", "где", "кто такой", "что случилось",
        "latest", "news", "current", "today", "price", "weather"
    ]
    lower = text.lower()
    return any(kw in lower for kw in keywords)


# ── ХАРАКТЕР LUNA ─────────────────────────────────────────────────────────────

BASE_CHARACTER = """Ты — Luna, умный и харизматичный ИИ-ассистент от команды Luna.

Твой характер:
- Ты дружелюбная, умная и немного с юмором
- Называешь себя Luna (не ChatGPT, не ИИ, не ассистент)
- Используешь эмодзи уместно
- Отвечаешь на языке пользователя
- Иногда добавляешь в конце: "🌙 Luna"
- Никогда не говоришь что ты Groq, LLaMA или OpenAI
- Ты часть экосистемы Luna — бесплатных Telegram ботов"""

MODES = {
    "default": {"name": "🌙 Luna", "emoji": "🌙", "desc": "Умная и дружелюбная",
        "prompt": BASE_CHARACTER + "\nОбщайся естественно и помогай с любыми вопросами."},
    "coder": {"name": "💻 Программист", "emoji": "💻", "desc": "Помощь с кодом",
        "prompt": BASE_CHARACTER + "\nТы эксперт-программист. Пиши чистый код с комментариями."},
    "psychologist": {"name": "🧠 Психолог", "emoji": "🧠", "desc": "Поддержка и советы",
        "prompt": BASE_CHARACTER + "\nТы поддерживающий психолог. Слушай, задавай вопросы, не осуждай."},
    "business": {"name": "💼 Бизнес", "emoji": "💼", "desc": "Советы по бизнесу",
        "prompt": BASE_CHARACTER + "\nТы бизнес-советник. Давай конкретные практичные советы."},
    "creative": {"name": "🎨 Творчество", "emoji": "🎨", "desc": "Тексты и идеи",
        "prompt": BASE_CHARACTER + "\nТы творческий писатель. Пиши красиво и образно."},
    "teacher": {"name": "📚 Учитель", "emoji": "📚", "desc": "Объяснения и обучение",
        "prompt": BASE_CHARACTER + "\nТы терпеливый учитель. Объясняй просто, с примерами."},
    "friend": {"name": "😊 Друг", "emoji": "😊", "desc": "Неформальное общение",
        "prompt": BASE_CHARACTER + "\nТы лучший друг. Общайся неформально, с юмором, на ты."},
    "translator": {"name": "🌍 Переводчик", "emoji": "🌍", "desc": "Перевод на любой язык",
        "prompt": BASE_CHARACTER + "\nТы переводчик. Переводи на указанный язык, без лишних слов."},
}

WELCOME = (
    "🌙 <b>Привет! Я Luna — твой умный ИИ-ассистент!</b>\n\n"
    "Что я умею:\n"
    "💬 Отвечать на любые вопросы\n"
    "🔍 Искать актуальную информацию в интернете\n"
    "🎨 Генерировать картинки\n"
    "💻 Помогать с кодом\n"
    "🧠 Поддерживать и советовать\n"
    "📄 Читать документы и файлы\n"
    "🎤 Расшифровывать голосовые\n"
    "🌍 Переводить\n\n"
    "Просто напиши мне что-нибудь!\n\n"
    "/mode — сменить режим\n"
    "/img описание — создать картинку\n"
    "/memory — моя память о тебе\n"
    "/clear — очистить историю\n\n"
    "🌙 @LunaaHubb"
)


def get_mode_kb(current: str = "default") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, mode in MODES.items():
        check = "✓ " if key == current else ""
        b.row(InlineKeyboardButton(
            text=f"{check}{mode['emoji']} {mode['name']} — {mode['desc']}",
            callback_data=f"mode_{key}"
        ))
    return b.as_markup()


async def extract_memory(uid: int, user_msg: str, ai_reply: str):
    """Извлекаем важные факты о пользователе и сохраняем."""
    user = await get_user(uid)
    try:
        memory = json.loads(user.get('memory', '{}'))
    except:
        memory = {}

    # Простое извлечение фактов из сообщения
    lower = user_msg.lower()
    if "меня зовут" in lower or "я " in lower:
        for word in user_msg.split():
            if len(word) > 2 and word[0].isupper():
                memory['name'] = word
                break
    if any(c in lower for c in ["город", "живу", "из "]):
        memory['mentioned_location'] = True
    if "работаю" in lower or "работа" in lower:
        memory['mentioned_work'] = True

    await update_user(uid, memory=memory)


async def ask_luna(uid: int, user_message: str, extra_context: str = "") -> str:
    user = await get_user(uid)
    mode_key = user.get('mode', 'default')
    system = MODES.get(mode_key, MODES['default'])['prompt']

    # Добавляем память
    try:
        memory = json.loads(user.get('memory', '{}'))
        if memory:
            mem_text = ", ".join(f"{k}: {v}" for k, v in memory.items() if v)
            system += f"\n\nЧто я знаю об этом пользователе: {mem_text}"
    except:
        pass

    # Добавляем контекст поиска
    if extra_context:
        system += f"\n\nАктуальная информация из интернета:\n{extra_context}"

    history = await get_history(uid)
    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": user_message}]

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=system
    )
    # Конвертируем историю в формат Gemini
    gemini_history = []
    for m in history:
        role = "user" if m["role"] == "user" else "model"
        gemini_history.append({"role": role, "parts": [m["content"]]})
    
    chat = model.start_chat(history=gemini_history)
    response = await asyncio.to_thread(
        chat.send_message, 
        user_message if not extra_context else user_message + "\n\n" + extra_context
    )
    reply = response.text
    await add_to_history(uid, "user", user_message)
    await add_to_history(uid, "assistant", reply)
    asyncio.create_task(extract_memory(uid, user_message, reply))
    return reply


async def generate_image(prompt: str) -> bytes | None:
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&enhance=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                return None
            return await resp.read()


async def transcribe_voice(file_path: str) -> str:
    # Используем Groq для транскрибации
    from groq import AsyncGroq
    groq = AsyncGroq(api_key=os.getenv("GROQ_API_KEY", ""))
    with open(file_path, "rb") as f:
        transcription = await groq.audio.transcriptions.create(
            file=("audio.ogg", f),
            model="whisper-large-v3",
            language="ru",
        )
    return transcription.text


# ── ХЭНДЛЕРЫ ─────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await init_db()
    name = msg.from_user.first_name or ""
    await get_user(msg.from_user.id, name)
    await update_user(msg.from_user.id, name=name)
    await msg.answer(WELCOME, parse_mode="HTML")


@dp.message(Command("clear"))
async def cmd_clear(msg: Message):
    await clear_history(msg.from_user.id)
    await msg.answer("🗑️ История очищена! Начнём заново 🌙")


@dp.message(Command("memory"))
async def cmd_memory(msg: Message):
    user = await get_user(msg.from_user.id)
    try:
        memory = json.loads(user.get('memory', '{}'))
    except:
        memory = {}
    hist_count = len(await get_history(msg.from_user.id))
    if memory:
        mem_text = "\n".join(f"• {k}: {v}" for k, v in memory.items())
        await msg.answer(
            f"🧠 <b>Что я помню о тебе:</b>\n\n{mem_text}\n\n"
            f"💬 Сообщений в истории: {hist_count}",
            parse_mode="HTML"
        )
    else:
        await msg.answer(f"🧠 Пока не знаю о тебе ничего особенного.\n💬 Сообщений: {hist_count}")


@dp.message(Command("mode"))
async def cmd_mode(msg: Message):
    user = await get_user(msg.from_user.id)
    current = user.get('mode', 'default')
    await msg.answer(
        "🎭 <b>Выбери режим:</b>",
        parse_mode="HTML",
        reply_markup=get_mode_kb(current)
    )


@dp.callback_query(F.data.startswith("mode_"))
async def cb_mode(call: CallbackQuery):
    mode_key = call.data[5:]
    if mode_key not in MODES:
        await call.answer("Режим не найден!")
        return
    await update_user(call.from_user.id, mode=mode_key)
    await clear_history(call.from_user.id)
    mode = MODES[mode_key]
    await call.message.edit_text(
        f"{mode['emoji']} <b>Режим: {mode['name']}</b>\n\n{mode['desc']}\n\nИстория очищена. Поехали! 🌙",
        parse_mode="HTML"
    )
    await call.answer(f"✅ {mode['name']}")


@dp.message(Command("img"))
async def cmd_img(msg: Message):
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.answer("🎨 Напиши описание:\n<code>/img закат над горами</code>", parse_mode="HTML")
        return
    prompt = args[1].strip()
    wait = await msg.answer("🎨 Генерирую... ⏳ (~20 сек)")
    try:
        image_bytes = await generate_image(prompt)
        if not image_bytes:
            await wait.edit_text("❌ Не удалось. Попробуй другое описание.")
            return
        await wait.delete()
        await msg.answer_photo(
            BufferedInputFile(image_bytes, filename="luna.jpg"),
            caption=f"🎨 <b>{prompt[:100]}</b>\n\n🌙 Luna AI",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Image error: {e}")
        await wait.edit_text("❌ Ошибка генерации.")


@dp.message(F.voice)
async def handle_voice(msg: Message):
    wait = await msg.answer("🎤 Расшифровываю голосовое...")
    try:
        import tempfile
        file = await bot.get_file(msg.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await bot.download_file(file.file_path, tmp.name)
            text = await transcribe_voice(tmp.name)

        await wait.edit_text(f"🎤 <i>{text}</i>\n\n🌙 Думаю...", parse_mode="HTML")

        search_ctx = ""
        if needs_search(text):
            search_ctx = await web_search(text)

        reply = await ask_luna(msg.from_user.id, text, search_ctx)
        await wait.edit_text(f"🎤 <i>{text}</i>\n\n{reply}", parse_mode="HTML")
    except Exception as e:
        logging.error(f"Voice error: {e}")
        await wait.edit_text("❌ Не удалось расшифровать голосовое.")


@dp.message(F.document | F.photo)
async def handle_document(msg: Message):
    wait = await msg.answer("📄 Читаю файл...")
    try:
        if msg.document:
            file = await bot.get_file(msg.document.file_id)
            fname = msg.document.file_name or "file"
        else:
            file = await bot.get_file(msg.photo[-1].file_id)
            fname = "image.jpg"

        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(fname)[1]) as tmp:
            await bot.download_file(file.file_path, tmp.name)

            if fname.endswith('.txt') or fname.endswith('.py') or fname.endswith('.js'):
                with open(tmp.name, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()[:3000]
                caption = msg.caption or "Проанализируй этот файл"
                prompt = f"Файл '{fname}':\n\n{content}\n\nЗапрос: {caption}"
            else:
                content = f"Пользователь отправил файл: {fname}"
                caption = msg.caption or "Что это?"
                prompt = f"{caption} (файл: {fname})"

        await wait.edit_text("🌙 Анализирую...")
        reply = await ask_luna(msg.from_user.id, prompt)
        await wait.edit_text(reply)
    except Exception as e:
        logging.error(f"Doc error: {e}")
        await wait.edit_text("❌ Не удалось обработать файл.")


@dp.message(F.text)
async def handle_text(msg: Message):
    await init_db()
    text = msg.text.strip()
    uid = msg.from_user.id

    name = msg.from_user.first_name or ""
    await update_user(uid, name=name)

    # Автогенерация картинки
    lower = text.lower()
    if any(w in lower for w in ["нарисуй", "создай картинку", "сгенерируй", "draw ", "generate image"]):
        wait = await msg.answer("🎨 Генерирую... ⏳")
        try:
            image_bytes = await generate_image(text)
            if image_bytes:
                await wait.delete()
                await msg.answer_photo(
                    BufferedInputFile(image_bytes, filename="luna.jpg"),
                    caption=f"🎨 🌙 Luna AI",
                )
                return
        except:
            pass
        await wait.delete()

    # Поиск если нужен
    search_ctx = ""
    if needs_search(text):
        wait = await msg.answer("🔍 Ищу актуальную информацию...")
        try:
            search_ctx = await web_search(text)
            await wait.edit_text("🌙 Думаю...")
        except:
            await wait.edit_text("🌙 Думаю...")
    else:
        wait = await msg.answer("🌙 Думаю...")

    try:
        reply = await ask_luna(uid, text, search_ctx)
        if len(reply) > 4000:
            await wait.delete()
            for part in [reply[i:i+4000] for i in range(0, len(reply), 4000)]:
                await msg.answer(part)
        else:
            await wait.edit_text(reply)
    except Exception as e:
        logging.error(f"Groq error: {e}")
        await wait.edit_text("❌ Ошибка. Попробуй /clear и напиши снова.")


async def main():
    await init_db()
    logging.info("LunaAI запущен!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
