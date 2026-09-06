# Handoff: scattered thoughts on paper — blog design

## Overview

A personal essay blog. Three views: a **post page** (the primary artifact), a **writing index**, and an **about page**. The design is built for long-form technical writing with many short paragraphs, a narrow measure, inline code blocks, and figures — including animated, data-driven diagrams.

Two design files are included:

- `Zen of Software.dc.html` — the real first post, "The Zen of Software", plus the index and about views. This is the fidelity reference.
- `Post Template.dc.html` — the same design with the writing removed: every element class the system supports, once each, as a skeleton for new posts.

## About the design files

**These are design references created in HTML, not production code.** They are prototypes showing intended look and behavior. The task is to recreate them in the target codebase using its own patterns — not to copy the markup.

Specifically: the design files use inline styles and a small custom runtime (`support.js`) that exists only in the design tool. Do not port either. Rebuild the visual system as React components with plain CSS, per the target below.

The files open directly in a browser if you want to click through them. Keep `support.js` alongside them.

## Fidelity

**High-fidelity.** Colors, typography, spacing, and motion are final. Recreate pixel-accurately; every value is listed under Design Tokens below.

## Target environment

Decided with the author:

- **Next.js / React**, static-exported (`output: 'export'`) and hosted on **GitHub Pages**. No server at runtime, no API routes, no server-side rendering at request time.
- **MDX** for post authoring — Markdown prose with React components dropped inline for code cards and figures.
- **Plain CSS with custom properties.** No Tailwind, no CSS-in-JS. The design is a small token set plus about a dozen element styles; a utility framework would be more machinery than it needs.
- **No syntax highlighting.** The posts are largely Lean 4, which most highlighters do not support and render worse than plain text. Keep the titled card wrapper and monospace body; leave the code uncolored.

### GitHub Pages notes

- Set `basePath` and `assetPrefix` if the site is served from `user.github.io/repo` rather than a custom domain.
- Add `.nojekyll` at the output root so files beginning with `_` are served.
- `next/image` optimization requires a server — use `images: { unoptimized: true }`, or plain `<img>`.

## Animations — component contract

The author's requirement: **each animation is its own component, owns its own rendering, and is driven by data passed in as props.** It must run entirely client-side with no server.

That means, per animated figure:

```jsx
// components/figures/EntropyChart.jsx
'use client';

export function EntropyChart({ series, accent }) {
  // owns its own canvas/SVG, its own clock, its own resize handling
}
```

Rules that the design depends on:

1. **`'use client'` on every animation component.** They use `requestAnimationFrame`, `IntersectionObserver`, and `matchMedia`, none of which exist during static export.
2. **Data in as props, never fetched at runtime.** Import a local JSON/TS module at build time and pass it in. This keeps the site static and makes figures diffable in git.
3. **Render something correct at frame zero.** Static export produces HTML with no JS executed; the first paint must be the finished state, not an empty box. Animate *from* the resting state, not *into* it.
4. **Respect `prefers-reduced-motion`.** Skip animation and render the final frame.
5. **Pause when off-screen.** Use `IntersectionObserver` and stop the RAF loop when not visible.
6. **Take `accent` as a prop** (or read `var(--accent)`) rather than hardcoding a color — the accent is user-configurable.

The design's own reveal-on-scroll behavior follows the same principle and should be lifted directly: elements are **visible by default** in CSS, and JS adds a class to the document root that *arms* the hidden-then-reveal transition — only after confirming the animation clock is actually advancing. If JS never runs, or motion is reduced, or the page is printed, everything is simply visible. See `[data-reveal]` and `.zos-armed` in the design file's `<style>` block. Do not invert this: hiding by default and revealing with JS produces a blank page for crawlers, print, and any failed hydration.

## Screens / views

### 1. Post page

The primary view. Vertical single column, centered.

**Layout**
- Page background `--bg`. Root has `overflow-x: clip` (a margin note can otherwise cause horizontal scroll).
- **Header**: `max-width: 1180px`, centered, `padding: 30px 40px`, flex row, `space-between`, `gap: 24px`. Left: wordmark. Right: nav + theme toggle, `gap: 26px`.
- **Hairline** under the header: `max-width: 1180px`, `padding: 0 40px`, a `1px` div of `--rule`.
- **Article**: `max-width: var(--measure)` = **720px**, centered, `padding: 100px 24px 40px`, `position: relative` (anchors margin notes).
- **Footer**: same 720px measure, `padding: 96px 24px 120px`, hairline above, then a flex row `space-between`.

**Header components**
- Wordmark: flex row, `gap: 11px`, `Instrument Sans` 13px, color `--muted`, no underline. Preceded by a `9px × 9px` square of `--accent` rotated 45°.
- Nav links: `Instrument Sans` 13px, `--muted`, no border-bottom.
- Theme toggle: `1px` border of `--rule`, transparent background, `--muted` text, `Instrument Sans` 12px, `padding: 5px 11px`, `border-radius: 999px`, `cursor: pointer`. Label is "Dark" in light mode and "Light" in dark mode.

**Hero (in order, each with a staggered entrance)**
1. **Eyebrow** — flex row, `gap: 14px`, `Instrument Sans` 12px, `letter-spacing: 0.1em`, uppercase, color `#8e9398`. Contents: kind ("Essay"), an `18px × 1px` rule at `rgba(38,40,43,0.2)`, month and year.
2. **H1** — `font-weight: 400`, **56px**, `line-height: 1.06`, `letter-spacing: -0.022em`, `margin: 22px 0 0`, `text-wrap: balance`.
3. **Standfirst** — italic, `font-weight: 300`, **21px**, `line-height: 1.5`, `--muted`, `margin: 20px 0 0`, `max-width: 34em`.
4. **Meta** — flex row, `gap: 12px`, `Instrument Sans` 13px, `--muted`, `margin: 38px 0 0`. Read time, a `·` at `opacity: 0.5`, date.

Then a `68px` spacer before the body.

Entrance animation: `@keyframes zos-rise { from { transform: translateY(12px) } to { transform: none } }`, `700ms ease`, delays `0 / 60ms / 120ms / 180ms`. **Transform only — it must not animate opacity**, so a stalled clock cannot leave the hero invisible.

**Body elements**

| Element | Style |
|---|---|
| Paragraph | 20px / `line-height: 1.74`, color `--ink`, `margin: 26px 0 0`. First after the spacer: `margin: 0`. A paragraph introducing a code card: `margin: 56px 0 0`. |
| H2 | `font-weight: 400`, 33px, `line-height: 1.2`, `letter-spacing: -0.012em`, `margin: 84px 0 0`. |
| Inline code | `JetBrains Mono`, `font-size: 0.82em`, inherits color. |
| Code card | See below. |
| Pull quote | `margin: 58px 0 0`, `padding-left: 26px`, `border-left: 2px solid var(--accent)`. Inner p: italic, `font-weight: 300`, 28px, `line-height: 1.38`. |
| Margin note | `Instrument Sans` 12.5px, `line-height: 1.65`, color `#6b7075`. Positioned absolute at `top: 30px; left: 100%; width: 170px; margin-left: 40px; padding-left: 14px; border-left: 1px solid rgba(38,40,43,0.14)`. **Below 1160px** it becomes static, `max-width: 32em`, `margin: 20px 0 0`. Requires a `position: relative` wrapper shared with its paragraph. |
| Figure | `margin: 62px 0 0`. Frame: `1px` border of `--rule`, `background: --card`, `border-radius: 3px`, `padding: 34px 26px`. |
| Full-bleed figure | Breaks the measure up to `max-width: 1180px` via `width: 100vw; position: relative; left: 50%; transform: translateX(-50%)`. Height 380px in the template. Caption stays constrained to `max-width: var(--measure)`. |
| Figcaption | `Instrument Sans` 12.5px, `line-height: 1.6`, color `#6b7075`, `margin: 14px 0 0`, flex row, `gap: 10px`. First span is the figure number in `--accent`, `flex: none`. |
| Section ornament | Centered flex row, `gap: 10px`, `margin: 76px 0 0`: a `40px × 1px` rule, a `5px` square of `--accent` rotated 45° at `opacity: 0.75`, another `40px × 1px` rule. |

**Code card** (the most repeated element — nine instances in the reference post)

```
container: 1px solid var(--rule), border-radius 3px, overflow hidden,
           background var(--card), margin 34px 0 0
  header:  flex, space-between, padding 9px 14px,
           border-bottom 1px solid rgba(38,40,43,0.09),
           JetBrains Mono 11.5px, color #6b7075
           left span:  definition name(s), e.g. "Next, Valid"
           right span: language label, color var(--accent), e.g. "Lean 4"
  pre:     margin 0, padding 18px 16px, JetBrains Mono 13.5px,
           line-height 1.85, color var(--ink), overflow-x auto
```

No callout footer — the sentence above each card carries the explanation. (An earlier version had one; it read as saying the same thing twice.)

**Reading rhythm.** The post alternates: one sentence, one code card, repeat. Prose paragraphs before a card use the wider `56px` top margin. There are no dense multi-paragraph runs — this is deliberate and should survive the port.

### 2. Writing index

- Same header, footer, and 720px measure.
- H1 "Writing": `font-weight: 400`, 44px, `line-height: 1.1`, `letter-spacing: -0.02em`.
- Standfirst: italic, `font-weight: 300`, 20px, `--muted`, `margin: 16px 0 0`.
- List begins `margin: 64px 0 0`. Each entry is a link, `display: grid`, `grid-template-columns: 92px 1fr`, `gap: 20px`, `padding: 26px 0`, `border-top: 1px solid var(--rule)`; last entry also gets a `border-bottom`. No underline.
  - Column 1: date as `JetBrains Mono` 11.5px, color `#8e9398`, `padding-top: 8px`, formatted `2026.09`.
  - Column 2: title at 25px `line-height: 1.3`, then a dek in `Instrument Sans` 13.5px `line-height: 1.6`, color `#6b7075`, `margin-top: 7px`.

### 3. About

- Same chrome and measure. H1 "About" matches the index H1.
- Portrait slot: `margin: 44px 0 0`, `height: 260px`, `1px` border of `--rule`, `border-radius: 3px`, `background: --card`, with a diagonal-stripe SVG pattern as placeholder. **Replace with a real image.**
- Prose paragraphs at the standard 20px / 1.74.
- Contact block: `margin: 56px 0 0`, `padding-top: 26px`, hairline above, flex column `gap: 12px`. Each row is a `grid-template-columns: 90px 1fr` with `gap: 16px` — label in `#8e9398`, value as a link.

## Interactions & behavior

- **Nav** — in the design file the three views are switched in client state because it is a single prototype file. In the real site these are routes: `/`, `/writing`, `/about`, and `/writing/<slug>`. Scroll resets to top on navigation.
- **Theme toggle** — swaps the token set (see Design Tokens). Persist the choice in `localStorage` and apply it before first paint (a tiny inline script in `<head>`) to avoid a flash. Also honor `prefers-color-scheme` as the initial default. The design file does not do either — it is a prototype.
- **Reveal on scroll** — `IntersectionObserver` with `rootMargin: '0px 0px -12% 0px'`; on intersect, add `zos-in` and unobserve. Transition: `opacity 700ms ease, transform 700ms cubic-bezier(0.2, 0.7, 0.2, 1)`, from `translateY(12px)`. Anything already at or above `88%` of the viewport height on first pass is revealed immediately without animating, so nothing above the fold sits hidden. Re-scan on scroll to catch late-mounted content.
- **Link hover** — body links carry `border-bottom: 1px solid var(--accentSoft)` and shift to `--accent` (both text and border) over `160ms ease`. Chrome links (header, footer, index entries) have no border at all.
- **Selection** — `::selection` background `--accentSoft` at 22% alpha.
- **Print** — all revealed content forced visible, transforms cleared.
- **Reduced motion** — reveals and hero entrances disabled; everything renders in its resting state.

## State management

Small. Per page:

- `dark: boolean` — theme, persisted to `localStorage`, initialized from `prefers-color-scheme`.
- Reveal state is DOM-only (a class), not React state — deliberately, so re-renders cannot re-trigger entrance animations.

Optional, exposed as knobs in the prototype and worth keeping as constants rather than UI: `accent` (color), `measure` (column width in px), `typeface` (serif / sans), `justify` (boolean).

No data fetching. Posts are MDX files in the repo; the index is generated at build time from their frontmatter.

## Design tokens

### Light (default)

| Token | Value | Used for |
|---|---|---|
| `--bg` | `#fcfcfd` | Page background |
| `--card` | `#ffffff` | Code cards, figure frames |
| `--ink` | `#26282b` | Body text, headings, code |
| `--muted` | `#5f6469` | Standfirst, meta, nav, chrome |
| `--rule` | `rgba(38,40,43,0.11)` | Hairlines, card borders |
| `--accent` | `#4a6b8f` | Figure numbers, language labels, ornaments, hover |
| `--accentSoft` | `--accent` + `3d` (24% alpha) | Link underlines, selection |

Secondary greys used inline: `#6b7075` (captions, card headers, margin notes), `#8e9398` (eyebrow, index dates, footer). Stronger rule variants: `rgba(38,40,43,0.14)` (borders on interactive chrome, ornaments), `rgba(38,40,43,0.09)` (card header divider), `rgba(38,40,43,0.2)` (eyebrow tick).

### Dark

| Token | Value |
|---|---|
| `--bg` | `#111315` |
| `--card` | `#181a1d` |
| `--ink` | `#e4e6e9` |
| `--muted` | `#949a9f` |
| `--rule` | `rgba(228,230,233,0.14)` |
| `--accent` | `#7d9fc4` |

Stripe placeholder fills, if you keep them: light `#f5f6f7` / `#e9eaec`, dark `#181a1d` / `#202427`.

The palette is deliberately near-neutral and cool. An earlier warm version (cream `#faf8f5`, terracotta `#a9552f`) was rejected — do not reintroduce warmth.

### Typography

| Role | Family | Notes |
|---|---|---|
| Prose, headings | **Source Serif 4** (variable, opsz 8–60), weights 300/400/500, italics | Fallback `Georgia, serif`. Body copy is 400; H1/H2 are 400, not bold. |
| Chrome, captions | **Instrument Sans**, weights 400/500/600 | Fallback `Helvetica, Arial, sans-serif` |
| Code, numerals | **JetBrains Mono**, weights 400/500 | Fallback `monospace` |

Loaded from Google Fonts in the prototype. **For a static site, self-host them** (`next/font/local` or plain `@font-face` with WOFF2) — it removes a third-party request and the FOUT.

Optional alternates the prototype supports, in case the author wants to switch: **Instrument Sans** as the prose face (body drops to 18.5px, H1/H2 go to weight 500 and `letter-spacing: -0.026em`), or **Newsreader** as a more mannered serif. Ship one; the switch is not a user-facing feature.

**Scale**

| Use | Size | Line height | Tracking |
|---|---|---|---|
| H1 (post) | 56px | 1.06 | -0.022em |
| H1 (index/about) | 44px | 1.1 | -0.02em |
| H2 | 33px | 1.2 | -0.012em |
| Pull quote | 28px | 1.38 | — |
| Index entry title | 25px | 1.3 | — |
| Standfirst | 21px | 1.5 | — |
| Body | 20px | 1.74 | — |
| Code block | 13.5px | 1.85 | — |
| Inline code | 0.82em | inherit | — |
| Chrome / nav / meta | 13px | — | — |
| Caption / dek | 12.5px | 1.6 | — |
| Eyebrow | 12px | — | 0.1em, uppercase |
| Card header | 11.5px | — | — |

Optional justification mode (off by default): body paragraphs get `text-align: justify; text-justify: inter-word; hyphens: auto` and the root needs `lang="en"` for hyphenation. Captions, pull quotes, margin notes, and `<pre>` stay ragged-left. Note for the author: it reads better at a wider measure (760–800px) and worse in the sans typeface.

### Other values

- **Measure**: 720px (article and footer). Chrome and full-bleed figures: 1180px.
- **Radius**: 3px everywhere except the pill toggle (999px).
- **Borders**: 1px hairlines; 2px only on the pull quote's left rule.
- **Shadows**: none. The design uses hairlines and background contrast instead — do not add elevation.
- **Motion**: 700ms `cubic-bezier(0.2, 0.7, 0.2, 1)` for reveals, 700ms `ease` for hero entrances, 160ms `ease` for link hover.
- **Vertical rhythm**: 26px between paragraphs, 56px before a code card's lead sentence, 34px from lead to card, 84px above an H2, 62–68px around figures, 76px around the ornament.

## Assets

None shipped. Two placeholders need real material:

- **About page portrait** — 260px tall, currently a striped SVG placeholder.
- **Full-bleed figure slot** in the template — 380px tall.

Fonts come from Google Fonts (Source Serif 4, Instrument Sans, JetBrains Mono) and should be self-hosted. All ornaments (diamonds, rules, ticks) are CSS, not images — keep them that way.

## Files

| File | What it is |
|---|---|
| `Zen of Software.dc.html` | Fidelity reference: the finished post, plus index and about views |
| `Post Template.dc.html` | Blank post skeleton — every supported element class, once each |
| `support.js` | Design-tool runtime. **Do not port.** Needed only to open the two HTML files locally. |

Suggested structure in the target repo:

```
app/
  layout.jsx                 # header, footer, theme init script
  page.jsx                   # writing index
  about/page.jsx
  writing/[slug]/page.jsx    # MDX post route
components/
  CodeCard.jsx               # titled card + <pre>
  Figure.jsx                 # frame + numbered caption
  FullBleedFigure.jsx
  PullQuote.jsx
  MarginNote.jsx
  Reveal.jsx                 # wraps children, applies data-reveal
  ThemeToggle.jsx
  figures/                   # one client component per animation
content/
  writing/*.mdx
styles/
  tokens.css                 # the token tables above
  prose.css                  # element styles
```

## Notes for implementation

- **The measure is the design.** The author writes in many short paragraphs; 720px is what makes that rhythm work. Do not widen it to fill large screens.
- **No density.** Generous vertical spacing is the point, not an oversight.
- **The caption carries the claim.** Figures illustrate; captions argue. Keep them to one sentence.
- **One sentence per code card.** The reference post is deliberately a chain of definition → one sentence → definition. Resist adding explanatory paragraphs between them.
