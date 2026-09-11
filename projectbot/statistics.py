"""Rolling counts of discovered projects, filtered separately for each subscriber."""
import html
import re
import time
from datetime import datetime

from .core import JST, matches

WINDOWS = (("30m", 1800), ("1h", 3600), ("1d", 86400), ("3d", 259200), ("1w", 604800))

# One primary category per project. Specific skill matches outrank title hints;
# ties use this stable order so counts always add up to the total.
CATEGORIES = (
    ("embedded", "⚙️ Embedded engineering", ("embedded", "embedded software", "embedded systems", "firmware", "arduino", "stm32", "esp32", "microcontroller", "pcb layout", "pcb design", "circuit design", "electronics", "electrical engineering", "fpga", "plc", "raspberry pi")),
    ("marketing", "📣 Marketing", ("marketing", "digital marketing", "social media marketing", "seo", "search engine marketing", "advertising", "facebook marketing", "email marketing", "lead generation", "google ads", "sales", "market research")),
    ("ai_data", "🤖 AI & data", ("artificial intelligence", "machine learning", "deep learning", "data science", "data analytics", "data analysis", "llm", "ai", "computer vision", "natural language processing", "pytorch", "tensorflow")),
    ("video_audio", "🎬 Video & audio", ("video editing", "video production", "videography", "animation", "after effects", "motion graphics", "audio production", "audio services", "music", "voice talent", "sound design")),
    ("design", "🎨 Design", ("design", "graphic design", "logo design", "website design", "web design", "ui / user interface", "user interface / ia", "user experience design", "ux / user experience", "illustration", "illustrator", "photoshop", "figma", "branding", "banner design", "book cover design")),
    ("software", "💻 Software development", ("software development", "web development", "app development", "mobile app development", "programming", "python", "javascript", "typescript", "react", "react.js", "next.js", "node.js", "php", "wordpress", "java", "c++", "c#", "android", "ios development", "flutter", "html", "css", "full stack development", "backend development")),
    ("writing", "✍️ Writing & translation", ("writing", "content writing", "copywriting", "article writing", "technical writing", "creative writing", "translation", "proofreading", "editing", "transcription", "english translation")),
    ("engineering", "🏗 Engineering & architecture", ("engineering", "mechanical engineering", "civil engineering", "structural engineering", "architecture", "building architecture", "autocad", "cad/cam", "solidworks", "revit", "3d modelling", "3d modeling", "product design")),
    ("business", "📊 Business & finance", ("accounting", "finance", "financial analysis", "bookkeeping", "business analysis", "business plans", "project management", "financial research", "financial modeling", "legal", "tax")),
    ("admin", "🗂 Admin & support", ("data entry", "virtual assistant", "customer support", "customer service", "excel", "web search", "administrative support", "desktop support")),
    ("other", "🧩 Other", ()),
)

RULES = [(key, set(terms), [re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I)
                          for term in terms]) for key, _, terms in CATEGORIES]


def category_for(project):
    skills = [str(skill).strip().casefold() for skill in project.get("skills", [])]
    title = str(project.get("title") or "")
    best, best_score = "other", (0, 0)
    exact_counts = {key: sum(skill in terms for skill in set(skills)) for key, terms, _ in RULES}
    strongest = max(exact_counts.values())
    for key, _, patterns in RULES:
        exact = exact_counts[key]
        if exact < strongest:
            continue
        hints = sum(bool(pattern.search(title)) for pattern in patterns)
        score = (exact, hints)
        if score > best_score:
            best, best_score = key, score
    return best


def project_counts(store, config, now=None):
    now = time.time() if now is None else now
    totals = [0] * len(WINDOWS)
    counts = {key: [0] * len(WINDOWS) for key, _, _ in CATEGORIES}
    # Stats windows replace the alert age limit, while retaining all other filters.
    config = {**config, "max_age_minutes": WINDOWS[-1][1] // 60}
    for p in store.history(now - WINDOWS[-1][1], now):
        if not matches(p, config, now=now):
            continue
        key = category_for(p)
        age = now - p["created"]
        for index, (_, seconds) in enumerate(WINDOWS):
            if age <= seconds:
                counts[key][index] += 1
                totals[index] += 1
    return {"totals": totals, "counts": counts, "now": now,
            "tracking_since": store.get("stats_tracking_since"),
            "last_scan": store.get("stats_last_scan")}


def render_counts(report):
    start = report["tracking_since"]
    now = report["now"]
    headers = [label + ("*" if start is None or now - start < seconds else "") for label, seconds in WINDOWS]
    caption = "Projects matching your current skills and filters"
    rows = [("Total", report["totals"])] + [(label, report["counts"][key]) for key, label, _ in CATEGORIES]
    table = '<table striped compact><tr><th>Project type</th>' + ''.join(f'<th>{h}</th>' for h in headers) + '</tr>'
    for label, values in rows:
        table += '<tr><td>' + html.escape(label) + '</td>' + ''.join(f'<td align="right">{v}</td>' for v in values) + '</tr>'
    table += '</table>'
    stamp = lambda value: datetime.fromtimestamp(value, JST).strftime("%d %b %H:%M")
    notes = ["Updated " + stamp(now)]
    if start is None:
        notes.append("* History starts after the next successful scan.")
    elif any(h.endswith("*") for h in headers):
        notes.append("* Partial history: tracking since " + stamp(start) + ".")
    if report["last_scan"] is not None and now - report["last_scan"] > 120:
        notes.append("Last source scan: " + stamp(report["last_scan"]) + ".")
    notes.append("Discovered projects only; each ID counts once. Categories are inferred from skills and title.")
    text = "<b>📊 Project counts</b>\n<i>" + caption + "</i>\n\n"
    text += "\n\n".join("<b>" + html.escape(label) + "</b>\n" + " · ".join(
        f"{header}: {value}" for header, value in zip(headers, values)) for label, values in rows)
    text += "\n\n<i>" + html.escape("\n".join(notes)) + "</i>"
    rich = {"html": "<h2>📊 Project counts</h2><p>" + caption + "</p>" + table
            + "<footer>" + "<br>".join(html.escape(note) for note in notes) + "</footer>",
            "skip_entity_detection": True}
    return text, rich
