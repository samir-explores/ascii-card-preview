"""Build a card from a "Preview my card" issue: the photo and choices someone filled into
.github/ISSUE_TEMPLATE/preview.yml. The preview workflow runs it on every new or edited issue.

Run from the repo root: python -m card.preview <out dir>
Reads the issue text from $ISSUE_BODY and writes, into <out dir>, the cards, the config.yml
they were built from and comment.md (the reply, with __BASE__ where the cards' URL goes).
"""
import copy
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from PIL import Image, ImageOps

from . import build, config, portrait

MARKER = "<!-- ascii-profile-card:preview -->"
MAX_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_SIDE = 1600  # plenty for an 80-column portrait, and keeps background removal quick
MAX_ROWS = 40
MAX_TEXT = 120
# where github.com keeps files dragged into an issue
PHOTO_HOSTS = ("github.com", "user-images.githubusercontent.com", "private-user-images.githubusercontent.com")
NO_ANSWER = "_No response_"


class PreviewError(ValueError):
    pass


def parse_form(body):
    """An issue form's body is "### <label>" then the answer, per field. Returns {label: answer}."""
    fields, label, lines = {}, None, []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            if label is not None:
                fields[label] = "\n".join(lines).strip("\n")
            label, lines = line[4:].strip(), []
        elif label is not None:
            lines.append(line)
    if label is not None:
        fields[label] = "\n".join(lines).strip("\n")
    return {k: ("" if v.strip() == NO_ANSWER else v) for k, v in fields.items()}


def photo_url(text):
    """The first uploaded image: github.com writes it as ![name](url) or <img src="url">."""
    for url in re.findall(r'!\[[^\]]*\]\((\S+?)\)|<img[^>]*?\ssrc="([^"]+)"', text or ""):
        url = next(u for u in url if u)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == "https" and parts.hostname in PHOTO_HOSTS and (
            parts.hostname != "github.com" or parts.path.startswith("/user-attachments/assets/")
        ):
            return url
    raise PreviewError("I couldn't find a photo. Drag a jpg or png into the **Your photo** box.")


def download(url, token=None):
    req = urllib.request.Request(url, headers={"User-Agent": "ascii-profile-card"})
    if token and urllib.parse.urlsplit(url).hostname == "github.com":
        # only github.com gets the token: it redirects to storage that must never see it
        req.add_unredirected_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(MAX_BYTES + 1)
    except OSError as e:
        raise PreviewError(f"I couldn't download the photo ({e}). Try uploading it again.") from e
    if len(data) > MAX_BYTES:
        raise PreviewError("That photo is over 15 MB. Try a smaller one.")
    return data


def open_photo(path):
    try:
        image = Image.open(path)
        if image.width * image.height > MAX_PIXELS:
            raise PreviewError("That photo has too many pixels. Try a smaller one.")
        image = ImageOps.exif_transpose(image)  # phone photos are often stored sideways
        image.thumbnail((MAX_SIDE, MAX_SIDE))
    except (OSError, Image.DecompressionBombError) as e:
        raise PreviewError("That file doesn't look like a jpg or png photo.") from e
    return image


def clip(text):
    text = " ".join(text.split())
    return text[:MAX_TEXT]


def info_rows(text):
    """`Key: value` is a row, `# Name` a section, an empty line a spacer."""
    rows = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            rows.append("")
        elif line.startswith("#"):
            rows.append({"section": clip(line.lstrip("#"))})
        elif ":" in line:
            key, value = line.split(":", 1)
            rows.append({clip(key): clip(value)})
        else:
            rows.append({clip(line): ""})
    while rows and rows[-1] == "":
        rows.pop()
    if len(rows) > MAX_ROWS:
        raise PreviewError(f"**Details** has {len(rows)} rows; keep it to {MAX_ROWS} or fewer.")
    return rows


def ticked(text):
    return [m.strip() for m in re.findall(r"^- \[[xX]\] (.+)$", text or "", re.M)]


def choice(form, label, default):
    """A dropdown answer is the option's name, then maybe a description: "side (portrait left)"."""
    words = (form.get(label) or "").split()
    return words[0] if words else default


def to_config(form, base):
    """The issue's answers laid over the repo's config.yml, so everything else keeps its defaults."""
    raw = copy.deepcopy(base)
    raw.pop("header", None)
    raw["title"] = clip(form.get("Title") or "") or "you@yourname"
    raw["info"] = info_rows(form.get("Details") or "")
    raw["portrait"] = {**(raw.get("portrait") or {}), "photo": "photo.jpg", "ascii": "ascii.txt",
                       "framing": choice(form, "Framing", "head")}
    raw["portrait"].pop("crop", None)  # a crop for someone else's photo would be wrong for this one
    raw["style"] = {
        "theme": choice(form, "Theme", "github"),
        "layout": choice(form, "Layout", "side"),
        "info_style": choice(form, "Info style", "dots"),
        "charset": choice(form, "Portrait characters", "classic"),
        "invert": "invert" in ticked(form.get("Invert")),
        "frame": choice(form, "Frame", "plain"),
        "animations": ticked(form.get("Animations")),
    }
    raw.pop("themes", None)
    return raw


def make(body, out, token=None):
    """Build the cards into out. Raises PreviewError with a message for the issue's author."""
    form = parse_form(body)
    with open(config.ROOT / "config.yml", encoding="utf-8") as f:
        raw = to_config(form, yaml.safe_load(f) or {})
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.yml").write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        cfg = config.load(out / "config.yml")
    except config.ConfigError as e:
        raise PreviewError(str(e)) from e
    (out / "photo.jpg").write_bytes(download(photo_url(form.get("Your photo")), token))
    image = open_photo(out / "photo.jpg")
    try:
        lines = portrait.to_ascii(image, cfg["portrait"])
    except SystemExit as e:  # portrait explains itself this way, e.g. when it finds no person
        raise PreviewError(str(e.code)) from e
    cfg["portrait"]["ascii"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "photo.jpg").unlink()  # only the ASCII portrait leaves this step
    return [p.name for p in build.build(cfg, out)]


def card(name):
    return f'<img alt="{name}" src="__BASE__/{name}">'


def comment(out, cards, template_url):
    yml = (out / "config.yml").read_text(encoding="utf-8")
    # a fence longer than any run of backticks in the answers, so they can't close it early
    fence = "`" * max(3, 1 + max(map(len, re.findall("`+", yml)), default=0))
    pictures = "\n".join(f"<p><b>{n.removesuffix('_mode.svg')}</b><br>{card(n)}</p>" for n in cards)
    return f"""{MARKER}
## Your card

{pictures}

Want changes? **Edit this issue** and the preview is redrawn.

### Put it on your profile

1. [Use this template]({template_url}) and name the new repo exactly your GitHub username.
2. Upload the same photo as `photo.jpg`.
3. Replace `config.yml` with this one and commit:

<details>
<summary>config.yml</summary>

{fence}yaml
{yml}{fence}
</details>
"""


def failed(message):
    # messages can quote the issue's answers: keep an @name in them from pinging anyone
    message = message.replace("@", "@\u200b")
    return f"""{MARKER}
## Couldn't build your card

{message}

**Edit this issue** to fix it and I'll try again.
"""


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "preview-out")
    template_url = f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'samir-created/ascii-profile-card')}/generate"
    try:
        cards = make(os.environ.get("ISSUE_BODY", ""), out, os.environ.get("GITHUB_TOKEN"))
        text, ok = comment(out, cards, template_url), True
    except PreviewError as e:
        out.mkdir(parents=True, exist_ok=True)
        text, ok = failed(str(e)), False
    (out / "comment.md").write_text(text, encoding="utf-8")
    print(text)
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"ok={str(ok).lower()}\n")


if __name__ == "__main__":
    main()
