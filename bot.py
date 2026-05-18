"""
Telegram Action Item Bot
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

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

action_store: dict[int, dict[str, dict]] = {}
message_buffer: dict[int, list[str]] = {}
BUFFER_MAX = 100


def get_items(chat_id: int) -> dict[str, dict]:
    return action_store.setdefault(chat_id, {})


def next_item_id(chat_id: int) -> str:
    items = get_items(chat_id)
    existing = [int(k) for k in items.keys() if k.isdigit()]
    return str(max(existing, default=0) + 1)


EXTRACT_PROMPT = """당신은 팀 채팅에서 액션아이템을 추출하는 전문가입니다.

아래 대화 내용을 분석하여 명확한 액션아이템을 추출하세요.

규칙:
- 누군가 해야 할 구체적인 일만 추출 (막연한 논의는 제외)
- 담당자가 언급되면 반드시 포함
- 기한이 언급되면 반드시 포함 (ISO 날짜 형식: YYYY-MM-DD)
- 우선순위 판단: high(긴급/오늘/내일), medium(이번 주), low(그 외)
- 최대 10개까지 추출
- 원본 텍스트에 번호(예: 34., 35. 또는 /34, /35)가 있으면 반드시 original_id에 포함
- 번호가 없으면 original_id는 null

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{
  "items": [
    {
      "original_id": "34 (없으면 null)",
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
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": EXTRACT_PROMPT + conversation}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        return data.get("items", [])
    except Exception as e:
        logger.error(f"Claude API 오류: {e}")
        return []


def render_item_card(item_id: str, item: dict) -> tuple[str, InlineKeyboardMarkup]:
    is_done = item["status"] == "done"
    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(item["priority"], "🟡")
    status_text = "완료" if is_done else "진행중"

    assignee_line = f"담당자: {item['assignee']}" if item.get("assignee") else "담당자: 미지정"

    due_line = ""
    if item.get("due"):
        due_dt = datetime.strptime(item["due"], "%Y-%m-%d")
        diff = (due_dt.date() - datetime.now().date()).days
        if diff < 0:
            due_line = f"\n기한: {item['due']} (D+{abs(diff)} 초과)"
        elif diff == 0:
            due_line = f"\n기한: {item['due']} (오늘)"
        elif diff == 1:
            due_line = f"\n기한: {item['due']} (내일)"
        else:
            due_line = f"\n기한: {item['due']} (D-{diff})"

    completed_line = ""
    if is_done and item.get("completed_at"):
        completed_line = f"\n완료일: {item['completed_at'][:10]}"

    text = (
        f"{priority_emoji} [{item_id}] {item['text']}\n\n"
        f"{assignee_line}"
        f"{due_line}"
        f"{completed_line}\n\n"
        f"상태: {status_text}"
    )

    if is_done:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ 재개", callback_data=f"reopen:{item_id}")]])
    else:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 완료", callback_data=f"done:{item_id}"),
            InlineKeyboardButton("🗑 삭제", callback_data=f"delete:{item_id}"),
        ]])

    return text, keyboard


def render_summary(chat_id: int) -> str:
    items = get_items(chat_id)
    if not items:
        return "등록된 액션아이템이 없습니다."

    active = [i for i in items.values() if i["status"] == "active"]
    done = [i for i in items.values() if i["status"] == "done"]
    lines = [f"액션아이템 현황 - 총 {len(items)}개 (진행중 {len(active)} / 완료 {len(done)})\n"]

    if active:
        lines.append("[ 진행중 ]")
        for iid, item in items.items():
            if item["status"] != "active":
                continue
            p = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(item["priority"], "🟡")
            assignee = f" ({item['assignee']})" if item.get("assignee") else ""
            due = f" - {item['due']}" if item.get("due") else ""
            lines.append(f"{p} [{iid}] {item['text']}{assignee}{due}")

    if done:
        lines.append("\n[ 완료 ]")
        for iid, item in items.items():
            if item["status"] != "done":
                continue
            lines.append(f"[{iid}] {item['text']}")

    return "\n".join(lines)


async def cmd_extract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    custom_text = " ".join(context.args) if context.args else None

    if custom_text:
        conversation = custom_text
    else:
        buf = message_buffer.get(chat_id, [])
        if len(buf) < 1:
            await update.message.reply_text(
                "분석할 대화 내용이 부족합니다.\n\n"
                "/extract 뒤에 직접 대화 내용을 붙여넣을 수 있습니다.\n"
                "예시: /extract 팀장: K2 제안서 부탁해요. 박과장: 알겠습니다."
            )
            return
        conversation = "\n".join(buf[-50:])

    processing_msg = await update.message.reply_text("AI가 대화를 분석중입니다...")
    items_raw = await extract_action_items(conversation)

    if not items_raw:
        await processing_msg.edit_text("추출된 액션아이템이 없습니다. 대화 내용을 확인해주세요.")
        return

    await processing_msg.edit_text(f"{len(items_raw)}개의 액션아이템을 추출했습니다. 완료 시 버튼을 눌러주세요.")

    for raw in items_raw:
        original_id = str(raw.get("original_id", "")).strip()
        if original_id and original_id != "None" and original_id not in get_items(chat_id):
            item_id = original_id
        else:
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
        sent = await update.message.reply_text(card_text, reply_markup=keyboard)
        item["message_id"] = sent.message_id


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("사용법: /done [번호]\n예: /done 3")
        return

    item_id = context.args[0]
    items = get_items(chat_id)

    if item_id not in items:
        await update.message.reply_text(f"[{item_id}] 번 아이템을 찾을 수 없습니다.")
        return

    await _mark_done(update, context, chat_id, item_id)


async def cmd_assign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/assign [번호] [이름] — 담당자 지정"""
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("사용법: /assign [번호] [이름]\n예: /assign 1 홍길동")
        return

    item_id = context.args[0]
    assignee = " ".join(context.args[1:])
    items = get_items(chat_id)

    if item_id not in items:
        await update.message.reply_text(f"[{item_id}] 번 아이템을 찾을 수 없습니다.")
        return

    item = items[item_id]
    old_assignee = item.get("assignee") or "미지정"
    item["assignee"] = assignee

    # 카드 메시지 업데이트
    if item.get("message_id"):
        try:
            card_text, keyboard = render_item_card(item_id, item)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=item["message_id"],
                text=card_text,
                reply_markup=keyboard,
            )
        except Exception:
            pass

    await update.message.reply_text(
        f"[{item_id}] 담당자가 {old_assignee} -> {assignee} 로 변경되었습니다."
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(render_summary(chat_id))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status — 버퍼 현황 확인"""
    chat_id = update.effective_chat.id
    buf = message_buffer.get(chat_id, [])
    count = len(buf)

    if count == 0:
        msg = "버퍼에 저장된 메시지가 없습니다.\n대화가 쌓이면 /extract 로 분석할 수 있습니다."
    else:
        last = buf[-3:] if count >= 3 else buf
        preview = "\n".join([f"  {line}" for line in last])
        msg = (
            f"버퍼 현황: 최근 메시지 {count}개 저장됨 (최대 {BUFFER_MAX}개)\n\n"
            f"최근 메시지 미리보기:\n{preview}\n\n"
            f"/extract 입력 시 위 대화를 분석합니다."
        )

    await update.message.reply_text(msg)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    items = get_items(chat_id)
    to_remove = [k for k, v in items.items() if v["status"] == "done"]
    for k in to_remove:
        del items[k]
    await update.message.reply_text(f"완료 항목 {len(to_remove)}개를 삭제했습니다. (남은 항목: {len(items)}개)")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "액션아이템 봇 사용법\n\n"
        "/extract - 최근 대화에서 액션아이템 자동 추출\n"
        "/extract [대화내용] - 직접 붙여넣기 분석\n"
        "/done [번호] - 특정 아이템 완료 처리\n"
        "/assign [번호] [이름] - 담당자 지정 (예: /assign 1 홍길동)\n"
        "/status - 버퍼에 쌓인 메시지 현황 확인\n"
        "/list - 전체 현황 요약 보기\n"
        "/clear - 완료된 항목 정리\n"
        "/help - 이 도움말"
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    action, item_id = query.data.split(":", 1)
    items = get_items(chat_id)

    if item_id not in items:
        await query.edit_message_text("이미 삭제된 항목입니다.")
        return

    item = items[item_id]

    if action == "done":
        item["status"] = "done"
        item["completed_at"] = datetime.now().isoformat()
        card_text, keyboard = render_item_card(item_id, item)
        await query.edit_message_text(card_text, reply_markup=keyboard)
        user = query.from_user.first_name
        await context.bot.send_message(chat_id, f"{user}님이 [{item_id}] 을 완료했습니다.\n{item['text']}")

    elif action == "reopen":
        item["status"] = "active"
        item["completed_at"] = None
        card_text, keyboard = render_item_card(item_id, item)
        await query.edit_message_text(card_text, reply_markup=keyboard)

    elif action == "delete":
        del items[item_id]
        await query.edit_message_text(f"[{item_id}] 항목이 삭제되었습니다.")


async def buffer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    sender = update.message.from_user.first_name or "Unknown"
    text = update.message.text.strip()
    buf = message_buffer.setdefault(chat_id, [])
    buf.append(f"{sender}: {text}")
    if len(buf) > BUFFER_MAX:
        buf.pop(0)


async def _mark_done(update, context, chat_id, item_id):
    items = get_items(chat_id)
    item = items[item_id]
    item["status"] = "done"
    item["completed_at"] = datetime.now().isoformat()

    if item.get("message_id"):
        try:
            card_text, keyboard = render_item_card(item_id, item)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=item["message_id"],
                text=card_text,
                reply_markup=keyboard,
            )
        except Exception:
            pass

    await update.message.reply_text(f"[{item_id}] {item['text']} 완료 처리되었습니다.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("extract", cmd_extract))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("assign", cmd_assign))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, buffer_message))
    logger.info("액션아이템 봇 시작!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
