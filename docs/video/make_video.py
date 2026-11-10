"""Build the ~70 s demo video from real agent logs and dashboard screenshots.

    python docs/video/make_video.py --signal 3 --shots docs/video/assets/dash1.png docs/video/assets/dash2.png

Scenes: title, problem, how it works, terminal replay of one signal's lifecycle (taken from
agents/logs/agents.log), dashboard screenshots, outro. Output: docs/video/provable-alpha-demo.mp4
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
FFMPEG = shutil.which("ffmpeg") or r"H:\work\.tools\ffmpeg-pkg\ffmpeg-9.0.2-essentials_build\bin\ffmpeg.exe"
W, H = 1280, 720
BG, PANEL, TEXT, MUTED, ACCENT, LINE = "#0f1114", "#171a1f", "#e9ecf1", "#9aa3b0", "#3ccf9b", "#2a2f37"
FONTS = Path("C:/Windows/Fonts")


def font(name: str, size: int):
    return ImageFont.truetype(str(FONTS / name), size)


SANS, SANS_B, MONO = "segoeui.ttf", "segoeuib.ttf", "consola.ttf"


def canvas():
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)


def footer(d):
    d.text((40, H - 44), "Provable Alpha · NEAR HACKATHON 2026 · testnet", font=font(SANS, 18), fill=MUTED)


def wrap(d, text, f, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def title_card():
    img, d = canvas()
    d.text((80, 230), "Provable Alpha", font=font(SANS_B, 76), fill=TEXT)
    d.text((84, 335), "AI agents sell trading signals they can't fake.", font=font(SANS, 36), fill=ACCENT)
    d.text((84, 395), "Commit a hash  →  deliver sealed  →  forced reveal  →  on-chain hit/miss record", font=font(SANS, 24), fill=MUTED)
    footer(d)
    return img


def text_card(heading, body):
    img, d = canvas()
    d.text((80, 150), heading, font=font(SANS_B, 48), fill=TEXT)
    y = 250
    for para in body:
        for line in wrap(d, para, font(SANS, 30), W - 160):
            d.text((80, y), line, font=font(SANS, 30), fill=TEXT if para is body[0] else MUTED)
            y += 44
        y += 18
    footer(d)
    return img


STEPS = [
    ("Commit", "sha256(signal:salt) + price + bond. The seller can't change it any more."),
    ("Buy", "Buyer agent pays into escrow, publishes an X25519 key."),
    ("Deliver", "Signal stored sealed to the buyer. Payment released. Buyer checks the hash."),
    ("Reveal", "After the horizon the seller must publish it, or the bond goes to buyers."),
    ("Settle", "Verifier re-measures the market and records hit or miss on-chain."),
]


def steps_card(n):
    img, d = canvas()
    d.text((80, 70), "How a signal moves", font=font(SANS_B, 44), fill=TEXT)
    for i, (h, b) in enumerate(STEPS):
        y = 160 + i * 98
        on = i < n
        d.rounded_rectangle((80, y, W - 80, y + 82), radius=14, fill=PANEL, outline=ACCENT if on else LINE, width=2)
        d.text((104, y + 12), f"{i + 1}. {h}", font=font(SANS_B, 28), fill=ACCENT if on else MUTED)
        d.text((330, y + 18), b, font=font(SANS, 23), fill=TEXT if on else MUTED)
    footer(d)
    return img


CAPTIONS = [
    (r"committed signal", "1 · Seller commits only the hash, price and bond"),
    (r"bought #", "2 · Buyer agent checks the seller's record and pays into escrow"),
    (r"delivered #", "3 · Seller delivers the signal sealed to the buyer's key"),
    (r"decrypted", "3 · Buyer decrypts it: the hash matches the on-chain commitment"),
    (r"signal:", "3 · Buyer acts on it (paper trade)"),
    (r"revealed #", "4 · After the horizon the seller must reveal; the contract re-checks the hash"),
    (r"settled #", "5 · Verifier re-measures Intents vs CEX and records the outcome"),
    (r"public reveal", "Buyer confirms the public reveal equals the private delivery"),
]


def short_line(line: str) -> str:
    line = re.sub(r"\s+https://testnet\.nearblocks\.io/txns/(\w{6})\w+", r"  tx \1…", line)
    return line


def terminal_frames(lines):
    frames = []
    mono = font(MONO, 17)
    for k in range(1, len(lines) + 1):
        img, d = canvas()
        d.rounded_rectangle((40, 40, W - 40, H - 150), radius=14, fill="#0a0c0f", outline=LINE, width=2)
        for i, c in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
            d.ellipse((62 + i * 24, 58, 76 + i * 24, 72), fill=c)
        d.text((150, 54), "agents/run_all.py  —  market.provable-alpha.testnet", font=font(SANS, 18), fill=MUTED)
        y = 96
        shown = lines[max(0, k - 18):k]
        for j, line in enumerate(shown):
            color = ACCENT if j == len(shown) - 1 else TEXT
            for part in wrap(d, line, mono, W - 130)[:2]:
                d.text((64, y), part, font=mono, fill=color)
                y += 24
        cap = next((c for pat, c in CAPTIONS if re.search(pat, lines[k - 1])), "")
        d.rounded_rectangle((40, H - 130, W - 40, H - 62), radius=12, fill=PANEL)
        d.text((64, H - 116), cap, font=font(SANS_B, 28), fill=TEXT)
        footer(d)
        frames.append(img)
    return frames


def shot_card(path: Path, caption: str):
    img, d = canvas()
    shot = Image.open(path).convert("RGB")
    shot.thumbnail((W - 80, H - 170))
    x = (W - shot.width) // 2
    img.paste(shot, (x, 40))
    d.rectangle((x, 40, x + shot.width, 40 + shot.height), outline=LINE, width=2)
    d.text((60, H - 112), caption, font=font(SANS_B, 30), fill=TEXT)
    footer(d)
    return img


def outro():
    img, d = canvas()
    d.text((80, 200), "Provable Alpha", font=font(SANS_B, 60), fill=TEXT)
    rows = [
        "github.com/chaboss-work/provable-alpha",
        "contract: market.provable-alpha.testnet",
        "Rust contract · Python agents · NEAR Intents 1Click · NaCl sealed boxes",
        "@NEARProtocol  #NEARHackathon",
    ]
    for i, r in enumerate(rows):
        d.text((84, 300 + i * 52), r, font=font(SANS, 30), fill=ACCENT if i == 0 else MUTED)
    footer(d)
    return img


def lifecycle_lines(log: Path, signal_id: int) -> list[str]:
    tag = f"#{signal_id}"
    out = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.search(rf"{re.escape(tag)}\b", line) and not "error" in line:
            out.append(short_line(line))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", type=int, required=True)
    ap.add_argument("--log", default=str(ROOT / "agents/logs/agents.log"))
    ap.add_argument("--shots", nargs="*", default=[])
    ap.add_argument("--out", default=str(ROOT / "docs/video/provable-alpha-demo.mp4"))
    args = ap.parse_args()

    lines = lifecycle_lines(Path(args.log), args.signal)
    if len(lines) < 5:
        raise SystemExit(f"signal #{args.signal}: only {len(lines)} log lines, need a full lifecycle")

    scenes = [(title_card(), 4.0),
              (text_card("The problem", [
                  "Signal sellers delete losing calls and keep the winners.",
                  "When AI agents buy signals from other agents, nobody can check who is actually good."]), 6.0)]
    scenes += [(steps_card(n), 1.6) for n in range(1, 6)]
    scenes[-1] = (scenes[-1][0], 4.0)
    scenes += [(f, 2.6) for f in terminal_frames(lines)]
    shot_caps = ["Dashboard: every agent's on-chain record", "Anyone can verify a revealed signal in the browser"]
    for i, s in enumerate(args.shots):
        scenes.append((shot_card(Path(s), shot_caps[i % len(shot_caps)]), 5.0))
    scenes.append((outro(), 5.0))

    with tempfile.TemporaryDirectory() as tmp:
        listing = []
        for i, (img, dur) in enumerate(scenes):
            p = Path(tmp) / f"{i:03d}.png"
            img.save(p)
            listing.append(f"file '{p.as_posix()}'\nduration {dur}")
        listing.append(f"file '{(Path(tmp) / f'{len(scenes) - 1:03d}.png').as_posix()}'")
        (Path(tmp) / "list.txt").write_text("\n".join(listing), encoding="utf-8")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(Path(tmp) / "list.txt"),
                        "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-crf", "20", "-movflags", "+faststart", args.out], check=True)
    total = sum(d for _, d in scenes)
    print(f"{args.out}  {total:.1f}s  {len(scenes)} frames")


if __name__ == "__main__":
    main()
