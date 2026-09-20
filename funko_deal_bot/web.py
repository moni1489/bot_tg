from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from funko_deal_bot.archive import ARCHIVE_NAME, ensure_source_archive
from funko_deal_bot.config import Settings
from funko_deal_bot.engine import Scanner
from funko_deal_bot.store import Store

TEMPLATE = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")


def create_app(settings: Settings, store: Store, scanner: Scanner) -> FastAPI:
    app = FastAPI(title="Funko Deal Bot")

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        stats = store.stats()
        alerts = store.recent_alerts(20)
        cards = "\n".join(_card(alert) for alert in alerts) or (
            '<p class="empty">Пока нет алертов. Нажми «Прогнать демо» или подожди первый живой скан.</p>'
        )
        token_ok = "есть" if settings.telegram_bot_token.strip() else "нет — бот не шлёт в Telegram"
        return (
            TEMPLATE.replace("{{CARDS}}", cards)
            .replace("{{LISTINGS}}", str(stats["listings"]))
            .replace("{{ALERTS}}", str(stats["alerts"]))
            .replace("{{LAST_SCAN}}", stats["last_scan"] or "ещё не было")
            .replace("{{MODE}}", stats["last_mode"] or settings.ebay_mode)
            .replace("{{THRESHOLD}}", f"{settings.deal_threshold_pct:.0f}")
            .replace("{{QUERY}}", settings.query_label())
            .replace("{{TOKEN}}", token_ok)
            .replace("{{ERROR}}", stats["last_error"] or "нет")
            .replace("{{ZIP}}", settings.ship_to_zip)
        )

    @app.post("/api/scan-demo")
    def scan_demo() -> JSONResponse:
        result = scanner.demo_examples()
        return JSONResponse(
            {
                "mode": result.mode,
                "alerts": len(result.alerts),
                "new_count": result.new_count,
            }
        )

    @app.get("/api/status")
    def status() -> dict:
        return store.stats()

    @app.get("/api/parse")
    def parse_item(url: str = "") -> dict:
        from funko_deal_bot.check import parse_check_item_id
        item_id = parse_check_item_id(url)
        if not item_id:
            return {"error": "Invalid eBay URL or ID"}
        listing, err = scanner.client.fetch_item(item_id)
        if not listing:
            return {"error": err or "Failed to fetch item"}
        return {
            "name": listing.title,
            "price": listing.price,
            "shipping": listing.shipping_cost,
            "currency": listing.currency,
        }

    @app.get("/FunkoDealBot.zip")
    @app.get("/download")
    def download_archive() -> FileResponse:
        path = ensure_source_archive()
        return FileResponse(
            path,
            media_type="application/zip",
            filename=ARCHIVE_NAME,
        )

    return app


def _card(alert: dict) -> str:
    listing = alert.get("listing") or {}
    kind = alert.get("kind")
    title = listing.get("title", "")
    url = listing.get("url", "#")
    price = listing.get("price")
    ship = listing.get("shipping_cost")
    price_n = float(price) if isinstance(price, (int, float)) else 0.0
    ship_n = 0.0 if ship is None else float(ship)
    price_s = f"${price_n:.2f}"
    ship_s = "доставка бесплатно" if ship_n == 0 else f"доставка ${ship_n:.2f}"
    total_s = f"${price_n + ship_n:.2f}"
    if kind == "bundle":
        badge = '<span class="badge bundle">лот vs синглы</span>'
        pct = alert.get("cheaper_pct") or 0
        meta = f"{ship_s}. −{pct:.0f}% к сумме синглов."
    else:
        pct = alert.get("cheaper_pct") or 0
        save = alert.get("savings_usd")
        avg = alert.get("average_price")
        n = alert.get("comparable_count") or 0
        badge = f'<span class="badge deal">−{pct:.0f}% vs cheapest</span>'
        save_s = f"${save:.2f}" if isinstance(save, (int, float)) else "—"
        avg_s = f"${avg:.2f}" if isinstance(avg, (int, float)) else "—"
        meta = f"{ship_s}. Выгода {save_s}. 2–3 cheapest {avg_s} ({n})"
    return f"""
    <article class="card">
      {badge}
      <h3>{_esc(title)}</h3>
      <p class="price">{total_s}</p>
      <p class="meta">Лот {price_s} · {_esc(meta)}</p>
      <a href="{url}" target="_blank" rel="noreferrer">Ссылка на eBay</a>
    </article>
    """


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
