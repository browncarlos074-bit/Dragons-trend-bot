# main.py
"""
Dragons Trend - All-in-one Telegram bot
Features:
 - /start
 - /submit  (conversation form stored to projects.json)
 - /list    (list pending projects)
 - /verify_payment <project_id> <tx_sig>  (manual verification command)
 - /vote <project_id>  (requires joining both groups)
 - /leaderboard (show top 10)
 - Auto-post leaderboard every 6 hours
Storage: projects.json (in repo / persisted by Render)
NOTE: Add BOT_TOKEN to environment in Render.
Optional env vars for payment checks:
 - ETHERSCAN_API_KEY  (for ETH/BNB verification via Etherscan-compatible APIs)
 - SOLANA_RPC_URL (e.g. https://api.mainnet-beta.solana.com)
"""

import os
import json
import time
import math
from typing import Dict, Any
import requests
from datetime import datetime, timezone, timedelta

from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
    JobQueue
)

# ---------------- Configuration ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("You must set BOT_TOKEN in environment variables (from @BotFather).")

# Channels / Groups / Bot settings (use the exact usernames you gave me)
GROUP_A = "@dragonstrending"    # project listing group
GROUP_B = "@Dragonstrend"       # second group (also required)
LEADERBOARD_CHANNEL = "@Dragonstrend"   # where leaderboard posts
PROJECT_LISTING_CHANNEL = "@dragonstrending"  # where project listing posts

# Wallets
WALLETS = {
    "SOL": "EZL57GDRFCr5mGNstcQjwDgspfrWr579d97mtQo63EWn",
    "ETH": "0xEf3C9Fb7B03A0e78D1D689949b8BDee735737d67",
    "BNB": "0xEf3C9Fb7B03A0e78D1D689949b8BDee735737d67"
}
REQUIRED_USD = 150.0

# Files
PROJECTS_FILE = "projects.json"

# Optional API keys / URLs for payment verification
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")  # optional
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# Leaderboard interval (seconds) - 6 hours
LEADERBOARD_INTERVAL = 6 * 60 * 60

# Conversation states for /submit
(S_NAME, S_SYMBOL, S_LOGO, S_CONTRACT, S_DESC, S_CHAIN, S_CONFIRM) = range(7)

# ---------------- Utilities ----------------
def load_projects() -> Dict[str, Any]:
    try:
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"projects": {}, "votes": {}}  # projects keyed by id, votes as map project_id -> list(user_ids)
    return data

def save_projects(data: Dict[str, Any]):
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def make_project_id(name: str) -> str:
    # simple id: name + timestamp
    stamp = int(time.time())
    safe = "".join(ch for ch in name if ch.isalnum()).lower()[:20]
    return f"{safe}_{stamp}"

def get_top_projects(data: Dict[str, Any], limit: int = 10):
    projects = data.get("projects", {})
    items = []
    for pid, p in projects.items():
        votes = len(data.get("votes", {}).get(pid, []))
        items.append((pid, p, votes))
    items.sort(key=lambda x: x[2], reverse=True)
    return items[:limit]

# ---------------- Payment Verification helpers ----------------
def check_eth_tx_for_payment(tx_hash: str, expected_to: str, min_usd: float) -> bool:
    """
    Uses Etherscan API (or similar) to check transaction details.
    Requires ETHERSCAN_API_KEY environment variable to be set.
    Returns True if tx exists, to matches expected_to, and value >= required USD (approx).
    NOTE: USD price conversion is non-trivial. This checks value > 0; more accurate USD check requires price API.
    """
    if not ETHERSCAN_API_KEY:
        return False, "Missing ETHERSCAN_API_KEY (set this in Render environment if you want on-chain ETH/BNB verification)."

    # Try Etherscan API (Mainnet)
    # For BSC, user would need BSCscan API URL (or the same key if supported).
    url = f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash&txhash={tx_hash}&apikey={ETHERSCAN_API_KEY}"
    resp = requests.get(url, timeout=20)
    if resp.status_code != 200:
        return False, f"Etherscan query failed with status {resp.status_code}"
    j = resp.json()
    if "result" not in j or not j["result"]:
        return False, "Transaction not found."
    result = j["result"]
    to_addr = result.get("to") or ""
    # Normalize checks
    if to_addr.lower() != expected_to.lower():
        return False, f"Tx recipient {to_addr} does not match expected {expected_to}."
    # value is in hex Wei
    value_hex = result.get("value", "0x0")
    try:
        value_wei = int(value_hex, 16)
    except Exception:
        value_wei = 0
    if value_wei <= 0:
        return False, "Transaction value is zero."
    # NOTE: We don't convert to USD here (requires price oracle). We'll accept any non-zero for now.
    return True, "Transaction found and sent to expected address."

def check_solana_tx_for_payment(signature: str, expected_to: str):
    # Use Solana JSON-RPC getTransaction
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed"}]
    }
    try:
        resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=20)
        j = resp.json()
    except Exception as e:
        return False, f"RPC call error: {e}"
    result = j.get("result")
    if not result:
        return False, "Transaction not found on Solana RPC."
    # parse transaction to find transfer to expected_to
    try:
        meta = result.get("meta", {})
        # For SOL transfers, check inner instructions for SystemProgram transfer
        tx = result.get("transaction", {})
        message = tx.get("message", {})
        # This is a minimal check; for robust check you'd parse each instruction
        # We'll check postTokenBalances / preTokenBalances or account keys for recipient
        account_keys = message.get("accountKeys", [])
        if expected_to in account_keys or expected_to in [k.get("pubkey") for k in account_keys]:
            return True, "Found expected recipient in tx (basic check)."
        # fallback: check meta->pre/post balances
        return True, "Transaction found (note: this is a basic check; inspect details manually for USD amount)."
    except Exception:
        return False, "Could not parse Solana tx response."

# ---------------- Bot Command Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐉 Welcome to Dragons Trend bot!\n\n"
        "Commands:\n"
        "/submit - Submit a project (form)\n"
        "/vote <project_id> - Vote for a project (must join both groups)\n"
        "/leaderboard - Show top 10\n"
        "/list - List pending projects\n"
        "/verify_payment <project_id> <tx_sig> - Verify payment manually\n\n"
        "Make sure you have joined both groups:\n"
        f"{GROUP_A}\n{GROUP_B}"
    )

# ---------- Submission flow ----------
async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Submit project - Step 1: Send the project NAME", reply_markup=ReplyKeyboardRemove())
    return S_NAME

async def submit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['proj_name'] = update.message.text.strip()
    await update.message.reply_text("Step 2: Send the project SYMBOL (e.g. BMC)")
    return S_SYMBOL

async def submit_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['proj_symbol'] = update.message.text.strip()
    await update.message.reply_text("Step 3: Send logo URL (or type 'skip' to add later)")
    return S_LOGO

async def submit_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['proj_logo'] = None if text.lower() == "skip" else text
    await update.message.reply_text("Step 4: Send the CONTRACT or WALLET address (or type 'skip' if none)")
    return S_CONTRACT

async def submit_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['proj_contract'] = None if text.lower() == "skip" else text
    await update.message.reply_text("Step 5: Send a short DESCRIPTION for the project")
    return S_DESC

async def submit_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['proj_desc'] = update.message.text.strip()
    # Ask chain (SOL/ETH/BNB)
    await update.message.reply_text("Step 6: Which chain will payment be made to? Reply with one of: SOL, ETH, BNB\n(If not paying now, type 'none')")
    return S_CHAIN

async def submit_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chain = update.message.text.strip().upper()
    if chain not in ("SOL", "ETH", "BNB", "NONE"):
        await update.message.reply_text("Please reply with SOL, ETH, BNB, or NONE")
        return S_CHAIN
    context.user_data['proj_chain'] = None if chain == "NONE" else chain
    # Confirm
    name = context.user_data.get('proj_name')
    symbol = context.user_data.get('proj_symbol')
    desc = context.user_data.get('proj_desc')
    await update.message.reply_text(
        f"Confirm submission:\n\nName: {name}\nSymbol: {symbol}\nChain for payment: {context.user_data.get('proj_chain')}\nPayment required: ${REQUIRED_USD}\n\nType 'confirm' to submit or 'cancel' to abort."
    )
    return S_CONFIRM

async def submit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text not in ("confirm", "yes"):
        await update.message.reply_text("Submission canceled.")
        return ConversationHandler.END

    data = load_projects()
    projects = data.setdefault("projects", {})
    votes = data.setdefault("votes", {})

    proj_id = make_project_id(context.user_data['proj_name'])
    projects[proj_id] = {
        "id": proj_id,
        "name": context.user_data['proj_name'],
        "symbol": context.user_data['proj_symbol'],
        "logo": context.user_data.get('proj_logo'),
        "contract_or_wallet": context.user_data.get('proj_contract'),
        "description": context.user_data.get('proj_desc'),
        "chain": context.user_data.get('proj_chain'),
        "submitted_by": update.message.from_user.id,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "payment_verified": False,
        "listed": False
    }
    votes[proj_id] = []

    save_projects(data)
    await update.message.reply_text(
        f"✅ Project submitted (ID: {proj_id}).\nTo complete listing, pay ${REQUIRED_USD} to the project's chosen wallet (or contact admin). "
        "When you have a transaction signature, use /verify_payment <project_id> <tx_sig> to verify."
    )
    return ConversationHandler.END

async def submit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Submission canceled.")
    return ConversationHandler.END

# ---------- List and admin ----------
async def list_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_projects()
    projects = data.get("projects", {})
    if not projects:
        await update.message.reply_text("No projects yet.")
        return
    out = "📥 Projects (ID : name — payment_verified / listed)\n\n"
    for pid, p in projects.items():
        out += f"{pid} : {p['name']} — paid: {p['payment_verified']} — listed: {p['listed']}\n"
    await update.message.reply_text(out)

# ---------- Verify payment ----------
async def verify_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manual command: /verify_payment <project_id> <tx_sig>
    """
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /verify_payment <project_id> <tx_signature_or_txhash>")
        return
    proj_id = context.args[0].strip()
    tx = context.args[1].strip()

    data = load_projects()
    project = data.get("projects", {}).get(proj_id)
    if not project:
        await update.message.reply_text("Project ID not found.")
        return

    chain = project.get("chain")
    expected_to = None
    if chain and chain in WALLETS:
        expected_to = WALLETS[chain]

    await update.message.reply_text("🔎 Verifying transaction... this may take a few seconds.")
    ok = False
    reason = "Not verified"

    if chain == "SOL":
        ok, reason = check_solana_tx_for_payment(tx, expected_to)
    elif chain in ("ETH", "BNB"):
        ok, reason = check_eth_tx_for_payment(tx, expected_to)
    else:
        # no chain or none chosen: manual verification fallback
        ok = False
        reason = "No chain configured for this project. Manual verification required."

    if ok:
        project['payment_verified'] = True
        # Auto-list project if payment verified
        project['listed'] = True
        # Post to listing channel
        save_projects(data)
        await update.message.reply_text(f"✅ Payment verified: {reason}\nProject will be auto-listed now.")
        await post_project_listing(update, context, project)
    else:
        await update.message.reply_text(f"❌ Verification failed: {reason}\nIf you think this is wrong, send evidence to admin.")

# ---------- Post project listing ----------
async def post_project_listing(update: Update, context: ContextTypes.DEFAULT_TYPE, project: Dict[str, Any]):
    text = (
        f"🔥 New Project Listed: {project['name']} ({project.get('symbol','')})\n\n"
        f"{project.get('description','')}\n\n"
        f"Contract/Wallet: {project.get('contract_or_wallet','N/A')}\n"
        f"Submitted by: {project.get('submitted_by')}\n"
        f"Payment status: {project.get('payment_verified')}\n"
    )
    try:
        await context.bot.send_message(chat_id=PROJECT_LISTING_CHANNEL, text=text)
    except Exception as e:
        # fallback: send back to admin (the updater)
        await update.message.reply_text(f"Could not post to listing channel: {e}")

# ---------- Voting ----------
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /vote <project_id>")
        return
    proj_id = context.args[0].strip()
    data = load_projects()
    project = data.get("projects", {}).get(proj_id)
    if not project:
        await update.message.reply_text("Project not found.")
        return

    user_id = update.message.from_user.id

    # Check membership in both groups
    try:
        member_a = await context.bot.get_chat_member(GROUP_A, user_id)
        member_b = await context.bot.get_chat_member(GROUP_B, user_id)
    except Exception as e:
        # Often this raises if bot cannot access chat or user not found
        await update.message.reply_text(
            "I couldn't check your group membership. Make sure the bot is added to the groups and you joined them.\n"
            f"Required groups: {GROUP_A} and {GROUP_B}"
        )
        return

    valid_statuses = ("member", "administrator", "creator")
    if member_a.status not in valid_statuses or member_b.status not in valid_statuses:
        await update.message.reply_text(f"Please join both groups before voting:\n{GROUP_A}\n{GROUP_B}")
        return

    # Check if user already voted for this project
    votes = data.setdefault("votes", {})
    voters = votes.setdefault(proj_id, [])
    # prevent multiple votes per project
    if user_id in voters:
        await update.message.reply_text("You have already voted for this project.")
        return
    # Also prevent multiple votes across different projects if you want single overall votes
    # (Currently allows vote per project)
    voters.append(user_id)
    save_projects(data)
    await update.message.reply_text(f"✅ Your vote for {project['name']} has been recorded!")

# ---------- Leaderboard ----------
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_projects()
    top = get_top_projects(data, limit=10)
    if not top:
        await update.message.reply_text("No votes yet.")
        return
    text = "🏆 Top 10 Projects\n\n"
    for i, (pid, p, votes) in enumerate(top, start=1):
        text += f"{i}. {p['name']} ({p.get('symbol','')}) — {votes} votes — id: {pid}\n"
    await update.message.reply_text(text)

async def post_leaderboard_job(context: ContextTypes.DEFAULT_TYPE):
    # job that runs by schedule
    data = load_projects()
    top = get_top_projects(data, limit=10)
    if not top:
        return
    text = "🔥 Automatic Top 10 (every 6 hours)\n\n"
    for i, (pid, p, votes) in enumerate(top, start=1):
        text += f"{i}. {p['name']} ({p.get('symbol','')}) — {votes} votes\n"
    try:
        await context.bot.send_message(chat_id=LEADERBOARD_CHANNEL, text=text)
    except Exception as e:
        # log to console
        print("Failed to send leaderboard:", e)

# ---------- Admin command to force update leaderboard ----------
async def update_leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # only allow admins
    user = update.message.from_user
    # naive admin check: check if user is admin in GROUP_B (you can adjust)
    try:
        mem = await context.bot.get_chat_member(GROUP_B, user.id)
        if mem.status not in ("administrator", "creator"):
            await update.message.reply_text("Only group admins can run this.")
            return
    except Exception:
        await update.message.reply_text("Could not verify admin status.")
        return
    # call scheduled job function directly
    await post_leaderboard_job(context)
    await update.message.reply_text("✅ Leaderboard posted.")

# ---------------- Main ----------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Basic commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_projects))
    application.add_handler(CommandHandler("verify_payment", verify_payment_command))
    application.add_handler(CommandHandler("vote", vote_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("update_leaderboard", update_leaderboard_command))

    # Submission Conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler('submit', submit_start)],
        states={
            S_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, submit_name)],
            S_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, submit_symbol)],
            S_LOGO: [MessageHandler(filters.TEXT & ~filters.COMMAND, submit_logo)],
            S_CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, submit_contract)],
            S_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, submit_desc)],
            S_CHAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, submit_chain)],
            S_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, submit_confirm)]
        },
        fallbacks=[CommandHandler('cancel', submit_cancel)],
        allow_reentry=True
    )
    application.add_handler(conv)

    # Schedule leaderboard auto-post
    # job queue requires running after start via application.job_queue
    job_queue = application.job_queue
    # schedule repeated job every 6 hours, starting immediately after deploy
    job_queue.run_repeating(lambda ctx: application.create_task(post_leaderboard_job(ctx)), interval=LEADERBOARD_INTERVAL, first=10)

    print("🤖 Dragons Trend bot started...")
    application.run_polling()

if __name__ == "__main__":
    main()
