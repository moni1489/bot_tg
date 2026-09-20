from urllib.parse import quote, unquote, urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from funko_deal_bot.queries import funko_focused, parse_search_queries

MIN_POLL_SECONDS = 60
DEFAULT_POLL_SECONDS = 120


def normalize_proxy_url(raw: str) -> str:
    """Turn PROXY_URL into http://user:pass@host:port for httpx.

    Accepts that URL as-is, or the provider form host:port:user:pass.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" in text:
        return text
    parts = text.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return text


def playwright_proxy(raw: str) -> dict | None:
    """Playwright wants server without userinfo; username/password as fields."""
    text = normalize_proxy_url(raw)
    if not text:
        return None
    parsed = urlparse(text)
    host = parsed.hostname
    if not host:
        return None
    scheme = parsed.scheme or "http"
    server = f"{scheme}://{host}"
    if parsed.port:
        server = f"{server}:{parsed.port}"
    spec: dict = {"server": server}
    if parsed.username:
        spec["username"] = unquote(parsed.username)
    if parsed.password is not None and parsed.password != "":
        spec["password"] = unquote(parsed.password)
    return spec


def proxy_enabled_flag(raw: str) -> str:
    """yes/no for logs — never the URL (it can contain a password)."""
    return "yes" if bool(normalize_proxy_url(raw)) else "no"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_owner_chat_id: str = ""
    telegram_allowed_usernames: str = "givingbadpeoplegoodidea,goitislav"
    deal_threshold_pct: float = 20.0
    poll_seconds: int = 60
    search_query: str = "Funko Pop"
    search_queries: str = "Funko Pop,Funko Pop!,Funko"
    ebay_site: str = "https://www.ebay.com"
    ebay_mode: str = "live"
    ebay_app_id: str = ""
    ebay_oauth_token: str = ""
    database_path: str = "data/bot.sqlite"
    host: str = "0.0.0.0"
    port: int = 43147
    min_comparables: int = 1
    min_price_usd: float = 3.0
    max_lot_usd: float = 500.0
    ship_to_zip: str = "19801"
    page_size: int = 60
    proxy_url: str = ""
    telegram_proxy: str = ""
    openai_api_key: str = ""
    vision_model: str = "microsoft/Florence-2-base"
    vision_optimized_cpu: bool = True
    vision_cpu_threads: int = 0
    vision_ocr_max_tokens: int = 384
    vision_detect_max_tokens: int = 128
    vision_num_beams: int = 1
    enable_playwright: bool = True
    ebay_skip_playwright: bool = True
    seed_existing_on_start: bool = True

    @field_validator("proxy_url", mode="before")
    @classmethod
    def _normalize_proxy_url(cls, value: object) -> str:
        if value is None:
            return ""
        return normalize_proxy_url(str(value))

    @field_validator("telegram_proxy", mode="before")
    @classmethod
    def _strip_telegram_proxy(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def resolved_queries(self) -> list[str]:
        return funko_focused(parse_search_queries(self.search_queries, self.search_query))

    def query_label(self) -> str:
        return ", ".join(self.resolved_queries())

    def poll_interval(self) -> int:
        return max(MIN_POLL_SECONDS, int(self.poll_seconds or DEFAULT_POLL_SECONDS))

    def playwright_allowed(self) -> bool:
        """Home zip skips Playwright (EBAY_SKIP_PLAYWRIGHT=1) so a hung goto cannot stall a scan."""
        if self.ebay_skip_playwright:
            return False
        return bool(self.enable_playwright)


def load_settings() -> Settings:
    return Settings()
