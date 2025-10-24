import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- simple data storage ---
projects = {}  # {project_name: votes}
user_votes = {}  # {user_id: project_name}


# --- COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐉 Welcome to Dragons Trend!\n\n"
        "🔥 Here you can vote for your favorite projects.\n\n"
        "To vote, use: /vote <project_name>\n"
        "To see leaderboard: /leaderboard"
    )

async def vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) == 0:
        await update.message.reply_text("⚠️ Please enter a project name. Example: /vote Bitmart")
        return

    user_id = update.message.from_user.id
    project_name = context.args[0].capitalize()

    # check if user already voted
    if user_id in user_votes:
        await update.message.reply_text("❌ You’ve already voted.")
        return

    projects[project_name] = projects.get(project_name, 0) + 1
    user_votes[user_id] = project_name
    await update.message.reply_text(f"✅ You voted for {project_name}!")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not projects:
        await update.message.reply_text("No votes yet.")
        return

    sorted_projects = sorted(projects.items(), key=lambda x: x[1], reverse=True)
    leaderboard_text = "🏆 *Top 10 Projects*\n\n"
    for i, (name, votes) in enumerate(sorted_projects[:10], start=1):
        leaderboard_text += f"{i}. {name} — {votes} votes\n"

    await update.message.reply_text(leaderboard_text, parse_mode="Markdown")


# --- MAIN ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("vote", vote))
    app.add_handler(CommandHandler("leaderboard", leaderboard))

    print("🤖 Bot started successfully...")
    app.run_polling()

if __name__ == "__main__":
    main()
