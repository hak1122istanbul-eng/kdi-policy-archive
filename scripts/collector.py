import argparse
import json
import os
import hashlib
from datetime import datetime, timezone, timedelta
import urllib.request
import xml.etree.ElementTree as ET
from classifier import load_categories, classify_item
import sys
import re
import requests # type: ignore
import feedparser # type: ignore
from bs4 import BeautifulSoup # type: ignore

RSS_URL = "https://eiec.kdi.re.kr/policy/rss.do?rss_type=material"
ARCHIVE_PATH = "docs/data/archive.json"
CATEGORIES_PATH = "docs/data/categories.json"
KST = timezone(timedelta(hours=9))

def extract_author(description):
    if not description:
        return ""
    snippet = description.strip()[:80]
    match = re.search(r'^([가-힣a-zA-Z0-9\s·(),&·/]+?)(?:은|는)(?=\s|’|\'|\"|\d|\.|\()', snippet)
    if match:
        author = match.group(1).strip()
        author = re.sub(r'\(이하\s+[가-힣]+\)', '', author).strip()
        if author.endswith('와') or author.endswith('과'):
            author = author[:-1].strip()
        return author
    return ""

def fetch_rss(url, retries=3):
    for attempt in range(retries):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            response = requests.get(url, headers=headers, timeout=10, verify=False)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            print(f"Fetch attempt {attempt + 1} failed: {e}")
    raise Exception("Failed to fetch RSS after 3 attempts")

def fetch_web_dates():
    """Scrape the KDI website list page to get num -> date mapping."""
    # Fetch first 2 pages to be safe
    mapping = {}
    for pg in [1, 2]:
        url = f"https://eiec.kdi.re.kr/policy/materialList.do?topic=P&pg={pg}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            response = requests.get(url, headers=headers, verify=False, timeout=30)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, "html.parser")
                # Based on subagent, each item is in a link
                for a_tag in soup.select("a[href*='materialView.do?num=']"):
                    num_match = re.search(r'num=(\d+)', a_tag['href'])
                    if num_match:
                        num = num_match.group(1)
                        # The date is in a span inside the div in the link
                        # But simpler: look for YYYY.MM.DD in the a_tag's text
                        # but EXCLUDE the title (which might have dates like '5월 1일')
                        
                        # Let's find all spans
                        spans = a_tag.find_all('span')
                        for span in spans:
                            text = span.get_text().strip()
                            # Look for YYYY.MM.DD
                            date_match = re.search(r'(\d{4})\.(\d{2})\.(\d{2})', text)
                            if date_match:
                                mapping[num] = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
                                break
        except Exception as e:
            print(f"Warning: Failed to fetch web dates for page {pg}: {e}")
    return mapping

def parse_rss(xml_content, batch_time, web_dates):
    feed = feedparser.parse(xml_content)
    items = []
    today_str = batch_time.strftime("%Y-%m-%d")
    
    for entry in feed.entries:
        link = entry.link
        item_id = hashlib.md5(link.encode('utf-8')).hexdigest()
        
        # Extract num from link
        num_match = re.search(r'num=(\d+)', link)
        num = num_match.group(1) if num_match else ""
        
        # Clean description HTML tags
        description = entry.description if 'description' in entry else ""
        if description:
            soup = BeautifulSoup(description, "html.parser")
            description = soup.get_text(separator=" ", strip=True)
            
        # 1. Try web scraped dates first
        pub_date = web_dates.get(num, "")
        
        # 2. Try RSS published field
        if not pub_date:
            pub_date = entry.published if 'published' in entry else ""
        
        # 3. Extract from description
        if not pub_date and description:
            header = description[:150]
            # Use a list of patterns and find all matches with their positions
            patterns = [
                (r'(?:\D|^)(?P<y>\d{2,4})\.\s*(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.\s*\((?P<day>[월화수목금토일])\)', 3), # Full with day name
                (r'(?:\D|^)(?P<y>\d{2,4})\.\s*(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.', 1), # Full without day name
                (r'(?:\s|^)(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.\s*\((?P<day>[월화수목금토일])\)', 2), # Short with day name
                (r'(?:\s|^)(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.', 0) # Short without day name
            ]
            
            matches = []
            for pattern, priority in patterns:
                for m in re.finditer(pattern, header):
                    d_dict = m.groupdict()
                    y = d_dict.get('y')
                    if not y:
                        y = str(batch_time.year)
                    elif len(y) == 2:
                        y = "20" + y
                    
                    date_str = f"{y}-{int(d_dict['m']):02d}-{int(d_dict['d']):02d}"
                    
                    if date_str <= today_str:
                        # Priority: 
                        # 1. Has day name (priority 2 or 3)
                        # 2. Position in text (earlier is better)
                        matches.append({
                            'date': date_str,
                            'priority': priority,
                            'pos': m.start()
                        })
            
            if matches:
                # Sort by priority DESC, then pos ASC
                matches.sort(key=lambda x: (-x['priority'], x['pos']))
                pub_date = matches[0]['date']
        
        category = entry.category if 'category' in entry else ""
        
        author = extract_author(description)
        if not author:
            author = entry.author if 'author' in entry else ""
            
        item = {
            "id": item_id,
            "title": entry.title,
            "link": link,
            "description": description,
            "pub_date": pub_date,
            "author": author,
            "original_category": category,
            "collected_at": batch_time.isoformat()
        }
        items.append(item)
    return items

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reclassify", action="store_true", help="Reclassify all items")
    args = parser.parse_args()

    # Load categories
    if not os.path.exists(CATEGORIES_PATH):
        print(f"Error: {CATEGORIES_PATH} not found.")
        sys.exit(1)
    categories_config = load_categories(CATEGORIES_PATH)

    # Load existing archive
    archive = {"items": []}
    if os.path.exists(ARCHIVE_PATH):
        try:
            with open(ARCHIVE_PATH, 'r', encoding='utf-8') as f:
                archive = json.load(f)
        except json.JSONDecodeError:
            pass

    existing_items = {item['id']: item for item in archive.get('items', [])}
    batch_time = datetime.now(KST)
    today_str = batch_time.strftime("%Y-%m-%d")
    
    # 1. Fetch web dates for mapping
    print("Fetching actual dates from KDI website...")
    web_dates = fetch_web_dates()
    if web_dates:
        print(f"Retrieved {len(web_dates)} dates from website.")

    # 2. Update existing items if missing or future
    print("Checking existing items for date accuracy...")
    updated_existing = 0
    MANUAL_OVERRIDES = {
        "280841": "2026-05-11",
        "280846": "2026-05-11"
    }

    for item in existing_items.values():
        num_match = re.search(r'num=(\d+)', item.get('link', ''))
        num = num_match.group(1) if num_match else ""
        
        current_pub_date = item.get('pub_date', '')
        
        # Update author if missing or generic
        current_author = item.get('author', '')
        if not current_author or current_author == 'KDI':
            extracted = extract_author(item.get('description', ''))
            if extracted:
                item['author'] = extracted
                updated_existing += 1

        # Check manual overrides first
        if num in MANUAL_OVERRIDES and current_pub_date != MANUAL_OVERRIDES[num]:
            item['pub_date'] = MANUAL_OVERRIDES[num]
            updated_existing += 1
            continue

        # Normal update logic
        needs_update = not current_pub_date or current_pub_date > today_str
        
        if needs_update and item.get('description'):
            # Try web date
            if num in web_dates:
                item['pub_date'] = web_dates[num]
                updated_existing += 1
                continue
                
            # Try improved regex
            header = item['description'][:150]
            patterns = [
                (r'(?:\D|^)(?P<y>\d{2,4})\.\s*(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.\s*\((?P<day>[월화수목금토일])\)', 3),
                (r'(?:\D|^)(?P<y>\d{2,4})\.\s*(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.', 1),
                (r'(?:\s|^)(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.\s*\((?P<day>[월화수목금토일])\)', 2),
                (r'(?:\s|^)(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\.', 0)
            ]
            matches = []
            for pattern, priority in patterns:
                for m in re.finditer(pattern, header):
                    d_dict = m.groupdict()
                    y = d_dict.get('y')
                    if not y:
                        c_at = item.get('collected_at', '')
                        y = c_at[:4] if c_at else str(batch_time.year)
                    elif len(y) == 2:
                        y = "20" + y
                    
                    date_str = f"{y}-{int(d_dict['m']):02d}-{int(d_dict['d']):02d}"
                    if date_str <= today_str:
                        matches.append({
                            'date': date_str,
                            'priority': priority,
                            'pos': m.start()
                        })
            
            if matches:
                matches.sort(key=lambda x: (-x['priority'], x['pos']))
                item['pub_date'] = matches[0]['date']
                updated_existing += 1
            elif current_pub_date > today_str:
                # Fallback for future dates
                item['pub_date'] = item.get('collected_at', today_str)[:10]
                updated_existing += 1

    if updated_existing > 0:
        print(f"Updated {updated_existing} existing items with accurate dates.")

    if args.reclassify:
        print("Reclassifying all existing items...")
        for item_id, item in existing_items.items():
            item['matched_categories'] = classify_item(item, categories_config)
        new_count = 0
    else:
        # Fetch and parse RSS
        print("Fetching RSS...")
        try:
            xml_content = fetch_rss(RSS_URL)
            new_items = parse_rss(xml_content, batch_time, web_dates)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)

        new_count = 0
        # Add items in reverse order of the feed (oldest in batch first)
        # to preserve dictionary insertion order for stable sort
        for item in reversed(new_items):
            if item['id'] not in existing_items:
                item['matched_categories'] = classify_item(item, categories_config)
                existing_items[item['id']] = item
                new_count += 1
                
    # Save back to archive
    all_items = list(existing_items.values())
    
    # Custom sort: 
    # 1. Primary: pub_date (extracted date) DESC
    # 2. Secondary: collected_at DESC
    # 3. Tertiary: Dictionary insertion order (handled by stability of Python's sort)
    
    # We want latest first. 
    # For items with same date and collected_at (same batch), 
    # we want to maintain the RSS feed order (which is latest first).
    # Since we added new items to existing_items in reverse order [Oldest in batch ... Latest in batch],
    # the dictionary values end with [Latest in batch].
    # So if we sort stably, the latest item in the batch will stay last in its group.
    # We want it to be first.
    
    # Let's simplify:
    def sort_key(x):
        # Use pub_date if it looks like a date, otherwise use collected_at
        p_date = x.get('pub_date', '')
        c_date = x.get('collected_at', '')
        # Return a tuple for sorting. We'll use reverse=True for DESC
        return (p_date, c_date)

    # Sort all items. Python's sort is stable.
    # To maintain feed order within same batch, we should have them in the right order initially.
    # If we want the latest in feed to be top, and the feed is already latest first:
    # We should add them to the dict in feed order [Latest, ..., Oldest].
    # Then when we sort by (date, collected_at) DESC, items with same keys keep their relative order.
    # So [Latest, ..., Oldest] stays [Latest, ..., Oldest].
    
    # Let's re-fix the insertion order.
    existing_items = {item['id']: item for item in archive.get('items', [])}
    if not args.reclassify:
        for item in new_items: # Feed order: [Latest, ..., Oldest]
            if item['id'] not in existing_items:
                item['matched_categories'] = classify_item(item, categories_config)
                existing_items[item['id']] = item
                new_count += 1
    
    all_items = list(existing_items.values())
    all_items.sort(key=sort_key, reverse=True)
    
    archive['items'] = all_items
    archive['total_count'] = len(all_items)
    archive['last_updated'] = batch_time.isoformat()
    archive['source'] = RSS_URL

    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    with open(ARCHIVE_PATH, 'w', encoding='utf-8') as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)

    print(f"Total items: {len(all_items)}")
    print(f"New items added: {new_count}")

if __name__ == "__main__":
    main()
