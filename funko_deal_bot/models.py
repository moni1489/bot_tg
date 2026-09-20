from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

# /check copy when /itm?_stpos= still has no rate after retries.
SHIPPING_UNKNOWN = (
    "не посчитал доставку до ZIP 19801 после повторов — без Total нет сравнения"
)


@dataclass(slots=True)
class Listing:
    item_id: str
    title: str
    url: str
    price: float | None
    currency: str = "USD"
    image_url: str | None = None
    image_hash: str | None = None
    listing_type: str = "unknown"
    source: str = "ebay"
    shipping_cost: float | None = None
    shipping_label: str = ""
    ship_to_zip: str = "19801"
    pop_name: str | None = None
    pop_number: str | None = None
    exclusive: str | None = None
    members: list[dict] = field(default_factory=list)
    ocr_text: str | None = None
    shipping_quoted: bool = False
    vision_checked: bool = False
    vision_expected_count: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Listing:
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def shipping_or_free(self) -> float | None:
        """Quoted rate, or $0 after Free fallback."""
        if self.shipping_cost is not None:
            return float(self.shipping_cost)
        return None

    def assume_free_shipping(self) -> None:
        """If ZIP rate never parsed, treat as Free ($0) and still compare."""
        if self.shipping_cost is not None:
            return
        self.shipping_cost = 0.0
        self.shipping_label = "Free"
        self.shipping_quoted = True

    def assume_comp_shipping(self) -> None:
        """Unknown comparable shipping counts as Free ($0) so comparison always has a Total."""
        self.assume_free_shipping()

    def landed_cost(self) -> float | None:
        """Price + quoted ZIP shipping. None until shipping is known — no Total."""
        if self.price is None or self.shipping_cost is None:
            return None
        return round(float(self.price) + float(self.shipping_cost), 2)

    def comparison_cost(self) -> float | None:
        """Conservative comparison value: landed cost, or sticker price if ship is unknown.

        Using the sticker price when shipping is unavailable can only make the
        comparable benchmark lower than (or equal to) its true delivered cost.
        That avoids silently inventing free shipping while still allowing useful
        comparisons when eBay hides the delivery module.
        """
        landed = self.landed_cost()
        if landed is not None:
            return landed
        if self.price is None:
            return None
        return round(float(self.price), 2)


@dataclass(slots=True)
class DealAlert:
    listing: Listing
    kind: str  # deal | bundle
    cheaper_pct: float | None
    average_price: float | None
    median_price: float | None
    comparable_count: int = 0
    reason: str = ""
    comparable_urls: list[str] = field(default_factory=list)
    savings_usd: float | None = None
    ship_to_zip: str = "19801"
    figure_count: int | None = None
    bundle_names_unparsed: bool = False
    bundle_value_sum: float | None = None
    top_comps: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class CheckResult:
    listing: Listing | None = None
    alert: DealAlert | None = None
    error: str | None = None
