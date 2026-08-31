import asyncio
import logging
import os
import random
import string
import asyncpg
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, 
    KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove, WebAppInfo, MenuButtonWebApp, MenuButtonDefault
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import aiohttp
from aiohttp import web

load_dotenv()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bot-tg-uyoe.onrender.com/cards")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в файле .env")
if not DATABASE_URL:
    raise ValueError("Не найден DATABASE_URL в файле .env (нужна ссылка на PostgreSQL)")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

class CaptchaMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if hasattr(event, "chat") and event.chat and event.chat.type in ["group", "supergroup"]:
            # If it's a new member message, we don't intercept it here, we let the handler do it
            if event.new_chat_members:
                return await handler(event, data)
                
            async with pool.acquire() as db:
                captcha_entry = await db.fetchrow(
                    "SELECT welcome_msg_id, prompt_msg_id FROM group_captcha WHERE user_id = $1 AND chat_id = $2",
                    event.from_user.id, event.chat.id
                )
                if captcha_entry:
                    msg_text = event.text or event.caption or ""
                    emoji_match = ("🛑" in msg_text)
                    if not emoji_match and event.sticker and event.sticker.emoji:
                        emoji_match = ("🛑" in event.sticker.emoji)
                        
                    if emoji_match:
                        # Passed
                        try:
                            await bot.delete_message(event.chat.id, captcha_entry['welcome_msg_id'])
                        except Exception as e:
                            logging.error(f"Failed to delete welcome msg: {e}")
                        if captcha_entry['prompt_msg_id']:
                            try:
                                await bot.delete_message(event.chat.id, captcha_entry['prompt_msg_id'])
                            except Exception as e:
                                logging.error(f"Failed to delete prompt msg: {e}")
                        try:
                            await event.delete()
                        except Exception as e:
                            logging.error(f"Failed to delete user emoji msg (maybe user is admin?): {e}")
                        
                        await db.execute("DELETE FROM group_captcha WHERE user_id = $1 AND chat_id = $2", event.from_user.id, event.chat.id)
                        return # Solved
                    else:
                        # Failed/Spam
                        try:
                            await event.delete()
                        except Exception as e:
                            logging.error(f"Failed to delete spam msg (maybe user is admin?): {e}")
                        return # Stop propagation
        return await handler(event, data)

router.message.outer_middleware(CaptchaMiddleware())

pool = None

cards_dir = os.path.join(os.path.dirname(__file__), "cards_app")

def safe_int(val, default=0):
    try:
        if val is None: return default
        s = str(val).strip()
        if s in ("", "undefined", "null", "NaN"): return default
        return int(s)
    except Exception:
        return default

async def get_card_profile(request):
    try:
        tg_id = safe_int(request.query.get("tg_id"))
        username = request.query.get("username")
        first_name = request.query.get("first_name")
        
        if not tg_id:
            return web.json_response({
                "packs_count": 3,
                "last_daily_pack": None,
                "user_cards": {},
                "completed_tasks": [],
                "ref_count": 0,
                "bot_username": "funkostop_bot",
                "is_admin": False
            })
        
        async with pool.acquire() as db:
            if username and username != "player" or first_name and first_name != "Игрок":
                # Only update if it's real data from WebApp, not fallback
                db_username = username if username and username != "player" else None
                db_first_name = first_name if first_name and first_name != "Игрок" else None
                
                await db.execute("""
                    INSERT INTO card_users (telegram_id, username, first_name, packs_count)
                    VALUES ($1, $2, $3, 3)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = COALESCE($2, NULLIF(card_users.username, 'player'), card_users.username),
                        first_name = COALESCE($3, NULLIF(card_users.first_name, 'Игрок'), card_users.first_name)
                """, tg_id, db_username, db_first_name)
            else:
                await db.execute("INSERT INTO card_users (telegram_id, packs_count) VALUES ($1, 3) ON CONFLICT DO NOTHING", tg_id)

            user = await db.fetchrow("SELECT packs_count, last_daily_pack, completed_tasks FROM card_users WHERE telegram_id = $1", tg_id)
            if not user:
                packs_count = 3
                last_daily = None
                completed_tasks = []
            else:
                packs_count = user["packs_count"]
                if user["last_daily_pack"]:
                    ld = user["last_daily_pack"]
                    if hasattr(ld, 'tzinfo') and ld.tzinfo is not None:
                        last_daily = ld.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        last_daily = ld.strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    last_daily = None

                try:
                    completed_tasks = json.loads(user["completed_tasks"] or "[]")
                except Exception:
                    completed_tasks = []
                
            cards_rows = await db.fetch("SELECT series_slug, card_index, count FROM user_cards WHERE telegram_id = $1", tg_id)
            user_cards = {f"{r['series_slug']}_{r['card_index']}": r['count'] for r in cards_rows}
            
            # Count referred friends
            ref_count = await db.fetchval("SELECT COUNT(*) FROM card_users WHERE referred_by = $1", tg_id) or 0

            bot_username = "funkostop_bot"
            try:
                bot_info = await bot.get_me()
                if bot_info and bot_info.username:
                    bot_username = bot_info.username
            except Exception:
                pass

            is_adm = await is_admin(tg_id)
            
            # Beta testers have access to the game without being admins
            # BETA_TESTERS = [8908317814]
            # if not is_adm and tg_id not in BETA_TESTERS:
            #     return web.json_response({
            #         "error": "not_admin",
            #         "message": "Игра находится на стадии тестирования и пока доступна только администраторам."
            #     }, status=403)

            # Check channel subscription
            is_sub = False
            try:
                member = await bot.get_chat_member(chat_id="@FunkoStop", user_id=tg_id)
                status = member.status.value if hasattr(member.status, 'value') else member.status
                if status not in ["left", "kicked", "banned"]:
                    is_sub = True
            except Exception as e:
                logging.error(f"Error checking sub: {e}")
                
            if not is_sub and not is_adm:
                return web.json_response({
                    "error": "not_subscribed",
                    "message": "Для участия в игре необходимо быть подписанным на наш Telegram канал @FunkoStop!"
                }, status=403)

            drop_settings = await get_drop_settings()
            return web.json_response({
                "packs_count": packs_count,
                "last_daily_pack": last_daily,
                "user_cards": user_cards,
                "completed_tasks": completed_tasks,
                "ref_count": ref_count,
                "bot_username": bot_username,
                "is_admin": is_adm,
                "drop_settings": drop_settings
            })
    except Exception as e:
        logging.error(f"get_card_profile error: {e}")
        return web.json_response({
            "packs_count": 3,
            "last_daily_pack": None,
            "user_cards": {},
            "completed_tasks": [],
            "ref_count": 0,
            "bot_username": "funkostop_bot",
            "drop_settings": DEFAULT_DROP_SETTINGS
        })

async def claim_task_reward(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 0))
        task_id = body.get("task_id")
        
        if not tg_id or not task_id:
            return web.json_response({"error": "Invalid params"}, status=400)
            
        async with pool.acquire() as db:
            user = await db.fetchrow("SELECT packs_count, completed_tasks FROM card_users WHERE telegram_id = $1", tg_id)
            if not user:
                return web.json_response({"error": "Пользователь не найден"}, status=404)
                
            completed = []
            try:
                completed = json.loads(user["completed_tasks"] or "[]")
            except:
                pass
                
            if task_id in completed:
                return web.json_response({"success": False, "message": "Задание уже выполнено"})
                
            # Verify Telegram Channel Subscription (@FunkoStop)
            if task_id == 'tg_sub':
                try:
                    member = await bot.get_chat_member(chat_id="@FunkoStop", user_id=tg_id)
                    status = member.status if isinstance(member.status, str) else member.status.value
                    if status not in ["member", "administrator", "creator"]:
                        return web.json_response({
                            "success": False, 
                            "message": "Вы еще не подписались на канал @FunkoStop! Подпишитесь и попробуйте снова."
                        })
                except Exception as sub_err:
                    logging.warning(f"Sub check warning: {sub_err}")
                    return web.json_response({
                        "success": False, 
                        "message": "❌ Не удалось проверить подписку. Убедитесь, что бот является администратором канала @FunkoStop."
                    })
            
            # Verify Order > 2000 Rubles in Database
            elif task_id == 'order_2000':
                order = await db.fetchrow("""
                    SELECT o.id FROM orders o
                    JOIN clients c ON o.client_id = c.id
                    WHERE c.user_tg_id = $1 AND o.total_price >= 2000
                """, tg_id)
                if not order:
                    return web.json_response({
                        "success": False, 
                        "message": "У вас пока нет оформленных и оплаченных заказов от 2000 рублей."
                    })

            completed.append(task_id)
            reward_count = 3 if task_id == 'order_2000' else 1
            new_count = user["packs_count"] + reward_count
            
            await db.execute(
                "UPDATE card_users SET packs_count = $1, completed_tasks = $2 WHERE telegram_id = $3", 
                new_count, json.dumps(completed), tg_id
            )
            return web.json_response({"success": True, "packs_count": new_count, "completed_tasks": completed})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def claim_daily_pack(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 0))
        if not tg_id:
            return web.json_response({"error": "tg_id missing"}, status=400)
            
        async with pool.acquire() as db:
            user = await db.fetchrow("SELECT packs_count, last_daily_pack FROM card_users WHERE telegram_id = $1", tg_id)
            if not user:
                await db.execute("INSERT INTO card_users (telegram_id, packs_count, last_daily_pack) VALUES ($1, 6, CURRENT_TIMESTAMP)", tg_id)
                return web.json_response({"success": True, "packs_count": 6})
                
            last_daily = user["last_daily_pack"]
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            
            if last_daily:
                if hasattr(last_daily, 'tzinfo') and last_daily.tzinfo is not None:
                    last_daily = last_daily.astimezone(timezone.utc).replace(tzinfo=None)
                
                elapsed = (now - last_daily).total_seconds()
                if elapsed < 86400:
                    seconds_left = int(86400 - elapsed)
                    return web.json_response({
                        "success": False, 
                        "message": "Ежедневный пак уже получен!", 
                        "seconds_left": seconds_left,
                        "packs_count": user["packs_count"]
                    })
                
            new_count = user["packs_count"] + 1
            await db.execute("UPDATE card_users SET packs_count = $1, last_daily_pack = CURRENT_TIMESTAMP, daily_notified = FALSE WHERE telegram_id = $2", new_count, tg_id)
            return web.json_response({"success": True, "packs_count": new_count})
    except Exception as e:
        logging.error(f"Daily pack error: {e}")
        return web.json_response({"error": str(e)}, status=500)



SERIES_CONFIG = [
    {
        "slug": 'breaking_bad',
        "cards": [
            { "index": 1, "rarity": 'legendary' },
            { "index": 2, "rarity": 'common' },
            { "index": 3, "rarity": 'rare' },
            { "index": 4, "rarity": 'epic' }
        ]
    },
    {
        "slug": 'stranger_things',
        "cards": [
            { "index": 1, "rarity": 'common' },
            { "index": 2, "rarity": 'rare' },
            { "index": 3, "rarity": 'epic' },
            { "index": 4, "rarity": 'legendary' }
        ]
    },
    {
        "slug": 'resident_evil',
        "cards": [
            { "index": 1, "rarity": 'common' },
            { "index": 2, "rarity": 'rare' },
            { "index": 3, "rarity": 'epic' },
            { "index": 4, "rarity": 'legendary' }
        ]
    },
    {
        "slug": 'death_note',
        "cards": [
            { "index": 1, "rarity": 'common' },
            { "index": 2, "rarity": 'rare' },
            { "index": 3, "rarity": 'epic' },
            { "index": 4, "rarity": 'legendary' }
        ]
    },
    {
        "slug": 'invincible',
        "cards": [
            { "index": 1, "rarity": 'common' },
            { "index": 2, "rarity": 'rare' },
            { "index": 3, "rarity": 'epic' },
            { "index": 4, "rarity": 'legendary' }
        ]
    },
    {
        "slug": 'one_piece',
        "cards": [
            { "index": 1, "rarity": 'common' },
            { "index": 2, "rarity": 'rare' },
            { "index": 3, "rarity": 'epic' },
            { "index": 4, "rarity": 'legendary' }
        ]
    },
    {
        "slug": 'universal',
        "cards": [
            { "index": 1, "rarity": 'legendary' },
            { "index": 2, "rarity": 'legendary' },
            { "index": 3, "rarity": 'legendary' },
            { "index": 4, "rarity": 'legendary' }
        ]
    }
]

async def craft_cards_api(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 0))
        cards = body.get("cards", [])
        
        if not tg_id or len(cards) != 4:
            return web.json_response({"error": "Invalid params"}, status=400)
            
        async with pool.acquire() as db:
            # 1. Check if user has all cards
            # and deduct them
            async with db.transaction():
                rarities = []
                for c_key in cards:
                    parts = c_key.split('_')
                    c_idx = int(parts.pop())
                    s_slug = "_".join(parts)
                    
                    row = await db.fetchrow("SELECT count FROM user_cards WHERE telegram_id = $1 AND series_slug = $2 AND card_index = $3", tg_id, s_slug, c_idx)
                    if not row or row["count"] < 1:
                        return web.json_response({"error": f"Missing card {c_key}"}, status=400)
                    
                    # Deduct
                    await db.execute("UPDATE user_cards SET count = count - 1 WHERE telegram_id = $1 AND series_slug = $2 AND card_index = $3", tg_id, s_slug, c_idx)
                    
                    # Find rarity
                    for sc in SERIES_CONFIG:
                        if sc["slug"] == s_slug:
                            for cc in sc["cards"]:
                                if cc["index"] == c_idx:
                                    rarities.append(cc["rarity"])
                                    break
                                    
                # 2. Determine new rarity based on rules with post-series penalty multiplier
                # Check if user has completed any series
                user_cards_rows = await db.fetch("SELECT series_slug, card_index, count FROM user_cards WHERE telegram_id = $1", tg_id)
                has_completed_code = await db.fetchval("SELECT 1 FROM series_codes WHERE telegram_id = $1 LIMIT 1", tg_id)
                has_completed_series = bool(has_completed_code)

                if not has_completed_series and user_cards_rows:
                    user_cards_map = {f"{r['series_slug']}_{r['card_index']}": r['count'] for r in user_cards_rows}
                    for sc in SERIES_CONFIG:
                        if sc["slug"] == "bonus_card": continue
                        if all(user_cards_map.get(f"{sc['slug']}_{cc['index']}", 0) > 0 for cc in sc["cards"]):
                            has_completed_series = True
                            break

                drop_settings = await get_drop_settings()
                penalty_pct = float(drop_settings.get("series_penalty", 67.0))
                chance_multiplier = max(0.05, (100.0 - penalty_pct) / 100.0) if has_completed_series else 1.0

                import random
                counts = {"common": 0, "rare": 0, "epic": 0, "legendary": 0}
                for r in rarities: counts[r] += 1
                
                n_c = counts["common"]
                n_r = counts["rare"]
                n_e = counts["epic"]
                n_l = counts["legendary"]

                rand = random.uniform(0, 100)
                new_rarity = "common"

                if n_c == 4:
                    # 4× Common → Rare 30% * multiplier, rest Common (70%)
                    rare_chance = 30.0 * chance_multiplier
                    if rand <= rare_chance: new_rarity = "rare"
                    else:                   new_rarity = "common"

                elif n_r == 4:
                    # 4× Rare → Epic 30% * multiplier, rest Rare
                    epic_chance = 30.0 * chance_multiplier
                    if rand <= epic_chance: new_rarity = "epic"
                    else:                   new_rarity = "rare"

                elif n_e == 4:
                    # 4× Epic → Legendary 20% * multiplier, rest Epic
                    leg_chance = 20.0 * chance_multiplier
                    if rand <= leg_chance: new_rarity = "legendary"
                    else:                  new_rarity = "epic"

                elif n_l == 4:
                    new_rarity = "legendary"

                elif n_l > 0:
                    # Наборы с легендарками (1-3 леги)
                    leg_chance = min(75.0, 25.0 * n_l) * chance_multiplier
                    if rand <= leg_chance: new_rarity = "legendary"
                    else:                  new_rarity = "epic"

                elif n_e > 0:
                    # Смеси с Эпиками (без лег):
                    leg_chance = (4.0 * n_e if n_e < 3 else 14.0) * chance_multiplier
                    epic_chance = leg_chance + (35.0 + 15.0 * n_e + 5.0 * n_r) * chance_multiplier
                    if rand <= leg_chance:
                        new_rarity = "legendary"
                    elif rand <= epic_chance:
                        new_rarity = "epic"
                    else:
                        if n_c >= 2 and random.uniform(0, 100) <= 30.0:
                            new_rarity = "common"
                        else:
                            new_rarity = "rare"

                else:
                    # Смеси только Common + Rare (без эпиков и без лег)
                    # 0% шанс на Legendary!
                    if n_r == 1:
                        # 3C + 1R
                        epic_chance = 5.0 * chance_multiplier
                        rare_chance = epic_chance + (50.0 * chance_multiplier)
                        if rand <= epic_chance:    new_rarity = "epic"
                        elif rand <= rare_chance:  new_rarity = "rare"
                        else:                      new_rarity = "common"
                    elif n_r == 2:
                        # 2C + 2R
                        epic_chance = 12.0 * chance_multiplier
                        rare_chance = epic_chance + (58.0 * chance_multiplier)
                        if rand <= epic_chance:    new_rarity = "epic"
                        elif rand <= rare_chance:  new_rarity = "rare"
                        else:                      new_rarity = "common"
                    elif n_r == 3:
                        # 1C + 3R
                        epic_chance = 22.0 * chance_multiplier
                        rare_chance = epic_chance + (68.0 * chance_multiplier)
                        if rand <= epic_chance:    new_rarity = "epic"
                        elif rand <= rare_chance:  new_rarity = "rare"
                        else:                      new_rarity = "common"

                # 3. Pick random card of that rarity
                matching = []
                for sc in SERIES_CONFIG:
                    if sc["slug"] == "bonus_card": continue
                    for cc in sc["cards"]:
                        if cc["rarity"] == new_rarity:
                            matching.append((sc["slug"], cc["index"]))
                            
                if not matching:
                    matching.append(("breaking_bad", 1)) # Fallback
                
                s_slug, c_idx = random.choice(matching)
                
                # 4. Give new card
                await db.execute("""
                    INSERT INTO user_cards (telegram_id, series_slug, card_index, count)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (telegram_id, series_slug, card_index)
                    DO UPDATE SET count = user_cards.count + 1
                """, tg_id, s_slug, c_idx)
                
                return web.json_response({
                    "success": True, 
                    "series": s_slug, 
                    "card_index": c_idx, 
                    "rarity": new_rarity
                })
                
    except Exception as e:
        import logging
        logging.error(f"Craft error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def open_card_pack(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 0))
        series_slug = body.get("series_slug")
        card_index = int(body.get("card_index", 1))
        
        if not tg_id or not series_slug:
            return web.json_response({"error": "Invalid params"}, status=400)
            
        async with pool.acquire() as db:
            # Bonus cards are awarded for free alongside regular cards — skip pack check
            if series_slug == 'bonus_card':
                await db.execute("""
                    INSERT INTO user_cards (telegram_id, series_slug, card_index, count)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (telegram_id, series_slug, card_index)
                    DO UPDATE SET count = user_cards.count + 1
                """, tg_id, series_slug, card_index)
                return web.json_response({"success": True})

            user = await db.fetchrow("SELECT packs_count, referred_by, inviter_rewarded FROM card_users WHERE telegram_id = $1", tg_id)
            if not user or user["packs_count"] < 1:
                return web.json_response({"error": "Недостаточно паков"}, status=403)

            await db.execute("UPDATE card_users SET packs_count = packs_count - 1 WHERE telegram_id = $1", tg_id)
            await db.execute("""
                INSERT INTO user_cards (telegram_id, series_slug, card_index, count)
                VALUES ($1, $2, $3, 1)
                ON CONFLICT (telegram_id, series_slug, card_index)
                DO UPDATE SET count = user_cards.count + 1
            """, tg_id, series_slug, card_index)
            
            # If this user was invited, check if this is their first non-bonus pack open
            # Only reward inviter ONCE (inviter_rewarded guards against double-payout)
            inviter_id = user.get("referred_by")
            inviter_rewarded = user.get("inviter_rewarded", False)
            if inviter_id and not inviter_rewarded and series_slug != 'bonus_card':
                total_cards = await db.fetchval(
                    "SELECT SUM(count) FROM user_cards WHERE telegram_id = $1 AND series_slug != 'bonus_card'", tg_id
                )
                if total_cards == 1:  # This is the very first card
                    await db.execute("""
                        INSERT INTO card_users (telegram_id, packs_count) VALUES ($1, 1)
                        ON CONFLICT (telegram_id) DO UPDATE SET packs_count = card_users.packs_count + 1
                    """, inviter_id)
                    # Mark so we never pay this inviter again for this user
                    await db.execute("UPDATE card_users SET inviter_rewarded = TRUE WHERE telegram_id = $1", tg_id)
                    try:
                        await bot.send_message(inviter_id, "🎉 По вашей ссылке зарегистрировался друг и открыл первый пак! Вам начислен <b>+1 пак</b>!", parse_mode="HTML")
                    except Exception:
                        pass
            
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def claim_bonus_packs_api(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 0))
        bonus_id = int(body.get("bonus_id", 0))
        packs_to_add = int(body.get("packs", body.get("count", 0)))
        
        if not tg_id or not packs_to_add:
            return web.json_response({"error": "Invalid params"}, status=400)
            
        async with pool.acquire() as db:
            await db.execute("UPDATE card_users SET packs_count = packs_count + $1 WHERE telegram_id = $2", packs_to_add, tg_id)
            new_count = await db.fetchval("SELECT packs_count FROM card_users WHERE telegram_id = $1", tg_id)
            
        return web.json_response({"success": True, "packs_count": new_count})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def serve_cards_app(request):
    index_path = os.path.join(cards_dir, "index.html")
    if os.path.exists(index_path):
        return web.FileResponse(index_path)
    return web.Response(text="index.html not found", status=404)

async def health_check(request):
    return web.Response(text="Bot & Cards Mini App is alive!")

async def give_test_packs_api(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 0))
        if not tg_id:
            return web.json_response({"error": "tg_id missing"}, status=400)
        async with pool.acquire() as db:
            await db.execute("""
                INSERT INTO card_users (telegram_id, packs_count)
                VALUES ($1, 10)
                ON CONFLICT (telegram_id)
                DO UPDATE SET packs_count = card_users.packs_count + 10
            """, tg_id)
            new_count = await db.fetchval("SELECT packs_count FROM card_users WHERE telegram_id = $1", tg_id)
        return web.json_response({"success": True, "packs_count": new_count})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def reset_daily_test_api(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 0))
        if not tg_id:
            return web.json_response({"error": "tg_id missing"}, status=400)
        async with pool.acquire() as db:
            await db.execute("UPDATE card_users SET last_daily_pack = NULL WHERE telegram_id = $1", tg_id)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def generate_series_code(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 0))
        series_slug = body.get("series_slug")
        
        if not tg_id or not series_slug:
            return web.json_response({"error": "Invalid params"}, status=400)
            
        async with pool.acquire() as db:
            # Verify user has all 4 cards of this series
            cards_count = await db.fetchval("SELECT COUNT(*) FROM user_cards WHERE telegram_id = $1 AND series_slug = $2 AND count > 0", tg_id, series_slug)
            if cards_count < 4:
                return web.json_response({"error": "Серия не собрана полностью!"}, status=400)
                
            existing_code = await db.fetchval("SELECT code FROM series_codes WHERE telegram_id = $1 AND series_slug = $2", tg_id, series_slug)
            if existing_code:
                return web.json_response({"success": True, "code": existing_code})
                
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            await db.execute("INSERT INTO series_codes (telegram_id, series_slug, code) VALUES ($1, $2, $3)", tg_id, series_slug, code)
            
            # Get all admin IDs (from .env + from database)
            admin_targets = set(ADMIN_IDS)
            db_admins = await db.fetch("SELECT user_tg_id FROM users WHERE role = 'admin' AND user_tg_id IS NOT NULL")
            for r in db_admins:
                admin_targets.add(r['user_tg_id'])
                
            # Send notification to admins
            for admin_id in admin_targets:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=f"🎁 Игрок [{tg_id}](tg://user?id={tg_id}) собрал серию **{series_slug.upper()}**!\n\nСгенерирован код: `{code}`",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                    
            return web.json_response({"success": True, "code": code})
    except Exception as e:
        logging.error(f"Generate code error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def notify_order_status_api(request):
    try:
        body = await request.json()
        order_id = int(body.get("order_id", 0))
        new_status = body.get("status", "")
        
        if not order_id or not new_status:
            return web.json_response({"error": "Missing order_id or status"}, status=400)
            
        order_details = await get_order_details(order_id)
        if order_details and order_details.get('user_tg_id'):
            client_tg_id = order_details['user_tg_id']
            # We assume the DB is already updated by the website, so order_details has the NEW status
            # If not, we can format the message manually:
            order_details['status'] = new_status
            notify_msg = format_status_notification(order_details)
            
            if order_details.get('photo_id'):
                await bot.send_photo(chat_id=client_tg_id, photo=order_details['photo_id'], caption=notify_msg, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id=client_tg_id, text=notify_msg, parse_mode="Markdown")
                
            return web.json_response({"success": True, "message": f"Notified user {client_tg_id}"})
        else:
            return web.json_response({"error": "Order or linked user not found"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def start_webserver():
    images_dir = os.path.join(cards_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    
    # Copy official logo if present
    logo_src = r"C:\Users\kraie\.gemini\antigravity-ide\brain\e27b6ef9-e81b-4a8e-980a-b4b4d8458b05\media__1785225617410.png"
    logo_dst = os.path.join(cards_dir, "logo.png")
    if os.path.exists(logo_src):
        try:
            import shutil
            shutil.copy(logo_src, logo_dst)
            shutil.copy(logo_src, os.path.join(images_dir, "logo.png"))
        except Exception:
            pass
            
    # Rename bonus cards if needed
    if os.path.exists(images_dir):
        for i in range(1, 9):
            old_path = os.path.join(images_dir, f"bonus_card ({i}).png")
            new_path = os.path.join(images_dir, f"bonus_card_{i}.png")
            if os.path.exists(old_path) and not os.path.exists(new_path):
                try:
                    os.rename(old_path, new_path)
                except:
                    pass

    # Slice 7 series frames if available

    frames_src = r"C:\Users\kraie\.gemini\antigravity-ide\brain\e27b6ef9-e81b-4a8e-980a-b4b4d8458b05\media__1785321073494.png"
    if os.path.exists(frames_src):
        try:
            from PIL import Image
            img = Image.open(frames_src)
            w, h = img.size
            card_w = w / 4
            card_h = h / 2
            
            names_r1 = ["breaking_bad", "stranger_things", "dc", "death_note"]
            for i, name in enumerate(names_r1):
                box = (int(i * card_w), 0, int((i + 1) * card_w), int(card_h))
                cropped = img.crop(box)
                cropped.save(os.path.join(images_dir, f"frame_{name}.png"))
                cropped.save(os.path.join(cards_dir, f"frame_{name}.png"))
                
            card_w2 = w / 3
            names_r2 = ["invincible", "one_piece", "universal"]
            for i, name in enumerate(names_r2):
                box = (int(i * card_w2), int(card_h), int((i + 1) * card_w2), int(h))
                cropped = img.crop(box)
                cropped.save(os.path.join(images_dir, f"frame_{name}.png"))
                cropped.save(os.path.join(cards_dir, f"frame_{name}.png"))
        except Exception as e:
            logging.error(f"Frame slicing error: {e}")

    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/cards', serve_cards_app)
    app.router.add_get('/cards/', serve_cards_app)
    app.router.add_get('/api/cards/profile', get_card_profile)
    app.router.add_post('/api/cards/claim_daily', claim_daily_pack)
    app.router.add_post('/api/cards/open', open_card_pack)
    app.router.add_post('/api/cards/craft', craft_cards_api)
    app.router.add_post('/api/cards/tasks/claim', claim_task_reward)
    app.router.add_post('/api/cards/give_test_packs', give_test_packs_api)
    app.router.add_post('/api/cards/reset_daily_test', reset_daily_test_api)
    app.router.add_post('/api/cards/generate_code', generate_series_code)
    app.router.add_post('/api/cards/claim_bonus_packs', claim_bonus_packs_api)
    app.router.add_post('/api/cards/claim_prize', claim_prize_api)
    app.router.add_post('/api/notify_order_status', notify_order_status_api)
    
    if os.path.exists(cards_dir):
        app.router.add_static('/cards', cards_dir)
        
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 7860))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"🌐 Веб-сервер и Cards Mini App запущены на порту {port}")

async def daily_notification_task():
    """Background task: notifies users when their 24h daily pack timer is ready."""
    logging.info("Daily notification task started")
    while True:
        try:
            await asyncio.sleep(300)  # Check every 5 minutes
            async with pool.acquire() as db:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                users = await db.fetch("""
                    SELECT telegram_id FROM card_users
                    WHERE last_daily_pack IS NOT NULL
                      AND daily_notified = FALSE
                      AND EXTRACT(EPOCH FROM ($1 - last_daily_pack)) >= 86400
                """, now)
                logging.info(f"Daily task: found {len(users)} users to notify")
                for row in users:
                    tg_id = row['telegram_id']
                    try:
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🎁 Открыть игру",
                                web_app=WebAppInfo(url=WEBAPP_URL)
                            )]
                        ])
                        await bot.send_message(
                            tg_id,
                            "Вам доступен ежедневный пак! 🎁\nЗаходите в игру и забирайте его, пока он не пропал!",
                            reply_markup=kb
                        )
                        await db.execute("UPDATE card_users SET daily_notified = TRUE WHERE telegram_id = $1", tg_id)
                        logging.info(f"Daily notify sent to {tg_id}")
                    except Exception as e:
                        err_str = str(e).lower()
                        # If user blocked bot or chat not found — mark done, won't fix itself
                        if "blocked" in err_str or "chat not found" in err_str or "user is deactivated" in err_str:
                            await db.execute("UPDATE card_users SET daily_notified = TRUE WHERE telegram_id = $1", tg_id)
                            logging.warning(f"Daily notify skipped (blocked/not found) for {tg_id}: {e}")
                        else:
                            # Temporary error — don't mark, retry next cycle
                            logging.error(f"Daily notify error for {tg_id}: {e}")
                            
                # --- GROUP CAPTCHA KICK LOGIC ---
                expired_users = await db.fetch("""
                    SELECT user_id, chat_id, welcome_msg_id, prompt_msg_id 
                    FROM group_captcha 
                    WHERE EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - join_time)) >= 3600
                """)
                for row in expired_users:
                    try:
                        # Kick user (ban and unban)
                        await bot.ban_chat_member(chat_id=row['chat_id'], user_id=row['user_id'])
                        await bot.unban_chat_member(chat_id=row['chat_id'], user_id=row['user_id'])
                        
                        # Delete welcome message
                        if row['welcome_msg_id']:
                            try:
                                await bot.delete_message(chat_id=row['chat_id'], message_id=row['welcome_msg_id'])
                            except Exception:
                                pass
                        if row['prompt_msg_id']:
                            try:
                                await bot.delete_message(chat_id=row['chat_id'], message_id=row['prompt_msg_id'])
                            except Exception:
                                pass
                                
                        # Delete from db
                        await db.execute("DELETE FROM group_captcha WHERE user_id = $1 AND chat_id = $2", row['user_id'], row['chat_id'])
                        logging.info(f"Kicked {row['user_id']} from {row['chat_id']} due to captcha timeout")
                    except Exception as e:
                        logging.error(f"Error kicking user {row['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Daily notification task error: {e}")
            await asyncio.sleep(60)

# --- CURRENCY ---
async def get_usd_rate():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.cbr.ru/scripts/XML_daily.asp") as resp:
                xml_data = await resp.text()
                root = ET.fromstring(xml_data)
                for valute in root.findall("Valute"):
                    if valute.attrib["ID"] == "R01235":
                        value_str = valute.find("Value").text
                        return float(value_str.replace(",", "."))
    except Exception as e:
        logging.error(f"Error fetching CBRF rate: {e}")
    return 100.0

# --- FSM ---
class CreateOrder(StatesGroup):
    waiting_for_client_id = State()
    waiting_for_items = State()
    waiting_for_total_price = State()
    waiting_for_paid_amount = State()
    waiting_for_photo = State()

class CheckStatus(StatesGroup):
    waiting_for_id = State()
    waiting_for_password = State()

class CheckArchive(StatesGroup):
    waiting_for_id = State()
    waiting_for_password = State()

class UpdatePayment(StatesGroup):
    waiting_for_new_paid = State()

class ParseLink(StatesGroup):
    waiting_for_weight = State()

class CheckPlayerCollection(StatesGroup):
    waiting_for_id = State()

class CheckCode(StatesGroup):
    waiting_for_code = State()

class GivePacks(StatesGroup):
    waiting_for_input = State()

class TakePacksFromPlayer(StatesGroup):
    waiting_for_input = State()

class ResetPlayerAccount(StatesGroup):
    waiting_for_input = State()

class CreatePaymentLink(StatesGroup):
    waiting_for_desc = State()
    waiting_for_amount = State()

class EditDropRate(StatesGroup):
    waiting_for_legendary = State()
    waiting_for_epic = State()
    waiting_for_rare = State()
    waiting_for_penalty = State()


# --- DATABASE ---
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                user_tg_id BIGINT,
                login_id TEXT UNIQUE,
                password TEXT,
                role TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                password TEXT,
                user_tg_id BIGINT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                client_id INTEGER REFERENCES clients(id),
                items TEXT,
                total_price INTEGER,
                paid_amount INTEGER,
                status TEXT,
                photo_id TEXT,
                archived BOOLEAN DEFAULT FALSE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS card_users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                packs_count INTEGER DEFAULT 3,
                last_daily_pack TIMESTAMP,
                ref_code TEXT,
                referred_by BIGINT,
                completed_tasks TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            await db.execute("ALTER TABLE card_users ADD COLUMN completed_tasks TEXT DEFAULT '[]'")
        except asyncpg.exceptions.DuplicateColumnError:
            pass
        try:
            await db.execute("ALTER TABLE card_users ADD COLUMN daily_notified BOOLEAN DEFAULT FALSE")
        except asyncpg.exceptions.DuplicateColumnError:
            pass
        try:
            await db.execute("ALTER TABLE card_users ADD COLUMN inviter_rewarded BOOLEAN DEFAULT FALSE")
        except asyncpg.exceptions.DuplicateColumnError:
            pass
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_cards (
                telegram_id BIGINT,
                series_slug TEXT,
                card_index INTEGER,
                count INTEGER DEFAULT 1,
                PRIMARY KEY (telegram_id, series_slug, card_index)
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS series_codes (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT,
                series_slug TEXT,
                code TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (telegram_id, series_slug)
            )
        """)
        
        try:
            await db.execute("ALTER TABLE series_codes ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        except asyncpg.exceptions.DuplicateColumnError:
            pass
            
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_captcha (
                user_id BIGINT,
                chat_id BIGINT,
                join_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                welcome_msg_id BIGINT,
                prompt_msg_id BIGINT,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        try:
            await db.execute("ALTER TABLE group_captcha ADD COLUMN prompt_msg_id BIGINT")
        except asyncpg.exceptions.DuplicateColumnError:
            pass
        
        # Admin check
        admin = await db.fetchrow("SELECT id FROM users WHERE role = 'admin'")
        if not admin:
            try:
                await db.execute(
                    "INSERT INTO users (login_id, password, role) VALUES ($1, $2, $3)",
                    "admin", "admin123", "admin"
                )
            except asyncpg.exceptions.UniqueViolationError:
                pass

DEFAULT_DROP_SETTINGS = {
    "legendary_rate": 1.5,
    "epic_rate": 5.0,
    "rare_rate": 26.0,
    "series_penalty": 67.0 # % reduction after 1+ completed series (e.g. 67% reduction = multiplier 0.33)
}

async def get_drop_settings() -> dict:
    try:
        async with pool.acquire() as db:
            val = await db.fetchval("SELECT value FROM game_settings WHERE key = 'drop_settings'")
            if val:
                d = json.loads(val)
                res = DEFAULT_DROP_SETTINGS.copy()
                res.update(d)
                return res
    except Exception as e:
        logging.error(f"Error fetching drop settings: {e}")
    return DEFAULT_DROP_SETTINGS.copy()

async def save_drop_settings(data: dict):
    async with pool.acquire() as db:
        await db.execute("""
            INSERT INTO game_settings (key, value)
            VALUES ('drop_settings', $1)
            ON CONFLICT (key) DO UPDATE SET value = $1
        """, json.dumps(data))

async def is_admin(user_tg_id: int) -> bool:
    if user_tg_id in ADMIN_IDS:
        return True
    async with pool.acquire() as db:
        row = await db.fetchrow("SELECT id FROM users WHERE user_tg_id = $1 AND role = 'admin'", user_tg_id)
        return row is not None

async def authenticate_admin(login_id: str, password: str, user_tg_id: int) -> bool:
    async with pool.acquire() as db:
        admin = await db.fetchrow("SELECT id FROM users WHERE login_id = $1 AND password = $2 AND role = 'admin'", login_id, password)
        if admin:
            await db.execute("UPDATE users SET user_tg_id = $1 WHERE id = $2", user_tg_id, admin['id'])
            return True
        return False

async def create_client_db(password: str) -> int:
    async with pool.acquire() as db:
        return await db.fetchval("INSERT INTO clients (password) VALUES ($1) RETURNING id", password)

async def check_client(client_id: int) -> bool:
    async with pool.acquire() as db:
        row = await db.fetchrow("SELECT id FROM clients WHERE id = $1", client_id)
        return row is not None

async def create_order(client_id: int, items: str, total_price: int, paid_amount: int, photo_id: str) -> int:
    async with pool.acquire() as db:
        order_date = datetime.now().strftime("%d.%m.%Y")
        return await db.fetchval(
            "INSERT INTO orders (client_id, items, total_price, paid_amount, status, photo_id, archived, order_date) VALUES ($1, $2, $3, $4, $5, $6, FALSE, $7) RETURNING id",
            client_id, items, total_price, paid_amount, "Заказ принят в обработку", photo_id, order_date
        )

async def get_all_orders():
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT id, items, status FROM orders WHERE archived = FALSE")
        return [tuple(r) for r in rows]

async def get_archived_orders():
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT id, items, status FROM orders WHERE archived = TRUE")
        return [tuple(r) for r in rows]

async def get_all_clients():
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT id, password FROM clients")
        return [tuple(r) for r in rows]

async def get_client_orders(client_id: int, password: str):
    async with pool.acquire() as db:
        user = await db.fetchrow("SELECT id FROM clients WHERE id = $1 AND password = $2", client_id, password)
        if not user:
            return None
        rows = await db.fetch("SELECT id, items, total_price, paid_amount, status, photo_id FROM orders WHERE client_id = $1 AND archived = FALSE", client_id)
        return [tuple(r) for r in rows]

async def get_client_archived_orders(client_id: int, password: str):
    async with pool.acquire() as db:
        user = await db.fetchrow("SELECT id FROM clients WHERE id = $1 AND password = $2", client_id, password)
        if not user:
            return None
        rows = await db.fetch("SELECT id, items, total_price, paid_amount, status, photo_id FROM orders WHERE client_id = $1 AND archived = TRUE", client_id)
        return [tuple(r) for r in rows]

async def update_order_status(order_id: int, new_status: str):
    async with pool.acquire() as db:
        archived = (new_status == "Выдано")
        await db.execute("UPDATE orders SET status = $1, archived = $2 WHERE id = $3", new_status, archived, order_id)

async def update_order_payment(order_id: int, paid_amount: int):
    async with pool.acquire() as db:
        await db.execute("UPDATE orders SET paid_amount = $1 WHERE id = $2", paid_amount, order_id)

async def bind_client_tg_id(client_id: int, user_tg_id: int):
    async with pool.acquire() as db:
        await db.execute("UPDATE clients SET user_tg_id = $1 WHERE id = $2", user_tg_id, client_id)

async def get_client_tg_id_by_order(order_id: int):
    async with pool.acquire() as db:
        res = await db.fetchrow("""
            SELECT c.user_tg_id 
            FROM orders o
            JOIN clients c ON o.client_id = c.id
            WHERE o.id = $1
        """, order_id)
        if res and res['user_tg_id']:
            return res['user_tg_id']
        return None

async def get_order_details(order_id: int):
    async with pool.acquire() as db:
        res = await db.fetchrow("""
            SELECT o.id, o.items, o.total_price, o.paid_amount, o.status, o.photo_id, c.user_tg_id 
            FROM orders o
            JOIN clients c ON o.client_id = c.id
            WHERE o.id = $1
        """, order_id)
        if res:
            return dict(res)
        return None

async def delete_order_db(order_id: int):
    async with pool.acquire() as db:
        await db.execute("DELETE FROM orders WHERE id = $1", order_id)

async def unarchive_order_db(order_id: int):
    async with pool.acquire() as db:
        await db.execute("UPDATE orders SET archived = FALSE WHERE id = $1", order_id)

# --- KEYBOARDS ---
BETA_TESTERS = [8908317814]

def get_start_kb(user_id=None):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🧮 Калькулятор стоимости")],
            [KeyboardButton(text="📦 Отследить заказы"), KeyboardButton(text="🗃 Архив заказов")]
        ],
        resize_keyboard=True
    )
    
    return kb

def get_admin_kb(user_id=None):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Список клиентов")],
            [KeyboardButton(text="👤 Создать клиента"), KeyboardButton(text="➕ Добавить заказ")],
            [KeyboardButton(text="🔄 Изменить статус заказа"), KeyboardButton(text="💰 Изменить оплату")],
            [KeyboardButton(text="🗃 Архив заказов (Админ)")],
            [KeyboardButton(text="💳 Создать ссылку на оплату")],
            [KeyboardButton(text="🎮 Админка Игры")]
        ],
        resize_keyboard=True
    )

def get_game_admin_kb(user_id=None):
    url = f"{WEBAPP_URL}?tg_id={user_id}" if user_id else WEBAPP_URL
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎴 Играть в Funko Cards", web_app=WebAppInfo(url=url))],
            [KeyboardButton(text="📊 Статистика Игры"), KeyboardButton(text="🔍 Проверить Игрока")],
            [KeyboardButton(text="🎲 Шансы дропа"), KeyboardButton(text="🎫 Проверить код")],
            [KeyboardButton(text="🎁 Выдать паки"), KeyboardButton(text="📤 Забрать паки")],
            [KeyboardButton(text="🔄 Сброс аккаунта"), KeyboardButton(text="🎫 Все промокоды")],
            [KeyboardButton(text="🎁 Выдать приз"), KeyboardButton(text="🔙 Назад в гл. меню")]
        ],
        resize_keyboard=True
    )

def get_cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )

def get_orders_kb(orders, action="status"):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for order in orders:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"Заказ #{order[0]} - {order[1][:15]}", callback_data=f"{action}_{order[0]}")
        ])
    return kb

STATUSES = [
    "Заказ принят в обработку",
    "Заказ ожидает отправки из магазина",
    "Заказ едет на склад США",
    "Заказ начал сортировку на складе США",
    "Заказ отправлен из США на наш склад в Россию",
    "Ожидает выхода в продажу",
    "Заказ проходит таможенное оформление",
    "Заказ прибыл в магазин и готов к выдаче",
    "Выдано"
]

STATUS_METADATA = {
    "Заказ принят в обработку": {
        "emoji": "📝",
        "desc": "Ваш заказ успешно принят в обработку и оформляется."
    },
    "Заказ ожидает отправки из магазина": {
        "emoji": "📦",
        "desc": "Заказ выкуплен и ожидает отправки со склада магазина."
    },
    "Заказ едет на склад США": {
        "emoji": "🚚",
        "desc": "Магазин отправил посылку. Заказ находится в пути на наш склад в США."
    },
    "Заказ начал сортировку на складе США": {
        "emoji": "🏢",
        "desc": "Посылка поступила на наш склад в США и проходит обработку перед отправкой."
    },
    "Заказ отправлен из США на наш склад в Россию": {
        "emoji": "✈️",
        "desc": "Упаковка завершена и посылка отправлена в РФ."
    },
    "Ожидает выхода в продажу": {
        "emoji": "⏳",
        "desc": "Товар оформлен и ожидает официального релиза / выхода в продажу."
    },
    "Заказ проходит таможенное оформление": {
        "emoji": "🛃",
        "desc": "Посылка прибыла на границу и проходит таможенное оформление."
    },
    "Заказ прибыл в магазин и готов к выдаче": {
        "emoji": "🏠",
        "desc": "Ваш заказ приехал в магазин. Обратитесь к менеджеру для его получения. Обязательно обращайтесь прикрепляя скриншот данного уведомления!"
    },
    "Выдано": {
        "emoji": "✅",
        "desc": "Заказ успешно вручен! Спасибо, что выбираете FunkoSTOP!"
    }
}

def format_status_notification(order: dict) -> str:
    status_name = order['status']
    meta = STATUS_METADATA.get(status_name, {
        "emoji": "📦",
        "desc": "Статус вашего заказа обновлен."
    })
    
    order_id = order['id']
    items = order['items']
    total_price = order['total_price'] or 0
    paid_amount = order['paid_amount'] or 0
    remaining = total_price - paid_amount
    
    msg = f"**Изменение статуса!**\n"
    msg += f"Наименование позиции: {items}\n"
    msg += f"Track - ID: {order_id}\n"
    msg += f"Новый статус: {status_name} {meta['emoji']}\n\n"
    msg += f"{meta['desc']}\n"
    
    if remaining > 0:
        msg += f"\nОстаток доплаты за заказ: {remaining}р."
        
    return msg

def get_status_kb(order_id):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for i, status in enumerate(STATUSES):
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=status, callback_data=f"setstatus_{order_id}_{i}")
        ])
    return kb

def get_admin_archive_kb(order_id):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Восстановить (Убрать из архива)", callback_data=f"unarchive_{order_id}")],
        [InlineKeyboardButton(text="🗑 Удалить навсегда", callback_data=f"delete_{order_id}")]
    ])
    return kb

def get_skip_photo_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Пропустить фото")]],
        resize_keyboard=True
    )

def generate_random_password(length=6):
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

# --- HANDLERS ---
@router.message(F.new_chat_members)
async def welcome_new_member(message: Message):
    async with pool.acquire() as db:
        for new_member in message.new_chat_members:
            if new_member.is_bot:
                continue
                
            welcome_text = f"""{new_member.mention_html()}, добро пожаловать! 

Основные правила:

- Аккуратно делимся ссылками 
- Без рекламы и навязчивого спама
- Продажа / обмен - только там, где это разрешено администрацией
- Уважаем участников и не устраиваем конфликты
- Изучайте содержание чата
- Администраторы всевластны. Их решения окончательны, обжалованию не подлежат"""
            
            prompt_text = "<b>Если согласен, отправь эмодзи «🛑»</b>"
            try:
                # Try to delete the system "joined group" message
                try:
                    await message.delete()
                except Exception:
                    pass
                
                welcome_msg = await message.answer(welcome_text, parse_mode="HTML")
                prompt_msg = await message.answer(prompt_text, parse_mode="HTML")
                
                await db.execute(
                    "INSERT INTO group_captcha (user_id, chat_id, welcome_msg_id, prompt_msg_id) VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (user_id, chat_id) DO UPDATE SET join_time = CURRENT_TIMESTAMP, welcome_msg_id = EXCLUDED.welcome_msg_id, prompt_msg_id = EXCLUDED.prompt_msg_id",
                    new_member.id, message.chat.id, welcome_msg.message_id, prompt_msg.message_id
                )
            except Exception as e:
                logging.error(f"Error in welcome_new_member: {e}")

@router.message(F.text == "❌ Отмена", StateFilter("*"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    if await is_admin(message.from_user.id):
        await message.answer("Действие отменено.", reply_markup=get_game_admin_kb(message.from_user.id))
    else:
        await message.answer("Действие отменено.", reply_markup=get_start_kb(message.from_user.id))

@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    
    # Handle Referral deep link (e.g. /start ref_12345)
    args = message.text.split()
    is_referral = False
    
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            inviter_id = int(args[1].replace("ref_", ""))
            if inviter_id != message.from_user.id:
                async with pool.acquire() as db:
                    # Only truly new users (not in DB at all) can use referral links
                    user = await db.fetchrow("SELECT telegram_id FROM card_users WHERE telegram_id = $1", message.from_user.id)
                    
                    if not user:
                        # Check subscription before giving referral bonus
                        is_sub = False
                        try:
                            member = await bot.get_chat_member(chat_id="@FunkoStop", user_id=message.from_user.id)
                            status = member.status.value if hasattr(member.status, 'value') else member.status
                            if status not in ["left", "kicked", "banned"]:
                                is_sub = True
                        except Exception as e:
                            logging.error(f"Error checking sub in start: {e}")
                            
                        is_adm = await is_admin(message.from_user.id)
                        
                        if not is_sub and not is_adm:
                            await message.answer("⚠️ Чтобы получить бонус по реферальной ссылке (и начать играть), **сначала подпишитесь на наш канал** @FunkoStop!\n\nПосле подписки нажмите на ссылку друга еще раз.", parse_mode="Markdown")
                            return
                        
                        # Brand new user: give 3 base + 1 referral bonus = 4 packs, store pending inviter
                        await db.execute(
                            "INSERT INTO card_users (telegram_id, username, first_name, packs_count, referred_by) VALUES ($1, $2, $3, 4, $4)",
                            message.from_user.id, message.from_user.username, message.from_user.first_name, inviter_id
                        )
                        is_referral = True
                        await message.answer("🎉 Вы зарегистрировались по приглашению и получили бонусный <b>+1 пак</b>!\n\nОткройте хотя бы 1 пак, чтобы активировать бонус вашему другу!", parse_mode="HTML")
                    else:
                        await message.answer("ℹ️ Бонус за приглашение получают только новые игроки. Вы уже зарегистрированы!")
        except Exception as e:
            logging.error(f"Ref error: {e}")

    # Ensure user is in db if not created by referral
    if not is_referral:
        async with pool.acquire() as db:
            await db.execute("""
                INSERT INTO card_users (telegram_id, username, first_name, packs_count)
                VALUES ($1, $2, $3, 3)
                ON CONFLICT (telegram_id) DO UPDATE SET 
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name
            """, message.from_user.id, message.from_user.username, message.from_user.first_name)

    if await is_admin(message.from_user.id):
        await message.answer("Добро пожаловать в панель администратора!", reply_markup=get_admin_kb(message.from_user.id))
    else:
        await message.answer("Добро пожаловать в Личный Кабинет! Нажмите кнопку ниже, чтобы проверить свои заказы.", reply_markup=get_start_kb(message.from_user.id))

@router.message(Command("myid"), StateFilter("*"))
async def myid_cmd(message: Message):
    uid = message.from_user.id
    uname = message.from_user.username or "нет username"
    fname = message.from_user.first_name or "нет имени"
    is_adm = await is_admin(uid)
    await message.answer(
        f"🆔 **Ваш Telegram ID:** `{uid}`\n"
        f"👤 Имя: {fname}\n"
        f"@ Username: @{uname}\n"
        f"🔑 Вы админ: {'✅ Да' if is_adm else '❌ Нет'}",
        parse_mode="Markdown"
    )

@router.message(Command("admin"), StateFilter("*"))
async def admin_cmd(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора. Войдите через /admin_login [логин] [пароль]")
        return
    await message.answer("🔑 Панель администратора", reply_markup=get_admin_kb(message.from_user.id))


@router.message(Command("reset_daily"), StateFilter("*"))
async def reset_daily_cmd(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    try:
        async with pool.acquire() as db:
            await db.execute("UPDATE card_users SET last_daily_pack = NULL WHERE telegram_id = $1", message.from_user.id)
        await message.answer("✅ Ваш ежедневный подарок сброшен! Зайдите в игру — кнопка снова будет в состоянии **ГОТОВО**.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@router.message(Command("give_packs", "give", "packs"), StateFilter("*"))
async def give_packs_cmd(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) == 2:
        # /give_packs 10 -> gives 10 packs to current user
        target_id = message.from_user.id
        count = int(args[1])
    elif len(args) >= 3:
        # /give_packs [id] [count]
        target_id = int(args[1])
        count = int(args[2])
    else:
        await message.answer("⚠️ Использование: `/give_packs 10` (выдать себе 10 паков)\nИли `/give_packs 123456 10` (выдать другому)", parse_mode="Markdown")
        return
    try:
        async with pool.acquire() as db:
            await db.execute("""
                INSERT INTO card_users (telegram_id, packs_count)
                VALUES ($1, $2)
                ON CONFLICT (telegram_id)
                DO UPDATE SET packs_count = card_users.packs_count + $2
            """, target_id, count)
        await message.answer(f"✅ Успешно выдано **+{count} паков** пользователю `{target_id}`!", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@router.message(Command("check", "c"), StateFilter("*"))
async def check_code_cmd(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
        
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Использование: `/check A1B2C3D4`", parse_mode="Markdown")
        return
        
    code = args[1].upper()
    
    async with pool.acquire() as db:
        record = await db.fetchrow("SELECT telegram_id, series_slug, created_at FROM series_codes WHERE code = $1", code)
        
    if record:
        tg_id = record['telegram_id']
        series = record['series_slug'].upper()
        date = record['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        
        await message.answer(
            f"✅ **Код действителен!**\n\n"
            f"👤 Игрок: [ID: {tg_id}](tg://user?id={tg_id})\n"
            f"🎁 Серия: **{series}**\n"
            f"📅 Сгенерирован: {date}",
            parse_mode="Markdown"
        )
    else:
        await message.answer(f"❌ **Код `{code}` не найден!** Возможно, он недействителен или написан с ошибкой.", parse_mode="Markdown")

@router.message(Command("admin_login"))
async def admin_login_start(message: Message, state: FSMContext):
    await state.clear()
    args = message.text.split()
    if len(args) == 3:
        login, password = args[1], args[2]
        if await authenticate_admin(login, password, message.from_user.id):
            await message.answer("Авторизация успешна. Вы добавлены как администратор.", reply_markup=get_admin_kb(message.from_user.id))
        else:
            await message.answer("Неверный логин или пароль администратора.")
    else:
        await message.answer("Использование: /admin_login [логин] [пароль]")

@router.message(Command("admin_logout"))
@router.message(Command("logout"))
async def admin_logout_cmd(message: Message, state: FSMContext):
    await state.clear()
    async with pool.acquire() as db:
        await db.execute("UPDATE users SET user_tg_id = NULL WHERE user_tg_id = $1 AND role = 'admin'", message.from_user.id)
    await message.answer("🚪 Вы вышли из режима администратора. Теперь вы видите меню обычного пользователя.", reply_markup=get_start_kb(message.from_user.id))

@router.message(Command("add_admin"))
async def add_new_admin(message: Message):
    if not await is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) == 3:
        new_login, new_pass = args[1], args[2]
        async with pool.acquire() as db:
            try:
                await db.execute("INSERT INTO users (login_id, password, role) VALUES ($1, $2, $3)", new_login, new_pass, "admin")
                await message.answer(f"✅ Новый админ успешно создан!\nЛогин: `{new_login}`\nПароль: `{new_pass}`\n\nПередайте эти данные вашему партнеру, чтобы он отправил команду:\n`/admin_login {new_login} {new_pass}`", parse_mode="Markdown")
            except asyncpg.exceptions.UniqueViolationError:
                await message.answer("❌ Админ с таким логином уже существует!")
    else:
        await message.answer("Использование: `/add_admin [новый_логин] [новый_пароль]`", parse_mode="Markdown")

@router.message(Command("logout"))
async def admin_logout(message: Message, state: FSMContext):
    await state.clear()
    async with pool.acquire() as db:
        await db.execute("UPDATE users SET user_tg_id = NULL WHERE user_tg_id = $1 AND role = 'admin'", message.from_user.id)
    
    if message.from_user.id in ADMIN_IDS:
        await message.answer("⚠️ Ваш ID прописан в конфигурационном файле (вы Супер-Админ). Для вас админка будет открыта всегда, выйти нельзя.", reply_markup=get_admin_kb())
    else:
        await message.answer("✅ Вы успешно вышли из панели администратора.", reply_markup=get_start_kb())

# --- ADMIN: CREATE CLIENT ---
@router.message(F.text == "👤 Создать клиента", StateFilter("*"))
async def add_client_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
        
    password = generate_random_password(6)
    client_id = await create_client_db(password)
    
    await message.answer(
        f"✅ Клиент успешно создан!\n\n"
        f"🆔 **Номер клиента:** `{client_id}`\n"
        f"🔑 **Пароль:** `{password}`\n\n"
        f"Теперь вы можете добавлять заказы на этот Номер клиента.",
        parse_mode="Markdown", reply_markup=get_admin_kb()
    )
    await state.clear()

# --- ADMIN: LIST CLIENTS ---
@router.message(F.text == "👥 Список клиентов", StateFilter("*"))
async def list_clients(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    clients = await get_all_clients()
    if not clients:
        await message.answer("Клиентов пока нет.")
        return
        
    response = "👥 **Список всех клиентов:**\n\n"
    for client in clients:
        response += f"🆔 ID: `{client[0]}` | 🔑 Пароль: `{client[1]}`\n"
        
    await message.answer(response, parse_mode="Markdown")

# --- ADMIN: GAME ADMIN MENU ---
@router.message(F.text == "🎮 Админка Игры", StateFilter("*"))
async def game_admin_menu(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Добро пожаловать в админку игры Funko Cards!", reply_markup=get_game_admin_kb(message.from_user.id))

@router.message(F.text == "🔙 Назад в гл. меню", StateFilter("*"))
async def back_to_main_admin(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Возврат в главное меню администратора.", reply_markup=get_admin_kb(message.from_user.id))

@router.message(F.text == "🎫 Проверить код", StateFilter("*"))
async def check_code_btn(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Введите одноразовый промокод игрока (например, A1B2C3D4):", reply_markup=get_cancel_kb())
    await state.set_state(CheckCode.waiting_for_code)

@router.message(CheckCode.waiting_for_code)
async def process_check_code(message: Message, state: FSMContext):
    if not message.text:
        return
    code = message.text.strip().upper()
    try:
        async with pool.acquire() as db:
            record = await db.fetchrow("SELECT telegram_id, series_slug, created_at FROM series_codes WHERE code = $1", code)
            
        if record:
            tg_id = record['telegram_id']
            # Экранируем символы Markdown (особенно _) чтобы избежать ошибки "can't parse entities"
            series = record['series_slug'].upper().replace("_", "\\_").replace("*", "\\*")
            date_obj = record['created_at']
            date = date_obj.strftime('%Y-%m-%d %H:%M:%S') if date_obj else "Неизвестно"
            
            await message.answer(
                f"✅ **Код действителен!**\n\n"
                f"👤 Игрок: [ID: {tg_id}](tg://user?id={tg_id})\n"
                f"🎁 Серия: **{series}**\n"
                f"📅 Сгенерирован: {date}",
                parse_mode="Markdown",
                reply_markup=get_game_admin_kb(message.from_user.id)
            )
        else:
            await message.answer(f"❌ **Код `{code}` не найден!** Возможно, он недействителен или написан с ошибкой.", parse_mode="Markdown", reply_markup=get_game_admin_kb(message.from_user.id))
    except Exception as e:
        await message.answer(f"❌ Ошибка базы данных при проверке кода: {e}", reply_markup=get_game_admin_kb(message.from_user.id))
    await state.clear()

# --- ADMIN: GAME STATS ---
@router.message(F.text == "📊 Статистика Игры", StateFilter("*"))
async def game_stats(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    
    try:
        async with pool.acquire() as db:
            total_players = await db.fetchval("SELECT COUNT(*) FROM card_users") or 0
            total_cards_collected = await db.fetchval("SELECT SUM(count) FROM user_cards") or 0
            total_unique = await db.fetchval("SELECT COUNT(*) FROM user_cards") or 0
            
            leaderboard = await db.fetch("""
                SELECT u.telegram_id, u.username, u.first_name, COUNT(c.series_slug) as unique_cards, SUM(c.count) as total_cards
                FROM card_users u
                LEFT JOIN user_cards c ON u.telegram_id = c.telegram_id
                GROUP BY u.telegram_id, u.username, u.first_name
                ORDER BY unique_cards DESC, total_cards DESC
            """)
            
            top_referrers = await db.fetch("""
                SELECT u.telegram_id, u.username, u.first_name, COUNT(r.telegram_id) as refs_count
                FROM card_users u
                JOIN card_users r ON u.telegram_id = r.referred_by
                GROUP BY u.telegram_id, u.username, u.first_name
                ORDER BY refs_count DESC
            """)
            
        import html as html_mod
        msg_parts = []
        current_msg = f"📊 <b>Статистика Funko Cards</b>\n\n"
        current_msg += f"👥 Всего игроков: <b>{total_players}</b>\n"
        current_msg += f"🎴 Карт выбито (с повторками): <b>{total_cards_collected}</b>\n"
        current_msg += f"🗂 Уникальных позиций: <b>{total_unique}</b>\n\n"
        current_msg += f"🏆 <b>ТОП КОЛЛЕКЦИОНЕРОВ:</b>\n"
        
        if not leaderboard:
            current_msg += "Пока нет данных."
        else:
            medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(leaderboard, 1):
                unique = row['unique_cards'] or 0
                total = row['total_cards'] or 0
                tg_id = row['telegram_id']
                username = row['username'] or ""
                first_name = html_mod.escape(row['first_name'] or "")
                username_esc = html_mod.escape(username)
                
                if first_name and username:
                    name_str = f"{first_name} (@{username_esc})"
                elif first_name:
                    name_str = first_name
                elif username:
                    name_str = f"@{username_esc}"
                else:
                    name_str = f"Без имени"
                    
                medal = medals[i-1] if i <= 3 else f"{i}."
                line = f"{medal} <a href='tg://user?id={tg_id}'>{name_str}</a> [<code>{tg_id}</code>] — {unique} уник. / {total} всего\n"
                
                if len(current_msg) + len(line) > 3900:
                    msg_parts.append(current_msg)
                    current_msg = ""
                current_msg += line
                
        if top_referrers:
            top_refs_title = f"\n🤝 <b>ТОП ПРИГЛАСИТЕЛЕЙ (РЕФЕРАЛОВ):</b>\n"
            if len(current_msg) + len(top_refs_title) > 3900:
                msg_parts.append(current_msg)
                current_msg = top_refs_title
            else:
                current_msg += top_refs_title
                
            for i, row in enumerate(top_referrers, 1):
                refs = row['refs_count']
                tg_id = row['telegram_id']
                username = row['username'] or ""
                first_name = html_mod.escape(row['first_name'] or "")
                username_esc = html_mod.escape(username)
                
                if first_name and username:
                    name_str = f"{first_name} (@{username_esc})"
                elif first_name:
                    name_str = first_name
                elif username:
                    name_str = f"@{username_esc}"
                else:
                    name_str = f"Без имени"
                    
                medal = medals[i-1] if i <= 3 else f"{i}."
                line = f"{medal} <a href='tg://user?id={tg_id}'>{name_str}</a> [<code>{tg_id}</code>] — {refs} реф.\n"
                
                if len(current_msg) + len(line) > 3900:
                    msg_parts.append(current_msg)
                    current_msg = ""
                current_msg += line
                
        if current_msg:
            msg_parts.append(current_msg)
            
        for i, part in enumerate(msg_parts):
            if i == len(msg_parts) - 1:
                await message.answer(part, parse_mode="HTML", reply_markup=get_game_admin_kb(message.from_user.id))
            else:
                await message.answer(part, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка при получении статистики: {e}", reply_markup=get_game_admin_kb(message.from_user.id))


# --- ADMIN: CHECK PLAYER ---
@router.message(F.text == "🔍 Проверить Игрока", StateFilter("*"))
async def check_player_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Введите Telegram ID или @username игрока (можно без @):", reply_markup=get_cancel_kb())
    await state.set_state(CheckPlayerCollection.waiting_for_id)

@router.message(CheckPlayerCollection.waiting_for_id)
async def check_player_collection(message: Message, state: FSMContext):
    try:
        target_id = None
        input_text = message.text.strip()
        
        # Numeric ID
        if input_text.isdigit():
            target_id = int(input_text)
        else:
            # Try username lookup — first in our DB
            username_clean = input_text.lstrip("@")
            async with pool.acquire() as db:
                row = await db.fetchrow("SELECT telegram_id FROM card_users WHERE username ILIKE $1", username_clean)
            if row:
                target_id = row['telegram_id']
            else:
                # Try resolving via Telegram API
                try:
                    chat = await bot.get_chat(f"@{username_clean}")
                    target_id = chat.id
                    # Save the username if we found them via API
                    async with pool.acquire() as db:
                        await db.execute("""
                            INSERT INTO card_users (telegram_id, username, first_name, packs_count)
                            VALUES ($1, $2, $3, 0)
                            ON CONFLICT (telegram_id) DO UPDATE SET
                                username = COALESCE(EXCLUDED.username, card_users.username),
                                first_name = COALESCE(EXCLUDED.first_name, card_users.first_name)
                        """, chat.id, chat.username, chat.first_name)
                except Exception:
                    target_id = None
        
        if target_id is None:
            await message.answer(
                f"❌ Игрок <b>{input_text}</b> не найден.\n\n"
                "Возможные причины:\n"
                "• Он никогда не запускал игру\n"
                "• Username написан неверно\n"
                "• У него нет публичного username — попробуйте по Telegram ID",
                parse_mode="HTML",
                reply_markup=get_game_admin_kb(message.from_user.id)
            )
            await state.clear()
            return
        
        async with pool.acquire() as db:
            user = await db.fetchrow("SELECT telegram_id, packs_count, username, first_name, referred_by FROM card_users WHERE telegram_id = $1", target_id)
            user_exists_in_cards = await db.fetchval("SELECT 1 FROM user_cards WHERE telegram_id = $1 LIMIT 1", target_id)
            
            if not user and not user_exists_in_cards:
                await message.answer(
                    f"❌ Игрок <code>{target_id}</code> найден в Telegram, но <b>ещё не играл</b> в Funko Cards.",
                    parse_mode="HTML",
                    reply_markup=get_game_admin_kb(message.from_user.id)
                )
                await state.clear()
                return
            
            packs_count = user['packs_count'] if user else 0
            username = user['username'] if user else None
            first_name = user['first_name'] if user else None
            ref_count = await db.fetchval("SELECT COUNT(*) FROM card_users WHERE referred_by = $1", target_id) or 0
            referred_by_id = user['referred_by'] if user and 'referred_by' in user.keys() else None
            cards = await db.fetch("SELECT series_slug, card_index, count FROM user_cards WHERE telegram_id = $1", target_id)
        
        import html as html_mod
        fn_esc = html_mod.escape(first_name or "")
        un_esc = html_mod.escape(username or "")
        
        # Build display name
        if fn_esc and un_esc:
            display_name = f"{fn_esc} (@{un_esc})"
        elif fn_esc:
            display_name = fn_esc
        elif un_esc:
            display_name = f"@{un_esc}"
        else:
            display_name = f"ID: {target_id}"
            
        SERIES_TOTALS = 4
        
        msg = f"👤 <b>Игрок: {display_name}</b>\n"
        msg += f"🆔 Telegram ID: <code>{target_id}</code>\n"
        msg += f"📦 Доступно паков: <b>{packs_count}</b>\n"
        msg += f"🤝 Приглашено друзей: {ref_count}\n"
        
        # Who invited this user?
        if referred_by_id:
            async with pool.acquire() as db:
                inviter = await db.fetchrow("SELECT username, first_name FROM card_users WHERE telegram_id = $1", referred_by_id)
            if inviter:
                inv_name = html_mod.escape(inviter['first_name'] or inviter['username'] or str(referred_by_id))
                inv_un = f" (@{html_mod.escape(inviter['username'])})".replace('@None', '') if inviter['username'] else ''
                msg += f"📩 Приглашен пользователем: <b>{inv_name}{inv_un}</b> (<code>{referred_by_id}</code>)\n"
        
        msg += f"\n🎴 <b>Коллекция:</b>\n"
        
        if not cards:
            msg += "Игрок пока не выбил ни одной карты."
        else:
            collection = {}
            for row in cards:
                slug = row['series_slug']
                if slug not in collection:
                    collection[slug] = []
                collection[slug].append((row['card_index'], row['count']))
                
            total_unique_cards = sum(len(v) for v in collection.values())
            total_all_cards = sum(cnt for items in collection.values() for _, cnt in items)
            msg += f"Всего уникальных: <b>{total_unique_cards}</b> | с повторами: <b>{total_all_cards}</b>\n"
                
            for slug, items in collection.items():
                items.sort(key=lambda x: x[0])
                unique_in_series = len(items)
                completed = " ✅" if unique_in_series >= SERIES_TOTALS else ""
                msg += f"\n🔹 <b>{html_mod.escape(slug.upper())}</b> ({unique_in_series}/{SERIES_TOTALS}){completed}\n"
                for idx, cnt in items:
                    extra = f" (x{cnt})" if cnt > 1 else ""
                    msg += f"  — Карточка №{idx}{extra}\n"
        
        # Build inline keyboard with 'show referrals' button if there are any
        reply_markup = get_game_admin_kb(message.from_user.id)
        inline_kb = None
        if ref_count > 0:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            inline_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"👥 Показать рефералов ({ref_count})", callback_data=f"show_refs_{target_id}")]
            ])
        
        await message.answer(msg, parse_mode="HTML", reply_markup=reply_markup)
        if inline_kb:
            await message.answer("👇 Нажмите, чтобы увидеть список приглашённых:", reply_markup=inline_kb)
        await state.clear()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        await message.answer(f"❌ Ошибка в обработчике:\n<code>{html_mod.escape(str(err)[:3000]) if 'html_mod' in locals() else str(err)[:3000]}</code>", parse_mode="HTML", reply_markup=get_game_admin_kb(message.from_user.id))
        await state.clear()

# --- ADMIN: GIVE PACKS ---
@router.message(F.text == "🎁 Выдать паки", StateFilter("*"))
async def give_packs_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer(
        "Введите Telegram ID или @username игрока и количество паков через пробел:\n\n"
        "Пример: `123456789 10` или `@username 10`\n"
        "Или только количество, чтобы выдать себе: `10`",
        parse_mode="Markdown",
        reply_markup=get_cancel_kb()
    )
    await state.set_state(GivePacks.waiting_for_input)

@router.message(GivePacks.waiting_for_input)
async def give_packs_process(message: Message, state: FSMContext):
    parts = message.text.strip().split()
    try:
        if len(parts) == 1:
            target_id = message.from_user.id
            count = int(parts[0])
        elif len(parts) >= 2:
            input_text = parts[0]
            count = int(parts[1])
            
            if input_text.isdigit():
                target_id = int(input_text)
            else:
                username_clean = input_text.lstrip("@")
                target_id = None
                async with pool.acquire() as db:
                    row = await db.fetchrow("SELECT telegram_id FROM card_users WHERE username ILIKE $1", username_clean)
                if row:
                    target_id = row['telegram_id']
                else:
                    try:
                        chat = await bot.get_chat(f"@{username_clean}")
                        target_id = chat.id
                    except Exception:
                        pass
                
                if target_id is None:
                    await message.answer(f"❌ Игрок `{input_text}` не найден.", parse_mode="Markdown", reply_markup=get_game_admin_kb(message.from_user.id))
                    await state.clear()
                    return
        else:
            raise ValueError("Wrong format")
            
        if count <= 0 or count > 9999:
            raise ValueError("Count out of range")
            
        async with pool.acquire() as db:
            await db.execute("""
                INSERT INTO card_users (telegram_id, packs_count)
                VALUES ($1, $2)
                ON CONFLICT (telegram_id) DO UPDATE SET packs_count = card_users.packs_count + $2
            """, target_id, count)
            new_count = await db.fetchval("SELECT packs_count FROM card_users WHERE telegram_id = $1", target_id)
            
        await message.answer(
            f"✅ **Выдано +{count} паков** игроку `{target_id}`\n"
            f"Теперь у него: **{new_count} паков**",
            parse_mode="Markdown",
            reply_markup=get_game_admin_kb(message.from_user.id)
        )
        
        # Try to notify the player
        try:
            if target_id != message.from_user.id:
                await bot.send_message(target_id, f"🎁 Вам выдано **+{count} паков** от администратора! Заходите в игру!", parse_mode="Markdown")
        except Exception:
            pass
            
    except (ValueError, IndexError):
        await message.answer(
            "❌ Неверный формат. Введите:\n`@username 10` — выдать 10 паков\nИли `10` — выдать 10 паков себе",
            parse_mode="Markdown",
            reply_markup=get_game_admin_kb(message.from_user.id)
        )
    await state.clear()

# --- ADMIN: TAKE PACKS ---
@router.message(F.text == "📤 Забрать паки", StateFilter("*"))
async def take_packs_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer(
        "Введите Telegram ID или @username игрока и количество паков через пробел:\n\n"
        "Пример: `123456789 5` или `@username 5`",
        parse_mode="Markdown",
        reply_markup=get_cancel_kb()
    )
    await state.set_state(TakePacksFromPlayer.waiting_for_input)

@router.message(TakePacksFromPlayer.waiting_for_input)
async def take_packs_process(message: Message, state: FSMContext):
    parts = message.text.strip().split()
    try:
        if len(parts) < 2:
            raise ValueError("Need 2 args")
        input_text = parts[0]
        count = int(parts[1])
        
        if input_text.isdigit():
            target_id = int(input_text)
        else:
            username_clean = input_text.lstrip("@")
            async with pool.acquire() as db:
                row = await db.fetchrow("SELECT telegram_id FROM card_users WHERE username ILIKE $1", username_clean)
            target_id = row['telegram_id'] if row else None
            if not target_id:
                try:
                    chat = await bot.get_chat(f"@{username_clean}")
                    target_id = chat.id
                except Exception:
                    pass
            if not target_id:
                await message.answer(f"❌ Игрок `{input_text}` не найден.", parse_mode="Markdown", reply_markup=get_game_admin_kb(message.from_user.id))
                await state.clear()
                return
        
        async with pool.acquire() as db:
            current = await db.fetchval("SELECT packs_count FROM card_users WHERE telegram_id = $1", target_id) or 0
            new_count = max(0, current - count)
            await db.execute("UPDATE card_users SET packs_count = $1 WHERE telegram_id = $2", new_count, target_id)
        
        await message.answer(
            f"✅ У игрока `{target_id}` забрано паков: было {current}, стало **{new_count}**",
            parse_mode="Markdown",
            reply_markup=get_game_admin_kb(message.from_user.id)
        )
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат. Пример: `@username 5`", parse_mode="Markdown", reply_markup=get_game_admin_kb(message.from_user.id))
    await state.clear()

# --- ADMIN: RESET ACCOUNT ---
@router.message(F.text == "🔄 Сброс аккаунта", StateFilter("*"))
async def reset_account_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer(
        "⚠️ СБРОС АККАУНТА — удалит все карты, очищает задания и устанавливает 3 пака.\n\n"
        "Введите Telegram ID или @username игрока:",
        reply_markup=get_cancel_kb()
    )
    await state.set_state(ResetPlayerAccount.waiting_for_input)

@router.message(ResetPlayerAccount.waiting_for_input)
async def reset_account_process(message: Message, state: FSMContext):
    input_text = message.text.strip()
    try:
        if input_text.isdigit():
            target_id = int(input_text)
        else:
            username_clean = input_text.lstrip("@")
            async with pool.acquire() as db:
                row = await db.fetchrow("SELECT telegram_id FROM card_users WHERE username ILIKE $1", username_clean)
            target_id = row['telegram_id'] if row else None
            if not target_id:
                await message.answer(f"❌ Игрок `{input_text}` не найден.", parse_mode="Markdown", reply_markup=get_game_admin_kb(message.from_user.id))
                await state.clear()
                return
        
        async with pool.acquire() as db:
            await db.execute("DELETE FROM user_cards WHERE telegram_id = $1", target_id)
            await db.execute("""
                UPDATE card_users SET 
                    packs_count = 3,
                    completed_tasks = '[]',
                    referred_by = NULL,
                    last_daily_pack = NULL,
                    daily_notified = FALSE
                WHERE telegram_id = $1
            """, target_id)
        
        await message.answer(
            f"✅ Аккаунт `{target_id}` сброшен: 3 пака, все карты удалены, задания сброшены.",
            parse_mode="Markdown",
            reply_markup=get_game_admin_kb(message.from_user.id)
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=get_game_admin_kb(message.from_user.id))
    await state.clear()

# --- ADMIN: ALL CODES ---
@router.message(F.text.contains("Все промокоды"), StateFilter("*"))
async def all_codes(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    async with pool.acquire() as db:
        codes = await db.fetch("""
            SELECT sc.code, sc.series_slug, sc.created_at, cu.username, cu.first_name, sc.telegram_id
            FROM series_codes sc
            LEFT JOIN card_users cu ON sc.telegram_id = cu.telegram_id
            ORDER BY sc.created_at DESC
        """)
    
    if not codes:
        await message.answer("📭 Промокодов пока нет.", reply_markup=get_game_admin_kb(message.from_user.id))
        return
        
    import html as html_mod
    
    chunks = []
    current_chunk = f"🏷 <b>Все промокоды</b> ({len(codes)} шт):\n\n"
    
    for row in codes:
        parts = []
        if row['first_name']:
            parts.append(html_mod.escape(row['first_name']))
        if row['username']:
            parts.append(f"@{html_mod.escape(row['username'])}")
        tg_id = row['telegram_id']
        id_link = f"<a href=\"tg://user?id={tg_id}\">ID:{tg_id}</a>"
        name = " ".join(parts) + f" [{id_link}]" if parts else id_link
        
        date = row['created_at'].strftime('%d.%m %H:%M')
        line = f"<code>{html_mod.escape(row['code'])}</code> — <b>{html_mod.escape(row['series_slug'].upper())}</b> — {name} ({date})\n"
        
        if len(current_chunk) + len(line) > 3800:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk += line
            
    if current_chunk:
        chunks.append(current_chunk)
        
    for i, chunk in enumerate(chunks):
        markup = get_game_admin_kb(message.from_user.id) if i == len(chunks) - 1 else None
        await message.answer(chunk, parse_mode="HTML", reply_markup=markup)


# --- ADMIN: DROP RATES SETTINGS ---
def get_drop_settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟡 Изм. % Легендарных", callback_data="edit_drop_legendary"),
            InlineKeyboardButton(text="🟣 Изм. % Эпических", callback_data="edit_drop_epic")
        ],
        [
            InlineKeyboardButton(text="🔵 Изм. % Редких", callback_data="edit_drop_rare"),
            InlineKeyboardButton(text="📉 Снижение после серии", callback_data="edit_drop_penalty")
        ],
        [
            InlineKeyboardButton(text="🔄 Сбросить на стандартные", callback_data="edit_drop_reset")
        ]
    ])

async def build_drop_settings_text() -> str:
    s = await get_drop_settings()
    leg = float(s.get("legendary_rate", 1.5))
    epic = float(s.get("epic_rate", 5.0))
    rare = float(s.get("rare_rate", 26.0))
    common = max(0.0, round(100.0 - leg - epic - rare, 2))
    penalty = float(s.get("series_penalty", 67.0))
    multiplier = max(0.0, round((100.0 - penalty) / 100.0, 2))

    return (
        "🎲 <b>Настройки шансов дропа из паков</b>\n\n"
        "<b>Текущие базовые шансы:</b>\n"
        f"• 🟡 <b>Легендарные:</b> <code>{leg}%</code>\n"
        f"• 🟣 <b>Эпические:</b> <code>{epic}%</code>\n"
        f"• 🔵 <b>Редкие:</b> <code>{rare}%</code>\n"
        f"• ⚪ <b>Обычные:</b> <code>{common}%</code>\n\n"
        "<b>Снижение после собранной серии:</b>\n"
        f"• 📉 <b>Штраф:</b> <code>-{penalty}%</code> (множитель <code>x{multiplier}</code>)\n"
        f"<i>(После того как игрок собрал 1+ серию, его шансы на редкие карты умножаются на x{multiplier})</i>\n\n"
        "👇 <i>Выберите что хотите изменить:</i>"
    )

@router.message(F.text == "🎲 Шансы дропа", StateFilter("*"))
async def show_drop_settings_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    text = await build_drop_settings_text()
    await message.answer(text, parse_mode="HTML", reply_markup=get_drop_settings_kb())

@router.callback_query(F.data == "edit_drop_legendary")
async def edit_drop_legendary_cb(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return await callback.answer("Нет прав", show_alert=True)
    await callback.message.answer("Введите новый % для <b>Легендарных</b> карт (например: <code>1.5</code> или <code>2.0</code>):", parse_mode="HTML", reply_markup=get_cancel_kb())
    await state.set_state(EditDropRate.waiting_for_legendary)
    await callback.answer()

@router.message(EditDropRate.waiting_for_legendary)
async def process_edit_legendary(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Отменено.", reply_markup=get_game_admin_kb(message.from_user.id))
    try:
        val = float(message.text.replace(",", ".").strip())
        if val < 0 or val > 100:
            raise ValueError()
    except ValueError:
        return await message.answer("❌ Введите корректное число от 0 до 100.")
    s = await get_drop_settings()
    s["legendary_rate"] = round(val, 2)
    await save_drop_settings(s)
    await state.clear()
    await message.answer(f"✅ Шанс для <b>Легендарных</b> карт установлен на <b>{round(val, 2)}%</b>!\n\n" + await build_drop_settings_text(), parse_mode="HTML", reply_markup=get_drop_settings_kb())

@router.callback_query(F.data == "edit_drop_epic")
async def edit_drop_epic_cb(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return await callback.answer("Нет прав", show_alert=True)
    await callback.message.answer("Введите новый % для <b>Эпических</b> карт (например: <code>5.0</code>):", parse_mode="HTML", reply_markup=get_cancel_kb())
    await state.set_state(EditDropRate.waiting_for_epic)
    await callback.answer()

@router.message(EditDropRate.waiting_for_epic)
async def process_edit_epic(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Отменено.", reply_markup=get_game_admin_kb(message.from_user.id))
    try:
        val = float(message.text.replace(",", ".").strip())
        if val < 0 or val > 100:
            raise ValueError()
    except ValueError:
        return await message.answer("❌ Введите корректное число от 0 до 100.")
    s = await get_drop_settings()
    s["epic_rate"] = round(val, 2)
    await save_drop_settings(s)
    await state.clear()
    await message.answer(f"✅ Шанс для <b>Эпических</b> карт установлен на <b>{round(val, 2)}%</b>!\n\n" + await build_drop_settings_text(), parse_mode="HTML", reply_markup=get_drop_settings_kb())

@router.callback_query(F.data == "edit_drop_rare")
async def edit_drop_rare_cb(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return await callback.answer("Нет прав", show_alert=True)
    await callback.message.answer("Введите новый % для <b>Редких</b> карт (например: <code>26.0</code>):", parse_mode="HTML", reply_markup=get_cancel_kb())
    await state.set_state(EditDropRate.waiting_for_rare)
    await callback.answer()

@router.message(EditDropRate.waiting_for_rare)
async def process_edit_rare(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Отменено.", reply_markup=get_game_admin_kb(message.from_user.id))
    try:
        val = float(message.text.replace(",", ".").strip())
        if val < 0 or val > 100:
            raise ValueError()
    except ValueError:
        return await message.answer("❌ Введите корректное число от 0 до 100.")
    s = await get_drop_settings()
    s["rare_rate"] = round(val, 2)
    await save_drop_settings(s)
    await state.clear()
    await message.answer(f"✅ Шанс для <b>Редких</b> карт установлен на <b>{round(val, 2)}%</b>!\n\n" + await build_drop_settings_text(), parse_mode="HTML", reply_markup=get_drop_settings_kb())

@router.callback_query(F.data == "edit_drop_penalty")
async def edit_drop_penalty_cb(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return await callback.answer("Нет прав", show_alert=True)
    await callback.message.answer("Введите % снижения шансов после сбора 1-й серии (например <code>50</code> для снижения в 2 раза, или <code>67</code>):", parse_mode="HTML", reply_markup=get_cancel_kb())
    await state.set_state(EditDropRate.waiting_for_penalty)
    await callback.answer()

@router.message(EditDropRate.waiting_for_penalty)
async def process_edit_penalty(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Отменено.", reply_markup=get_game_admin_kb(message.from_user.id))
    try:
        val = float(message.text.replace(",", ".").strip())
        if val < 0 or val > 99:
            raise ValueError()
    except ValueError:
        return await message.answer("❌ Введите число от 0 до 99 (например 50 или 67).")
    s = await get_drop_settings()
    s["series_penalty"] = round(val, 2)
    await save_drop_settings(s)
    await state.clear()
    mult = max(0.0, round((100.0 - val) / 100.0, 2))
    await message.answer(f"✅ Снижение шансов после серии установлено на <b>-{round(val, 2)}%</b> (множитель <code>x{mult}</code>)!\n\n" + await build_drop_settings_text(), parse_mode="HTML", reply_markup=get_drop_settings_kb())

@router.callback_query(F.data == "edit_drop_reset")
async def edit_drop_reset_cb(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return await callback.answer("Нет прав", show_alert=True)
    await save_drop_settings(DEFAULT_DROP_SETTINGS.copy())
    await callback.answer("Шансы сброшены на стандартные!", show_alert=True)
    text = await build_drop_settings_text()
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_drop_settings_kb())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=get_drop_settings_kb())



# --- ADMIN: CREATE ORDER ---
@router.message(F.text == "➕ Добавить заказ", StateFilter("*"))
async def add_order_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Введите Номер клиента (ID), к которому нужно привязать заказ:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CreateOrder.waiting_for_client_id)

@router.message(CreateOrder.waiting_for_client_id)
async def add_order_client_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("ID клиента должен быть числом.")
        return
    client_id = int(message.text)
    
    if not await check_client(client_id):
        await message.answer("❌ Клиент с таким ID не найден. Сначала создайте клиента.", reply_markup=get_admin_kb())
        await state.clear()
        return
        
    await state.update_data(client_id=client_id)
    await message.answer("Введите позиции заказа (что купили):")
    await state.set_state(CreateOrder.waiting_for_items)

@router.message(CreateOrder.waiting_for_items)
async def add_order_items(message: Message, state: FSMContext):
    await state.update_data(items=message.text)
    await message.answer("Введите общую стоимость заказа (число):")
    await state.set_state(CreateOrder.waiting_for_total_price)

@router.message(CreateOrder.waiting_for_total_price)
async def add_order_total(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число.")
        return
    await state.update_data(total_price=int(message.text))
    await message.answer("Сколько клиент уже оплатил? (число):")
    await state.set_state(CreateOrder.waiting_for_paid_amount)

@router.message(CreateOrder.waiting_for_paid_amount)
async def add_order_paid(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число.")
        return
    await state.update_data(paid_amount=int(message.text))
    await message.answer("Пришлите фото заказа (или нажмите кнопку 'Пропустить фото'):", reply_markup=get_skip_photo_kb())
    await state.set_state(CreateOrder.waiting_for_photo)

@router.message(CreateOrder.waiting_for_photo)
async def add_order_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.text != "Пропустить фото":
        await message.answer("Пожалуйста, пришлите фото или нажмите 'Пропустить фото'.")
        return
    
    order_id = await create_order(
        data['client_id'], 
        data['items'], 
        data['total_price'], 
        data['paid_amount'], 
        photo_id
    )
    
    msg = f"✅ Заказ успешно добавлен клиенту #{data['client_id']}!\n\n" \
          f"🆔 Номер заказа: {order_id}\n" \
          f"🛒 Позиции: {data['items']}\n" \
          f"💰 Стоимость: {data['total_price']} | Оплачено: {data['paid_amount']}"
          
    await message.answer(msg, reply_markup=get_admin_kb())
    await state.clear()
    
    # Notify user via bot if tg id is linked
    client_tg_id = await get_client_tg_id_by_order(order_id)
    if client_tg_id:
        try:
            notify_msg = f"🎉 **У вас новый заказ!**\n\n🆔 Заказ #{order_id}\n🛒 Позиции:\n{data['items']}\n\n💰 Стоимость: {data['total_price']}\n✅ Оплачено: {data['paid_amount']}"
            if photo_id:
                await bot.send_photo(chat_id=client_tg_id, photo=photo_id, caption=notify_msg, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id=client_tg_id, text=notify_msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не удалось уведомить пользователя о новом заказе: {e}")

# --- ADMIN: UPDATE STATUS ---
@router.message(F.text == "🔄 Изменить статус заказа", StateFilter("*"))
async def change_status_start(message: Message, state: FSMContext):
    await state.clear()
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    orders = await get_all_orders()
    if not orders:
        await message.answer("Нет активных заказов.")
        return
    await message.answer("Выберите заказ для изменения статуса (Архивные здесь не отображаются):", reply_markup=get_orders_kb(orders, "status"))

@router.callback_query(F.data.startswith("status_"))
async def select_order_for_status(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await callback.message.edit_text(f"Выберите новый статус для заказа #{order_id}:", reply_markup=get_status_kb(order_id))

@router.callback_query(F.data.startswith("setstatus_"))
async def set_order_status(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    parts = callback.data.split("_")
    order_id = int(parts[1])
    status_idx = int(parts[2])
    new_status = STATUSES[status_idx]
    
    await update_order_status(order_id, new_status)
    
    if new_status == "Выдано":
        await callback.message.edit_text(f"✅ Статус заказа #{order_id} изменен на:\n'{new_status}'.\n\n🗃 Заказ автоматически перемещен в Архив.")
    else:
        await callback.message.edit_text(f"✅ Статус заказа #{order_id} изменен на:\n'{new_status}'.")
        
    await callback.answer("Статус обновлен")
    
    order_details = await get_order_details(order_id)
    logging.info(f"[NOTIFY] order_details for #{order_id}: {order_details}")
    if order_details and order_details.get('user_tg_id'):
        client_tg_id = order_details['user_tg_id']
        logging.info(f"[NOTIFY] Sending to {client_tg_id}, status: {new_status}")
        try:
            notify_msg = format_status_notification(order_details)
            if order_details.get('photo_id'):
                await bot.send_photo(chat_id=client_tg_id, photo=order_details['photo_id'], caption=notify_msg, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id=client_tg_id, text=notify_msg, parse_mode="Markdown")
            logging.info(f"[NOTIFY] Sent OK to {client_tg_id}")
        except Exception as e:
            logging.error(f"[NOTIFY] FAILED for {client_tg_id}: {e}")
    else:
        logging.warning(f"[NOTIFY] No user_tg_id for order #{order_id} — user not linked")

# --- ADMIN: UPDATE PAYMENT ---
@router.message(F.text == "💰 Изменить оплату по заказу", StateFilter("*"))
async def change_payment_start(message: Message, state: FSMContext):
    await state.clear()
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    orders = await get_all_orders()
    if not orders:
        await message.answer("Нет активных заказов.")
        return
    await message.answer("Выберите заказ:", reply_markup=get_orders_kb(orders, "pay"))

@router.callback_query(F.data.startswith("pay_"))
async def select_order_for_payment(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await state.update_data(pay_order_id=order_id)
    await callback.message.answer(f"Введите новую сумму, которую клиент УЖЕ оплатил по заказу #{order_id}:")
    await state.set_state(UpdatePayment.waiting_for_new_paid)
    await callback.answer()

@router.message(UpdatePayment.waiting_for_new_paid)
async def update_payment_value(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число.")
        return
    data = await state.get_data()
    order_id = data['pay_order_id']
    new_paid = int(message.text)
    
    await update_order_payment(order_id, new_paid)
    await message.answer(f"✅ Сумма оплаты по заказу #{order_id} обновлена до {new_paid}.", reply_markup=get_admin_kb())
    await state.clear()
    
    order_details = await get_order_details(order_id)
    if order_details and order_details.get('user_tg_id'):
        client_tg_id = order_details['user_tg_id']
        try:
            notify_msg = format_status_notification(order_details)
            if order_details.get('photo_id'):
                await bot.send_photo(chat_id=client_tg_id, photo=order_details['photo_id'], caption=notify_msg, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id=client_tg_id, text=notify_msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление об оплате клиенту {client_tg_id}: {e}")

# --- ADMIN: ARCHIVE LIST ---
@router.message(F.text == "🗃 Архив заказов (Админ)", StateFilter("*"))
async def admin_archive_list(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    
    archived_orders = await get_archived_orders()
    if not archived_orders:
        await message.answer("Архив пуст.")
        return
        
    await message.answer("🗃 Выберите архивный заказ для действий:", reply_markup=get_orders_kb(archived_orders, "archiveadmin"))

@router.callback_query(F.data.startswith("archiveadmin_"))
async def select_archived_order(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await callback.message.edit_text(
        f"🗃 **Архивный заказ #{order_id}**\n\nВыберите действие:",
        reply_markup=get_admin_archive_kb(order_id),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("unarchive_"))
async def action_unarchive_order(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await unarchive_order_db(order_id)
    await callback.message.edit_text(f"✅ Заказ #{order_id} успешно восстановлен из архива.")
    await callback.answer("Восстановлено")

@router.callback_query(F.data.startswith("delete_"))
async def action_delete_order(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await delete_order_db(order_id)
    await callback.message.edit_text(f"🗑 Заказ #{order_id} был окончательно удален из базы.")
    await callback.answer("Удалено")

@router.callback_query(F.data.startswith("show_refs_"))
async def show_player_refs(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    
    try:
        target_id = int(callback.data.replace("show_refs_", ""))
        async with pool.acquire() as db:
            refs = await db.fetch(
                "SELECT telegram_id, username, first_name, packs_count FROM card_users WHERE referred_by = $1 ORDER BY telegram_id",
                target_id
            )
        
        import html as html_mod
        if not refs:
            await callback.answer("У этого игрока нет рефералов.", show_alert=True)
            return
        
        msg = f"👥 <b>Рефералы игрока <code>{target_id}</code>:</b>\n\n"
        for i, r in enumerate(refs, 1):
            fn = html_mod.escape(r['first_name'] or '')
            un = html_mod.escape(r['username'] or '')
            name = f"{fn}" if fn else f"@{un}" if un else f"ID:{r['telegram_id']}"
            un_part = f" (@{un})" if un and fn else ""
            msg += f"{i}. <b>{name}{un_part}</b> — <code>{r['telegram_id']}</code> | 📦 паков: {r['packs_count']}\n"
        
        await callback.message.answer(msg, parse_mode="HTML")
        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)


# --- CLIENT INTERFACE ---
@router.message(F.text == "📦 Отследить заказы", StateFilter("*"))
async def check_status_start(message: Message, state: FSMContext):
    await state.clear()
    # First check if this Telegram ID is already linked to a client
    async with pool.acquire() as db:
        linked = await db.fetchrow("SELECT id FROM clients WHERE user_tg_id = $1", message.from_user.id)
    
    if linked:
        # Already linked — show orders directly without password
        client_id = linked['id']
        async with pool.acquire() as db:
            orders = await db.fetch(
                "SELECT id, items, total_price, paid_amount, status, photo_id FROM orders WHERE client_id = $1 AND archived = FALSE",
                client_id
            )
        
        if len(orders) == 0:
            await message.answer(f"✅ Личный кабинет #{client_id}\n\nАктивных заказов нет.", reply_markup=get_start_kb())
        else:
            await message.answer(f"✅ **Личный кабинет #{client_id}**\n\nАктивных заказов: {len(orders)}", parse_mode="Markdown", reply_markup=get_start_kb())
            for order in orders:
                order_id, items, total_price, paid_amount, status, photo_id = order
                debt = total_price - paid_amount
                response = f"📦 **Заказ #{order_id}**\n\n"
                response += f"🛒 **Позиции:**\n{items}\n\n"
                response += f"💵 **Общая стоимость:** {total_price}\n"
                response += f"✅ **Оплачено:** {paid_amount}\n"
                response += f"❗️ **Осталось доплатить:** {debt if debt > 0 else 0}\n\n"
                response += f"🚚 **Текущий статус:**\n_{status}_"
                if photo_id:
                    await message.answer_photo(photo=photo_id, caption=response, parse_mode="Markdown")
                else:
                    await message.answer(response, parse_mode="Markdown")
        return
    
    # Not linked yet — ask for ID and password (one-time login)
    await message.answer(
        "Введите ваш Номер клиента (ID) и Пароль.\n"
        "После первого входа вам не придётся вводить их снова.",
        reply_markup=ReplyKeyboardRemove()
    )
    await message.answer("Введите ваш Номер клиента (ID):")
    await state.set_state(CheckStatus.waiting_for_id)

@router.message(CheckStatus.waiting_for_id)
async def check_status_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Номер клиента должен быть числом.")
        return
    await state.update_data(client_id=int(message.text))
    await message.answer("Введите ваш Пароль:")
    await state.set_state(CheckStatus.waiting_for_password)

@router.message(CheckStatus.waiting_for_password)
async def check_status_password(message: Message, state: FSMContext):
    data = await state.get_data()
    client_id = data['client_id']
    password = message.text
    
    orders = await get_client_orders(client_id, password)
    
    if orders is None:
        await message.answer("❌ Ошибка: Неверный ID клиента или пароль.", reply_markup=get_start_kb())
    elif len(orders) == 0:
        await bind_client_tg_id(client_id, message.from_user.id)
        await message.answer(
            f"✅ Вы вошли в личный кабинет (ID: {client_id}).\n"
            f"Теперь вам не нужно вводить пароль — вы будете узнаваться автоматически!\n\n"
            f"Активных заказов пока нет.",
            reply_markup=get_start_kb()
        )
    else:
        await bind_client_tg_id(client_id, message.from_user.id)
        await message.answer(
            f"✅ **Личный кабинет #{client_id}**\n"
            f"Теперь вам не нужно вводить пароль — вы будете узнаваться автоматически!\n\n"
            f"Активных заказов: {len(orders)}",
            parse_mode="Markdown"
        )
        for order in orders:
            order_id, items, total_price, paid_amount, status, photo_id = order
            debt = total_price - paid_amount
            
            response = f"📦 **Заказ #{order_id}**\n\n"
            response += f"🛒 **Позиции:**\n{items}\n\n"
            response += f"💵 **Общая стоимость:** {total_price}\n"
            response += f"✅ **Оплачено:** {paid_amount}\n"
            response += f"❗️ **Осталось доплатить:** {debt if debt > 0 else 0}\n\n"
            response += f"🚚 **Текущий статус:**\n_{status}_"
            
            if photo_id:
                await message.answer_photo(photo=photo_id, caption=response, parse_mode="Markdown")
            else:
                await message.answer(response, parse_mode="Markdown")
                
        await message.answer("Все активные заказы загружены.", reply_markup=get_start_kb())
            
    await state.clear()

# CLIENT: ARCHIVE
@router.message(F.text == "🗃 Архив заказов", StateFilter("*"))
async def check_archive_start(message: Message, state: FSMContext):
    await state.clear()
    # First check if this Telegram ID is already linked
    async with pool.acquire() as db:
        linked = await db.fetchrow("SELECT id FROM clients WHERE user_tg_id = $1", message.from_user.id)
    
    if linked:
        client_id = linked['id']
        async with pool.acquire() as db:
            orders = await db.fetch(
                "SELECT id, items, total_price, paid_amount, status, photo_id FROM orders WHERE client_id = $1 AND archived = TRUE",
                client_id
            )
        
        if len(orders) == 0:
            await message.answer(f"🗃 Архив заказов пуст.", reply_markup=get_start_kb())
        else:
            await message.answer(f"🗃 **Архив заказов #{client_id}**\n\nВыданных заказов: {len(orders)}", parse_mode="Markdown", reply_markup=get_start_kb())
            for order in orders:
                order_id, items, total_price, paid_amount, status, photo_id = order
                response = f"📦 **Архивный заказ #{order_id}**\n\n"
                response += f"🛒 **Позиции:**\n{items}\n\n"
                response += f"💵 **Общая стоимость:** {total_price}\n"
                response += f"✅ **Оплачено:** {paid_amount}\n\n"
                response += f"🚚 **Финальный статус:**\n_{status}_"
                if photo_id:
                    await message.answer_photo(photo=photo_id, caption=response, parse_mode="Markdown")
                else:
                    await message.answer(response, parse_mode="Markdown")
        return
    
    # Not linked — ask for credentials
    await message.answer("Введите ваш Номер клиента (ID) для доступа к Архиву:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CheckArchive.waiting_for_id)

@router.message(CheckArchive.waiting_for_id)
async def check_archive_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Номер клиента должен быть числом.")
        return
    await state.update_data(client_id=int(message.text))
    await message.answer("Введите ваш Пароль:")
    await state.set_state(CheckArchive.waiting_for_password)

@router.message(CheckArchive.waiting_for_password)
async def check_archive_password(message: Message, state: FSMContext):
    data = await state.get_data()
    client_id = data['client_id']
    password = message.text
    
    orders = await get_client_archived_orders(client_id, password)
    
    if orders is None:
        await message.answer("❌ Ошибка: Неверный ID клиента или пароль.", reply_markup=get_start_kb())
    elif len(orders) == 0:
        await message.answer(f"🗃 Ваш архив заказов пуст.", reply_markup=get_start_kb())
    else:
        await message.answer(f"🗃 **Архив заказов #{client_id}**\n\nВыданных заказов: {len(orders)}", parse_mode="Markdown")
        for order in orders:
            order_id, items, total_price, paid_amount, status, photo_id = order
            
            response = f"📦 **Архивный заказ #{order_id}**\n\n"
            response += f"🛒 **Позиции:**\n{items}\n\n"
            response += f"💵 **Общая стоимость:** {total_price}\n"
            response += f"✅ **Оплачено:** {paid_amount}\n\n"
            response += f"🚚 **Финальный статус:**\n_{status}_"
            
            if photo_id:
                await message.answer_photo(photo=photo_id, caption=response, parse_mode="Markdown")
            else:
                await message.answer(response, parse_mode="Markdown")
                
        await message.answer("Все архивные заказы загружены.", reply_markup=get_start_kb())
            
    await state.clear()

@router.message(F.text == "🧮 Калькулятор стоимости", StateFilter("*"))
async def calculator_prompt(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔗 Просто пришлите мне ссылку на товар с сайта **eBay**, и я автоматически рассчитаю его итоговую стоимость с учетом доставки в РФ!", parse_mode="Markdown")

# --- LINK PARSER ---
@router.message(F.text.regexp(r'https?://'))
async def handle_link(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    url = message.text.strip()
    if not url.startswith("http"):
        return

    # Force US region for eBay to ensure domestic shipping is visible
    if "ebay.com" in url or "ebay.io" in url:
        from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
        parsed = urlparse(url)
        q = parse_qs(parsed.query)
        q['_ul'] = ['US']
        url = urlunparse(parsed._replace(query=urlencode(q, doseq=True)))

    await message.answer("🔍 Секунду, анализирую ссылку (загружаю страницу и запускаю ИИ)...")
    
    if not SCRAPER_API_KEY or not OPENAI_API_KEY:
        await message.answer("❌ API ключи не настроены в .env")
        return
        
    try:
        scraper_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={url}&country_code=us&render=true"
        timeout = aiohttp.ClientTimeout(total=80)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(scraper_url) as resp:
                if resp.status != 200:
                    await message.answer(f"❌ Ошибка парсера: {resp.status}")
                    return
                html = await resp.text()
    except asyncio.TimeoutError:
        await message.answer("❌ Ошибка: Сайт загружался слишком долго (более 80 секунд) и парсер отменил запрос. Попробуйте еще раз.")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка загрузки страницы: {e}")
        return
        
    # Убираем лишний код (скрипты и стили), чтобы сэкономить лимиты (токены) бесплатного Groq
    clean_html = re.sub(r'<script.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    clean_html = re.sub(r'<style.*?</style>', '', clean_html, flags=re.DOTALL | re.IGNORECASE)
    
    # Чтобы уместить всю страницу в бесплатные лимиты, вырезаем ВООБЩЕ все HTML-теги, оставляем только чистый текст
    text_content = re.sub(r'<[^>]+>', ' ', clean_html)
    text_content = re.sub(r'\s+', ' ', text_content).strip()

    prompt = """
    Analyze the following extracted text from a product page (e.g. eBay, Funko, Mercari). 
    Find:
    1. "name": Product Name (short)
    2. "price": MAIN Product price in USD (float, no symbol). E.g. 49.99. WARNING: Ignore prices of "sponsored" or "similar" items! Find the actual price of the main item being sold.
    3. "shipping": US Domestic shipping cost in USD (float). If free or not specified, output 0.0.
    4. "weight": Product weight in kg (float). Look for weight in lbs/oz and convert to kg (1 lb = 0.45 kg, 1 oz = 0.028 kg). If absolutely not found, output null.
    
    Output valid JSON ONLY, exactly like this:
    {"name": "Funko Pop Batman", "price": 49.99, "shipping": 5.99, "weight": 0.5}
    """
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}", 
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me/Funko_Stop",
                "X-Title": "FunkoBot"
            }
            payload = {
                "model": "qwen/qwen3.6-27b",
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text_content[:20000]} # send up to 20000 chars of pure text
                ],
                "response_format": {"type": "json_object"}
            }
            async with session.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=30) as resp:
                ai_data = await resp.json()
                if "error" in ai_data:
                    await message.answer(f"❌ Ошибка ИИ: {ai_data['error']['message']}")
                    return
                result_str = ai_data["choices"][0]["message"]["content"]
                result = json.loads(result_str)
    except Exception as e:
        await message.answer(f"❌ Ошибка обработки ИИ: {e}")
        return
        
    price = float(result.get("price", 0.0) or 0.0)
    raw_shipping = float(result.get("shipping", 0.0) or 0.0)
    # Add +$2.00 to US shipping cost as requested
    shipping = raw_shipping + 2.0
    weight = result.get("weight")
    name = result.get("name", "Товар")
    
    if not price:
        await message.answer("❌ Нейросеть не смогла найти цену товара на странице.")
        return

    await state.update_data(
        name=name,
        price=price,
        shipping=shipping,
        weight=weight
    )

    if weight is None:
        await message.answer(f"📦 **{name}**\n💵 Цена: ${price}\n🚚 Доставка по США: ${shipping:.2f}\n\n⚖️ Вес товара не найден на странице.\nПожалуйста, напишите примерный вес товара в **кг** (например, 0.5):", parse_mode="Markdown")
        await state.set_state(ParseLink.waiting_for_weight)
    else:
        await calculate_and_send_result(message, state, float(weight))

@router.message(ParseLink.waiting_for_weight)
async def process_weight(message: Message, state: FSMContext):
    try:
        weight = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число (например: 0.5 или 1.2).")
        return
        
    await calculate_and_send_result(message, state, weight)
    
async def calculate_and_send_result(message: Message, state: FSMContext, weight: float):
    data = await state.get_data()
    await state.clear()
    
    name = data['name']
    price = data['price']
    shipping = data['shipping']
    
    base_price = price + shipping
    
    if base_price <= 50:
        commission = base_price * 0.25
    elif base_price <= 100:
        commission = base_price * 0.20
    else:
        commission = base_price * 0.15
        
    delivery_rf_rub = weight * 1200.0
    
    cbrf_rate = await get_usd_rate()
    rate = cbrf_rate + 5.0
    
    total_usd = base_price + commission
    total_rub = (total_usd * rate) + delivery_rf_rub
    
    final_price_rub = math.ceil(total_rub / 50.0) * 50
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Заказать (Funko Stop Manager)", url="https://t.me/Funko_Stop")] 
    ])
    
    response = f"📦 **{name}**\n\n"
    response += f"💵 Цена на сайте: ${price:.2f}\n"
    response += f"🚚 Доставка по США: ${shipping:.2f}\n"
    response += f"⚖️ Вес: {weight} кг\n\n"
    response += f"💰 **Итого к оплате: ~{final_price_rub} ₽**\n"
    response += f"_(Включая доставку в РФ и комиссию сервиса)_\n\n"
    response += f"⚠️ Цена ориентировочная. Для точного расчета и оформления заказа напишите менеджеру."
    
    await message.answer(response, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=kb)

# --- MAIN ---
# --- ADMIN: GIVE PRIZE ---
class GivePrize(StatesGroup):
    waiting_for_user_id = State()

BONUS_CARD_NAMES = {
    1: "Funko Pop",
    2: "Скидка 20%",
    3: "Скидка 25%",
    4: "Скидка 500₽",
    5: "Скидка 1000₽",
    6: "10 Бонус Паков",
    7: "Скидка 300₽",
    8: "5 Бонус Паков"
}

@router.message(F.text.contains("Выдать приз"), StateFilter("*"))
async def give_prize_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer(
        "Введите ID пользователя (или перешлите его сообщение), которому нужно выдать приз:",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)
    )
    await state.set_state(GivePrize.waiting_for_user_id)

@router.message(GivePrize.waiting_for_user_id)
async def give_prize_user_id(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
        
    try:
        user_id = int(message.text.strip())
    except ValueError:
        if message.forward_from:
            user_id = message.forward_from.id
        else:
            await message.answer("❌ Введите корректный числовой ID.")
            return

    async with pool.acquire() as db:
        cards = await db.fetch("SELECT card_index, count FROM user_cards WHERE telegram_id = $1 AND series_slug = 'bonus_card' AND count > 0", user_id)
        
        if not cards:
            await message.answer("У этого пользователя нет бонусных карт.", reply_markup=get_game_admin_kb(message.from_user.id))
            await state.clear()
            return
            
        builder = InlineKeyboardBuilder()
        for c in cards:
            c_idx = c['card_index']
            c_name = BONUS_CARD_NAMES.get(c_idx, f"Бонус {c_idx}")
            builder.button(text=f"Сжечь: {c_name} ({c['count']} шт)", callback_data=f"give_prize:{user_id}:{c_idx}")
        builder.adjust(1)
        
        await message.answer("Выберите карту для списания и выдачи промокода:", reply_markup=builder.as_markup())
        await message.answer("Меню админа", reply_markup=get_game_admin_kb(message.from_user.id))
        await state.clear()

import string
import random

@router.callback_query(F.data.startswith("give_prize:"))
async def process_give_prize(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав.", show_alert=True)
        return
        
    parts = callback.data.split(":")
    target_id = int(parts[1])
    card_idx = int(parts[2])
    
    async with pool.acquire() as db:
        res = await db.execute("UPDATE user_cards SET count = count - 1 WHERE telegram_id = $1 AND series_slug = 'bonus_card' AND card_index = $2 AND count > 0", target_id, card_idx)
        if res == "UPDATE 0":
            await callback.answer("Ошибка: карта уже списана или её нет.", show_alert=True)
            return
        await db.execute("DELETE FROM user_cards WHERE telegram_id = $1 AND count <= 0", target_id)
        
    promo_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    c_name = BONUS_CARD_NAMES.get(card_idx, f"Бонус {card_idx}")
    
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO series_codes (code, series_slug, telegram_id) VALUES ($1, $2, $3)",
            promo_code, f"Приз: {c_name}", target_id
        )
    
    try:
        await bot.send_message(target_id, f"🎁 Поздравляем! Ваша бонусная карта обменена на приз!\nВы выиграли: **{c_name}**\n\nВаш промокод: `{promo_code}`\n\nСделайте скриншот и покажите его администратору или в магазине.", parse_mode="Markdown")
        await callback.message.edit_text(f"✅ Карта '{c_name}' успешно сожжена.\nПромокод `{promo_code}` отправлен пользователю {target_id}.", parse_mode="Markdown")
    except Exception as e:
        await callback.message.edit_text(f"⚠️ Карта списана, но не удалось отправить сообщение пользователю. Промокод: `{promo_code}`. Ошибка: {e}", parse_mode="Markdown")

# --- WEB APP CLAIM PRIZE API ---
BONUS_CARD_NAMES = {
    1: "Funko Pop",
    2: "Скидка 20%",
    3: "Скидка 25%",
    4: "Скидка 500₽",
    5: "Скидка 1000₽",
    6: "10 Бонус Паков",
    7: "Скидка 300₽",
    8: "5 Бонус Паков"
}

async def claim_prize_api(request):
    try:
        data = await request.json()
        tg_id = int(data.get("tg_id") or 0)
        card_idx = int(data.get("card_index") or 0)
        
        if not tg_id or not card_idx:
            return web.json_response({"success": False, "message": "Invalid data"})

        # Pack cards are auto-issued on reveal — don't allow claiming them via this API
        pack_card_ids = [6, 8]
        if card_idx in pack_card_ids:
            return web.json_response({"success": False, "message": "Паки выдаются автоматически при открытии карты"})

        async with pool.acquire() as db:
            res = await db.execute("UPDATE user_cards SET count = count - 1 WHERE telegram_id = $1 AND series_slug = 'bonus_card' AND card_index = $2 AND count > 0", tg_id, card_idx)
            if res == "UPDATE 0":
                return web.json_response({"success": False, "message": "Карта не найдена или уже использована"})
            await db.execute("DELETE FROM user_cards WHERE telegram_id = $1 AND count <= 0", tg_id)
            
        promo_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        c_name = BONUS_CARD_NAMES.get(card_idx, f"Бонус {card_idx}")
        
        async with pool.acquire() as db:
            # Use timestamp suffix to avoid UNIQUE(telegram_id, series_slug) conflict
            # when same user claims the same prize type multiple times
            import time
            unique_slug = f"Приз: {c_name} #{int(time.time())}"
            await db.execute(
                "INSERT INTO series_codes (code, series_slug, telegram_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                promo_code, unique_slug, tg_id
            )
        
        # Send promo code to player via bot DM
        try:
            await bot.send_message(
                tg_id,
                f"🎁 Поздравляем! Вы активировали бонусную карту!\n"
                f"Вы выиграли: **{c_name}**\n\n"
                f"Ваш промокод: `{promo_code}`\n\n"
                f"Сделайте скриншот и отправьте его менеджеру @Funko_Stop.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        # Notify all admins about the prize claim
        try:
            async with pool.acquire() as db:
                user_row = await db.fetchrow("SELECT username, first_name FROM card_users WHERE telegram_id = $1", tg_id)
            user_name = ""
            if user_row:
                if user_row['first_name'] and user_row['username']:
                    user_name = f"{user_row['first_name']} (@{user_row['username']})"
                elif user_row['first_name']:
                    user_name = user_row['first_name']
                elif user_row['username']:
                    user_name = f"@{user_row['username']}"
            if not user_name:
                user_name = f"ID:{tg_id}"

            admin_targets = set(ADMIN_IDS)
            async with pool.acquire() as db:
                db_admins = await db.fetch("SELECT user_tg_id FROM users WHERE role = 'admin' AND user_tg_id IS NOT NULL")
            for r in db_admins:
                admin_targets.add(r['user_tg_id'])

            for admin_id in admin_targets:
                try:
                    await bot.send_message(
                        admin_id,
                        f"🎟 Игрок [{user_name}](tg://user?id={tg_id}) активировал бонус-карту!\n"
                        f"Приз: **{c_name}**\n"
                        f"Промокод: `{promo_code}`\n\n"
                        f"Используй /all_promos или кнопку «🎫 Все промокоды» для проверки.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"Admin notify error in claim_prize_api: {e}")
            
        return web.json_response({"success": True, "promo_code": promo_code})
    except Exception as e:
        import traceback
        logging.error(f"claim_prize_api error: {e}\n{traceback.format_exc()}")
        return web.json_response({"success": False, "message": "Server error"})


# --- ADMIN: TINKOFF PAYMENT LINK ---
import hashlib
import uuid
import aiohttp

@router.message(F.text == "💳 Создать ссылку на оплату", StateFilter("*"))
async def tbank_link_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Введите название позиции (товара/услуги):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CreatePaymentLink.waiting_for_desc)

@router.message(CreatePaymentLink.waiting_for_desc)
async def tbank_link_desc(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Пожалуйста, введите текст.")
    await state.update_data(desc=message.text)
    await message.answer("Введите стоимость в рублях (только число):")
    await state.set_state(CreatePaymentLink.waiting_for_amount)

@router.message(CreatePaymentLink.waiting_for_amount)
async def tbank_link_amount(message: Message, state: FSMContext):
    try:
        amount_rub = float(message.text.strip())
        amount_kopecks = int(amount_rub * 100)
    except ValueError:
        return await message.answer("Неверный формат. Введите число.")
    
    data = await state.get_data()
    desc = data.get("desc", "Оплата")
    await state.clear()
    
    try:
        # Реальные данные терминала (не DEMO)
        TERMINAL_KEY = os.getenv("TBANK_TERMINAL_KEY", "1788107588985")
        PASSWORD = os.getenv("TBANK_PASSWORD", "XV7#HPN$fi%yN1gl")
        order_id = str(uuid.uuid4())
        
        payload = {
            "TerminalKey": TERMINAL_KEY,
            "Amount": amount_kopecks,
            "OrderId": order_id,
            "Description": desc
        }
        
        sign_data = payload.copy()
        sign_data["Password"] = PASSWORD
        
        sorted_keys = sorted(sign_data.keys())
        token_str = "".join([str(sign_data[k]) for k in sorted_keys])
        token = hashlib.sha256(token_str.encode("utf-8")).hexdigest()
        payload["Token"] = token
        
        async with aiohttp.ClientSession() as session:
            async with session.post("https://securepay.tinkoff.ru/v2/Init", json=payload, ssl=False) as resp:
                resp_data = await resp.json()
                
        if resp_data.get("Success"):
            link = resp_data.get("PaymentURL")
            await message.answer(f"✅ Ссылка на оплату успешно создана:\n\n{link}", reply_markup=get_admin_kb(message.from_user.id))
        else:
            err = resp_data.get("Message", "Unknown") + " - " + resp_data.get("Details", "")
            await message.answer(f"❌ Ошибка API Т-Банк: {err}", reply_markup=get_admin_kb(message.from_user.id))
            
    except Exception as e:
        await message.answer(f"❌ Системная ошибка: {str(e)}", reply_markup=get_admin_kb(message.from_user.id))

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    await init_db()
    
    await start_webserver()
    
    # Start daily notification background task
    asyncio.create_task(daily_notification_task())
    
    logging.info("Бот запущен. Ожидание сообщений...")
    try:
        # Устанавливаем кнопку START THE GAME для всех пользователей
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                type="web_app",
                text="START THE GAME",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
    except Exception as e:
        logging.error(f"Failed to set menu button: {e}")
        
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
