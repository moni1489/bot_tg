import asyncio
import json
import logging
import os
from aiogram import Bot
import asyncpg
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CHAT_ID = -1003974667084 # @FunkoStopChat

async def main():
    bot_token = os.getenv("BOT_TOKEN")
    db_url = os.getenv("DATABASE_URL")
    if not bot_token or not db_url:
        logging.error("Missing BOT_TOKEN or DATABASE_URL in .env")
        return

    bot = Bot(token=bot_token)
    conn = await asyncpg.connect(db_url)
    
    try:
        rows = await conn.fetch("SELECT telegram_id, packs_count, completed_tasks FROM card_users")
        logging.info(f"Loaded {len(rows)} users from card_users")

        awarded_count = 0
        already_had_count = 0
        not_in_chat_count = 0
        errors_count = 0

        for idx, row in enumerate(rows, 1):
            tg_id = row["telegram_id"]
            packs = row["packs_count"]
            completed = []
            try:
                completed = json.loads(row["completed_tasks"] or "[]")
            except Exception:
                completed = []

            if "join_chat" in completed:
                already_had_count += 1
                continue

            try:
                member = await bot.get_chat_member(chat_id=CHAT_ID, user_id=tg_id)
                status = member.status if isinstance(member.status, str) else member.status.value
                if status not in ["left", "kicked", "banned"]:
                    # Member is in chat!
                    completed.append("join_chat")
                    new_packs = packs + 1
                    await conn.execute(
                        "UPDATE card_users SET packs_count = $1, completed_tasks = $2 WHERE telegram_id = $3",
                        new_packs, json.dumps(completed), tg_id
                    )
                    awarded_count += 1
                    logging.info(f"[{idx}/{len(rows)}] Awarded +1 pack to {tg_id} (packs: {packs} -> {new_packs})")
                    
                    try:
                        await bot.send_message(
                            tg_id,
                            "✅ Вы состоите в нашем чате FunkoStop!\n🎁 Вам начислен <b>+1 пак</b> за задание «Вступить в беседу»!",
                            parse_mode="HTML"
                        )
                    except Exception as msg_err:
                        logging.debug(f"Could not send DM to {tg_id}: {msg_err}")
                else:
                    not_in_chat_count += 1
            except Exception as e:
                # User might not exist or blocked bot
                errors_count += 1
                logging.debug(f"User {tg_id} check error: {e}")

            # Avoid Telegram rate limits
            await asyncio.sleep(0.05)

        logging.info("=" * 40)
        logging.info(f"DONE! Summary:")
        logging.info(f"Total checked: {len(rows)}")
        logging.info(f"Already had task completed: {already_had_count}")
        logging.info(f"Newly awarded in chat: {awarded_count}")
        logging.info(f"Not in chat: {not_in_chat_count}")
        logging.info(f"Errors / non-members: {errors_count}")
        logging.info("=" * 40)

    finally:
        await conn.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
