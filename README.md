<h1 align="center">🪙 Coin-Op</h1>

<p align="center">A tiny pixel dashboard of where my Claude tokens go.<br>
Day by day, by focus area, and against the product's own roadmap. Set up for <a href="https://github.com/chrisqtruong/vox2">Vox2</a>.</p>

<p align="center"><a href="https://chrisqtruong.github.io/coin-op/"><b>Open the dashboard</b></a> · <a href="https://github.com/chrisqtruong/coin-op/releases">Releases</a></p>

<p align="center"><img src="screenshots/top.png" width="820" alt="Coin-Op: $137 spent across 3 days and 204 turns, with a pixel-block chart of spend per day colored by focus area"></p>

## Why

I build [Vox2](https://github.com/chrisqtruong/vox2), a small desktop translator, mostly by working with Claude. A product has a lot of sides: the UI, bug fixes, the Mac port, releases, and lately the meaning check and match score, which was eating a lot of tokens. I wanted to see that: which bucket the work is going into each day, what it would cost, and whether it lines up with what the roadmap says matters most.

It's a small personal project. The sorting is a good guess, not an audit. But it's already useful: it showed that the meaning check took 48% of roadmap spend while holding 32% of the roadmap's value, and that the everyday features were the ones falling behind.

## What it shows

<p align="center"><img src="screenshots/full.png" width="560" alt="The full dashboard: spend by day, high scores per bucket, quest log comparing spend with roadmap themes, and next-up recommendations"></p>

- **Score row.** Estimated cost, tokens, days and turns.
- **Spend by day.** Each day is a column of pixel blocks (1 block = $1), colored by bucket. Hover a day for the breakdown; switch between dollars and tokens.
- **High scores.** The buckets, biggest first: meaning & match score, look & features, releases & GitHub, docs & planning, Mac version, snip & voice, bugs, learning.
- **Quest log.** Buckets roll up into the roadmap's themes (*trustworthy*, *useful every day*, *reach more people*) plus *upkeep*. For each theme: its share of spend next to its share of the roadmap's value, and its items as done / started / not started.
- **Next up.** What to aim at next: finish what's started, then the best value-for-effort item in the theme that's furthest behind, then the best quick win. It uses the value and effort tags already in Vox2's roadmap.
- **Recent turns** (on your own computer only). Your last few requests and what each cost.

## How it works

```
~/.claude/projects/*.jsonl      Claude Code's own transcripts, on each computer
          │
     coinop.py                  keeps the Vox2 sessions, sorts each turn into a bucket,
          │                     prices it at API rates, reads the roadmap from Vox2's README
          ├──▶ localhost:8642   live page on that computer, updates every few seconds
          └──▶ data branch      every 5 minutes: that computer's totals (no prompts)
                    │
          GitHub Pages site     reads the data branch, checks every 2 minutes
```

- **Which turns count.** A turn is your message plus everything Claude did for it. It counts as Vox2 work when Claude touched the Vox2 repo and no other project in it.
- **Buckets.** Picked from the words in your message and the files Claude touched. Short replies like "yes do it" go by the files; background-task notes and context summaries count toward the turn before. The word lists are at the top of `coinop.py`.
- **Cost.** Tokens priced at Claude API list prices (input, output, cache writes and cache reads). On a Claude plan you pay a flat fee, so treat it as a measure of work, not a bill. Most tokens are Claude re-reading the conversation (cache reads), which are cheap, so tokens and cost don't move together.
- **More than one computer.** Each computer writes its own file on the `data` branch and the totals add them up, so a Mac and a PC never overwrite each other.

## Set it up

Python 3.9+ and git signed in to GitHub. Nothing to install.

```
git clone https://github.com/chrisqtruong/coin-op.git
cd coin-op
python3 coinop.py --install
```

`--install` starts Coin-Op whenever you log in (macOS: `launchd`; Windows: the Startup folder), keeps [localhost:8642](http://localhost:8642) live, and publishes to the website every 5 minutes. Do it once per computer.

| | |
|---|---|
| Run once, without installing | `python3 coinop.py` |
| Stop and remove | `python3 coinop.py --uninstall` |
| Count transcripts copied from elsewhere | `python3 coinop.py --projects <folder>` |
| Log (macOS) | `~/.coin-op/log.txt` |

To point it at a different product, change the block marked *the product being tracked* near the top of `coinop.py`: its name, where its README roadmap lives, and the paths that mark its work.

## Privacy

Everything is read on your own computer. What goes up to GitHub is only totals per session, day and bucket, plus the parsed roadmap: never your prompts, file contents or anything Claude wrote. The "recent turns" list, which shows your prompts, appears only on the local page.

## Limits

- The buckets are keyword rules: good enough to see the shape, not exact. A turn that fixes a bug *and* merges counts once, for whichever it was mostly about.
- It only knows what Claude Code saved on the computers that run it.
- Prices are API list prices for the model each turn used (Claude Opus 5.5 so far).

## License

[MIT](LICENSE)
