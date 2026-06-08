#!/usr/bin/env python3
"""
PSG Stock Monitor — version GitHub Actions (corrigee)
Alertes : Email + WhatsApp + SMS (Twilio) + Discord Webhook
 
Points cles de cette version :
- Detection a 3 etats : "in_stock" / "out_of_stock" / "unknown".
  -> On n'alerte JAMAIS sur "unknown" et on ne re-ecrit pas l'etat dans ce cas,
     pour ne pas confondre "vraiment hors stock" et "page bloquee / illisible".
- Signal de stock priorise sur le JSON-LD schema.org (offers.availability),
  qui est stable et independant des noms de classes CSS.
- Fallback textuel cible (epuise / rupture / indisponible) + bouton panier.
- Detection des pages-challenge anti-bot (Akamai, 403/429, page minuscule).
"""
 
import json
import logging
import os
import time
from datetime import datetime, timezone
 
import requests
from bs4 import BeautifulSoup
 
# Charge un fichier .env s'il existe (utile en local).
# Sur GitHub Actions, le module peut etre absent : on ignore silencieusement.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
 
# ─────────────────────────────────────────
#  Config via variables d'environnement (.env en local / Secrets sur Actions)
# ─────────────────────────────────────────
 
PRODUCT_URL = os.environ.get("PRODUCT_URL", "").strip()
 
# Email
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "").strip()
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "").strip()
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "").strip()
 
# Twilio (WhatsApp + SMS)
TWILIO_SID        = os.environ.get("TWILIO_SID", "").strip()
TWILIO_TOKEN      = os.environ.get("TWILIO_TOKEN", "").strip()
TWILIO_FROM_PHONE = os.environ.get("TWILIO_FROM_PHONE", "").strip()   # ex: +12015551234
TWILIO_TO_PHONE   = os.environ.get("TWILIO_TO_PHONE", "").strip()     # ex: +33612345678
TWILIO_FROM_WA    = os.environ.get("TWILIO_FROM_WA", "").strip()      # ex: whatsapp:+14155238886
TWILIO_TO_WA      = os.environ.get("TWILIO_TO_WA", "").strip()        # ex: whatsapp:+33612345678
 
# Discord
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
 
# Taille a surveiller (informatif, utilise dans les messages)
TARGET_SIZE = os.environ.get("TARGET_SIZE", "M").strip().upper()
 
STATE_FILE = "stock_state.json"
DEFAULT_NAME = "Maillot PSG Back2Back"
 
# ─────────────────────────────────────────
#  Logger
# ─────────────────────────────────────────
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("psg_monitor")
 
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
 
# Etats possibles
IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"
UNKNOWN = "unknown"
 
# ─────────────────────────────────────────
#  Recuperation de la page (avec retries)
# ─────────────────────────────────────────
 
def fetch_html(url: str, retries: int = 3) -> tuple[str | None, int | None]:
    """Renvoie (html, status_code). html=None si echec total."""
    last_status = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            last_status = resp.status_code
            if resp.status_code == 200 and resp.text:
                return resp.text, resp.status_code
            log.warning(f"Tentative {attempt}/{retries} : HTTP {resp.status_code}")
        except requests.RequestException as e:
            log.warning(f"Tentative {attempt}/{retries} : {e}")
        time.sleep(2 * attempt)
    return None, last_status
 
 
def looks_like_bot_block(html: str, status: int | None) -> bool:
    """Detecte une page-challenge / blocage (Akamai, captcha, page vide)."""
    if status in (403, 429, 503):
        return True
    if not html or len(html) < 1500:
        return True
    low = html.lower()
    markers = [
        "access denied", "reference #", "akamai",
        "request unsuccessful", "captcha", "are you a human",
        "pardon our interruption", "bot detection",
    ]
    return any(m in low for m in markers)
 
# ─────────────────────────────────────────
#  Detection du stock (3 etats)
# ─────────────────────────────────────────
 
OOS_PATTERNS = [
    "épuisé", "epuise", "rupture de stock", "rupture",
    "indisponible", "out of stock", "sold out",
]
 
 
def _availability_from_jsonld(soup: BeautifulSoup) -> str | None:
    """Lit offers.availability dans les blocs JSON-LD schema.org."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
 
        # JSON-LD peut etre un objet, une liste, ou contenir @graph
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = data.get("@graph", [data])
        else:
            candidates = []
 
        for node in candidates:
            if not isinstance(node, dict):
                continue
            offers = node.get("offers")
            if not offers:
                continue
            offer_list = offers if isinstance(offers, list) else [offers]
            avails = []
            for off in offer_list:
                if isinstance(off, dict) and off.get("availability"):
                    avails.append(str(off["availability"]).lower())
            if not avails:
                continue
            # Au moins une offre dispo => in stock
            if any("instock" in a or "limitedavailability" in a for a in avails):
                return IN_STOCK
            if all(("outofstock" in a or "soldout" in a or "discontinued" in a) for a in avails):
                return OUT_OF_STOCK
    return None
 
 
def _product_name(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    return DEFAULT_NAME
 
 
def _availability_from_text(soup: BeautifulSoup) -> str | None:
    """Fallback : signaux dans la zone produit (main/h1) plutot que toute la page."""
    region = soup.find("main") or soup.find("h1") or soup
    text = region.get_text(separator=" ", strip=True).lower() if region else ""
 
    if any(p in text for p in OOS_PATTERNS):
        return OUT_OF_STOCK
 
    # Bouton "ajouter au panier" actif ?
    def is_cart(t):
        if t.name not in ("button", "a"):
            return False
        blob = (" ".join(t.get("class", [])) + " " + t.get_text()).lower()
        return any(k in blob for k in
                   ["ajouter au panier", "add to cart", "add-to-cart", "addtocart", "btn-cart"])
 
    cart = soup.find(is_cart)
    if cart and not cart.get("disabled"):
        return IN_STOCK
    return None
 
 
def check_stock(url: str) -> tuple[str, str]:
    """Renvoie (status, product_name) ; status in {in_stock, out_of_stock, unknown}."""
    if not url:
        log.error("PRODUCT_URL vide.")
        return UNKNOWN, DEFAULT_NAME
 
    html, status = fetch_html(url)
    if html is None:
        log.error(f"Impossible de recuperer la page (HTTP {status}).")
        return UNKNOWN, DEFAULT_NAME
 
    if looks_like_bot_block(html, status):
        log.warning("Page de blocage / challenge anti-bot detectee -> etat UNKNOWN.")
        return UNKNOWN, DEFAULT_NAME
 
    soup = BeautifulSoup(html, "html.parser")
    name = _product_name(soup)
 
    # 1) Source la plus fiable : JSON-LD schema.org
    via_jsonld = _availability_from_jsonld(soup)
    if via_jsonld:
        log.info(f"Dispo via JSON-LD : {via_jsonld}")
        return via_jsonld, name
 
    # 2) Fallback textuel cible
    via_text = _availability_from_text(soup)
    if via_text:
        log.info(f"Dispo via texte/bouton : {via_text}")
        return via_text, name
 
    # 3) On n'a rien pu conclure : UNKNOWN (et surtout pas "hors stock")
    log.warning("Aucun signal de stock exploitable -> etat UNKNOWN.")
    return UNKNOWN, name
 
# ─────────────────────────────────────────
#  Etat precedent (anti-spam)
# ─────────────────────────────────────────
 
def load_last_state() -> str | None:
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("status")
    except (FileNotFoundError, json.JSONDecodeError):
        return None
 
 
def save_state(status: str):
    with open(STATE_FILE, "w") as f:
        json.dump(
            {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()},
            f,
            ensure_ascii=False,
            indent=2,
        )
 
# ─────────────────────────────────────────
#  Alertes
# ─────────────────────────────────────────
 
def send_email(product_name: str, url: str):
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECEIVER):
        log.info("Email non configure, ignore.")
        return
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"DISPO : {product_name} (taille {TARGET_SIZE})"
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER
        when = datetime.now().strftime("%d/%m/%Y a %H:%M:%S")
        html = f"""
        <html><body style="margin:0;padding:0;background:#001529;font-family:Arial,sans-serif">
          <div style="max-width:520px;margin:40px auto;background:#0a2240;border-radius:12px;overflow:hidden">
            <div style="background:#e30613;padding:24px 32px;text-align:center">
              <h1 style="margin:0;color:white;font-size:26px;letter-spacing:2px">PSG STOCK ALERT</h1>
            </div>
            <div style="padding:32px">
              <h2 style="color:white;margin:0 0 16px">{product_name}</h2>
              <p style="color:#7fb3d3;margin:0 0 24px">La taille <strong style="color:white">{TARGET_SIZE}</strong> est de nouveau disponible.</p>
              <a href="{url}" style="display:inline-block;background:#e30613;color:white;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:bold">Acheter maintenant</a>
            </div>
            <div style="padding:16px 32px;border-top:1px solid #1a3a5c">
              <p style="color:#4a7a9b;margin:0;font-size:12px">Alerte du {when}</p>
            </div>
          </div>
        </body></html>
        """
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        log.info("Email envoye.")
    except Exception as e:
        log.error(f"Email echoue : {e}")
 
 
def send_sms_and_whatsapp(product_name: str, url: str):
    if not (TWILIO_SID and TWILIO_TOKEN):
        log.info("Twilio non configure, SMS/WhatsApp ignores.")
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        message = f"PSG STOCK DISPO !\n{product_name} - Taille {TARGET_SIZE}\nAchete vite : {url}"
 
        if TWILIO_FROM_PHONE and TWILIO_TO_PHONE:
            client.messages.create(body=message, from_=TWILIO_FROM_PHONE, to=TWILIO_TO_PHONE)
            log.info("SMS envoye.")
        if TWILIO_FROM_WA and TWILIO_TO_WA:
            client.messages.create(body=message, from_=TWILIO_FROM_WA, to=TWILIO_TO_WA)
            log.info("WhatsApp envoye.")
    except Exception as e:
        log.error(f"Twilio echoue : {e}")
 
 
def send_discord(product_name: str, url: str):
    if not DISCORD_WEBHOOK:
        log.info("Discord non configure, ignore.")
        return
    try:
        payload = {
            "username": "PSG Stock Bot",
            "embeds": [{
                "title": "STOCK DISPONIBLE !",
                "description": f"**{product_name}**\nLa taille **{TARGET_SIZE}** est de nouveau en stock.",
                "color": 14886144,
                "fields": [
                    {"name": "Action", "value": f"[Acheter maintenant]({url})", "inline": False},
                    {"name": "Detecte a", "value": datetime.now().strftime("%d/%m/%Y a %H:%M:%S"), "inline": False},
                ],
                "footer": {"text": "PSG Stock Monitor - GitHub Actions"},
            }]
        }
        resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info("Discord envoye.")
        else:
            log.error(f"Discord echoue : {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.error(f"Discord echoue : {e}")
 
 
def send_all_alerts(product_name: str, url: str):
    send_email(product_name, url)
    send_sms_and_whatsapp(product_name, url)
    send_discord(product_name, url)
 
# ─────────────────────────────────────────
#  Main
# ─────────────────────────────────────────
 
def main():
    log.info(f"Verification : {PRODUCT_URL or '(URL manquante)'}")
    status, product_name = check_stock(PRODUCT_URL)
    last_state = load_last_state()
 
    log.info(f"Produit : {product_name}")
    log.info(f"Etat : {status} (precedent : {last_state})")
 
    if status == UNKNOWN:
        # On ne touche PAS a l'etat : on garde la derniere valeur fiable connue.
        log.info("Etat indetermine : aucune alerte, etat precedent conserve.")
        return
 
    if status == IN_STOCK and last_state != IN_STOCK:
        log.info("NOUVEAU STOCK -> envoi des alertes.")
        send_all_alerts(product_name, PRODUCT_URL)
    elif status == IN_STOCK:
        log.info("Toujours en stock, pas de re-notification.")
    else:
        log.info("Hors stock.")
 
    save_state(status)
 
 
if __name__ == "__main__":
    main()
