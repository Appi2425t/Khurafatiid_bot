import os
import io
import logging
import pyotp
import openpyxl
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from database import Database

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "./data/dashboard.db")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database(DB_PATH)


# --- Helpers ---

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_admin_full(user_id: int) -> bool:
    """Check .env admins AND database admins."""
    if user_id in ADMIN_IDS:
        return True
    return await db.is_db_admin(user_id)


def generate_otp(secret: str) -> tuple[str, int]:
    """Returns (otp_code, seconds_remaining)."""
    try:
        totp = pyotp.TOTP(secret)
        code = totp.now()
        remaining = 30 - (int(datetime.utcnow().timestamp()) % 30)
        return code, remaining
    except Exception:
        return "ERROR", 0


def parse_excel(file_bytes: bytes) -> list[dict]:
    """Parse Excel file and return list of account dicts."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
    ws = wb.active

    headers = []
    accounts = []

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            headers = [str(h).strip().lower() if h else "" for h in row]
            continue
        if not any(row):
            continue
        row_dict = dict(zip(headers, row))

        account_id = str(row_dict.get("id") or row_dict.get("username") or row_dict.get("email") or "").strip()
        password = str(row_dict.get("password") or "").strip()
        totp_secret = str(row_dict.get("totp secret") or row_dict.get("totp_secret") or row_dict.get("totp") or "").strip()

        if account_id and password and totp_secret:
            accounts.append({
                "id": account_id,
                "password": password,
                "totp_secret": totp_secret
            })

    return accounts


# --- Admin Commands ---

async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    await update.message.reply_text(
        "📤 *Upload your Excel file now.*\n\n"
        "The file must have these columns:\n"
        "`ID` | `Password` | `TOTP Secret` | `Status` (optional)\n\n"
        "Send the `.xlsx` file as a document.",
        parse_mode="Markdown"
    )
    context.user_data["waiting_for_upload"] = True


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        return
    if not context.user_data.get("waiting_for_upload"):
        return

    doc = update.message.document
    if not doc.file_name.endswith(".xlsx"):
        await update.message.reply_text("❌ Please upload a `.xlsx` file.")
        return

    await update.message.reply_text("⏳ Processing file...")

    try:
        file = await doc.get_file()
        file_bytes = await file.download_as_bytearray()
        accounts = parse_excel(bytes(file_bytes))
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to parse file: {e}")
        return

    if not accounts:
        await update.message.reply_text(
            "❌ No valid accounts found.\n"
            "Make sure columns are named: `ID`, `Password`, `TOTP Secret`"
        )
        return

    added = 0
    updated = 0
    for acc in accounts:
        existing = await db.get_all_accounts()
        existing_ids = [a["account_id"] for a in existing]
        if acc["id"] in existing_ids:
            updated += 1
        else:
            added += 1
        await db.upsert_account(acc["id"], acc["password"], acc["totp_secret"])

    context.user_data["waiting_for_upload"] = False
    stats = await db.get_stats()

    await update.message.reply_text(
        f"✅ *Upload Complete!*\n\n"
        f"📥 Added: {added} new accounts\n"
        f"🔄 Updated: {updated} existing accounts\n\n"
        f"📊 Total: {stats['total']} | Available: {stats['available']} | Assigned: {stats['assigned']}",
        parse_mode="Markdown"
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    accounts = await db.get_all_accounts()
    if not accounts:
        await update.message.reply_text("📭 No accounts in the database.")
        return

    stats = await db.get_stats()

    # Send in chunks to avoid message length limits
    chunk_size = 20
    chunks = [accounts[i:i+chunk_size] for i in range(0, len(accounts), chunk_size)]

    for idx, chunk in enumerate(chunks):
        lines = []
        for acc in chunk:
            status_emoji = "🟢" if acc["status"] == "available" else "🔴"
            assigned = f"(User: {acc['assigned_to']})" if acc.get("assigned_to") else ""
            lines.append(f"{status_emoji} `{acc['account_id']}` {assigned}")

        header = f"📋 *Account List ({idx+1}/{len(chunks)})*\n\n" if idx == 0 else ""
        await update.message.reply_text(
            f"{header}" + "\n".join(lines),
            parse_mode="Markdown"
        )

    await update.message.reply_text(
        f"📊 *Summary:* Total: {stats['total']} | 🟢 Available: {stats['available']} | 🔴 Assigned: {stats['assigned']}",
        parse_mode="Markdown"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    await db.reset_all()
    stats = await db.get_stats()
    await update.message.reply_text(
        f"✅ All {stats['total']} accounts reset to *available*.",
        parse_mode="Markdown"
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/remove <account_id>`", parse_mode="Markdown")
        return

    account_id = " ".join(context.args).strip()
    success = await db.remove_account(account_id)
    if success:
        await update.message.reply_text(f"✅ Account `{account_id}` removed.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Account `{account_id}` not found.", parse_mode="Markdown")


async def cmd_resetaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/resetaccount <account_id>`", parse_mode="Markdown")
        return
    account_id = " ".join(context.args).strip()
    await db.reset_account(account_id)
    await update.message.reply_text(f"✅ Account `{account_id}` reset to available.", parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    stats = await db.get_stats()
    await update.message.reply_text(
        f"📊 *Account Statistics*\n\n"
        f"Total: `{stats['total']}`\n"
        f"🟢 Available: `{stats['available']}`\n"
        f"🔴 Assigned: `{stats['assigned']}`",
        parse_mode="Markdown"
    )


# --- User Commands ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_notice = "\n\n👑 *You are an admin.* Use /upload to add accounts." if await is_admin_full(update.effective_user.id) else ""
    await update.message.reply_text(
        f"👋 *Welcome to Dashboard Account Bot!*\n\n"
        f"Use /getaccount to receive an available account.\n"
        f"Use /myotp to refresh your current OTP code.{admin_notice}",
        parse_mode="Markdown"
    )


async def cmd_getaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Check if user already has an account
    existing = await db.get_user_account(user_id)
    if existing:
        otp, remaining = generate_otp(existing["totp_secret"])
        await update.message.reply_text(
            f"ℹ️ You already have an assigned account.\n\n"
            f"🆔 *Account ID:* `{existing['account_id']}`\n"
            f"🔑 *Password:* `{existing['password']}`\n"
            f"🔐 *OTP Code:* `{otp}`\n"
            f"⏱ *Expires in:* {remaining}s\n\n"
            f"Use /myotp to get a fresh OTP anytime.",
            parse_mode="Markdown"
        )
        return

    # Get available account
    account = await db.get_available_account()
    if not account:
        await update.message.reply_text(
            "❌ *No accounts available right now.*\n"
            "Please contact an admin.",
            parse_mode="Markdown"
        )
        return

    # Assign it
    await db.assign_account(account["account_id"], user_id)
    otp, remaining = generate_otp(account["totp_secret"])

    # Send via DM if command used in group
    target = update.effective_chat
    await target.send_message(
        f"✅ *Account Assigned to You!*\n\n"
        f"🆔 *Account ID:* `{account['account_id']}`\n"
        f"🔑 *Password:* `{account['password']}`\n"
        f"🔐 *OTP Code:* `{otp}`\n"
        f"⏱ *OTP Expires in:* {remaining}s\n\n"
        f"Use /myotp to get a fresh OTP anytime.\n"
        f"⚠️ Keep these credentials private!",
        parse_mode="Markdown"
    )

    logger.info(f"Account {account['account_id']} assigned to user {user_id}")


async def cmd_myotp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    account = await db.get_user_account(user_id)

    if not account:
        await update.message.reply_text(
            "❌ You don't have an assigned account.\n"
            "Use /getaccount to get one.",
            parse_mode="Markdown"
        )
        return

    otp, remaining = generate_otp(account["totp_secret"])
    await update.message.reply_text(
        f"🔐 *Your Current OTP*\n\n"
        f"🆔 Account: `{account['account_id']}`\n"
        f"🔢 *Code:* `{otp}`\n"
        f"⏱ *Valid for:* {remaining} seconds\n\n"
        f"Run /myotp again after 30s for a new code.",
        parse_mode="Markdown"
    )


async def cmd_myaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    account = await db.get_user_account(user_id)

    if not account:
        await update.message.reply_text(
            "❌ You don't have an assigned account.\n"
            "Use /getaccount to get one."
        )
        return

    otp, remaining = generate_otp(account["totp_secret"])
    await update.message.reply_text(
        f"📋 *Your Account Details*\n\n"
        f"🆔 *Account ID:* `{account['account_id']}`\n"
        f"🔑 *Password:* `{account['password']}`\n"
        f"🔐 *OTP Code:* `{otp}`\n"
        f"⏱ *OTP Expires in:* {remaining}s",
        parse_mode="Markdown"
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User signals they are done and want to withdraw."""
    user_id = update.effective_user.id
    account = await db.get_user_account(user_id)

    if not account:
        await update.message.reply_text(
            "❌ You don't have an assigned account.\n"
            "Use /getaccount to get one first."
        )
        return

    # Check if already has a pending withdrawal
    existing = await db.get_user_withdrawal(user_id)
    if existing:
        await update.message.reply_text(
            "⏳ You already have a pending withdrawal request.\n"
            "Please wait for admin to process it.\n\n"
            f"Request ID: `#{existing['id']}`",
            parse_mode="Markdown"
        )
        return

    # Get the active withdrawal wallet
    wallet = await db.get_active_wallet()
    if not wallet:
        await update.message.reply_text(
            "❌ No withdrawal wallet configured yet.\n"
            "Please contact an admin."
        )
        return

    # Create withdrawal record
    withdrawal_id = await db.create_withdrawal(user_id, account["account_id"])

    # Ask for transaction ID
    context.user_data["pending_withdrawal_id"] = withdrawal_id
    context.user_data["waiting_for_txn"] = True

    await update.message.reply_text(
        f"✅ *Withdrawal Request Started*\n\n"
        f"Please withdraw your earnings to this wallet:\n\n"
        f"🌐 *Network:* `{wallet['network']}`\n"
        f"💳 *Wallet Address:*\n`{wallet['address']}`\n\n"
        f"After sending, reply with your *Transaction ID / TXN Hash* so admin can verify.",
        parse_mode="Markdown"
    )


async def handle_txn_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle transaction ID submitted by user after /done."""
    if not context.user_data.get("waiting_for_txn"):
        return

    user_id = update.effective_user.id
    txn_id = update.message.text.strip()
    withdrawal_id = context.user_data.get("pending_withdrawal_id")

    if not withdrawal_id:
        return

    await db.update_withdrawal_txn(withdrawal_id, txn_id)
    context.user_data["waiting_for_txn"] = False
    context.user_data["pending_withdrawal_id"] = None

    account = await db.get_user_account(user_id)

    # Notify all admins
    all_admin_ids = list(ADMIN_IDS)
    db_admins = await db.get_admins()
    for a in db_admins:
        if a["user_id"] not in all_admin_ids:
            all_admin_ids.append(a["user_id"])

    for admin_id in all_admin_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🔔 *New Withdrawal Request*\n\n"
                    f"Request ID: `#{withdrawal_id}`\n"
                    f"User ID: `{user_id}`\n"
                    f"Account: `{account['account_id'] if account else 'N/A'}`\n"
                    f"TXN ID: `{txn_id}`\n\n"
                    f"Use:\n"
                    f"`/approve {withdrawal_id}` — Approve & free account\n"
                    f"`/reject {withdrawal_id} [reason]` — Reject request"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await update.message.reply_text(
        f"✅ *Withdrawal request submitted!*\n\n"
        f"Request ID: `#{withdrawal_id}`\n"
        f"TXN ID: `{txn_id}`\n\n"
        f"Admin will verify and process your request shortly. "
        f"Your account will be released after approval.",
        parse_mode="Markdown"
    )


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin approves a withdrawal and frees the account."""
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/approve <withdrawal_id> [note]`", parse_mode="Markdown")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid withdrawal ID.")
        return

    note = " ".join(context.args[1:]) if len(context.args) > 1 else None
    withdrawal = await db.get_withdrawal_by_id(wid)
    if not withdrawal:
        await update.message.reply_text(f"❌ Withdrawal `#{wid}` not found.", parse_mode="Markdown")
        return
    if withdrawal["status"] != "pending":
        await update.message.reply_text(f"❌ Withdrawal `#{wid}` is already `{withdrawal['status']}`.", parse_mode="Markdown")
        return

    await db.approve_withdrawal(wid, update.effective_user.id, note)
    # Free the account
    await db.reset_account(withdrawal["account_id"])

    await update.message.reply_text(
        f"✅ Withdrawal `#{wid}` *approved*.\n"
        f"Account `{withdrawal['account_id']}` is now available again.",
        parse_mode="Markdown"
    )

    # Notify user
    try:
        msg = f"✅ *Your withdrawal has been approved!*\n\nRequest ID: `#{wid}`\nYour account has been released."
        if note:
            msg += f"\nAdmin note: {note}"
        await context.bot.send_message(chat_id=withdrawal["user_id"], text=msg, parse_mode="Markdown")
    except Exception:
        pass


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin rejects a withdrawal."""
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/reject <withdrawal_id> [reason]`", parse_mode="Markdown")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid withdrawal ID.")
        return

    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason provided"
    withdrawal = await db.get_withdrawal_by_id(wid)
    if not withdrawal:
        await update.message.reply_text(f"❌ Withdrawal `#{wid}` not found.", parse_mode="Markdown")
        return

    await db.reject_withdrawal(wid, update.effective_user.id, reason)

    await update.message.reply_text(
        f"❌ Withdrawal `#{wid}` *rejected*.\nReason: {reason}",
        parse_mode="Markdown"
    )

    # Notify user
    try:
        await context.bot.send_message(
            chat_id=withdrawal["user_id"],
            text=f"❌ *Your withdrawal was rejected.*\n\nRequest ID: `#{wid}`\nReason: {reason}\n\nPlease contact admin.",
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def cmd_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin views all pending withdrawals."""
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    pending = await db.get_pending_withdrawals()
    if not pending:
        await update.message.reply_text("✅ No pending withdrawals.")
        return

    lines = []
    for w in pending:
        lines.append(
            f"📌 *#{w['id']}* — User `{w['user_id']}`\n"
            f"  Account: `{w['account_id']}`\n"
            f"  TXN: `{w.get('txn_id') or 'Not provided yet'}`\n"
            f"  Requested: {str(w['requested_at'])[:16]}"
        )
    await update.message.reply_text(
        f"📋 *Pending Withdrawals ({len(pending)})*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown"
    )


async def cmd_setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sets the withdrawal wallet address."""
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/setwallet <address> <network> [label]`\n\n"
            "Example:\n`/setwallet TRxxxxxxxxxxxxxxxx USDT_TRC20 MainWallet`",
            parse_mode="Markdown"
        )
        return

    address = context.args[0]
    network = context.args[1]
    label = " ".join(context.args[2:]) if len(context.args) > 2 else "Main Wallet"

    wid = await db.add_wallet(label, address, network)
    await update.message.reply_text(
        f"✅ Wallet added (ID: `{wid}`)\n\n"
        f"Label: {label}\nNetwork: `{network}`\nAddress: `{address}`",
        parse_mode="Markdown"
    )


async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin views all withdrawal wallets."""
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    wallets = await db.get_all_wallets()
    if not wallets:
        await update.message.reply_text("No wallets set. Use `/setwallet` to add one.", parse_mode="Markdown")
        return

    lines = []
    for w in wallets:
        active = "✅ Active" if w["active"] else "❌ Inactive"
        lines.append(f"*#{w['id']} — {w['label']}* {active}\nNetwork: `{w['network']}`\nAddress: `{w['address']}`")

    await update.message.reply_text(
        "💳 *Withdrawal Wallets*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown"
    )


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/addadmin <user_id>`\n\n"
            "To get a user's ID, ask them to forward a message to @userinfobot",
            parse_mode="Markdown"
        )
        return
    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
        return

    await db.add_admin(new_admin_id, update.effective_user.id)

    # Update their command list in Telegram
    from telegram import BotCommandScopeChat
    admin_cmds = [
        BotCommand("start", "Welcome message"),
        BotCommand("getaccount", "Get an available account with OTP"),
        BotCommand("myotp", "Refresh your current OTP code"),
        BotCommand("myaccount", "View your account details"),
        BotCommand("help", "Show all commands"),
        BotCommand("upload", "Upload Excel file with accounts"),
        BotCommand("list", "View all accounts and their status"),
        BotCommand("stats", "View account statistics"),
        BotCommand("reset", "Mark all accounts as available"),
        BotCommand("resetaccount", "Reset a specific account"),
        BotCommand("remove", "Remove an account permanently"),
        BotCommand("addadmin", "Add a new admin"),
        BotCommand("removeadmin", "Remove an admin"),
        BotCommand("admins", "List all admins"),
    ]
    try:
        await context.bot.set_my_commands(
            admin_cmds,
            scope=BotCommandScopeChat(chat_id=new_admin_id)
        )
    except Exception:
        pass

    await update.message.reply_text(
        f"✅ User `{new_admin_id}` added as admin.\n"
        f"They will see admin commands next time they open the bot.",
        parse_mode="Markdown"
    )


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/removeadmin <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    # Prevent removing .env admins
    if target_id in ADMIN_IDS:
        await update.message.reply_text("❌ Cannot remove admins set in `.env` from here. Remove them from ADMIN_IDS instead.")
        return

    success = await db.remove_admin(target_id)

    # Restore user-only command list
    from telegram import BotCommandScopeChat
    user_cmds = [
        BotCommand("start", "Welcome message"),
        BotCommand("getaccount", "Get an available account with OTP"),
        BotCommand("myotp", "Refresh your current OTP code"),
        BotCommand("myaccount", "View your account details"),
        BotCommand("help", "Show all commands"),
    ]
    try:
        await context.bot.set_my_commands(
            user_cmds,
            scope=BotCommandScopeChat(chat_id=target_id)
        )
    except Exception:
        pass

    if success:
        await update.message.reply_text(f"✅ User `{target_id}` removed from admins.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ User `{target_id}` is not a database admin.", parse_mode="Markdown")


async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_full(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    admins = await db.get_admins()
    lines = []

    # .env admins
    for uid in ADMIN_IDS:
        lines.append(f"👑 `{uid}` — from .env (permanent)")

    # DB admins
    for a in admins:
        if a["user_id"] not in ADMIN_IDS:
            lines.append(f"🔑 `{a['user_id']}` — added by `{a['added_by']}` on {str(a['added_at'])[:10]}")

    if not lines:
        await update.message.reply_text("No admins configured.")
        return

    embed_text = "👑 *Admin List*\n\n" + "\n".join(lines)
    await update.message.reply_text(embed_text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_commands = (
        "👤 *User Commands*\n"
        "/getaccount — Get an available account with OTP\n"
        "/myotp — Refresh your current OTP code\n"
        "/myaccount — View your account details\n"
        "/done — Signal you're done & request withdrawal\n"
        "/help — Show this message\n"
        "/start — Welcome message"
    )
    admin_commands = (
        "\n\n👑 *Admin Commands*\n"
        "/upload — Upload Excel file with accounts\n"
        "/list — View all accounts and their status\n"
        "/stats — View account statistics\n"
        "/reset — Mark all accounts as available\n"
        "/resetaccount <id> — Reset a specific account\n"
        "/remove <id> — Remove an account permanently\n"
        "/withdrawals — View all pending withdrawals\n"
        "/approve <id> [note] — Approve a withdrawal\n"
        "/reject <id> [reason] — Reject a withdrawal\n"
        "/setwallet <address> <network> [label] — Set wallet\n"
        "/wallets — View all withdrawal wallets\n"
        "/addadmin <user\\_id> — Add a new admin\n"
        "/removeadmin <user\\_id> — Remove an admin\n"
        "/admins — List all admins"
    )

    text = user_commands
    if await is_admin_full(update.effective_user.id):
        text += admin_commands

    await update.message.reply_text(text, parse_mode="Markdown")


# --- App Setup ---

async def post_init(application: Application):
    await db.init()

    # User-facing commands (shown to everyone)
    user_cmds = [
        BotCommand("start", "Welcome message"),
        BotCommand("getaccount", "Get an available account with OTP"),
        BotCommand("myotp", "Refresh your current OTP code"),
        BotCommand("myaccount", "View your account details"),
        BotCommand("done", "Withdraw earnings & release account"),
        BotCommand("help", "Show all commands"),
    ]
    await application.bot.set_my_commands(user_cmds)

    # Admin commands (shown only in admin private chats)
    admin_cmds = user_cmds + [
        BotCommand("upload", "Upload Excel file with accounts"),
        BotCommand("list", "View all accounts and their status"),
        BotCommand("stats", "View account statistics"),
        BotCommand("reset", "Mark all accounts as available"),
        BotCommand("resetaccount", "Reset a specific account"),
        BotCommand("remove", "Remove an account permanently"),
        BotCommand("withdrawals", "View pending withdrawals"),
        BotCommand("approve", "Approve a withdrawal request"),
        BotCommand("reject", "Reject a withdrawal request"),
        BotCommand("setwallet", "Set withdrawal wallet address"),
        BotCommand("wallets", "View all withdrawal wallets"),
        BotCommand("addadmin", "Add a new admin"),
        BotCommand("removeadmin", "Remove an admin"),
        BotCommand("admins", "List all admins"),
    ]

    from telegram import BotCommandScopeChat
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                admin_cmds,
                scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as e:
            logger.warning(f"Could not set admin commands for {admin_id}: {e}")

    # Also set for DB admins
    db_admins = await db.get_admins()
    for a in db_admins:
        if a["user_id"] not in ADMIN_IDS:
            try:
                await application.bot.set_my_commands(
                    admin_cmds,
                    scope=BotCommandScopeChat(chat_id=a["user_id"])
                )
            except Exception as e:
                logger.warning(f"Could not set admin commands for DB admin {a['user_id']}: {e}")

    logger.info(f"Dashboard bot started. Admins: {ADMIN_IDS}")


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # User commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("getaccount", cmd_getaccount))
    app.add_handler(CommandHandler("myotp", cmd_myotp))
    app.add_handler(CommandHandler("myaccount", cmd_myaccount))
    app.add_handler(CommandHandler("done", cmd_done))

    # Admin commands
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("resetaccount", cmd_resetaccount))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("withdrawals", cmd_withdrawals))
    app.add_handler(CommandHandler("setwallet", cmd_setwallet))
    app.add_handler(CommandHandler("wallets", cmd_wallets))

    # File upload handler
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # TXN ID handler (text messages from users waiting after /done)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_txn_id))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
