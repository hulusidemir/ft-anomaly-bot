"""
Telegram message sender and Gemini API client.
Uses direct HTTP calls — no heavy SDK dependencies.
"""

import logging
import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY, GEMINI_URL

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


async def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured")
        return False

    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                logger.error(f"Telegram API error {resp.status}: {body}")

                # If message too long, try splitting
                if resp.status == 400 and "message is too long" in body.lower():
                    return await _send_telegram_chunked(text, parse_mode)
                return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


async def _send_telegram_chunked(text: str, parse_mode: str) -> bool:
    """Split long messages into 4000-char chunks."""
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        # Find a good split point
        split_at = text.rfind("\n", 0, 4000)
        if split_at == -1:
            split_at = 4000
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    success = True
    for chunk in chunks:
        ok = await send_telegram(chunk, parse_mode)
        if not ok:
            success = False
    return success


def format_anomaly_message(
    home_team: str, away_team: str,
    score_home: int, score_away: int,
    minute: int, league: str,
    condition_type: str, triggered_rules: list[str],
    stats: dict,
) -> str:
    """Format an anomaly alert for Telegram."""
    cond_label = "🔴 BERABERLİK Anomalisi" if condition_type == "A" else "🟡 1 Fark Anomalisi"
    emoji = "⚽"

    lines = [
        f"<b>{cond_label} (Koşul {condition_type})</b>",
        "",
        f"{emoji} <b>{home_team}</b> {score_home} - {score_away} <b>{away_team}</b>",
        f"⏱ Dakika: {minute}'",
        f"🏆 {league}",
        "",
        "<b>Tetiklenen Kurallar:</b>",
    ]

    for i, rule in enumerate(triggered_rules, 1):
        lines.append(f"  {i}. {rule}")

    lines.append("")
    lines.append("<b>İstatistik Özeti:</b>")
    lines.append(f"  Topa Sahip Olma: {stats.get('possession_home', 0):.0f}% - {stats.get('possession_away', 0):.0f}%")
    lines.append(f"  Tehlikeli Ataklar: {stats.get('dangerous_attacks_home', 0)} - {stats.get('dangerous_attacks_away', 0)}")
    lines.append(f"  Toplam Şut: {stats.get('total_shots_home', 0)} - {stats.get('total_shots_away', 0)}")
    lines.append(f"  İsabetli Şut: {stats.get('shots_on_target_home', 0)} - {stats.get('shots_on_target_away', 0)}")
    lines.append(f"  Sarı Kart: {stats.get('yellow_cards_home', 0)} - {stats.get('yellow_cards_away', 0)}")
    lines.append(f"  Kırmızı Kart: {stats.get('red_cards_home', 0)} - {stats.get('red_cards_away', 0)}")

    return "\n".join(lines)


async def ask_gemini(prompt: str) -> str | None:
    """Send a prompt to Google Gemini and return the response text."""
    if not GEMINI_API_KEY:
        logger.warning("Gemini API key not configured")
        return None

    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096,
        },
    }

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", "")
                    return None
                else:
                    body = await resp.text()
                    logger.error(f"Gemini API error {resp.status}: {body[:500]}")
                    return None
    except Exception as e:
        logger.error(f"Gemini request failed: {e}")
        return None


def build_gemini_prompt(matches_text: str) -> str:
    """Build the analysis prompt for Gemini."""
    return f"""Sen 20+ yıllık deneyime sahip profesyonel bir futbol bahis analisti ve eski futbol menajerisin.

Aşağıdaki bugünün yaklaşan futbol maçlarını analiz et. Seçtiğin her maç için:

1. Takımların son form durumunu araştır (son 5 maç).
2. Motivasyon faktörlerini değerlendir: şampiyonluk, düşme hattı, Avrupa bileti vb. için kazanma ihtiyacı var mı?
3. Muhtemel sakatlık, cezalı oyuncular ve rotasyon etkisini dikkate al.
4. Cazip görünen ama riskli "tuzak" maçları tespit et.
5. Oranların takımı küçümsediği yüksek değerli fırsatları belirle.

KURALLAR:
- Sadece YÜKSEK GÜVEN duyduğun maçları öner.
- Tüm riskli/tuzak maçları önerilerin dışında bırak.
- Her önerilen maç için şunları ver:
    * Tahmin edilen sonuç (1X2, 2.5 Üst/Alt, KG Var/Yok vb.)
    * Güven seviyesi (Yüksek/Çok Yüksek)
    * Kısa gerekçe (2-3 cümle)
- Metni Telegram için temiz ve okunaklı biçimlendir (uygun emoji kullan).
- Cevabı Türkçe yaz.

BUGÜNÜN MAÇLARI:
{matches_text}

Aşağıda profesyonel analizini paylaş:"""
