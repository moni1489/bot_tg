"""
Локальный тест-сервер для cards_app.
Запускай: python test_server.py
Потом открой: http://localhost:8080/cards
"""
import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

CARDS_APP_DIR = os.path.join(os.path.dirname(__file__), "cards_app")

# Mock user data — меняй чтобы тестировать разные состояния
MOCK_PROFILE = {
    "packs_count": 55,
    "last_daily_pack": None,
    "user_cards": {
        # Breaking Bad
        "breaking_bad_1": 5, "breaking_bad_2": 5, "breaking_bad_3": 5, "breaking_bad_4": 5,
        # Stranger Things
        "stranger_things_1": 5, "stranger_things_2": 5, "stranger_things_3": 5, "stranger_things_4": 5,
        # DC
        "dc_1": 5, "dc_2": 5, "dc_3": 5, "dc_4": 5,
        # Death Note
        "death_note_1": 5, "death_note_2": 5, "death_note_3": 5, "death_note_4": 5,
        # Invincible
        "invincible_1": 5, "invincible_2": 5, "invincible_3": 5, "invincible_4": 5,
        # One Piece
        "one_piece_1": 5, "one_piece_2": 5, "one_piece_3": 5, "one_piece_4": 5,
        # Universal
        "universal_1": 5, "universal_2": 5, "universal_3": 5, "universal_4": 5,
        # Resident Evil
        "resident_evil_1": 5, "resident_evil_2": 5, "resident_evil_3": 5, "resident_evil_4": 5,
        # Bonus cards (для теста клейма)
        "bonus_card_1": 1, "bonus_card_2": 2, "bonus_card_3": 1,
        "bonus_card_4": 1, "bonus_card_7": 1,
    },
    "completed_tasks": ["tg_sub"],
    "ref_count": 0,
    "bot_username": "funkostop_bot",
    "is_admin": True,
    "drop_settings": {
        "legendary_rate": 1.5,
        "epic_rate": 5.0,
        "rare_rate": 26.0,
        "series_penalty": 67.0
    }
}

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

MIME_TYPES = {
    ".html": "text/html",
    ".css":  "text/css",
    ".js":   "application/javascript",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".webp": "image/webp",
}

class TestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"  {self.address_string()} - {format % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path):
        if not os.path.isfile(path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return
        ext = os.path.splitext(path)[1].lower()
        mime = MIME_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", len(data))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        from urllib.parse import unquote
        path = unquote(parsed.path.rstrip("/"))

        if path in ("", "/", "/cards", "/cards/"):
            self.serve_file(os.path.join(CARDS_APP_DIR, "index.html"))
            return

        if path.startswith("/cards/"):
            rel = path[len("/cards/"):]
            self.serve_file(os.path.join(CARDS_APP_DIR, rel))
            return

        if path == "/api/cards/profile":
            self.send_json(MOCK_PROFILE)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/cards/open":
            self.send_json({"success": True})
        elif path == "/api/cards/claim_daily":
            MOCK_PROFILE["packs_count"] += 1
            self.send_json({"success": True, "packs_count": MOCK_PROFILE["packs_count"]})
        elif path == "/api/cards/tasks/claim":
            task_id = body.get("task_id", "")
            if task_id not in MOCK_PROFILE["completed_tasks"]:
                MOCK_PROFILE["completed_tasks"].append(task_id)
                MOCK_PROFILE["packs_count"] += 1
            self.send_json({
                "success": True,
                "packs_count": MOCK_PROFILE["packs_count"],
                "completed_tasks": MOCK_PROFILE["completed_tasks"],
                "is_admin": True
            })
        elif path == "/api/cards/give_test_packs":
            MOCK_PROFILE["packs_count"] += 10
            self.send_json({"success": True, "packs_count": MOCK_PROFILE["packs_count"]})
        elif path == "/api/cards/reset_daily_test":
            MOCK_PROFILE["last_daily_pack"] = None
            self.send_json({"success": True})
        elif path == "/api/cards/claim_bonus_packs":
            count = body.get("count", 0)
            MOCK_PROFILE["packs_count"] += count
            self.send_json({"success": True, "packs_count": MOCK_PROFILE["packs_count"]})
        elif path == "/api/cards/claim_prize":
            card_idx = body.get("card_index", 0)
            key = f"bonus_card_{card_idx}"
            if MOCK_PROFILE["user_cards"].get(key, 0) > 0:
                MOCK_PROFILE["user_cards"][key] -= 1
                if MOCK_PROFILE["user_cards"][key] <= 0:
                    del MOCK_PROFILE["user_cards"][key]
                self.send_json({"success": True, "promo_code": "TEST-1234"})
            else:
                self.send_json({"success": False, "message": "Карта не найдена или уже использована"})
        elif path == "/api/cards/craft":
            cards = body.get("cards", [])
            if len(cards) != 4:
                self.send_json({"error": "Need 4 cards"}, 400)
                return
            
            rarities = []
            for c in cards:
                if MOCK_PROFILE["user_cards"].get(c, 0) > 0:
                    MOCK_PROFILE["user_cards"][c] -= 1
                parts = c.split('_')
                c_idx = int(parts.pop())
                s_slug = "_".join(parts)
                for sc in SERIES_CONFIG:
                    if sc["slug"] == s_slug:
                        for cc in sc["cards"]:
                            if cc["index"] == c_idx:
                                rarities.append(cc["rarity"])
                                break

            # Check completed series in MOCK_PROFILE
            has_completed_series = False
            user_cards_map = MOCK_PROFILE.get("user_cards", {})
            for sc in SERIES_CONFIG:
                if sc["slug"] == "bonus_card": continue
                if all(user_cards_map.get(f"{sc['slug']}_{cc['index']}", 0) > 0 for cc in sc["cards"]):
                    has_completed_series = True
                    break

            drop_settings = MOCK_PROFILE.get("drop_settings", {})
            penalty_pct = float(drop_settings.get("series_penalty", 67.0))
            chance_multiplier = max(0.05, (100.0 - penalty_pct) / 100.0) if has_completed_series else 1.0

            import random
            counts = {"common": 0, "rare": 0, "epic": 0, "legendary": 0}
            for r in rarities: counts[r] = counts.get(r, 0) + 1

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

            matching = []
            for sc in SERIES_CONFIG:
                if sc["slug"] == "bonus_card": continue
                for cc in sc["cards"]:
                    if cc["rarity"] == new_rarity:
                        matching.append((sc["slug"], cc["index"]))
            if not matching:
                matching.append(("breaking_bad", 1))

            s_slug, c_idx = random.choice(matching)
            new_key = f"{s_slug}_{c_idx}"
            MOCK_PROFILE["user_cards"][new_key] = MOCK_PROFILE["user_cards"].get(new_key, 0) + 1

            print(f"[TEST CRAFT] Cards in: {cards} | Rarities: {rarities} | Rolled: {rand:.1f}% -> {new_rarity} ({s_slug} #{c_idx})")
            self.send_json({"success": True, "series": s_slug, "card_index": c_idx, "rarity": new_rarity})
        elif path == "/api/cards/generate_code":
            series = body.get("series_slug", "unknown")
            self.send_json({"success": True, "code": f"FULL-{series.upper()[:5]}-TEST"})
        else:
            self.send_json({"success": True})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

if __name__ == "__main__":
    # Rename FAQ images
    images_dir = os.path.join(CARDS_APP_DIR, "images")
    if os.path.exists(images_dir):
        for i in range(1, 11):
            old_path = os.path.join(images_dir, f"faq ({i}).png")
            new_path = os.path.join(images_dir, f"faq_{i}.png")
            if os.path.exists(old_path) and not os.path.exists(new_path):
                try:
                    os.rename(old_path, new_path)
                except:
                    pass
        for i in range(1, 9):
            old_path = os.path.join(images_dir, f"bonus_card ({i}).png")
            new_path = os.path.join(images_dir, f"bonus_card_{i}.png")
            if os.path.exists(old_path) and not os.path.exists(new_path):
                try:
                    os.rename(old_path, new_path)
                except:
                    pass

    port = 5050
    server = HTTPServer(("localhost", port), TestHandler)
    print(f"\n  Тест-сервер запущен!")
    print(f"  Открой: http://localhost:{port}/cards")
    print(f"  Ctrl+C - остановить\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Сервер остановлен.")
