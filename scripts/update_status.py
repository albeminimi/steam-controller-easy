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
    r"(got|received|recieved|get|getting|gotten|had)\s+(my|the|an|our|a|this)?\s*"
    r"(purchase|order|buy|reservation|invite|invitation)?\s*(e-?mail|invite|invitation)"
    r"|(e-?mail|invite)\s+(came|arrived|showed up|just came|finally came)"
    # chi ha mancato le 72 ore o ha già comprato, la mail l'ha ricevuta
    r"|missed\s+(it|the\s+(e-?mail|window|deadline|invite))\s+by"
    r"|\b(just\s+|finally\s+)?(bought|purchased|paid\s+for)\s+(it|mine|one|the\s+controller)\b"
    r"|\bi\s+(just\s+|finally\s+)?ordered\s+(it|mine|one|yesterday|today)\b"
    r"|(ricevut[oa]|arrivat[ao])\s+(la|l'|una)?\s*(e-?mail|mail)"
    r"|(e-?mail|mail)\s+(bekommen|erhalten|gekriegt)"
    r"|re[çc]u\s+(le|l'|un)\s*(e-?mail|mail|courriel)"
    r"|(recib[ií]|me\s+lleg[oó])\s+(el|un)?\s*(correo|e-?mail|mail)",
    re.I,
)

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
            m = EMAIL_GOT_RE.search(c["text"])
            if m and c["ts"] >= cutoff:
                reports.append({
                    "id": f"{t['id']}-{c['id']}",
                    "date": iso(c["ts"]),
                    "text": snippet(c["text"], m),
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

    status["events"] = sorted(events, key=lambda e: e["date"], reverse=True)[:30]
    status["errors"] = errors
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(status, ensure_ascii=False, indent=1) + "\n")
    print(f"scritto {OUT} — errori: {len(errors)}")
    for e in errors:
        print("  ", e, file=sys.stderr)


if __name__ == "__main__":
    main()
