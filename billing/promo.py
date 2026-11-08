from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from billing import store

KIND_LABELS = {
    "percent": "скидка %",
    "amount": "скидка ₽",
    "bonus_days": "+ дни",
    "free_days": "бесплатные дни",
}


class PromoError(ValueError):
    pass


@dataclass(frozen=True)
class Quote:
    code: str
    promo_id: str
    kind: str
    value: int
    discount_kopecks: int
    bonus_days: int
    days_granted: int
    final_kopecks: int
    needs_plan: bool = False

    @property
    def free(self) -> bool:
        return not self.needs_plan and self.final_kopecks <= 0

    @property
    def total_days(self) -> int:
        return self.days_granted + self.bonus_days

    @property
    def label(self) -> str:
        return describe({"kind": self.kind, "value": self.value})


def _parse_deadline(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def describe(promo: dict) -> str:
    value = int(promo.get("value") or 0)
    kind = promo.get("kind")
    if kind == "percent":
        return f"−{value}%"
    if kind == "amount":
        return f"−{Decimal(value) / Decimal(100):.0f} ₽"
    if kind == "bonus_days":
        return f"+{value} дн."
    if kind == "free_days":
        return f"{value} дн. бесплатно"
    return "—"


def _validate(db_path: str, code: str, *, user_id: str,
              plan: Optional[dict]) -> dict:
    """Every check that does not depend on a plan's price."""
    promo = store.get_promo_by_code(db_path, code)
    if not promo:
        raise PromoError("Промокод не найден")
    if not promo.get("active"):
        raise PromoError("Промокод больше не действует")
    if plan is not None and promo.get("plan_id") and promo["plan_id"] != plan["id"]:
        raise PromoError("Промокод действует на другой тариф")

    deadline = _parse_deadline(promo.get("valid_until"))
    if deadline and deadline < datetime.now(timezone.utc):
        raise PromoError("Срок действия промокода истёк")

    max_uses = int(promo.get("max_uses") or 0)
    if max_uses and int(promo.get("used_count") or 0) >= max_uses:
        raise PromoError("Промокод уже использован максимальное число раз")

    per_user = int(promo.get("per_user") or 1)
    if per_user and store.user_redemptions(db_path, promo["id"], user_id) >= per_user:
        raise PromoError("Вы уже использовали этот промокод")

    if not 1 <= int(promo.get("value") or 0) <= 3650 and promo.get("kind") != "percent":
        raise PromoError("Промокод настроен неверно")
    return promo


def quote(db_path: str, code: str, plan: Optional[dict], *, user_id: str) -> Quote:
    """Validate a promo code for this user, optionally against one plan.

    With ``plan=None`` the money part cannot be computed (a percentage means
    nothing without a price), so ``needs_plan`` is set and only the human
    label plus any day bonus are returned. The planner uses that to show
    "−10% на любой тариф" before the customer picks one.
    """
    promo = _validate(db_path, code, user_id=user_id, plan=plan)
    kind = promo.get("kind")
    value = int(promo.get("value") or 0)

    if kind == "percent":
        if not 1 <= value <= 100:
            raise PromoError("Промокод настроен неверно")
    elif kind in ("amount", "bonus_days", "free_days"):
        if value <= 0:
            raise PromoError("Промокод настроен неверно")
    else:
        raise PromoError("Неизвестный тип промокода")

    if plan is None:
        return Quote(
            code=promo["code"], promo_id=promo["id"], kind=kind, value=value,
            discount_kopecks=0,
            bonus_days=value if kind == "bonus_days" else 0,
            days_granted=value if kind == "free_days" else 0,
            final_kopecks=0,
            needs_plan=kind in ("percent", "amount"),
        )

    price = int(plan["price_kopecks"])
    discount = 0
    bonus_days = 0
    days_granted = 0

    if kind == "percent":
        discount = int(
            (Decimal(price) * Decimal(value) / Decimal(100)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP)
        )
    elif kind == "amount":
        discount = min(price, value)
    elif kind == "bonus_days":
        bonus_days = value
    elif kind == "free_days":
        discount = price
        days_granted = value

    return Quote(
        code=promo["code"],
        promo_id=promo["id"],
        kind=kind,
        value=value,
        discount_kopecks=discount,
        bonus_days=bonus_days,
        days_granted=days_granted,
        final_kopecks=max(0, price - discount),
    )
