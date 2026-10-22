#!/usr/bin/env python3
"""Raccoglie lo stato pubblico della coda Steam Controller e scrive data/status.json.

Fonti (tutte pubbliche, nessun login):
  - feed RSS ufficiale degli annunci hardware Valve (gruppo 45479024)
  - API store Steam (prezzo per paese) e pagina store (coda prenotazioni attiva o no)
  - forum "General Discussions" dello Steam Controller (app 4165870)

Solo libreria standard. Se una fonte fallisce si tengono i dati precedenti di
quella sezione e la si segna come non aggiornata.
"""

import datetime as dt
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

APP_ID = 4165870
PACKAGE_ID = 1558609
HARDWARE_GROUP = 45479024
STORE_PAGE = "https://store.steampowered.com/hardware/steamcontroller"
FEED_URL = f"https://store.steampowered.com/feeds/news/group/{HARDWARE_GROUP}/"
FORUM_URL = f"https://steamcommunity.com/app/{APP_ID}/discussions/0/"
SEARCH_URL = f"https://steamcommunity.com/app/{APP_ID}/discussions/search/"
# Paesi in cui controllare prezzo e disponibilità (codici store Steam).
COUNTRIES = ["it", "de", "fr", "es", "nl", "gb"]

FORUM_PAGES = 8          # pagine dell'elenco thread da scorrere (15 thread l'una)
THREADS_TO_SCAN = 20     # thread pertinenti di cui leggere l'ultima pagina
SEARCH_QUERIES = ["email", "reservation", "order email", "Europe", "EU"]
KEEP_DAYS = 60           # quanto a lungo tenere thread e segnalazioni
DELAY = 3.5              # secondi tra le richieste a steamcommunity.com

# Reddit: prima l'API pubblica di Reddit (dati freschi), che però blocca spesso i
# server cloud; in quel caso l'archivio PullPush (stessi post, ma con giorni di ritardo).
REDDIT_SUBS = ["SteamController", "Steam", "SteamMachine", "SteamFrame", "SteamDeck"]
REDDIT_QUERIES = ["controller email", "controller reserved", "controller reservation", "controller invite"]
REDDIT_POST_QUERIES = ["steam controller email", "steam controller reservation"]
PULLPUSH = "https://api.pullpush.io/reddit/search"

OUT = Path(__file__).resolve().parent.parent / "data" / "status.json"
UA = "Mozilla/5.0 (compatible; steam-controller-easy status bot; +https://github.com/albeminimi/steam-controller-easy)"

# Thread che parlano di coda, ordini, mail, spedizioni (più lingue).
# Nel titolo basta un termine generico; nell'anteprima del primo post servono termini
# specifici, altrimenti frasi come "in order to" fanno passare thread di supporto.
STRONG = (
    r"e-?mails?|inbox|reserv\w*|queue|wait\s?list|invit\w*|2027|order\s+window|purchase\s+window|"
    r"pre-?order\w*|tracking\s+(number|code|link)|shipped|shipment|delivery|"
    r"prenota\w*|spedi\w*|consegn\w*|"
    r"reservier\w*|bestellung|versand\w*|lieferung|warteschlange|"
    r"r[ée]servation\w*|commande\w*|livraison|"
    r"reserva\w*|pedido\w*|env[ií]o|"
    r"zam[oó]wieni\w*|rezerwac\w*|bestelling|reservering"
)
TITLE_RE = re.compile(r"\b(" + STRONG + r"|mail|order\w*|ship\w*|deliver\w*|purchase\w*|stock|available|when\s+(will|should|can|do)\s+i)\b", re.I)
PREVIEW_RE = re.compile(r"\b(" + STRONG + r")\b", re.I)

# Menzioni europee: paesi, nomi locali, valuta, corrieri UE.
EU_TERMS = [
    "europe", "european", "europa", "eu", "eea", "uk", "united kingdom", "britain", "england", "scotland",
    "ireland", "germany", "deutschland", "german", "austria", "österreich", "switzerland", "schweiz",
    "italy", "italia", "italian", "italiano", "france", "french", "français", "spain", "españa",
    "spanish", "portugal", "netherlands", "nederland", "dutch", "holland", "belgium", "belgië", "belgique",
    "luxembourg", "poland", "polska", "polish", "czech", "česko", "slovakia", "hungary", "magyar",
    "romania", "bulgaria", "greece", "croatia", "slovenia", "denmark", "danmark", "sweden", "sverige",
    "swedish", "norway", "norge", "finland", "suomi", "estonia", "latvia", "lithuania", "lietuva",
    "€", "eur", "euro", "euros", "gbp", "£", "dhl", "gls", "dpd", "hermes", "postnl", "bpost",
    "poste", "brt", "correos", "colissimo", "inpost",
]
EU_RE = re.compile(
    r"(?<![\w])(" + "|".join(re.escape(t) for t in sorted(EU_TERMS, key=len, reverse=True)) + r")(?![\w])",
    re.I,
)

# Frasi del tipo "ho ricevuto la mail per acquistare".
EMAIL_GOT_RE = re.compile(
    r"(got|received|recieved|gotten|had)\s+(?:[\w-]+\s+){0,3}?(e-?mail|invite|invitation)"
    r"|(got|received|recieved)\s+(mine|my\s+controller|the\s+controller|my\s+purchase|it\s+(today|yesterday|this\s+week))\b"
    r"|(got|received|recieved)\s+it\s+(sometime|in\s+(late|early|mid)|last\s+\w+|a\s+(few|couple))"
    r"|(e-?mail|invite)\s+(came|arrived|showed up|just came|finally came)"
    # chi ha mancato le 72 ore o ha già comprato, la mail l'ha ricevuta
    r"|missed\s+(it|the\s+(e-?mail|window|deadline|invite))\s+by"
    r"|missed\s+(the|my)\s+(purchase\s+|order\s+|72[\s-]?h(ou)?r?\s+)?(window|e-?mail|invite|deadline)"
    r"|\b(just\s+|finally\s+)?(bought|purchased|paid\s+for)\s+(it|mine|one|the\s+controller)\b"
    r"|\bi\s+(just\s+|finally\s+)?ordered\s+(it|mine|one|yesterday|today)\b"
    r"|(ricevut[oa]|arrivat[ao])\s+(la|l'|una)?\s*(e-?mail|mail)"
    r"|(e-?mail|mail)\s+(bekommen|erhalten|gekriegt)"
    r"|re[çc]u\s+(le|l'|un)\s*(e-?mail|mail|courriel)"
    r"|(recib[ií]|me\s+lleg[oó])\s+(el|un)?\s*(correo|e-?mail|mail)",
    re.I,
)

# Data di prenotazione citata nel messaggio ("reserved on May 16th", "pre-ordered day 1"...).
MONTHS = {
    "jan": 1, "january": 1, "gennaio": 1, "januar": 1, "janvier": 1, "enero": 1,
    "feb": 2, "february": 2, "febbraio": 2, "februar": 2, "février": 2, "fevrier": 2, "febrero": 2,
    "mar": 3, "march": 3, "marzo": 3, "märz": 3, "maerz": 3, "mars": 3,
    "apr": 4, "april": 4, "aprile": 4, "avril": 4, "abril": 4,
    "may": 5, "maggio": 5, "mai": 5, "mayo": 5,
    "jun": 6, "june": 6, "giugno": 6, "juni": 6, "juin": 6, "junio": 6,
    "jul": 7, "july": 7, "luglio": 7, "juli": 7, "juillet": 7, "julio": 7,
    "aug": 8, "august": 8, "agosto": 8, "août": 8, "aout": 8,
    "sep": 9, "sept": 9, "september": 9, "settembre": 9, "septembre": 9, "septiembre": 9,
    "oct": 10, "october": 10, "ottobre": 10, "oktober": 10, "octobre": 10, "octubre": 10,
    "nov": 11, "november": 11, "novembre": 11, "noviembre": 11,
    "dec": 12, "december": 12, "dicembre": 12, "dezember": 12, "décembre": 12, "decembre": 12, "diciembre": 12,
}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
RES_KW_RE = re.compile(
    r"reserv\w*|pre-?order\w*|preorder\w*|queued|queue|signed\s+up|wait\s?list|"
    r"prenot\w*|reservier\w*|vorbestell\w*|r[ée]serv\w*|reserv[ée]\w*", re.I)
DATE_RE = re.compile(
    r"(?<![\w/])(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>" + _MON + r")\.?(?![a-z])"
    r"|(?<![a-z])(?P<m2>" + _MON + r")\.?\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?(?![\d:])"
    r"|(?P<part>early|mid|late|end\s+of|beginning\s+of|inizio|met[aà]|fine)\s+(?:of\s+|di\s+)?(?P<m3>" + _MON + r")(?![a-z])"
    r"|(?:\bin|\bnel(?:la)?\s+(?:mese\s+di\s+)?|\bim|\ben)\s+(?P<m4>" + _MON + r")(?![a-z])"
    r"|(?P<day1>day\s+(?:1|one)\b|launch\s+day|first\s+day|opening\s+day|primo\s+giorno|"
    r"(?:right\s+)?when\s+(?:the\s+)?reservations?\s+(?:opened|went\s+live))",
    re.I)
ESTIMATE_RE = re.compile(
    r"(?:estimat\w*|window|stima|finestra|said|showed|shows|says)\D{0,25}?(?P<a>sept(?:ember)?|dec(?:ember)?|2027|settembre|dicembre)"
    r"|(?P<b>sept(?:ember)?|dec(?:ember)?|2027|settembre|dicembre)\s+(?:2026\s+)?(?:estimat\w*|window|finestra)",
    re.I)
RES_OPEN = dt.date(2026, 5, 8)
OTHER_EVENT_RE = re.compile(r"\b(got|received|recieved|e-?mail|mail|bought|paid|purchased|arrived|shipped|delivered|ricevut\w*|arrivat\w*|spedit\w*)\b", re.I)


def reservation_info(text, post_ts):
    """Cerca nel testo la data di prenotazione (vicino a parole come "reserved") e la stima Valve."""
    posted = dt.datetime.fromtimestamp(post_ts, dt.timezone.utc).date()
    out = {"reserved": None, "reserved_approx": False, "reserved_phrase": None, "estimate": None}
    for m in DATE_RE.finditer(text):
        # La parola di prenotazione deve stare subito prima della data senza un "got the
        # email" in mezzo ("reserved in May and got the email on Sept 3": il 3 settembre
        # non è la prenotazione), oppure subito dopo ("16th May reservation").
        before = text[max(0, m.start() - 60): m.start()]
        after = text[m.end(): m.end() + 15]
        res_b = [x.end() for x in RES_KW_RE.finditer(before)]
        oth_b = [x.end() for x in OTHER_EVENT_RE.finditer(before)]
        res_a = [x.start() for x in RES_KW_RE.finditer(after)]
        oth_a = [x.start() for x in OTHER_EVENT_RE.finditer(after)]
        ok_before = res_b and (not oth_b or max(res_b) > max(oth_b))
        ok_after = res_a and not oth_a and not (oth_b and (not res_b or max(oth_b) > max(res_b)))
        if not (ok_before or ok_after):
            continue
        if m.group("day1"):
            day, approx = RES_OPEN, False
        else:
            mon = MONTHS[(m.group("m1") or m.group("m2") or m.group("m3") or m.group("m4")).lower()]
            if m.group("m4"):
                d, approx = 15, True
            elif m.group("part"):
                part = m.group("part").lower()
                d = 5 if part.startswith(("early", "beginning", "inizio")) else 15 if part.startswith(("mid", "met")) else 25
                approx = True
            else:
                d = int(m.group("d1") or m.group("d2"))
                approx = False
            day = None
            for year in (posted.year, posted.year - 1):
                try:
                    cand = dt.date(year, mon, d)
                except ValueError:
                    break
                if RES_OPEN <= cand <= posted:
                    day = cand
                    break
            if day is None:
                continue
        out.update(reserved=day.isoformat(), reserved_approx=approx, reserved_phrase=m.group(0).strip())
        break
    e = ESTIMATE_RE.search(text)
    if e:
        w = (e.group("a") or e.group("b")).lower()
        out["estimate"] = "2027" if w == "2027" else "2026-09" if w.startswith(("sep", "set")) else "2026-12"
    return out


NEGATION_RE = re.compile(
    r"(haven'?t|have\s+not|hasn'?t|didn'?t|did\s+not|never|not\s+yet|still\s+no|no\s+one|nobody|"
    r"waiting\s+(to|for)|hope\s+to|could|would|will|might|(want|wanted|hoping|need|going|have|trying)\s+to|until\s+i|before\s+i|if\s+(you|i)|when\s+(you|i)|once\s+(you|i)|"
    r"non\s+(ho|mi)|ancora\s+(niente|nessuna)|nicht|noch\s+kein|pas\s+encore|todav[ií]a\s+no)\W+(\w+\W+){0,3}$",
    re.I)


def got_email(text):
    """Prima frase "ho ricevuto la mail" non negata ("haven't gotten an email" non conta)."""
    for m in EMAIL_GOT_RE.finditer(text):
        if not NEGATION_RE.search(text[max(0, m.start() - 40): m.start()]):
            return m
    return None


LAST_REQUEST = [0.0]


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def fetch(url, community=False, tries=3):
    if community:
        wait = DELAY - (time.time() - LAST_REQUEST[0])
        if wait > 0:
            time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read().decode("utf-8", "replace")
            LAST_REQUEST[0] = time.time()
            return body
        except urllib.error.HTTPError as e:
            LAST_REQUEST[0] = time.time()
            if e.code in (429, 502, 503) and attempt < tries - 1:
                time.sleep(15 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise


def strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<blockquote.*?</blockquote>", " ", s, flags=re.S | re.I)  # niente citazioni di altri post
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def snippet(text, match=None, size=220):
    if match is None or len(text) <= size:
        return text[:size] + ("…" if len(text) > size else "")
    start = max(0, match.start() - size // 3)
    end = min(len(text), start + size)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def eu_terms(text):
    found = []
    for m in EU_RE.finditer(text):
        t = m.group(1)
        # "EU"/"UK"/"EUR" solo se scritti in maiuscolo, per non confondere parole comuni.
        if t.lower() in ("eu", "uk", "eur", "eea", "gbp") and t != t.upper():
            continue
        if t.lower() not in [f.lower() for f in found]:
            found.append(t)
    return found


# ---------------------------------------------------------------- annunci ufficiali

def collect_official():
    root = ET.fromstring(fetch(FEED_URL))
    posts = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        desc = strip_html(it.findtext("description") or "")
        pub = it.findtext("pubDate")
        when = dt.datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z") if pub else None
        about_controller = "steam controller" in (title + " " + desc).lower()
        about_queue = bool(re.search(r"reserv|queue|waitlist|order window|order email", title + " " + desc, re.I))
        posts.append({
            "title": title,
            "url": it.findtext("link"),
            "date": when.astimezone(dt.timezone.utc).isoformat(timespec="seconds") if when else None,
            "controller": about_controller,
            "reservations": about_queue,
            "summary": snippet(desc, size=320),
        })
    posts.sort(key=lambda p: p["date"] or "", reverse=True)
    return posts[:20]


# ---------------------------------------------------------------- store

def collect_store():
    out = {"countries": {}}
    for cc in COUNTRIES:
        try:
            d = json.loads(fetch(f"https://store.steampowered.com/api/appdetails?appids={APP_ID}&cc={cc}&l=english"))
            data = d.get(str(APP_ID), {}).get("data") or {}
            po = data.get("price_overview") or {}
            out["countries"][cc] = {
                "price": po.get("final_formatted") or None,
                "currency": po.get("currency"),
                "cents": po.get("final"),
                "listed": bool(po),
            }
        except Exception as e:  # un paese che fallisce non blocca gli altri
            out["countries"][cc] = {"error": str(e)[:120]}
    page = fetch(f"{STORE_PAGE}?cc=it&l=english")
    out["reservation_queue"] = "reservation_widget" in page
    m = re.search(r"reservation_learn_more_link\\?&quot;:\\?&quot;(.*?)\\?&quot;", page)
    out["learn_more"] = m.group(1).replace("\\/", "/").replace("\\", "") if m else None
    out["page"] = STORE_PAGE
    return out


# ---------------------------------------------------------------- forum

TOPIC_BLOCK = re.compile(
    r'<div[^>]*class="forum_topic [^"]*"[^>]*data-gidforumtopic="(\d+)"[^>]*data-tooltip-forum="(.*?)">'
    r"(.*?)<div style=\"clear: both;\"></div>",
    re.S,
)


def parse_topic_list(page):
    topics = []
    for gid, tooltip, body in TOPIC_BLOCK.findall(page):
        name_m = re.search(r'forum_topic_name[^>]*>(.*?)</div>', body, re.S)
        if not name_m:
            continue
        title = strip_html(re.sub(r'<span class="forum_topic_label.*?</span>', "", name_m.group(1), flags=re.S))
        href = re.search(r'href="([^"]+)"', body)
        ts = re.search(r'data-timestamp="(\d+)"', body)
        rc = re.search(r'forum_topic_reply_count">.*?>\s*(\d+)', body, re.S)
        tip = html.unescape(tooltip)
        preview_m = re.search(r'topic_hover_text">(.*?)</div>', tip, re.S)
        topics.append({
            "id": gid,
            "title": title,
            "preview": strip_html(preview_m.group(1)) if preview_m else "",
            "url": href.group(1) if href else f"{FORUM_URL}{gid}/",
            "last_post": int(ts.group(1)) if ts else None,
            "replies": int(rc.group(1)) if rc else 0,
            "pinned": "sticky_label" in body,
            "locked": "forum_icon_Locked" in body,
        })
    return topics


def parse_thread_comments(page):
    comments = []
    op = re.search(r'class="forum_op[ "].*?data-timestamp="(\d+)".*?id="forum_op_content_\d+"[^>]*>(.*?)</div>', page, re.S)
    if op:
        comments.append({"id": "op", "ts": int(op.group(1)), "text": strip_html(op.group(2))})
    for cid, chunk in re.findall(r'id="comment_(\d+)"[^>]*>(.*?)(?=id="comment_\d+"|class="forum_paging|$)', page, re.S):
        ts = re.search(r'commentthread_comment_timestamp"[^>]*data-timestamp="(\d+)"', chunk)
        txt = re.search(r'commentthread_comment_text"[^>]*>(.*?)</div>', chunk, re.S)
        if ts and txt:
            comments.append({"id": cid, "ts": int(ts.group(1)), "text": strip_html(txt.group(1))})
    return comments


def collect_forum(errors):
    seen = {}
    pages_ok = 0
    for p in range(1, FORUM_PAGES + 1):
        try:
            page = fetch(f"{FORUM_URL}?fp={p}", community=True)
        except Exception as e:
            errors.append(f"forum pagina {p}: {e}")
            break
        pages_ok += 1
        for t in parse_topic_list(page):
            seen.setdefault(t["id"], t)
    for q in SEARCH_QUERIES:
        try:
            page = fetch(f"{SEARCH_URL}?gidforum=0&q={urllib.parse.quote(q)}", community=True, tries=2)
        except Exception as e:
            errors.append(f"ricerca forum '{q}': {e}")
            continue
        for t in parse_topic_list(page):
            seen.setdefault(t["id"], t)
    if pages_ok == 0:
        raise RuntimeError("nessuna pagina del forum leggibile")

    relevant = []
    for t in seen.values():
        if t["pinned"]:
            continue
        hay = f"{t['title']} {t['preview']}"
        if TITLE_RE.search(t["title"]) or PREVIEW_RE.search(t["preview"]):
            t["eu"] = eu_terms(hay)
            relevant.append(t)
    relevant.sort(key=lambda t: t["last_post"] or 0, reverse=True)

    reports = []
    cutoff = time.time() - KEEP_DAYS * 86400
    for t in relevant[:THREADS_TO_SCAN]:
        if (t["last_post"] or 0) < cutoff:
            continue
        last_page = max(1, math.ceil(t["replies"] / 15))
        try:
            page = fetch(t["url"] + (f"?ctp={last_page}" if last_page > 1 else ""), community=True, tries=2)
        except Exception as e:
            errors.append(f"thread {t['id']}: {e}")
            continue
        eu_in_thread = bool(t["eu"])
        for c in parse_thread_comments(page):
            terms = eu_terms(c["text"])
            if terms and not t["eu"]:
                t["eu"] = terms
            if c["ts"] < cutoff:
                continue
            m = got_email(c["text"])
            info = reservation_info(c["text"], c["ts"])
            # Teniamo chi ha ricevuto la mail, e chi aspetta ancora ma dice da quando.
            if not m and not info["reserved"]:
                continue
            reports.append({
                "id": f"{t['id']}-{c['id']}",
                "date": iso(c["ts"]),
                "status": "got" if m else "waiting",
                "text": snippet(c["text"], m or RES_KW_RE.search(c["text"])),
                **info,
                "eu": terms or (t["eu"] if eu_in_thread else []),
                "thread": t["title"],
                "url": t["url"] + (f"?ctp={last_page}" if last_page > 1 else "") + (f"#c{c['id']}" if c["id"] != "op" else ""),
            })
    for t in relevant:
        t["last_post"] = iso(t["last_post"]) if t["last_post"] else None
        t["preview"] = snippet(t["preview"], size=200)
        t.pop("pinned", None)
        t.pop("locked", None)
    return {"threads": relevant[:40], "reports": reports, "pages_read": pages_ok}


# ---------------------------------------------------------------- reddit

CONTROLLER_RE = re.compile(r"\bcontroller\b", re.I)
# Steam Machine e Frame: i bundle "512 w/ controller" hanno code e waitlist proprie
# (il Controller non ha waitlist), quindi le loro mail non contano.
OTHER_PRODUCT_RE = re.compile(
    r"\b(machine|frame|gabe\s?cube|deck|index|bundle|512|2\s?tb|1\s?tb|wait\s?-?list\w*|sku)\b"
    r"|\b(with|w/|w|without|w/o|no)\s+(a\s+)?controller\b", re.I)
EXPLICIT_RE = re.compile(r"steam\s+controller|controller\s+(queue|reservation|e-?mail|purchase)|my\s+controller|the\s+controller", re.I)


def about_controller(text, m, in_controller_sub):
    """La frase trovata parla del Controller e non di Steam Machine/Frame?"""
    window = text[max(0, m.start() - 90): m.end() + 90]
    if OTHER_PRODUCT_RE.search(window):
        return False
    return in_controller_sub or bool(EXPLICIT_RE.search(window))


def _reddit_json(url, wait=6):
    time.sleep(wait)
    try:
        return json.loads(fetch(url, tries=1))
    except urllib.error.HTTPError as e:
        if e.code != 429:
            raise
        time.sleep(45)  # PullPush e Reddit limitano le richieste ravvicinate
        return json.loads(fetch(url, tries=1))


def reddit_items_direct(since):
    """Post e commenti recenti dall'API pubblica di Reddit (spesso 403/429 dai server cloud)."""
    items = []
    subs = "+".join(REDDIT_SUBS)
    for q in REDDIT_QUERIES:
        url = (f"https://www.reddit.com/r/{subs}/search.json?q={urllib.parse.quote(q)}"
               f"&restrict_sr=1&sort=new&t=month&limit=50&raw_json=1")
        for ch in _reddit_json(url)["data"]["children"]:
            items.append(("post", ch["data"]))
    for ch in _reddit_json(f"https://www.reddit.com/r/SteamController/comments.json?limit=100&raw_json=1")["data"]["children"]:
        items.append(("comment", ch["data"]))
    return [(k, d) for k, d in items if d.get("created_utc", 0) >= since]


def reddit_items_pullpush(since):
    # PullPush accetta un solo subreddit per richiesta: cerchiamo su tutto Reddit e
    # teniamo solo i subreddit che ci interessano.
    items = []
    wanted = {x.lower() for x in REDDIT_SUBS}
    calls = [("comment", f"{PULLPUSH}/comment/?subreddit=SteamController&after={int(since)}&size=100&sort=desc")]
    calls += [("comment", f"{PULLPUSH}/comment/?q={urllib.parse.quote(q)}&after={int(since)}&size=100&sort=desc")
              for q in REDDIT_QUERIES]
    calls += [("post", f"{PULLPUSH}/submission/?q={urllib.parse.quote(q)}&after={int(since)}&size=100&sort=desc")
              for q in REDDIT_POST_QUERIES]
    failed = []
    for kind, url in calls:
        try:
            data = _reddit_json(url).get("data", [])
        except Exception as e:  # una ricerca che fallisce non butta via le altre
            failed.append(str(e))
            continue
        for d in data:
            if (d.get("subreddit") or "").lower() in wanted:
                items.append((kind, d))
    if len(failed) == len(calls):
        raise RuntimeError(f"PullPush non raggiungibile: {failed[0]}")
    return items


def collect_reddit(errors):
    since = time.time() - KEEP_DAYS * 86400
    try:
        items, via = reddit_items_direct(since), "reddit"
    except Exception as e:
        errors.append(f"reddit diretto: {e} (uso PullPush)")
        items, via = reddit_items_pullpush(since), "pullpush"
    reports, threads, seen = [], {}, set()
    latest = 0
    for kind, d in items:
        rid = d.get("name") or d.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        ts = int(d.get("created_utc") or 0)
        latest = max(latest, ts)
        sub = d.get("subreddit") or ""
        url = "https://www.reddit.com" + (d.get("permalink") or "")
        title = d.get("title") or d.get("link_title") or ""
        text = strip_html(html.escape(d.get("body") or d.get("selftext") or ""))
        whole = f"{title} {text}"
        if text in ("[deleted]", "[removed]"):
            continue
        # Fuori da r/SteamController serve che si parli del controller.
        if sub.lower() != "steamcontroller" and not CONTROLLER_RE.search(whole):
            continue
        if kind == "post" and (TITLE_RE.search(title) or PREVIEW_RE.search(text)):
            threads[rid] = {
                "id": f"reddit-{rid}", "source": "reddit", "title": f"{title} (r/{sub})",
                "preview": snippet(text, size=200), "url": url, "last_post": iso(ts),
                "replies": int(d.get("num_comments") or 0), "eu": eu_terms(whole),
            }
        body = text if kind == "comment" else whole
        in_sub = sub.lower() == "steamcontroller"
        m = got_email(body)
        if m and not about_controller(body, m, in_sub):
            m = None
        info = reservation_info(body, ts)
        if info["reserved"]:
            rm = RES_KW_RE.search(body)
            if not rm or not about_controller(body, rm, in_sub):
                info = {"reserved": None, "reserved_approx": False, "reserved_phrase": None, "estimate": None}
        if not m and not info["reserved"]:
            continue
        reports.append({
            "id": f"reddit-{rid}", "source": "reddit", "date": iso(ts),
            "status": "got" if m else "waiting",
            "text": snippet(body, m or RES_KW_RE.search(body)),
            **info,
            "eu": eu_terms(body),
            "thread": f"r/{sub}" + (f" · {title}" if title else ""),
            "url": url,
        })
    return {"via": via, "latest": iso(latest) if latest else None,
            "threads": sorted(threads.values(), key=lambda t: t["last_post"], reverse=True)[:20],
            "reports": reports}


# ---------------------------------------------------------------- unione con lo storico

def merge(prev, key, new, keep_old=True, id_key="id"):
    """Unisce elenchi per id, conservando first_seen e scartando ciò che è troppo vecchio.

    keep_old=False tiene solo i nuovi elementi (l'elenco thread è ricalcolato a ogni
    giro); keep_old=True conserva anche quelli non più visti (le segnalazioni stanno
    su pagine di thread che non rileggiamo ogni volta).
    """
    old = {x[id_key]: x for x in (prev or [])}
    stamp = now_utc().isoformat(timespec="seconds")
    out = {}
    for x in new:
        x["first_seen"] = old.get(x[id_key], {}).get("first_seen", stamp)
        out[x[id_key]] = x
    if keep_old:
        for k, x in old.items():
            out.setdefault(k, x)
    cutoff = (now_utc() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    items = [x for x in out.values() if (x.get(key) or "") >= cutoff]
    items.sort(key=lambda x: x.get(key) or "", reverse=True)
    return items


def main():
    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text())
        except json.JSONDecodeError:
            prev = {}
    errors = []
    status = {"generated_at": now_utc().isoformat(timespec="seconds"), "sources": {}}
    events = list(prev.get("events", []))

    def section(name, fn):
        try:
            value = fn()
            status["sources"][name] = {"ok": True, "updated_at": status["generated_at"]}
            return value
        except Exception as e:
            errors.append(f"{name}: {e}")
            status["sources"][name] = {
                "ok": False,
                "error": str(e)[:200],
                "updated_at": prev.get("sources", {}).get(name, {}).get("updated_at"),
            }
            return None

    official = section("official", collect_official)
    if official is not None:
        # Il feed restituisce solo gli ultimi ~20 post: teniamo quelli già visti, così
        # l'annuncio del 18 giugno sulla coda non sparisce quando escono post più nuovi.
        known = {p["url"]: p for p in prev.get("official", [])}
        if known:
            for p in official:
                if p["url"] not in known and "steam controller" in p["title"].lower():
                    events.append({"date": status["generated_at"], "type": "official",
                                   "text": f"Nuovo annuncio Valve: {p['title']}", "url": p["url"]})
        merged = {**known, **{p["url"]: p for p in official}}
        status["official"] = sorted(merged.values(), key=lambda p: p["date"] or "", reverse=True)[:40]
    else:
        status["official"] = prev.get("official", [])

    store = section("store", collect_store)
    status["store"] = store if store is not None else prev.get("store", {})
    if store is not None and prev.get("store") and prev["store"].get("reservation_queue") != store["reservation_queue"]:
        events.append({"date": status["generated_at"], "type": "store",
                       "text": "Lo store mostra di nuovo la coda di prenotazione" if store["reservation_queue"]
                       else "Lo store non mostra più la coda di prenotazione: forse acquisto diretto",
                       "url": STORE_PAGE})

    forum = section("forum", lambda: collect_forum(errors))
    if forum is not None:
        status["forum"] = {
            "threads": merge(prev.get("forum", {}).get("threads"), "last_post", forum["threads"], keep_old=False),
            "reports": merge(prev.get("forum", {}).get("reports"), "date", forum["reports"]),
            "pages_read": forum["pages_read"],
        }
    else:
        status["forum"] = prev.get("forum", {"threads": [], "reports": []})

    reddit = section("reddit", lambda: collect_reddit(errors))
    old_r = prev.get("reddit", {})
    if reddit is not None:
        status["reddit"] = {
            "via": reddit["via"],
            "latest": reddit["latest"],
            "threads": merge(old_r.get("threads"), "last_post", reddit["threads"], keep_old=False),
            "reports": merge(old_r.get("reports"), "date", reddit["reports"]),
        }
    else:
        status["reddit"] = old_r or {"threads": [], "reports": []}

    status["events"] = sorted(events, key=lambda e: e["date"], reverse=True)[:30]
    status["errors"] = errors
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(status, ensure_ascii=False, indent=1) + "\n")
    print(f"scritto {OUT} — errori: {len(errors)}")
    for e in errors:
        print("  ", e, file=sys.stderr)


if __name__ == "__main__":
    main()
