import os
import json
import random
import hashlib
import logging
import sqlite3
from contextlib import closing

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mmt-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "data/mmt.db")
DEFAULT_QUESTIONS = 10
LETTERS = ["A", "B", "C", "D"]
TYPES = {
    "reading": "Matn bo'yicha",
    "cloze": "Bo'sh joy to'ldirish",
    "matching": "Moslashtirish",
}

EXAMPLE_JSON = """{
  "tests": [
    {
      "type": "reading",
      "title": "Green City",
      "text": "Many cities are planting trees to reduce air pollution. Trees absorb carbon dioxide and provide shade.",
      "questions": [
        {"question": "Trees help to reduce ...", "options": {"A": "noise", "B": "pollution", "C": "traffic", "D": "rain"}, "answer": "B"},
        {"question": "Trees provide ...", "options": {"A": "heat", "B": "shade", "C": "snow", "D": "fog"}, "answer": "B"}
      ]
    },
    {
      "type": "cloze",
      "title": "Trees and air",
      "text": "Many cities plant trees to improve the air. Trees (1) carbon dioxide and provide (2) for people in hot weather.",
      "gaps": [
        {"number": 1, "options": {"A": "absorb", "B": "throw", "C": "break", "D": "paint"}, "answer": "A"},
        {"number": 2, "options": {"A": "shade", "B": "snow", "C": "noise", "D": "fog"}, "answer": "A"}
      ]
    },
    {
      "type": "matching",
      "title": "Weather words",
      "instruction": "Match the words in Column A with the definitions in Column B.",
      "column_a": {"A": "Snowfall", "B": "Barometer", "C": "Rainstorm"},
      "column_b": {
        "1": "a storm with heavy rain.",
        "2": "an amount of snow that falls in a particular period of time.",
        "3": "an instrument for measuring atmospheric pressure."
      },
      "pairs": {"A": "2", "B": "3", "C": "1"}
    }
  ]
}"""


# ---------------- Database ----------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with closing(db()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                hash TEXT NOT NULL,
                type TEXT NOT NULL,
                passage TEXT,
                passage_title TEXT,
                question TEXT NOT NULL,
                options TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, hash)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


# ---------------- Parsing / validation ----------------

def make_row(test_type, passage, title, q, where, free_keys=False):
    if not isinstance(q, dict):
        raise ValueError(f"{where}: savol obyekt bo'lishi kerak")

    question = str(q.get("question", "")).strip()
    answer = str(q.get("answer", "")).strip().upper()
    opts = q.get("options")

    if isinstance(opts, list):
        opts = {LETTERS[i]: v for i, v in enumerate(opts[:4])}
    if not isinstance(opts, dict):
        raise ValueError(f"{where}: options obyekt bo'lishi kerak")

    if free_keys:
        opts = {str(k).strip(): str(v).strip() for k, v in opts.items()}
    else:
        opts = {str(k).strip().upper(): str(v).strip() for k, v in opts.items()}
        if sorted(opts.keys()) != LETTERS:
            raise ValueError(f"{where}: options aynan A, B, C, D bo'lishi kerak")

    if not question:
        raise ValueError(f"{where}: question bo'sh")
    if answer not in opts:
        raise ValueError(f"{where}: answer options kalitlaridan biri bo'lishi kerak")

    return {
        "type": test_type,
        "passage": passage,
        "passage_title": title,
        "question": question,
        "options": opts,
        "answer": answer,
    }


def parse_reading(item: dict, i: int) -> list[dict]:
    text = str(item.get("text", "")).strip()
    title = str(item.get("title", "")).strip()
    questions = item.get("questions")
    if not text:
        raise ValueError(f"{i}-element: reading uchun text kerak")
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"{i}-element: questions ro'yxati bo'sh bo'lmasligi kerak")
    return [
        make_row("reading", text, title, q, f"{i}.{j}")
        for j, q in enumerate(questions, 1)
    ]


def parse_cloze(item: dict, i: int) -> list[dict]:
    text = str(item.get("text", "")).strip()
    title = str(item.get("title", "")).strip()
    gaps = item.get("gaps")
    if not text:
        raise ValueError(f"{i}-element: cloze uchun text kerak")
    if not isinstance(gaps, list) or not gaps:
        raise ValueError(f"{i}-element: gaps ro'yxati bo'sh bo'lmasligi kerak")

    rows = []
    for j, g in enumerate(gaps, 1):
        if not isinstance(g, dict):
            raise ValueError(f"{i}.{j}: gap obyekt bo'lishi kerak")
        num = str(g.get("number", j)).strip()
        if f"({num})" not in text:
            raise ValueError(f"{i}.{num}: textda ({num}) belgisi topilmadi")
        q = {
            "question": f"Bo'sh joy ({num}) ni to'ldiring:",
            "options": g.get("options"),
            "answer": g.get("answer"),
        }
        rows.append(make_row("cloze", text, title, q, f"{i}.{num}"))
    return rows


def parse_matching(item: dict, i: int) -> list[dict]:
    title = str(item.get("title", "")).strip()
    instruction = str(item.get("instruction", "")).strip()
    col_a = item.get("column_a")
    col_b = item.get("column_b")
    pairs = item.get("pairs")

    if not isinstance(col_a, dict) or not isinstance(col_b, dict) or not isinstance(pairs, dict):
        raise ValueError(f"{i}-element: column_a, column_b va pairs obyekt bo'lishi kerak")

    col_a = {str(k).strip().upper(): str(v).strip() for k, v in col_a.items()}
    col_b = {str(k).strip(): str(v).strip() for k, v in col_b.items()}
    pairs = {str(k).strip().upper(): str(v).strip() for k, v in pairs.items()}

    if not col_a or not col_b:
        raise ValueError(f"{i}-element: column_a va column_b bo'sh bo'lmasligi kerak")
    if set(pairs) != set(col_a):
        raise ValueError(f"{i}-element: pairs kalitlari column_a bilan bir xil bo'lishi kerak")
    if any(v not in col_b for v in pairs.values()):
        raise ValueError(f"{i}-element: pairs qiymatlari column_b kalitlaridan biri bo'lishi kerak")

    table = "\n".join(f"{k}) {v}" for k, v in col_b.items())
    passage = (f"{instruction}\n\n" if instruction else "") + table

    rows = []
    for letter, word in col_a.items():
        q = {
            "question": f"{letter}) {word} — to'g'ri ta'rifni tanlang:",
            "options": col_b,
            "answer": pairs[letter],
        }
        rows.append(make_row("matching", passage, title, q, f"{i}.{letter}", free_keys=True))
    return rows


def parse_payload(raw: str) -> list[dict]:
    data = json.loads(raw)
    items = data["tests"] if isinstance(data, dict) and "tests" in data else data
    if not isinstance(items, list):
        raise ValueError("JSON ichida 'tests' ro'yxati bo'lishi kerak")

    rows = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"{i}-element obyekt emas")

        t = item.get("type")
        if t == "reading":
            rows.extend(parse_reading(item, i))
        elif t == "cloze":
            rows.extend(parse_cloze(item, i))
        elif t == "matching":
            rows.extend(parse_matching(item, i))
        else:
            raise ValueError(f"{i}-element: type faqat reading, cloze yoki matching bo'lishi mumkin")

    if not rows:
        raise ValueError("Hech qanday savol topilmadi")
    return rows


def question_hash(row: dict) -> str:
    payload = [row["type"], row["passage"], row["question"], row["options"]]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def save_rows(user_id: int, rows: list[dict]) -> tuple[int, int]:
    added = skipped = 0
    with closing(db()) as conn, conn:
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO questions
                   (user_id, hash, type, passage, passage_title, question, options, answer)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    question_hash(r),
                    r["type"],
                    r["passage"],
                    r["passage_title"],
                    r["question"],
                    json.dumps(r["options"], ensure_ascii=False),
                    r["answer"],
                ),
            )
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
    return added, skipped


def load_bank(user_id: int, types: list[str] | None) -> list[dict]:
    with closing(db()) as conn:
        if types:
            marks = ",".join("?" * len(types))
            rows = conn.execute(
                f"SELECT * FROM questions WHERE user_id=? AND type IN ({marks})",
                [user_id, *types],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM questions WHERE user_id=?", (user_id,)
            ).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d["options"])
        result.append(d)
    return result


# ---------------- Test session ----------------

def build_session(rows: list[dict]) -> list[dict]:
    """Bitta matnga tegishli savollar ketma-ket chiqadi, matn esa bir marta."""
    groups = {}
    for r in rows:
        groups.setdefault((r["passage_title"], r["passage"]), []).append(r)

    blocks = list(groups.values())
    random.shuffle(blocks)

    items = []
    for block in blocks:
        items.extend(block)
        if len(items) >= DEFAULT_QUESTIONS:
            break
    return items


def split_text(text: str, limit: int = 4000) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


async def send_step(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    s = context.user_data["session"]
    if s["idx"] >= len(s["items"]):
        return await finish(chat_id, context)

    item = s["items"][s["idx"]]

    if item["passage"] and item["passage"] != s["last_passage"]:
        s["last_passage"] = item["passage"]
        header = f"📖 {item['passage_title']}\n\n" if item["passage_title"] else ""
        for chunk in split_text(header + item["passage"]):
            await context.bot.send_message(chat_id, chunk)

    body = (
        f"❓ {s['idx'] + 1}/{len(s['items'])} · {TYPES[item['type']]}\n\n"
        f"{item['question']}\n\n"
        + "\n".join(f"{k}) {v}" for k, v in item["options"].items())
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(k, callback_data=f"ans:{s['idx']}:{k}")
        for k in item["options"]
    ]])
    await context.bot.send_message(chat_id, body, reply_markup=kb)


async def finish(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    s = context.user_data.pop("session")
    total, score = len(s["items"]), s["score"]
    pct = round(score * 100 / total) if total else 0

    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO results (user_id, mode, score, total) VALUES (?, ?, ?, ?)",
            (s["user_id"], s["mode"], score, total),
        )

    await context.bot.send_message(
        chat_id,
        f"🏁 Test tugadi!\nNatija: {score}/{total} ({pct}%)\n\nYangi test: /test",
    )


# ---------------- Handlers ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! Bu MMT tayyorlov boti.\n\n"
        "Formatlar: moslashtirish, matn bo'yicha test, bo'sh joy to'ldirish.\n\n"
        "1️⃣ JSON testlarni chatga yuboring (yoki .json fayl)\n"
        "2️⃣ /test — test ishlash\n"
        "3️⃣ /stats — statistika\n"
        "4️⃣ /example — JSON namunasi\n"
        "5️⃣ /stop — testni to'xtatish\n\n"
        "Takrorlangan savollar qayta qo'shilmaydi."
    )


async def cmd_example(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(EXAMPLE_JSON)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Matn bo'yicha", callback_data="mode:reading")],
        [InlineKeyboardButton("🔤 Bo'sh joy to'ldirish", callback_data="mode:cloze")],
        [InlineKeyboardButton("🔗 Moslashtirish", callback_data="mode:matching")],
        [InlineKeyboardButton("🎲 Aralash", callback_data="mode:all")],
    ])
    await update.message.reply_text("Test turini tanlang:", reply_markup=kb)


async def on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = q.data.split(":")[1]
    user_id = q.from_user.id

    types = None if mode == "all" else [mode]
    rows = load_bank(user_id, types)
    if not rows:
        return await q.message.reply_text("Bu turda savollar yo'q. Avval JSON yuboring (/example).")

    items = build_session(rows)
    context.user_data["session"] = {
        "user_id": user_id,
        "items": items,
        "idx": 0,
        "score": 0,
        "mode": mode,
        "last_passage": None,
    }
    await q.message.reply_text(f"🚀 Test boshlandi: {len(items)} savol. To'xtatish: /stop")
    await send_step(q.message.chat_id, context)


async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    s = context.user_data.get("session")
    if not s:
        return

    _, idx_str, choice = q.data.split(":")
    if int(idx_str) != s["idx"]:
        return  # eski yoki ikki marta bosilgan tugma

    item = s["items"][s["idx"]]
    s["idx"] += 1
    correct = choice == item["answer"]
    if correct:
        s["score"] += 1

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if correct:
        feedback = "✅ To'g'ri!"
    else:
        feedback = (
            f"❌ Noto'g'ri. To'g'ri javob: "
            f"{item['answer']}) {item['options'][item['answer']]}"
        )

    await context.bot.send_message(q.message.chat_id, feedback)
    await send_step(q.message.chat_id, context)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.pop("session", None):
        await update.message.reply_text("Test to'xtatildi.")
    else:
        await update.message.reply_text("Faol test yo'q.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with closing(db()) as conn:
        bank = conn.execute(
            "SELECT type, COUNT(*) AS c FROM questions WHERE user_id=? GROUP BY type", (uid,)
        ).fetchall()
        res = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(score),0) AS s, COALESCE(SUM(total),0) AS t "
            "FROM results WHERE user_id=?", (uid,)
        ).fetchone()

    counts = {r["type"]: r["c"] for r in bank}
    lines = ["📚 Bankdagi savollar:"]
    for key, label in TYPES.items():
        lines.append(f"• {label}: {counts.get(key, 0)}")
    lines.append(f"\n📊 Ishlangan testlar: {res['n']} ta")
    lines.append(f"Umumiy natija: {res['s']}/{res['t']}")
    await update.message.reply_text("\n".join(lines))


async def process_payload(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    try:
        rows = parse_payload(raw)
    except json.JSONDecodeError as e:
        return await update.message.reply_text(f"❌ JSON xato: {e}")
    except ValueError as e:
        return await update.message.reply_text(f"❌ Format xato: {e}")
    except Exception:
        log.exception("Payload parse error")
        return await update.message.reply_text("❌ Kutilmagan xato.")

    added, skipped = save_rows(update.effective_user.id, rows)
    await update.message.reply_text(
        f"✅ Qo'shildi: {added}\n"
        f"♻️ Avval qo'shilgan, o'tkazib yuborildi: {skipped}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.startswith(("{", "[")):
        return await process_payload(update, context, text)
    await update.message.reply_text("JSON yuboring yoki /example bilan namunani ko'ring.")


async def handle_json_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        f = await update.message.document.get_file()
        raw = bytes(await f.download_as_bytearray()).decode("utf-8")
    except Exception:
        log.exception("File read error")
        return await update.message.reply_text("❌ Faylni o'qib bo'lmadi (UTF-8 bo'lishi kerak).")
    await process_payload(update, context, raw)


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("example", cmd_example))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_mode, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(on_answer, pattern=r"^ans:"))
    app.add_handler(MessageHandler(filters.Document.FileExtension("json"), handle_json_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot ishga tushdi")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
