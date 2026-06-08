"""
Centralized notification message strings — Swahili.
All push notification titles and bodies are defined here so they can be
updated in one place without hunting through multiple service files.
"""

# ── Customer — Order lifecycle ────────────────────────────────────────────────

def order_drafted(short_id: str) -> tuple[str, str]:
    return (
        "Oda Imeandaliwa",
        f"Oda #{short_id} iko tayari. Kamilisha malipo ili itumwe kwa mgahawa."
    )

def order_confirmed(short_id: str) -> tuple[str, str]:
    return (
        "Oda Imethibitishwa ✅",
        f"Oda #{short_id} imethibitishwa. Mkahawa wako unaanza kuiandaa sasa."
    )

def order_preparing(short_id: str) -> tuple[str, str]:
    return (
        "Chakula Kinaandaliwa 🍳",
        f"Oda #{short_id} inaandaliwa. Subiri kidogo, chakula chako kinakuja."
    )

def order_ready(short_id: str) -> tuple[str, str]:
    return (
        "Chakula Kiko Tayari 🎉",
        f"Oda #{short_id} iko tayari kuchukuliwa. Tunasubiri dereva."
    )

def driver_assigned(short_id: str, rider_name: str = "Dereva") -> tuple[str, str]:
    return (
        "Dereva Amepewa Oda 🛵",
        f"{rider_name} anakuja kuchukua oda #{short_id}. Atafika hivi karibuni."
    )

def order_picked(short_id: str) -> tuple[str, str]:
    return (
        "Oda Imechukuliwa 🛵",
        f"Dereva amechukua oda #{short_id} na anakuelekea wewe."
    )

def order_delivered(short_id: str) -> tuple[str, str]:
    return (
        "Oda Imefika! 😋",
        f"Oda #{short_id} imewasili. Furahia chakula chako! Tafadhali toa ukadiriaji."
    )

def order_cancelled_customer(short_id: str) -> tuple[str, str]:
    return (
        "Oda Imefutwa",
        f"Oda #{short_id} imefutwa. Tutakurudishia malipo yako hivi karibuni."
    )

def order_cancelled_restaurant_delay(short_id: str) -> tuple[str, str]:
    return (
        "Oda Imefutwa — Mkahawa Ulichelewa",
        f"Oda #{short_id} imefutwa kwa sababu mkahawa haukuanza kuiandaa kwa wakati. "
        "Utapata refund yako."
    )

def delivery_delayed(short_id: str) -> tuple[str, str]:
    return (
        "Oda Inachelewa ⏳",
        f"Oda #{short_id} inachelewa kidogo — tunaendelea kutafuta dereva. "
        "Tutakujulisha hivi karibuni."
    )

def refund_processed(short_id: str) -> tuple[str, str]:
    return (
        "Refund Imekamilika ✅",
        f"Malipo ya oda #{short_id} yamerudishwa kwenye akaunti yako."
    )

# ── Customer — Disputes ───────────────────────────────────────────────────────

def dispute_raised_customer() -> tuple[str, str]:
    return (
        "Malalamiko Yamepokelewa",
        "Tunakagua malalamiko yako. Tutakujibu hivi karibuni."
    )

def dispute_resolved_customer(resolution: str) -> tuple[str, str]:
    return (
        "Malalamiko Yametatuliwa ✅",
        f"Malalamiko yako yametatuliwa: {resolution}."
    )

# ── Vendor — Order events ─────────────────────────────────────────────────────

def vendor_new_order(short_id: str, items_count: int) -> tuple[str, str]:
    return (
        "Oda Mpya! 🍽️",
        f"Una oda mpya iliyolipwa: #{short_id} ({items_count} bidhaa). Ianze kuandaa sasa."
    )

def vendor_new_group_order(short_id: str, items_count: int) -> tuple[str, str]:
    return (
        "Oda ya Pamoja Mpya! 👥",
        f"Oda ya pamoja imepokelewa: #{short_id} ({items_count} bidhaa). Ianze kuandaa."
    )

def vendor_order_overdue(short_id: str, minutes: int) -> tuple[str, str]:
    return (
        "Oda Imechelewa — Inaendelea Kuandaliwa",
        f"Oda #{short_id} imekuwa katika hali ya THIBITISHO kwa dakika {minutes}. "
        "Imeanza moja kwa moja. Tafadhali iandae haraka."
    )

def vendor_batch_delivering(batch_short: str) -> tuple[str, str]:
    return (
        "Oda Zinasafirishwa! 🚗",
        f"Oda zako za kikundi {batch_short} zinaelekea kwa wateja na dereva."
    )

def vendor_funds_received(short_id: str) -> tuple[str, str]:
    return (
        "Malipo Yamepokelewa 💰",
        f"Malipo ya oda #{short_id} yameongezwa kwenye pochi yako."
    )

def vendor_dispute_raised(short_id: str) -> tuple[str, str]:
    return (
        "Oda Inapigiwa Kura",
        f"Oda #{short_id} imepewa malalamiko na mteja. Tunachunguza."
    )

def vendor_dispute_resolved(short_id: str) -> tuple[str, str]:
    return (
        "Malalamiko Yametatuliwa",
        f"Malalamiko ya oda #{short_id} yametatuliwa."
    )

def vendor_need_driver(order_count: int) -> tuple[str, str]:
    return (
        "Tunahitaji Dereva! 🚗",
        f"Una oda {order_count} zinazongoja kusafirishwa. "
        "Tutajaribu tena kila dakika 5."
    )

# ── Driver — Offer & earnings ─────────────────────────────────────────────────

def driver_new_offer(order_count: int, payout: str) -> tuple[str, str]:
    return (
        "Ofa Mpya ya Utoaji! 🛵",
        f"Kuna oda {order_count} zinazosubiri. Malipo: TSh {payout}. Kubali haraka!"
    )

def driver_earning_credited(short_id: str) -> tuple[str, str]:
    return (
        "Malipo ya Utoaji Yamepokelewa 💵",
        f"Mapato ya utoaji #{short_id} yamewekwa kwenye pochi yako."
    )

def driver_dispute_raised(short_id: str) -> tuple[str, str]:
    return (
        "Oda Inapigiwa Kura",
        f"Malalamiko yametolewa kwenye oda #{short_id}. "
        "Malipo yako yanashikiliwa mpaka suala litatatuliwe."
    )

def driver_dispute_resolved(short_id: str) -> tuple[str, str]:
    return (
        "Malalamiko Yametatuliwa",
        f"Malalamiko ya oda #{short_id} yametatuliwa na malipo yatatolewa."
    )

# ── Group orders (Pools) ──────────────────────────────────────────────────────

def pool_locked(restaurant_name: str, expires_min: int = 15) -> tuple[str, str]:
    return (
        "Oda ya Pamoja Imefungwa — Lipa Sasa! 🔒",
        f"Oda ya pamoja katika {restaurant_name} imefungwa. "
        f"Kamilisha malipo kabla ya dakika {expires_min} hazijakwisha."
    )

def pool_paid(restaurant_name: str) -> tuple[str, str]:
    return (
        "Malipo ya Pamoja Yamekamilika! 🎉",
        f"Oda ya pamoja katika {restaurant_name} imelipwa kikamilifu. "
        "Mkahawa unaandaa chakula chenu."
    )

def pool_cancelled(restaurant_name: str) -> tuple[str, str]:
    return (
        "Oda ya Pamoja Imefutwa",
        f"Oda ya pamoja katika {restaurant_name} imefutwa. "
        "Malipo yaliyolipwa yatarudishwa."
    )

def pool_completed(restaurant_name: str) -> tuple[str, str]:
    return (
        "Oda ya Pamoja Imekamilika! 🎉",
        f"Oda zote za kikundi kutoka {restaurant_name} zimewasili. Furahieni chakula!"
    )

# ── Admin alerts ──────────────────────────────────────────────────────────────

def admin_no_drivers(batch_short: str) -> tuple[str, str]:
    return (
        "Hakuna Madereva! ⚠️",
        f"Kikundi {batch_short} hakiwezi kupata madereva baada ya majaribio yote. "
        "Hatua ya mkono inahitajika."
    )

def admin_pool_cancelled_batch_transit(pool_short: str, batch_short: str, batch_status: str) -> tuple[str, str]:
    return (
        "Pool Imefutwa — Kikundi Kinaendelea ⚠️",
        f"Pool {pool_short} imefutwa lakini kikundi chake {batch_short} "
        f"kiko katika hali '{batch_status}'. Hatua ya mkono inahitajika."
    )

def admin_new_dispute(order_short: str, customer_name: str) -> tuple[str, str]:
    return (
        "Malalamiko Mapya",
        f"Oda #{order_short} imepewa malalamiko na {customer_name}."
    )

# ── Driver — Single-order offer ─────────────────────────────────────────────
# Used in apps/events/utils.py broadcast_order_status for OFFERED status
def single_order_offered(restaurant_name: str) -> tuple[str, str]:
    return (
        "Oda Mpya ya Utoaji! 🛵",
        f"Oda kutoka {restaurant_name} iko tayari kuchukuliwa."
    )

# ── Customer — Rider nearby ──────────────────────────────────────────────────

def rider_nearby(pin: str) -> tuple[str, str]:
    return (
        "Dereva Karibu! 🛵",
        f"Dereva wako yuko karibu na eneo lako. PIN: {pin}. Tafadhali jiandae!"
    )

# ── Customer — Unpaid order expiry ───────────────────────────────────────────

def order_expired_unpaid(short_id: str) -> tuple[str, str]:
    return (
        "Oda Imeisha Muda — Malipo Hayajafanywa",
        f"Oda #{short_id} imekwisha muda kwa sababu malipo hayakukamilika ndani ya dakika 30."
    )

# ── Customer — Nearby pool notification ─────────────────────────────────────

def new_nearby_pool(restaurant_name: str) -> tuple[str, str]:
    return (
        "Oda ya Pamoja Karibu! 👥",
        f"{restaurant_name} ina oda ya pamoja karibu nawe. Bofya kujiunga!"
    )

# ── Admin — Dispute escalated ───────────────────────────────────────────────

def dispute_escalated_admin(dispute_id: str, reason: str) -> tuple[str, str]:
    return (
        "Malalamiko Yamepandishwa ⏰",
        f"Malalamiko {dispute_id} yamepandishwa kiotomatiki ({reason})."
    )

# ── Demand/Reverse Auction ────────────────────────────────────────────────────

def get_demand_message(title: str, radius_km: int) -> tuple[str, str]:
    return (
        f"Ombi Jipya Karibu Nayo 📢",
        f"Mtu anatafuta '{title}' umbali wa {radius_km}km kwako. Bofya kuona na kujibu."
    )

def get_offer_message() -> tuple[str, str]:
    return (
        "Ofa Mpya Imewasili 💰",
        "Muuzaji ametuma ofa kwenye ombi lako. Angalia sasa!"
    )

def get_demand_accepted_message_for_buyer(title: str, seller_name: str) -> tuple[str, str]:
    return (
        "Ombi Limekubaliwa! ✅",
        f"'{title}' limekubaliwa na {seller_name}. Wasiliana naye kupitia maelezo ya mawasiliano."
    )

def get_demand_cancelled_message(title: str) -> tuple[str, str]:
    return (
        "Ombi Limefutwa",
        f"Ombi la '{title}' limefutwa na mnunuzi. Ofa yako imekataliwa."
    )

# ── Background worker (Flutter side) ─────────────────────────────────────────
# These are used in background_sync_service.dart — kept as Python constants
# for reference. The actual Dart strings are in background_sync_service.dart.

FLUTTER_DELIVERED = ("Oda Imefika! 😋", "Oda #{id} imewasili. Furahia chakula chako! Tafadhali toa ukadiriaji.")
FLUTTER_CANCELLED = ("Oda Imefutwa", "Oda #{id} imefutwa. Tutakurudishia malipo yako hivi karibuni.")
FLUTTER_EXPIRED = ("Oda Imekwisha Muda", "Oda #{id} iliisha muda kabla dereva hajapatikana.")
FLUTTER_DISPUTED = ("Malalamiko Yametolewa", "Malalamiko yametolewa kwenye oda #{id}. Timu yetu inachunguza.")
