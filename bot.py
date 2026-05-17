"""
Telegram Action Item Bot
팀 채널에서 /extract 명령어로 대화 내용을 분석해 액션아이템을 추출하고,
인라인 버튼으로 완료 처리를 지원합니다.
"""

import os
import json
import logging
import anthropic
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─── 설정 ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── 인메모리 저장소 (재시작 시 초기화됨, 필요 시 DB로 교체 가능) ─────────────
# 구조: { chat_id: { item_id: { ...item_data } } }
action_store: dict[int, dict[str, dict]] = {}
# 채널별 최근 메시지 버퍼 (분석용)
message_buffer: dict[int, list[str]] = {}
BUFFER_MAX = 100  # 최근 100개 메시지 유지


def get_items(chat_id: int) -> dict[str, dict]:
    return action_store.setdefault(chat_id, {})


def next_item_id(chat_id: int) -> str:
    items = get_items(chat_id)
    existing = [int(k) for k in items.keys() if k.isdigit()]
    return str(max(existing, default=0) + 1)


# ─── Claude AI 분석 ────────────────────────────────────────────────────────
EXTRACT_PROMPT = """당신은 팀 채팅에서 액션아이템을 추출하는 전문가입니다.

아래 대화 내용을 분석하여 명확한 액션아이템을 추출하세요.

규칙:
- 누군가 해야 할 구체적인 일만 추출 (막연한 논의는 제외)
- 담당자가 언급되면 반드시 포함
- 기한이 언급되면 반드시 포함 (ISO 날짜 형식: YYYY-MM-DD)
- 우선순위 판단: high(긴급/오늘/내일), medium(이번 주), low(그 외)
- 최대 10개까지 추출

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{
  "items": [
    {
      "text": "액션아이템 내용",
      "assignee": "담당자 이름 (없으면 null)",
      "due": "YYYY-MM-DD (없으면 null)",
      "priority": "high | medium | low"
    }
  ]
}

대화 내용:
"""


async def extract_action_items(conversation: str) -> list[dict]:
    """Claude API로 액션아이템 추출"""
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": EXTRACT_PROMPT + conversation}],
        )
        raw = response.content[0].text.strip()
        # JSON 코드블록 제거
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        return data.get("items", [])
    except Exception as e:
        logger.error(f"Claude API 오류: {e}")
        return []


# ─── 메시지 카드 렌더링 ────────────────────────────────────────────────────
def render_item_card(item_id: str, item: dict) -> tuple[str, InlineKeyboardMarkup]:
    """액션아이템 하나를 텔레그램 메시지 + 인라인 버튼으로 렌더링"""
    status = item["status"]
    is_done = status == "done"

    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(item["priority"], "🟡")
    status_text = "✅ 완료" if is_done else "🔄 진행중"

    assignee_line = f"👤 *담당자:* {item['assignee']}" if item.get("assignee") else "👤 *담당자:* 미지정"
    due_line = ""
    if item.get("due"):
        due_dt = datetime.strptime(item["due"], "%Y-%m-%d")
        diff = (due_dt.date() - datetime.now().date()).days
        if diff < 0:
            due_line = f"\n📅 *기한:* {item['due']} ⚠️ D+{abs(diff)} 초과"
        elif diff == 0:
            due_line = f"\n📅 *기한:* {item['due']} 🔥 오늘"
        elif diff == 1:
            due_line = f"\n📅 *기한:* {item['due']} ⏰ 내일"
        else:
            due_line = f"\n📅 *기한:* {item['due']} (D-{diff})"

    completed_line = ""
    if is_done and item.get("completed_at"):
        completed_line = f"\n🏁 *완료:* {item['completed_at'][:10]}"

    text = (
        f"{priority_emoji} *\\[{item_id}\\] {escape_md(item['text'])}*\n\n"
        f"{assignee_line}"
        f"{due_line}"
        f"{completed_line}\n\n"
        f"상태: {status_text}"
    )

    if is_done:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ 재개", callback_data=f"reopen:{item_id}")]
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 완료", callback_data=f"done:{item_id}"),
                InlineKeyboardButton("🗑 삭제", callback_data=f"delete:{item_id}"),
            ]
        ])

    return text, keyboard


def escape_md(text: str) -> str:
    """MarkdownV2 이스케이프"""
    chars = r"_*[]()~`>#+-=|{}.!"
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text


def render_summary(chat_id: int) -> str:
    """전체 현황 요약 메시지"""
    items = get_items(chat_id)
    if not items:
        return "📋 등록된 액션아이템이 없습니다."

    active = [i for i in items.values() if i["status"] == "active"]
    done = [i for i in items.values() if i["status"] == "done"]

    lines = [f"📊 *액션아이템 현황* — 총 {len(items)}개 (진행중 {len(active)} / 완료 {len(done)})\n"]

    if active:
        lines.append("*🔄 진행중*")
        for iid, item in items.items():
            if item["status"] != "active":
                continue
            p = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(item["priority"], "🟡")
            assignee = f" — {item['assignee']}" if item.get("assignee") else ""
            due = f" _{item['due']}_" if item.get("due") else ""
            lines.append(f"{p} `[{iid}]` {escape_md(item['text'])}{escape_md(assignee)}{due}")

    if done:
        lines.append("\n*✅ 완료*")
        for iid, item in items.items():
            if item["status"] != "done":
                continue
            lines.append(f"~~{escape_md(item['text'])}~~ `[{iid}]`")

    return "\n".join(lines)


# ─── 명령어 핸들러 ─────────────────────────────────────────────────────────
async def cmd_extract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/extract — 최근 대화 버퍼에서 액션아이템 추출"""
    chat_id = update.effective_chat.id
   # 명령어 뒤에 텍스트가 있으면 버퍼 무시하고 바로 사용
    custom_text = " ".join(context.args) if context.args else None
    if custom_text:
        conversation = custom_text
    else:
        buf = message_buffer.get(chat_id, [])
        if len(buf) < 2:
            await update.message.reply_text(
                "⚠️ 분석할 대화 내용이 부족합니다.\n"
                "대화가 더 쌓인 후 /extract 를 사용해주세요.\n\n"
                "또는 `/extract` 뒤에 직접 대화 내용을 붙여넣을 수 있습니다."
            )
            return
        conversation = "\n".join(buf[-50:])

    processing_msg = await update.message.reply_text("🤖 AI가 대화를 분석중입니다...")

    items_raw = await extract_action_items(conversation)

    if not items_raw:
        await processing_msg.edit_text("❌ 추출된 액션아이템이 없습니다. 대화 내용을 확인해주세요.")
        return

    await processing_msg.edit_text(
        f"✨ *{len(items_raw)}개의 액션아이템을 추출했습니다!*\n"
        "각 항목을 확인하고 완료 시 ✅ 버튼을 눌러주세요.",
        parse_mode="MarkdownV2"
    )

    # 각 아이템 카드 전송
    for raw in items_raw:
        item_id = next_item_id(chat_id)
        item = {
            "text": raw.get("text", ""),
            "assignee": raw.get("assignee"),
            "due": raw.get("due"),
            "priority": raw.get("priority", "medium"),
            "status": "active",
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "message_id": None,
        }
        get_items(chat_id)[item_id] = item

        card_text, keyboard = render_item_card(item_id, item)
        sent = await update.message.reply_text(
            card_text,
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
        )
        # 나중에 메시지 수정을 위해 message_id 저장
        item["message_id"] = sent.message_id


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/done [번호] — 특정 아이템 완료 처리"""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("사용법: `/done [번호]`\n예: `/done 3`", parse_mode="MarkdownV2")
        return

    item_id = context.args[0]
    items = get_items(chat_id)

    if item_id not in items:
        await update.message.reply_text(f"❌ `[{item_id}]` 번 아이템을 찾을 수 없습니다.", parse_mode="MarkdownV2")
        return

    await _mark_done(update, context, chat_id, item_id)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — 전체 현황 요약"""
    chat_id = update.effective_chat.id
    summary = render_summary(chat_id)
    await update.message.reply_text(summary, parse_mode="MarkdownV2")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/clear — 완료된 아이템 모두 삭제"""
    chat_id = update.effective_chat.id
    items = get_items(chat_id)
    before = len(items)
    to_remove = [k for k, v in items.items() if v["status"] == "done"]
    for k in to_remove:
        del items[k]
    await update.message.reply_text(
        f"🗑 완료 항목 {len(to_remove)}개를 삭제했습니다. (남은 항목: {len(items)}개)"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help — 도움말"""
    help_text = (
        "📋 *액션아이템 봇 사용법*\n\n"
        "*/extract* — 최근 대화에서 액션아이템 자동 추출\n"
        "*/extract \\[대화내용\\]* — 직접 붙여넣기 분석\n"
        "*/done \\[번호\\]* — 특정 아이템 완료 처리\n"
        "*/list* — 전체 현황 요약 보기\n"
        "*/clear* — 완료된 항목 정리\n"
        "*/help* — 이 도움말\n\n"
        "💡 각 아이템 카드의 ✅ 버튼으로도 완료 처리 가능"
    )
    await update.message.reply_text(help_text, parse_mode="MarkdownV2")


# ─── 인라인 버튼 콜백 ──────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    data = query.data  # "done:3", "reopen:3", "delete:3"
    action, item_id = data.split(":", 1)
    items = get_items(chat_id)

    if item_id not in items:
        await query.edit_message_text("⚠️ 이미 삭제된 항목입니다.")
        return

    item = items[item_id]

    if action == "done":
        item["status"] = "done"
        item["completed_at"] = datetime.now().isoformat()
        card_text, keyboard = render_item_card(item_id, item)
        await query.edit_message_text(card_text, parse_mode="MarkdownV2", reply_markup=keyboard)
        # 완료 알림
        user = query.from_user.first_name
        await context.bot.send_message(
            chat_id,
            f"✅ *{escape_md(user)}*님이 `[{item_id}]` 을 완료했습니다\\!\n_{escape_md(item['text'])}_",
            parse_mode="MarkdownV2"
        )

    elif action == "reopen":
        item["status"] = "active"
        item["completed_at"] = None
        card_text, keyboard = render_item_card(item_id, item)
        await query.edit_message_text(card_text, parse_mode="MarkdownV2", reply_markup=keyboard)

    elif action == "delete":
        del items[item_id]
        await query.edit_message_text(f"🗑 `[{item_id}]` 항목이 삭제되었습니다.", parse_mode="MarkdownV2")


# ─── 메시지 버퍼링 ────────────────────────────────────────────────────────
async def buffer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """일반 메시지를 분석용 버퍼에 저장"""
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    sender = update.message.from_user.first_name or "Unknown"
    text = update.message.text.strip()
    buf = message_buffer.setdefault(chat_id, [])
    buf.append(f"{sender}: {text}")
    # 버퍼 최대 크기 유지
    if len(buf) > BUFFER_MAX:
        buf.pop(0)


async def _mark_done(update, context, chat_id, item_id):
    items = get_items(chat_id)
    item = items[item_id]
    item["status"] = "done"
    item["completed_at"] = datetime.now().isoformat()

    # 원본 카드 메시지 수정 시도
    if item.get("message_id"):
        try:
            card_text, keyboard = render_item_card(item_id, item)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=item["message_id"],
                text=card_text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        except Exception:
            pass

    await update.message.reply_text(
        f"✅ `[{item_id}]` _{escape_md(item['text'])}_ 완료 처리되었습니다\\!",
        parse_mode="MarkdownV2"
    )


# ─── 메인 ─────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("extract", cmd_extract))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, buffer_message))

    logger.info("🤖 액션아이템 봇 시작!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
