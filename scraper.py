"""Scrape every Bellhaven community from the public website."""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import config

CITY_STATE_ZIP = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})")


def _soup(session, url):
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def community_urls(session, base=config.BASE_URL):
    """Walk the paginated directory, then pick up any community linked from other pages.

    The directory is not the full list. The homepage announces new communities
    (e.g. Findlay) before they show up in the directory, so we check it too.
    """
    urls = []
    page_url, visited = urljoin(base, "/communities"), set()
    while page_url and page_url not in visited:
        visited.add(page_url)
        soup = _soup(session, page_url)
        urls += [urljoin(base, a["href"]) for a in soup.select(".card h3 a[href]")]
        next_link = next((a for a in soup.select(".pager a[href]") if "next" in a.get_text().lower()), None)
        page_url = urljoin(base, next_link["href"]) if next_link else None

    for extra in ("/", "/about"):
        soup = _soup(session, urljoin(base, extra))
        urls += [urljoin(base, a["href"]) for a in soup.select('a[href^="/communities/"]')]

    return list(dict.fromkeys(urls))  # de-dupe, keep order


def parse_community(html, url):
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for dt in soup.select("dl.detail dt"):
        dd = dt.find_next_sibling("dd")
        fields[dt.get_text(strip=True).lower()] = dd

    address_lines = [line.strip() for line in fields["address"].get_text("\n").split("\n") if line.strip()]
    street = address_lines[0]
    m = CITY_STATE_ZIP.match(address_lines[-1])
    if not m:
        raise ValueError(f"Could not parse address on {url}: {address_lines}")

    care = [b.get_text(strip=True) for b in fields["care offerings"].select(".badge")]
    if not care:
        care = [fields["care offerings"].get_text(strip=True)]

    return {
        "slug": url.rstrip("/").rsplit("/", 1)[-1],
        "url": url,
        "name": soup.h1.get_text(strip=True),
        "street": street,
        "city": m["city"],
        "state": m["state"],
        "zip": m["zip"],
        "care_offerings": care,
        "administrator": fields["administrator"].get_text(strip=True) if "administrator" in fields else "",
        "phone": fields["phone"].get_text(strip=True) if "phone" in fields else "",
    }


def scrape(base=config.BASE_URL):
    session = requests.Session()
    session.headers["User-Agent"] = "bellhaven-sync/1.0"
    locations = []
    for url in community_urls(session, base):
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        locations.append(parse_community(resp.text, url))
    return locations


if __name__ == "__main__":
    for loc in scrape():
        print(f"{loc['name']:<50} {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']}")
