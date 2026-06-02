# LinkedIn launch post (draft)

Em-dash-free, ready to edit. Attach a demo GIF (the opening-picks or
trade-accept clip from `docs/media/`) and the promo tile. Swap the
`<link>` once the Chrome Web Store listing is live.

---

## Draft A (build story)

I built CatanBot: a live Settlers of Catan advisor that runs as a Chrome
side panel next to your colonist.io game and tells you the strongest
move in real time, the way a chess engine's eval bar sits next to a
board.

What it does, live:
- Ranks every opening settlement and follow-up road
- Scores each in-game action (settle, city, road, dev card, trades) on a
  1 to 10 scale
- Reads dev-card timing (play the knight now or hold?), robber targets,
  and incoming trades with an accept / decline / counter verdict
- Tracks which strategy archetype your board is pushing you toward and
  biases its picks accordingly

A few things I am proud of:
- It is a handcrafted heuristic engine over the catanatron Catan
  library, not an LLM and not a black box. Every recommendation has a
  readable reason.
- Public information only. It uses what any player can see and infers
  opponent hands the same way a sharp human would. No hidden state, no
  cheating.
- Local and private. The whole thing runs on your machine. Your game
  state never leaves 127.0.0.1.
- Two engines: a Python "lab" where I prototype heuristics and a
  standalone JavaScript port so the extension works with zero install.

Built solo, in the open, GPL-3.0. Link in the comments.

#SettlersOfCatan #boardgames #softwareengineering #python #javascript

---

## Draft B (shorter, hook-first)

Spent my spare cycles building a chess-engine-style analysis bar, but for
Settlers of Catan.

CatanBot is a Chrome side panel that watches your colonist.io game and
ranks your best move live: openings, builds, dev-card timing, robber
targets, and trade accept / decline / counter calls. Handcrafted
heuristics over the catanatron engine, public information only, and it
runs entirely on your machine (nothing leaves 127.0.0.1).

Open source, GPL-3.0. Built solo. Demo and link below.

#Catan #gamedev #python #javascript

---

## Posting notes

- Put the GitHub / Chrome Web Store link in the first comment, not the
  body (LinkedIn suppresses reach on posts with outbound links in the
  body).
- Lead with the demo GIF; it stops the scroll far better than text.
- If the listing is still unlisted at post time, link the GitHub repo
  and the unlisted CWS install link instead.
