#!/usr/bin/env python3
"""Watch DokkanDB cards, news and event listings for GLB and JP."""

from __future__ import annotations

import base64
import colorsys
import html
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
BASE = "https://www.dokkandb.com"
EVENT_CATEGORIES = [
    ("story", "Story Event", "/events?category=1"),
    ("bonus", "Bonus Event", "/events?category=2"),
    ("growth", "Growth Event", "/events?category=3"),
    ("limited", "Limited Event", "/events?category=4"),
    ("challenge", "Challenge Event", "/events?category=5"),
    ("z_battle", "Z Battle", "/events?category=999"),
    ("quest", "Quest", "/events?category=0"),
    ("db_saga", "DB Story · DB Saga", "/events?category=9000&dbStory=0"),
    ("db_z", "DB Story · Dragon Ball Z", "/events?category=9000&dbStory=1"),
    ("db_gt", "DB Story · Dragon Ball GT", "/events?category=9000&dbStory=2"),
    ("db_movies", "DB Story · Movies", "/events?category=9000&dbStory=3"),
    ("battlefield", "Battlefield", "/events?category=10000"),
]
TYPE_BY_CODE = {"10": "AGL", "11": "TEQ", "12": "INT", "13": "STR", "14": "PHY"}
TYPE_ICON_RE = re.compile(r"type[_-]icon[_-]?(1[0-4])", re.I)
TYPE_CODE_RE = re.compile(r"\b(AGL|TEQ|INT|STR|PHY)\b", re.I)
ROME = ZoneInfo("Europe/Rome")

def plain(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_listing_title(value: str) -> str:
    """Remove DokkanDB's broken-image placeholder from event/summon titles."""
    title = plain(value)
    title = re.sub(r"\[?\s*IMAGE\s+UNAVAILABLE\s*\]?", " ", title, flags=re.IGNORECASE)
    return plain(title).strip(" \t-:：|—–")


def clean_summon_title(value: str) -> str:
    """Backward-compatible title cleaner for summon listings."""
    return clean_listing_title(value)


def type_from_images(images: list[str]) -> str:
    """Read the DokkanDB type code from image metadata when it is explicit."""
    codes = {
        match.group(1)
        for image in images
        if (match := TYPE_ICON_RE.search(image or "")) is not None
    }
    if len(codes) != 1:
        return ""
    return TYPE_BY_CODE.get(next(iter(codes)), "")


def type_from_explicit_metadata(values: list[str]) -> str:
    """Use an explicit AGL/TEQ/INT/STR/PHY token from type-specific DOM metadata."""
    found = {
        match.group(1).upper()
        for value in values
        if (match := TYPE_CODE_RE.search(value or "")) is not None
    }
    return next(iter(found)) if len(found) == 1 else ""


def _css_rgb_values(value: str) -> list[tuple[int, int, int, float]]:
    """Extract RGB(A) and hex colors from computed CSS values."""
    result: list[tuple[int, int, int, float]] = []
    for match in re.finditer(
        r"rgba?\(\s*([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)(?:\s*[,/ ]\s*([\d.]+)%?)?\s*\)",
        value or "", re.I
    ):
        r, g, b = (max(0, min(255, int(float(match.group(i))))) for i in (1, 2, 3))
        alpha = float(match.group(4)) if match.group(4) else 1.0
        if alpha > 0.15:
            result.append((r, g, b, alpha))
    for match in re.finditer(r"#([0-9a-f]{3}|[0-9a-f]{6})\b", value or "", re.I):
        raw = match.group(1)
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        result.append(tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4)) + (1.0,))
    return result


def _dokkan_type_from_rgb(rgb: tuple[int, int, int, float]) -> str:
    red, green, blue, alpha = rgb
    hue, saturation, brightness = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    degrees = hue * 360
    if alpha <= 0.15 or saturation < 0.30 or brightness < 0.22:
        return ""
    if 185 <= degrees <= 250:
        return "AGL"       # blue
    if 80 <= degrees <= 175:
        return "TEQ"       # green
    if 255 <= degrees <= 325:
        return "INT"       # purple
    if degrees <= 15 or degrees >= 345:
        return "STR"       # red
    if 30 <= degrees <= 72:
        return "PHY"       # yellow
    return ""


def type_from_color_candidates(candidates: list[dict]) -> str:
    """Infer a type from weighted CSS colors; refuse ambiguous color evidence."""
    scores: dict[str, float] = {}
    for item in candidates or []:
        css = str(item.get("css", ""))
        try:
            weight = max(0.0, float(item.get("weight", 1.0)))
        except (TypeError, ValueError):
            weight = 1.0
        for rgb in _css_rgb_values(css):
            inferred = _dokkan_type_from_rgb(rgb)
            if inferred:
                scores[inferred] = scores.get(inferred, 0.0) + weight
    if not scores:
        return ""
    ranked = sorted(scores.items(), key=lambda entry: entry[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] < ranked[1][1] * 1.25:
        return ""
    return ranked[0][0]

def switch_region(page, path: str, region: str):
    """Open a DokkanDB page, select GLB/JP if needed, and return the response."""
    response = page.goto(urljoin(BASE, path), wait_until="domcontentloaded", timeout=60_000)
    try:
        jp_selector = page.get_by_role("checkbox", name="JP", exact=True)
        jp_selector.wait_for(state="attached", timeout=8_000)
        if jp_selector.is_checked() != (region == "JP"):
            selector = jp_selector if region == "JP" else page.get_by_role("checkbox", name="GLB", exact=True)
            selector.click()
            page.wait_for_timeout(1_200)
    except Exception:
        selector = page.locator(f'text="{region}"').first
        selector.wait_for(state="visible", timeout=15_000)
        selector.click()
        page.wait_for_timeout(1_200)
    return response

def extract_cards(page, region: str, known_ids: set[str] | None = None) -> list[dict[str, str]]:
    switch_region(page, "/cards", region)

    def read_page() -> list[dict]:
        return page.locator('a[href^="/cards/"]').evaluate_all(
            """els => {
              const seen = new Set(), out = [];
              for (const a of els) {
                const href = a.getAttribute('href');
                if (!href || !/^\\/cards\\/\\d+/.test(href) || seen.has(href)) continue;
                seen.add(href);
                // Keep the original image scope for rarity/EZA detection unchanged.
                let root = a;
                for (let i=0; i<6 && root; i++, root=root.parentElement) {
                  const cardLinks = root.querySelectorAll('a[href^="/cards/"]');
                  if (cardLinks.length === 1 && root.querySelector('img')) break;
                }
                root = root || a;
                const imgs = [...(root?.querySelectorAll('img') || [] )].map(i => ({src:i.getAttribute('src')||'', alt:i.alt||''}));

                // For the type only, find a card-specific container separately.
                // Do not fall back to a broad ancestor containing neighboring cards.
                let typeRoot = null;
                for (let node=a, i=0; node && i<10; i++, node=node.parentElement) {
                  const cardLinks = node.querySelectorAll('a[href^="/cards/"]');
                  if (cardLinks.length === 1 && node.querySelector('img')) {
                    typeRoot = node;
                    break;
                  }
                }
                typeRoot = typeRoot || a;
                const typeImgs = [...typeRoot.querySelectorAll('img')].map(i => ({src:i.getAttribute('src')||'', alt:i.alt||''}));

                // Capture explicit type metadata and CSS colors from the card itself.
                // Prefer the Type asset and its immediate wrappers, not neighboring tiles.
                const typeMeta = [];
                const colorCandidates = [];
                const seenNodes = new Set();
                const addNodeStyle = (node, weight) => {
                  if (!node || seenNodes.has(node) || weight <= 0) return;
                  seenNodes.add(node);
                  const attrs = [node.getAttribute?.('class')||'', node.id||'', node.getAttribute?.('title')||'',
                    node.getAttribute?.('aria-label')||''];
                  for (const attr of attrs) if (attr) typeMeta.push(attr);
                  const inlineStyle = node.getAttribute?.('style') || '';
                  if (inlineStyle) colorCandidates.push({css:inlineStyle, weight:weight * 0.8});
                  const style = getComputedStyle(node);
                  for (const prop of ['backgroundColor','borderTopColor','borderRightColor','borderBottomColor','borderLeftColor','color','fill','stroke']) {
                    const css = style[prop];
                    if (css && css !== 'transparent' && css !== 'rgba(0, 0, 0, 0)')
                      colorCandidates.push({css, weight: weight * (prop === 'backgroundColor' ? 1 : (['color','fill','stroke'].includes(prop) ? 0.35 : 0.65))});
                  }
                  for (const prop of ['backgroundImage','boxShadow']) {
                    const css = style[prop];
                    if (css && css !== 'none') colorCandidates.push({css, weight: weight * 0.55});
                  }
                };
                const typeImageNodes = [...typeRoot.querySelectorAll('img')];
                for (const img of typeImageNodes) {
                  const meta = [img.getAttribute('src')||'', img.alt||'', img.title||'', img.className||''];
                  typeMeta.push(...meta);
                  const isTypeAsset = /type/i.test(meta.join(' '));
                  let node = img.parentElement;
                  for (let depth=0; node && depth<4; depth++, node=node.parentElement)
                    addNodeStyle(node, (isTypeAsset ? 10 : 4) - depth * 1.8);
                }
                addNodeStyle(typeRoot, 2.5);
                out.push({href, name:(a.innerText||a.getAttribute('aria-label')||'').trim(),
                  tile:(root?.innerText||'').trim().slice(0,500), imgs, typeImgs, typeMeta, colorCandidates});
              }
              return out;
            }"""
        )

    raw_cards = read_page()
    collected = list(raw_cards)
    for _ in range(8):
        if not known_ids or any(re.search(r"/cards/(\d+)", card["href"])[1] in known_ids for card in raw_cards):
            break
        next_href = page.locator('a[href^="/cards"]').evaluate_all(
            """els => {
              const anchors=els.map(a=>({href:a.getAttribute('href'),text:(a.innerText||'').trim(),
                aria:a.getAttribute('aria-label')||'',title:a.title||'',rel:a.rel||''})).filter(a=>a.href);
              const next=anchors.find(a=>/next/i.test(a.aria+' '+a.title+' '+a.rel) || /^(next|›|»|→)$/i.test(a.text));
              if(next) return next.href;
              const current=Number(new URL(location.href).searchParams.get('page')||'1');
              const numbered=anchors.filter(a=>a.text===String(current+1));
              return numbered.length ? numbered[0].href : null;
            }"""
        )
        if not next_href:
            break
        response = page.goto(urljoin(BASE, next_href), wait_until="domcontentloaded", timeout=60_000)
        if response is not None and response.status >= 400:
            break
        
        try:
            selector = page.get_by_role("button", name=region, exact=True)
            selector.wait_for(state="visible", timeout=8_000)
            selector.click()
        except Exception:
            selector = page.locator(f'text="{region}"').first
            selector.wait_for(state="visible", timeout=15_000)
            selector.click()
            
        page.wait_for_timeout(700)
        raw_cards = read_page()
        known_page_ids = {re.search(r"/cards/(\d+)", card["href"])[1] for card in collected}
        collected.extend(card for card in raw_cards if re.search(r"/cards/(\d+)", card["href"])[1] not in known_page_ids)
    cards = collected
    output = []
    for card in cards:
        card_id = re.search(r"/cards/(\d+)", card["href"])[1]
        image_info = " ".join(f"{i['src']} {i['alt']}" for i in card.get("imgs", []))
        rarity_match = re.search(r"(?:rare[_-])?(LR|UR|SSR|SR|R|N)(?:\.png|\b)", image_info, re.I)
        type_image_values = [
            f"{image.get('src', '')} {image.get('alt', '')}"
            for image in card.get("typeImgs", [])
        ]
        card_type = (
            type_from_images(type_image_values)
            or type_from_explicit_metadata(card.get("typeMeta", []))
            or type_from_color_candidates(card.get("colorCandidates", []))
        )
        tile = plain(card.get("tile", ""))
        name = plain(card.get("name", "")).split("\n")[0]
        date_match = re.search(r"\b\d{1,2}[ /-][A-Za-z]{3,}[^\n]{0,24}\b\d{1,2}:\d{2}\b", tile)
        awakening = "SEZA" if re.search(r"\b(?:SEZA|SUPER EZA)\b", image_info, re.I) else (
            "EZA" if re.search(r"\bEZA\b", image_info, re.I) else "")
        detail = {
            "title": "", "name": name,
            "rarity": rarity_match.group(1).upper() if rarity_match else "",
            "type": card_type,
            "awakening": awakening, "date": date_match.group(0) if date_match else "",
        }
        output.append({"id": card_id, "url": urljoin(BASE, card["href"]), **detail,
                       "name": detail.get("name") or plain(card.get("name")) or f"Card {card_id}"})
    return output

def card_detail(page, href: str, region: str, fallback_type: str = "") -> dict[str, str]:
    switch_region(page, href, region)
    data = page.locator("body").evaluate(
        """body => {
            const main = body.querySelector('main') || body;
            const headings = [...main.querySelectorAll('h1,h2,h3,h4')];
            const cardHeading = headings.find(x => /\\s+-\\s+Card Details$/i.test((x.innerText||'').trim()));
            const typePattern = /type[_-]icon[_-]?(1[0-4])/i;
            const typeMeta = [];
            const colorCandidates = [];
            const seenNodes = new Set();
            const addNodeStyle = (node, weight) => {
              if (!node || seenNodes.has(node) || weight <= 0) return;
              seenNodes.add(node);
              for (const attr of [node.getAttribute?.('class')||'', node.id||'', node.getAttribute?.('title')||'',
                                  node.getAttribute?.('aria-label')||'']) {
                if (attr) typeMeta.push(attr);
              }
              const inlineStyle = node.getAttribute?.('style') || '';
              if (inlineStyle) colorCandidates.push({css:inlineStyle, weight:weight * 0.8});
              const style = getComputedStyle(node);
              for (const prop of ['backgroundColor','borderTopColor','borderRightColor','borderBottomColor','borderLeftColor','color','fill','stroke']) {
                const css = style[prop];
                if (css && css !== 'transparent' && css !== 'rgba(0, 0, 0, 0)')
                  colorCandidates.push({css, weight: weight * (prop === 'backgroundColor' ? 1 : (['color','fill','stroke'].includes(prop) ? 0.35 : 0.65))});
              }
              for (const prop of ['backgroundImage','boxShadow']) {
                const css = style[prop];
                if (css && css !== 'none') colorCandidates.push({css, weight: weight * 0.55});
              }
            };

            const leaderHeading = headings.find(x => /^Leader Skill/i.test((x.innerText||'').trim()));
            const leaderTop = leaderHeading ? leaderHeading.getBoundingClientRect().top + window.scrollY : Infinity;
            const topImages = [...main.querySelectorAll('img')].filter(img => {
              const y = img.getBoundingClientRect().top + window.scrollY;
              return y < leaderTop;
            });
            const typeImages = topImages.filter(img => /type/i.test([
              img.getAttribute('src')||'', img.alt||'', img.title||'', img.className||''
            ].join(' ')));
            const relevantImages = typeImages.length ? typeImages : topImages.slice(0, 12);
            for (const img of relevantImages) {
              const meta = [img.getAttribute('src')||'', img.alt||'', img.title||'', img.className||''];
              typeMeta.push(...meta);
              const isTypeAsset = /type/i.test(meta.join(' '));
              let node = img.parentElement;
              for (let depth=0; node && depth<5; depth++, node=node.parentElement)
                addNodeStyle(node, (isTypeAsset ? 12 : 4) - depth * 1.6);
            }
            // Also inspect the card heading's own visual container and nearby ancestors.
            for (let node=cardHeading, depth=0; node && depth<6; node=node.parentElement, depth++)
              addNodeStyle(node, 5 - depth * 0.7);

            const imgs = [...main.querySelectorAll('img')].map(x => x.getAttribute('src') || '');
            const explicitTypeImages = topImages.map(img => {
              const src = img.getAttribute('src') || '';
              const match = src.match(typePattern);
              return match ? match[0] : '';
            }).filter(Boolean);
            return {
                text: main.innerText || '',
                heads: headings.map(x => (x.innerText||'').trim()).filter(Boolean),
                imgs,
                explicit_type_images: explicitTypeImages,
                type_meta: typeMeta,
                color_candidates: colorCandidates
            };
        }"""
    )
    lines = [plain(x) for x in data["text"].splitlines() if plain(x)]
    heads = [plain(x) for x in data["heads"]]
    main_heading = next((x for x in heads if re.search(r"\s+-\s+Card Details$", x, re.I)), "")
    name = re.sub(r"\s+-\s+Card Details$", "", main_heading, flags=re.I)

    img_text = " ".join(data["imgs"])
    rarity = re.search(r"rare[_-](LR|UR|SSR|SR|R|N)\b", img_text, re.I)
    detail_type = (
        type_from_images(data.get("explicit_type_images", []))
        or type_from_explicit_metadata(data.get("type_meta", []))
        or type_from_color_candidates(data.get("color_candidates", []))
    )
    card_text = " ".join([name, *heads, *lines[-55:], img_text])
    awakening = "SEZA" if re.search(r"\b(?:SEZA|SUPER EZA)\b", card_text, re.I) else ("EZA" if re.search(r"\bEZA\b", card_text, re.I) else "")
    date = next((line for line in lines if re.search(r"\d{1,2}:\d{2}", line) and re.search(r"\d{4}", line)), "")
    
    return {
        "name": name,
        "rarity": rarity.group(1).upper() if rarity else "",
        "type": detail_type or fallback_type,
        "awakening": awakening,
        "date": date
    }
    
def extract_news(page, region: str) -> list[dict[str, str]]:
    switch_region(page, "/news", region)
    entries = page.locator('a[href^="/news/"]').evaluate_all(
        """els => { const seen=new Set(), out=[]; for(const a of els){
          const href=a.getAttribute('href'); if(!href || !/^\\/news\\/\\d+/.test(href) || seen.has(href)) continue;
          seen.add(href); const root=a.closest('article')||a.parentElement?.parentElement||a;
          out.push({href,title:(a.querySelector('h1,h2,h3,h4')?.innerText||a.innerText||'').trim(),text:(root?.innerText||'').trim().slice(0,300)});
        } return out; }"""
    )
    return [{"id": re.search(r"/news/(\d+)", e["href"])[1], "url": urljoin(BASE, e["href"]),
             "title": plain(e["title"]).split("\n")[0], "date": news_date(e["text"])} for e in entries]

def news_date(value: str) -> str:
    text = plain(value)
    months = {"jan": "jan", "feb": "feb", "mar": "mar", "apr": "apr", "may": "may", "jun": "jun",
              "jul": "jul", "aug": "aug", "sep": "sep", "oct": "oct", "nov": "nov", "dec": "dec"}
    patterns = [
        (r"\b(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})\D{0,12}(\d{1,2}):(\d{2})\b", "dmy"),
        (r"\b([A-Za-z]{3,})\s+(\d{1,2}),?\s+(\d{4})\D{0,12}(\d{1,2}):(\d{2})\b", "mdy"),
        (r"\b(\d{4})-(\d{2})-(\d{2})\D{0,12}(\d{1,2}):(\d{2})\b", "ymd"),
        (r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\D{0,12}(\d{1,2}):(\d{2})\b", "numeric"),
    ]
    for pattern, kind in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        if kind == "dmy":
            day, month, year, hour, minute = match.groups()
            month = months.get(month[:3].lower(), month[:3].lower())
        elif kind == "mdy":
            month, day, year, hour, minute = match.groups()
            month = months.get(month[:3].lower(), month[:3].lower())
        elif kind == "ymd":
            year, month_num, day, hour, minute = match.groups()
            month_names = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
            month = month_names[int(month_num) - 1]
        else:
            day, month, year, hour, minute = match.groups()
        return f"{int(day)} {month} {year}, {int(hour)}:{minute}"
    return ""

def extract_summons(page, region: str, category: int) -> list[dict[str, str]]:
    switch_region(page, f"/summons?category={category}&page=1", region)
    entries = page.locator('a[href^="/summons/"]').evaluate_all(
        """els => { const seen=new Set(), out=[]; for(const a of els){
          const href=a.getAttribute('href'); if(!href || !/^\\/summons\\/\\d+$/.test(href) || seen.has(href)) continue;
          seen.add(href); const root=a.closest('article')||a.parentElement?.parentElement||a;
          out.push({href,title:(a.querySelector('h1,h2,h3,h4')?.innerText||a.innerText||'').trim(),text:(root?.innerText||'').trim().slice(0,350)});
        } return out; }"""
    )
    return [{"id": re.search(r"/summons/(\d+)", item["href"])[1],
             "url": urljoin(BASE, item["href"]), "title": clean_summon_title(item["title"]),
             "date": news_date(item["text"])} for item in entries]

def extract_events(page, region: str, path: str) -> list[dict[str, str]]:
    switch_region(page, path, region)
    entries = page.locator('a[href^="/events/"]').evaluate_all(
        """els => { const seen=new Set(), out=[]; for(const a of els){
          const href=a.getAttribute('href');
          if(!href || !/^\\/events\\/[^/?]+\\/\\d+(?:\\?.*)?$/.test(href) || seen.has(href)) continue;
          seen.add(href); const root=a.closest('article')||a.parentElement?.parentElement||a;
          const heading=a.querySelector('h1,h2,h3,h4');
          out.push({href,title:(heading?.innerText||a.innerText||a.getAttribute('aria-label')||'').trim(),text:(root?.innerText||'').trim().slice(0,300)});
        } return out; }"""
    )
    result = []
    for e in entries:
        title = clean_listing_title(e["title"])
        if not title:
            title = clean_listing_title(e["text"].split(" ")[0])
        result.append({"id": e["href"], "url": urljoin(BASE, e["href"]), "title": title})
    return result

def quest_area(page, region: str, area_number: int) -> dict[str, object] | None:
    try:
        # switch_region already navigates to this URL: avoid the previous duplicate page.goto.
        response = switch_region(page, f"/events/quests/{area_number}", region)
        if response is not None and response.status >= 400:
            return None
        stage_ids = page.locator('a[href*="/events/quests/"]').evaluate_all(
            """els => { const ids=new Set(); for(const a of els){
              const m=(a.getAttribute('href')||'').match(/^\\/events\\/quests\\/(\\d+)\\/(\\d+)$/);
              if(m) ids.add(m[2]);
            } return [...ids]; }"""
        )
        stages = sorted({int(stage_id) % 1000 // 10 for stage_id in stage_ids})
        if not stages:
            return None
        return {"area": area_number, "url": urljoin(BASE, f"/events/quests/{area_number}"), "stages": stages}
    except Exception as exc:
        print(f"Avviso: lettura quest area {area_number} fallita per {region}: {exc}", file=sys.stderr)
        return None
        
def inspect_quests(page, region: str, previous: dict | None) -> tuple[dict[str, object] | None, list[int]]:
    current_area = int(previous.get("area", 42)) if previous else 42
    current = quest_area(page, region, current_area)
    if current is None:
        print(f"Avviso: Area Quest {current_area} non disponibile per {region}. Salto il controllo quest.")
        return previous, []
    candidate = quest_area(page, region, current_area + 1)
    if candidate is not None:
        promoted = candidate
        added = [] if previous is None else candidate["stages"]
        return promoted, added
    if previous is None or int(previous.get("area", 0)) != current_area:
        added = []
    else:
        added = sorted(set(current["stages"]) - set(previous.get("stages", [])))
    return current, added

def challenge_stages(page, event: dict[str, str], region: str) -> list[dict[str, str]]:
    """Return only the highest-difficulty Boss Stats link for each challenge stage.

    DokkanDB encodes the stage and difficulty in the child URL. For event 1768,
    for example, 17680063/64/65 are the three variants of stage 6; the last
    digit is the difficulty rank, and 5 is Super 3 (the maximum).
    """
    path = event["url"].replace(BASE, "")
    switch_region(page, path, region)
    event_match = re.search(r"/events/challenge/(\d+)", path)
    if not event_match:
        return []
    event_id = event_match.group(1)

    values = page.locator('a[href*="/events/challenge/"]').evaluate_all(
        """els => { const seen=new Set(), out=[]; for(const a of els){
          const href=a.getAttribute('href');
          if(!href || !/^\\/events\\/challenge\\/\\d+\\/\\d+$/.test(href) || seen.has(href)) continue;
          seen.add(href);
          let node=a, context='';
          for(let i=0;i<5 && node;i++,node=node.parentElement){
            const t=(node.innerText||'').trim();
            if(t.length>context.length && t.length<300 && /BOSS STATS/i.test(t)) context=t;
          }
          out.push({href,context});
        } return out; }"""
    )

    by_stage: dict[int, dict[str, str | int]] = {}
    difficulty_names = {3: "Super 1", 4: "Super 2", 5: "Super 3"}
    for item in values:
        child_match = re.search(r"/events/challenge/\d+/(\d+)$", item["href"])
        if not child_match:
            continue
        child_id = child_match.group(1)
        if not child_id.startswith(event_id):
            continue
        suffix = child_id[len(event_id):]
        if len(suffix) < 2 or not suffix[-1].isdigit():
            continue

        # The last digit is the difficulty rank; the preceding padded digits
        # identify the stage (e.g. suffix 0065 -> stage 6, difficulty 5).
        stage_digits = suffix[:-1].lstrip("0")
        if not stage_digits.isdigit():
            continue
        stage_number = int(stage_digits)
        difficulty_rank = int(suffix[-1])
        explicit = re.search(r"\bSUPER\s*([1-3])\b", item.get("context", ""), re.I)
        difficulty_label = (
            f"Super {explicit.group(1)}" if explicit
            else difficulty_names.get(difficulty_rank, f"Difficoltà {difficulty_rank}")
        )

        existing = by_stage.get(stage_number)
        if existing is None or difficulty_rank > int(existing["difficulty_rank"]):
            by_stage[stage_number] = {
                "url": urljoin(BASE, item["href"]),
                "number": str(stage_number),
                "label": difficulty_label,
                "difficulty_rank": difficulty_rank,
            }

    return [
        {"url": str(by_stage[stage]["url"]), "number": str(stage), "label": str(by_stage[stage]["label"])}
        for stage in sorted(by_stage, reverse=True)
    ]

def esc(text: str) -> str:
    return html.escape(plain(text), quote=True)

def link(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'

def telegram_send(text: str, chat_id: str | None = None) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = str(chat_id or os.environ["TELEGRAM_CHAT_ID"])
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=(10, 30),
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram ha rifiutato il messaggio: {result}")

def subscriber_cipher() -> Fernet:
    token = os.environ["TELEGRAM_BOT_TOKEN"].encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(token).digest())
    return Fernet(key)

def read_subscribers(state: dict) -> set[str]:
    encrypted = state.get("telegram_chat_ids_enc")
    if not encrypted:
        return set()
    try:
        values = json.loads(subscriber_cipher().decrypt(encrypted.encode("ascii")).decode("utf-8"))
        return {str(value) for value in values}
    except (InvalidToken, ValueError, TypeError) as exc:
        print(f"Non posso decifrare le chat iscritte: {exc}", file=sys.stderr)
        return set()

def load_chat_preferences(state: dict) -> list[dict]:
    """Load per-chat notification preferences from the Cloudflare Telegram service.

    On the first run, import subscribers from the legacy encrypted state.json list.
    The import is idempotent, so a failed run can safely retry it.
    """
    api_base = os.environ.get("MONITOR_API_URL", "").strip().rstrip("/")
    api_secret = os.environ.get("MONITOR_API_SECRET", "").strip()
    if not api_base or not api_secret:
        raise RuntimeError("MONITOR_API_URL e MONITOR_API_SECRET devono essere configurati nei GitHub Secrets.")

    headers = {"Authorization": f"Bearer {api_secret}"}
    if not state.get("telegram_preferences_migrated"):
        legacy_chat_ids = read_subscribers(state)
        configured_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if configured_chat:
            legacy_chat_ids.add(configured_chat)

        response = requests.post(
            f"{api_base}/api/import-legacy",
            json={"chat_ids": sorted(legacy_chat_ids)},
            headers=headers,
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Migrazione iscritti Telegram fallita: {payload}")
        state["telegram_preferences_migrated"] = True
        print(f"Migrazione preferenze Telegram completata: {payload.get('imported_candidates', 0)} chat legacy importate o già presenti.")

    response = requests.get(
        f"{api_base}/api/preferences",
        headers=headers,
        timeout=(10, 30),
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok") or not isinstance(payload.get("chats"), list):
        raise RuntimeError(f"Risposta non valida dal servizio preferenze Telegram: {payload}")
    chats = payload["chats"]
    print(f"Preferenze Telegram caricate: {len(chats)} chat attive.")
    return chats

def notify(title: str, lines: list[str], chat_preferences: list[dict], preference_key: str) -> None:
    """Send an alert only to active chats that enabled its region/category."""
    chunks: list[str] = []
    current = f"<b>{esc(title)}</b>"
    for line in lines:
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = f"<b>{esc(title)} (continua)</b>"
        current += "\n" + line
    if current:
        chunks.append(current)

    recipients = []
    for chat in chat_preferences:
        try:
            active = int(chat.get("active", 1)) == 1
            enabled = int(chat.get(preference_key, 0)) == 1
        except (TypeError, ValueError):
            active, enabled = False, False
        if active and enabled and chat.get("chat_id") is not None:
            recipients.append(str(chat["chat_id"]))

    for chat_id in sorted(set(recipients)):
        for chunk in chunks:
            try:
                telegram_send(chunk, chat_id)
            except Exception as exc:
                print(f"Invio Telegram fallito per chat {chat_id}: {exc}", file=sys.stderr)
                break

def get_scheduled_tasks(now: datetime) -> tuple[set[str], bool, bool, set[str], set[str], bool]:
    # TEST: controlla tutte le categorie evento, incluse le Challenge, per GLB e JP ogni 5 minuti.
    # Le Quest vengono controllate tramite inspect_quests(), senza rileggere la categoria /events?category=0.
    return {"GLB", "JP"}, True, True, {"GLB", "JP"}, {"GLB", "JP"}, True
"""
    if "--full" in sys.argv:
        return {"GLB", "JP"}, True, True, {"GLB", "JP"}, {"GLB", "JP"}, True

    ora = now.hour
    minuto = 5 * round(now.minute / 5)

    if minuto == 60:
        minuto = 0
        ora = (ora + 1) % 24

    card_regions, challenge_regions, event_scan_regions = set(), set(), set()
    news_due, summons_due = False, False

    # NEWS: ogni 30 minuti
    if minuto in [0, 30]:
        news_due = True

    # SUMMON: ogni ora
    if minuto == 0:
        summons_due = True

    # PERSONAGGI E CHALLENGE JP
    if (ora == 2 and minuto >= 30) or (ora == 3 and minuto <= 30):
        card_regions.add("JP")
        challenge_regions.add("JP")

    # PERSONAGGI E CHALLENGE GLB
    if (ora == 6 and minuto >= 30) or (ora == 7 and minuto <= 30):
        card_regions.add("GLB")
        challenge_regions.add("GLB")

    # PERSONAGGI E CHALLENGE ENTRAMBE LE REGIONI OGNI ORA
    if minuto == 0:
        card_regions.update(["GLB", "JP"])
        challenge_regions.update(["GLB", "JP"])

    # EVENTI COMPLETI GLB
    if ora in [3, 15] and minuto == 30:
        event_scan_regions.add("GLB")

    # EVENTI COMPLETI JP
    if ora in [7, 19] and minuto == 30:
        event_scan_regions.add("JP")

    return card_regions, news_due, summons_due, challenge_regions, event_scan_regions, False
"""

def main() -> int:
    if "--test-telegram" in sys.argv[1:]:
        test_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not test_chat:
            raise RuntimeError("TELEGRAM_CHAT_ID non configurato: impossibile inviare il messaggio di test.")
        telegram_send("✅ Test DokkanDB Bot: Telegram riceve correttamente i messaggi.", test_chat)
        print("Test Telegram inviato.")
        return 0

    now = datetime.now(ROME)
    card_regions, news_due, summons_due, challenge_regions, event_scan_regions, full_event_scan = get_scheduled_tasks(now)
    
    if not any([card_regions, news_due, summons_due, challenge_regions, event_scan_regions, full_event_scan]):
        print(f"Orario {now.strftime('%H:%M')} - Nessun controllo previsto in questo slot di 5 minuti.")
        return 0

    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    try:
        chat_preferences = load_chat_preferences(state)
    except Exception as exc:
        # Do not continue the scan with stale recipients: otherwise new IDs could be
        # committed to state.json without their notifications being delivered.
        print(f"Impossibile caricare le preferenze Telegram: {exc}", file=sys.stderr)
        return 1

    next_state = {
        k: v for k, v in state.items()
        if k in {"telegram_chat_ids_enc", "telegram_update_offset", "telegram_preferences_migrated"}
        or re.match(r"^(?:GLB|JP)\|(?:cards|news|summons|quests|events)", k)
    }
    alerts: list[tuple[str, str, list[str]]] = []
    errors: list[str] = []
    active_region = {"value": None}
    request_counts = {"GLB": 0, "JP": 0}
    category_by_key = {key: (key, label, path) for key, label, path in EVENT_CATEGORIES}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US", 
            timezone_id="Europe/Rome"
        )
        page = context.new_page()
        page.set_default_timeout(30_000)

        def trim_requests(route):
            request = route.request
            host = (urlparse(request.url).hostname or "").lower()
            same_site = host == "dokkandb.com" or host.endswith(".dokkandb.com")
            blocked_type = request.resource_type in {"image", "media", "font"}
            if not blocked_type and same_site and active_region["value"]:
                request_counts[active_region["value"]] += 1
            if blocked_type: route.abort()
            else: route.continue_()

        page.route("**/*", trim_requests)
        try:
            for region in ("GLB", "JP"):
                active_region["value"] = region
                try:
                    if region in card_regions:
                        card_key = f"{region}|cards"
                        old_cards = state.get(card_key)
                        cards = extract_cards(page, region, set(old_cards) if old_cards else None)
                        current_ids = [card["id"] for card in cards]
                        next_state[card_key] = current_ids
                        if old_cards is None:
                            print(f"Baseline carte creato: {region}")
                        else:
                            old_set = set(old_cards)
                            added = [c for c in cards if c["id"] not in old_set]
                            if added:
                                lines = []
                                for card in added:
                                    detail = card_detail(page, card["url"], region, fallback_type=card.get("type", ""))
                                    if not detail.get("type"):
                                        print(f"Avviso: type non rilevato per la carta {card['id']} ({card['url']}).", file=sys.stderr)
                                    name = detail["name"] or card["name"] or f"Card {card['id']}"
                                    attributes = " ".join(v for v in (detail["rarity"], detail["type"]) if v)
                                    prefix = f"{detail['awakening']} " if detail["awakening"] else ""
                                    
                                    # Formattazione data pulita (senza backslash dentro l'f-string)
                                    date_part = f" — {esc(detail['date'])}" if detail.get("date") else ""
                                    
                                    lines.append(f"• {esc(prefix + attributes)} {link(name, card['url'])}{date_part}")
                                if lines:
                                    alerts.append((f"cards_{region.lower()}", f"DokkanDB {region} update: nuovi personaggi", lines))
                                    
                    if news_due:
                        news_key = f"{region}|news"
                        news = extract_news(page, region)
                        old_news = state.get(news_key)
                        current_news_ids = [item["id"] for item in news]
                        next_state[news_key] = current_news_ids
                        if old_news is None:
                            print(f"Baseline news creato: {region}")
                        else:
                            old_set = set(old_news)
                            fresh = [item for item in news if item["id"] not in old_set]
                            if fresh:
                                lines = [f"• {esc(i['date']) + ' — ' if i['date'] else ''}<b>{link(i['title'] or 'News', i['url'])}</b>" for i in fresh]
                                alerts.append((f"news_{region.lower()}", f"DokkanDB {region} update: nuove news", lines))

                    if summons_due:
                        for category, summon_label in ((1, "stone"), (3, "ticket")):
                            summon_key = f"{region}|summons|{category}"
                            summons = extract_summons(page, region, category)
                            old_summons = state.get(summon_key)
                            current_summon_ids = [item["id"] for item in summons]
                            next_state[summon_key] = current_summon_ids
                            if old_summons is None:
                                print(f"Baseline summon {summon_label} creato: {region}")
                            else:
                                old_set = set(old_summons)
                                fresh = [item for item in summons if item["id"] not in old_set]
                                if fresh:
                                    lines = [f"• nuova summon {summon_label}: {esc(i['date']) + ' — ' if i['date'] else ''}<b>{link(i['title'] or 'Banner', i['url'])}</b>" for i in fresh]
                                    alerts.append((f"summons_{region.lower()}", f"DokkanDB {region} update: summon {summon_label}", lines))

                    if region in event_scan_regions or full_event_scan:
                        quest_key = f"{region}|quests"
                        previous = state.get(quest_key)
                        quest, added_stages = inspect_quests(page, region, previous)
                        if quest is not None:
                            next_state[quest_key] = {"area": quest["area"], "stages": quest["stages"]}
                            if previous is None:
                                print(f"Baseline Quest creato: {region} — area {quest['area']}")
                            elif added_stages:
                                stage_word = "nuovo stage" if len(added_stages) == 1 else "nuovi stage"
                                numbers = ", ".join(str(n) for n in added_stages)
                                message = f"{stage_word}: Quest, area {quest['area']}, stage {numbers}"
                                area_label = f"area {quest['area']}"
                                alerts.append((f"events_{region.lower()}", f"DokkanDB {region} update: Quest", [f"• {message} — {link(area_label, quest['url'])}"]))
                                
                    selected_keys: list[str] = []
                    if full_event_scan:
                        selected_keys = [k for k, _, _ in EVENT_CATEGORIES if k != "quest"]
                    else:
                        if region in challenge_regions:
                            selected_keys.append("challenge")
                        if region in event_scan_regions:
                            # Quest is tracked by inspect_quests() above; fetching this category
                            # would be a redundant page load and its new entries are not notified.
                            selected_keys.extend(
                                k for k, _, _ in EVENT_CATEGORIES if k not in {"challenge", "quest"}
                            )
                    
                    if selected_keys:
                        selected_categories = [category_by_key[k] for k in selected_keys]
                        for cat_key, cat_label, path in selected_categories:
                            key = f"{region}|events|{cat_key}"
                            events = extract_events(page, region, path)
                            old_ids = set(state.get(key, []))
                            next_state[key] = [event["id"] for event in events]
                            if key not in state:
                                print(f"Baseline eventi creato: {region} — {cat_label}")
                                continue
                            for event in (item for item in events if item["id"] not in old_ids):
                                if cat_key == "quest": continue
                                lines = [f"• {link(event['title'] or cat_label, event['url'])}"]
                                if cat_key == "challenge":
                                    stages = challenge_stages(page, event, region)
                                    if stages:
                                        lines.extend(
                                            f"  • Stage {esc(s['number'])} — {esc(s['label'])} — {link('Boss Stats', s['url'])}"
                                            for s in stages
                                        )
                                    else:
                                        print(
                                            f"Avviso: nessun link Boss Stats riconosciuto per il Challenge Event {event['url']}",
                                            file=sys.stderr,
                                        )
                                alerts.append((f"events_{region.lower()}", f"DokkanDB {region} update: nuovo {cat_label.lower()}", lines))
                except Exception as exc:
                    errors.append(f"{region}: {type(exc).__name__}: {exc}")
                    print(f"Errore nel controllo {region}: {exc}", file=sys.stderr)
        finally:
            active_region["value"] = None
            browser.close()

    print("Richieste DokkanDB consentite: " + ", ".join(f"{region}={count}" for region, count in request_counts.items()))
    if alerts:
        months = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre")
        stamp = f"{now.day} {months[now.month - 1]} {now.year}, {now:%H:%M}"
        for preference_key, title, lines in alerts:
            notify(f"{title} · {stamp}", lines, chat_preferences, preference_key)
            
    STATE_PATH.write_text(json.dumps(next_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if errors else 0

if __name__ == "__main__":
    raise SystemExit(main())
