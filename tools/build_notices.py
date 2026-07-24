#!/usr/bin/env python3
"""
CREQ-13: 공지 소스(`_notices/*.md`) → 발행물(`notices/`) 빌더.

정적 호스팅에는 "폴더 안에 뭐가 있는지" 물어볼 방법이 없다(GitHub Pages·R2·S3 공개 버킷 모두).
그래서 앱이 읽을 목록 파일(`notices/index.json`)이 반드시 하나 필요한데, **그걸 사람이 쓰지 않게**
하는 것이 이 스크립트의 목적이다. 글(`.md`)만 추가하면 목록·HTML·이미지가 따라 만들어진다.

생성물은 **의존성 없는 순수 정적 파일**이다(Jekyll 등 렌더러가 끼지 않는다) — 나중에 R2/S3 로
옮길 때 `notices/` 폴더를 그대로 복사하면 끝나게 하려는 것.

    사용법:  python tools/build_notices.py            # 저장소 루트 기준
             python tools/build_notices.py --root .   # 루트를 직접 지정

    필요 패키지:  pip install markdown PyYAML
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import markdown
import yaml

# 소스(사람이 쓰는 곳) / 발행물(자동 생성, 손대지 않는 곳).
# `_` 로 시작하는 폴더는 GitHub Pages(Jekyll)가 게시하지 않으므로 소스가 그대로 노출되지 않는다.
SRC_DIR = "_notices"
OUT_DIR = "notices"

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
FIRST_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")
FIRST_HEADING_RE = re.compile(r"^#\s+(.+)$", re.M)
FILENAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

PAGE_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root { color-scheme: light dark; --fg:#1b1b1b; --muted:#6b6b6b; --bg:#fff; --line:#e5e5e5; --accent:#2e7d32; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#ececec; --muted:#a0a0a0; --bg:#121212; --line:#2c2c2c; --accent:#7bc47f; }
}
* { box-sizing: border-box; }
body {
  margin:0 auto; padding:20px 18px 48px; max-width:720px; background:var(--bg); color:var(--fg);
  font:16px/1.7 -apple-system,"Noto Sans KR","Malgun Gothic",sans-serif; word-break:keep-all;
}
h1 { font-size:22px; line-height:1.4; margin:0 0 6px; }
.date { color:var(--muted); font-size:13px; margin:0 0 20px; }
img { max-width:100%; height:auto; border-radius:10px; display:block; margin:20px 0; }
h2 { font-size:17px; margin:28px 0 8px; }
p { margin:0 0 14px; }
ul,ol { padding-left:20px; margin:0 0 14px; }
li { margin-bottom:6px; }
a { color:var(--accent); }
code { background:rgba(127,127,127,.16); padding:2px 5px; border-radius:4px; font-size:14px; }
pre { background:rgba(127,127,127,.12); padding:12px; border-radius:8px; overflow-x:auto; }
pre code { background:none; padding:0; }
blockquote { margin:0 0 14px; padding:2px 14px; border-left:3px solid var(--line); color:var(--muted); }
table { border-collapse:collapse; width:100%; margin:0 0 14px; display:block; overflow-x:auto; }
th,td { border:1px solid var(--line); padding:6px 10px; text-align:left; }
hr { border:0; border-top:1px solid var(--line); margin:32px 0 16px; }
footer { color:var(--muted); font-size:13px; }
</style>
</head>
<body>
<h1>{{TITLE}}</h1>
<p class="date">{{DATE}}</p>
{{BODY}}
<hr>
<footer>코스트레이더스</footer>
</body>
</html>
"""


def parse_post(path: Path) -> dict | None:
    """
    글 하나(`.md`)를 읽어 매니페스트 항목 + 본문 HTML 로 만든다.

    앞머리(front matter)는 **전부 선택**이다 — 제목·날짜·썸네일은 없으면 본문/파일명에서 추론한다.
    "md 파일만 하나 두면 공지가 하나 생긴다"가 이 스크립트의 계약이라, 필수 입력을 두지 않는다.
    """
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    fm = FRONT_MATTER_RE.match(raw)
    body_md = raw
    if fm:
        meta = yaml.safe_load(fm.group(1)) or {}
        body_md = raw[fm.end():]
    if not isinstance(meta, dict):
        print(f"  ! {path.name}: 앞머리가 딕셔너리가 아니라 무시합니다.", file=sys.stderr)
        meta = {}

    if meta.get("draft"):
        print(f"  - {path.name}: draft 라 건너뜁니다.")
        return None

    slug = path.stem

    # 제목: 앞머리 > 본문 첫 `# 제목` > 파일명.
    title = str(meta.get("title") or "").strip()
    if not title:
        heading = FIRST_HEADING_RE.search(body_md)
        title = heading.group(1).strip() if heading else slug
        if heading:
            # 제목을 페이지 <h1> 로 따로 그리므로 본문에서는 뺀다(같은 제목이 두 번 나오지 않게).
            body_md = body_md[:heading.start()] + body_md[heading.end():]

    # 날짜: 앞머리 > 파일명 앞 `yyyy-MM-dd` > 오늘.
    published = str(meta.get("date") or "").strip()
    if not published:
        m = FILENAME_DATE_RE.match(slug)
        published = m.group(1) if m else date.today().isoformat()

    # 썸네일: 앞머리 > 본문 첫 이미지. 둘 다 없으면 빈 값(앱이 제목 문구로 폴백한다).
    thumbnail = str(meta.get("thumbnail") or "").strip()
    if not thumbnail:
        img = FIRST_IMAGE_RE.search(body_md)
        thumbnail = img.group(1) if img else ""
    if not thumbnail:
        print(f"  ! {path.name}: 썸네일이 없어 배너에 제목 문구만 보입니다.", file=sys.stderr)

    body_html = markdown.markdown(body_md.strip(), extensions=["extra", "sane_lists"])
    page = (
        PAGE_TEMPLATE
        .replace("{{TITLE}}", html.escape(title))
        .replace("{{DATE}}", html.escape(published))
        .replace("{{BODY}}", body_html)
    )

    return {
        "page_name": f"{slug}.html",
        "page_html": page,
        "entry": {
            "id": slug,
            "title": title,
            "thumbnail": thumbnail,
            "link": f"{slug}.html",
            "publishedAt": published,
            "startAt": _date_or_none(meta.get("start")),
            "endAt": _date_or_none(meta.get("end")),
            "priority": int(meta.get("priority") or 0),
        },
    }


def _date_or_none(value) -> str | None:
    """`yyyy-MM-dd` 문자열로 정규화. YAML 이 `2026-08-31` 을 date 객체로 파싱하는 경우도 받는다."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def build(root: Path) -> int:
    src = root / SRC_DIR
    out = root / OUT_DIR
    if not src.is_dir():
        print(f"소스 폴더가 없습니다: {src}", file=sys.stderr)
        return 1

    # 발행 폴더를 통째로 다시 만든다 — 소스에서 지운 글의 잔재(HTML·이미지)가 남지 않게 하려는 것.
    # 그래서 **글을 지우면 URL 도 사라진다**. 배너에서만 내리고 싶으면 앞머리에 `end:` 를 쓴다.
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # 이미지 등 `.md` 가 아닌 파일은 구조 그대로 복사한다. 덕분에 본문·매니페스트에 적은
    # 상대 경로(`images/foo.webp`)가 소스에서도 발행물에서도 똑같이 맞는다.
    for item in src.rglob("*"):
        if item.is_file() and item.suffix.lower() != ".md":
            dest = out / item.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)

    entries = []
    for md_path in sorted(src.glob("*.md")):
        post = parse_post(md_path)
        if post is None:
            continue
        (out / post["page_name"]).write_text(post["page_html"], encoding="utf-8")
        entries.append(post["entry"])
        print(f"  + {md_path.name} → {OUT_DIR}/{post['page_name']}")

    # 앱과 같은 순서로 정렬해 둔다(앱도 다시 정렬하지만, 사람이 파일을 열어 볼 때 순서가 맞는 편이 낫다).
    entries.sort(key=lambda e: (-e["priority"], _desc(e["publishedAt"]), e["id"]))

    manifest = {"version": 1, "notices": entries}
    (out / "index.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n공지 {len(entries)}건 → {OUT_DIR}/index.json")
    return 0


class _desc:
    """내림차순 정렬용 문자열 래퍼(`yyyy-MM-dd` 는 사전순 = 날짜순)."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: "_desc") -> bool:
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _desc) and self.value == other.value


def main() -> int:
    parser = argparse.ArgumentParser(description="공지 소스를 정적 발행물로 빌드합니다.")
    parser.add_argument(
        "--root",
        default=None,
        help=f"저장소 루트(`{SRC_DIR}`/`{OUT_DIR}` 의 부모). 기본값: 이 스크립트의 상위 폴더의 상위",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    print(f"루트: {root}")
    return build(root)


if __name__ == "__main__":
    raise SystemExit(main())
