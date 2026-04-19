"""
Telegram message sender and Gemini API client.
Uses direct HTTP calls — no heavy SDK dependencies.
"""

import logging
import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_CHAT_IDS, GEMINI_API_KEY, GEMINI_URL

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


async def send_telegram(
    text: str, parse_mode: str = "HTML",
    _allow_chunked: bool = True, reply_to_message_id: int | None = None,
) -> int | None:
    """Send a message to all configured Telegram chats. Returns message_id from primary chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        logger.warning("Telegram credentials not configured")
        return None

    primary_msg_id = None
    for idx, chat_id in enumerate(TELEGRAM_CHAT_IDS):
        # A reply message id only belongs to the chat where it was created.
        # For secondary chats we send the same content without reply threading.
        chat_reply_id = reply_to_message_id if idx == 0 else None
        msg_id = await _send_to_chat(
            chat_id, text, parse_mode, _allow_chunked, chat_reply_id,
        )
        if primary_msg_id is None:
            primary_msg_id = msg_id
    return primary_msg_id


async def _send_to_chat(
    chat_id: str, text: str, parse_mode: str = "HTML",
    _allow_chunked: bool = True, reply_to_message_id: int | None = None,
) -> int | None:
    """Send a message to a single Telegram chat. Returns message_id on success."""
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result", {}).get("message_id")
                body = await resp.text()
                logger.error(f"Telegram API error {resp.status} for chat {chat_id}: {body}")

                # If message too long, try splitting
                if _allow_chunked and resp.status == 400 and "message is too long" in body.lower():
                    return await _send_telegram_chunked(
                        chat_id, text, parse_mode, reply_to_message_id
                    )
                return None
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return None


async def _send_telegram_chunked(
    chat_id: str, text: str, parse_mode: str, reply_to_message_id: int | None = None,
) -> int | None:
    """Split long messages into 4000-char chunks. Returns last message_id."""
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

    last_msg_id = None
    for i, chunk in enumerate(chunks):
        reply_id = reply_to_message_id if i == 0 else None
        msg_id = await _send_to_chat(
            chat_id, chunk, parse_mode, _allow_chunked=False,
            reply_to_message_id=reply_id,
        )
        if msg_id:
            last_msg_id = msg_id
    return last_msg_id


def format_anomaly_message(
    home_team: str, away_team: str,
    score_home: int, score_away: int,
    minute: int, league: str,
    condition_type: str, triggered_rules: list[str],
    stats: dict,
    alert_number: int = 1,
) -> str:
    """Format an anomaly alert for Telegram."""
    cond_label = "🔴 BERABERLİK Anomalisi" if condition_type == "A" else "🟡 1 Fark Anomalisi"
    emoji = "⚽"
    alert_tag = f"🔔 <b>{alert_number}. Uyarı</b>" if alert_number > 1 else "🔔 <b>1. Uyarı</b>"

    lines = [
        f"{alert_tag} — {cond_label} (Koşul {condition_type})",
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

    ph = stats.get('possession_home', 0)
    pa = stats.get('possession_away', 0)
    dah = stats.get('dangerous_attacks_home', 0)
    daa = stats.get('dangerous_attacks_away', 0)
    tsh = stats.get('total_shots_home', 0)
    tsa = stats.get('total_shots_away', 0)
    soth = stats.get('shots_on_target_home', 0)
    sota = stats.get('shots_on_target_away', 0)
    ych = stats.get('yellow_cards_home', 0)
    yca = stats.get('yellow_cards_away', 0)
    rch = stats.get('red_cards_home', 0)
    rca = stats.get('red_cards_away', 0)

    na = "Veri yok"
    lines.append(f"  Topa Sahip Olma: {f'{ph:.0f}% - {pa:.0f}%' if ph or pa else na}")
    lines.append(f"  Tehlikeli Ataklar: {f'{dah} - {daa}' if dah or daa else na}")
    lines.append(f"  Toplam Şut: {f'{tsh} - {tsa}' if tsh or tsa else na}")
    lines.append(f"  İsabetli Şut: {f'{soth} - {sota}' if soth or sota else na}")
    lines.append(f"  Sarı Kart: {ych} - {yca}")
    lines.append(f"  Kırmızı Kart: {rch} - {rca}")

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
    """Build the analysis prompt for Gemini.

    Note: this model has no live web access; any "form", "injury" or
    "league standing" claim is from pre-training memory and therefore
    stale. The prompt forces the model to state its uncertainty explicitly
    and to downgrade confidence when data is unavailable, so users do not
    mistake hallucination for insight.
    """
    return f"""Sen 20+ yıllık deneyime sahip profesyonel bir futbol bahis analistisin.

ÖNEMLİ VERİ UYARISI:
- Gerçek zamanlı internet erişimin YOK. Takım formları, sakatlıklar,
  lig sıralamaları hakkında sahip olduğun bilgiler eğitim kesim tarihine
  ait ve GÜNCEL OLMAYABİLİR.
- Bir takım/lig hakkında yeterli ve güncel bilgin yoksa, o maç için
  "Yetersiz veri — yorum yok" de ve atla. UYDURMA.

Aşağıdaki yaklaşan futbol maçlarını analiz et. Her seçtiğin maç için:

1. Takımların son form durumu (eğer güvenilir bilgin varsa).
2. Motivasyon: şampiyonluk, düşme hattı, Avrupa kupası yarışı etkisi.
3. Olası sakatlık/cezalı etkisi (eğer bilgin varsa; yoksa belirtme).
4. "Tuzak" maçları tespit et ve önerilerin dışında tut.
5. Kesinlikle yüksek güven duymadığın hiçbir maçı ÖNERME.

ÇIKTI KURALLARI:
- Sadece YÜKSEK veya ÇOK YÜKSEK güvenli maçlar.
- Her önerilen maç için:
    * Tahmin (1X2, 2.5 Üst/Alt, KG Var/Yok vb.)
    * Güven seviyesi (Yüksek / Çok Yüksek)
    * 2-3 cümle gerekçe
    * Gerekçede varsayım varsa açıkça belirt ("bilinen son form itibarıyla...")
- Telegram için temiz biçim (uygun emoji). Türkçe yaz.
- Güvenli hiçbir öneri yoksa: "Bugün güvenli önerilecek maç bulunmuyor." de.

BUGÜNÜN MAÇLARI:
{matches_text}

Profesyonel analizini paylaş:"""
