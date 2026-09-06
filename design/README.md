# design

The site. Next.js, static-exported to GitHub Pages, posts authored in MDX.

```
npm install
npm run dev        # http://localhost:3000
npm run build      # static export into out/
```

`GITHUB_PAGES=true npm run build` sets `basePath` for `user.github.io/repo`.
Deployment is `.github/workflows/pages.yml`; enable Pages with source
"GitHub Actions" in repository settings.

## Writing a post

Posts are `content/writing/*.mdx`. Frontmatter drives the hero and the index:

```
---
title: From Ephemeral to Durable
dek: One line for the writing index.
standfirst: One italic line under the title.
kind: Note
date: 2026-09-06
readTime: 12 min read
---
```

Code blocks are ordinary fenced blocks; the info string carries the card name:

    ```python name="Store"
    class Store:
        ...
    ```

Do **not** pass code to `<CodeCard>` as a template-literal prop. MDX re-indents
template literals inside JSX attributes — four spaces become two — which is
silently wrong for Python. Fenced blocks are preserved exactly.

There is no syntax highlighting, by design. Keep it that way.

## Components

| Component | Use |
|---|---|
| `<Figure number caption>` | Framed figure with a numbered caption |
| `<StackEvolution columns accentFrames>` | The animated call-stack diagram |
| `<PullQuote>` | Accent-ruled quote |
| `<MarginNote>` inside `<NoteWrap>` | Side note, collapses inline below 1160px |
| `<Ornament />` | Section break |

### Animated figures

One component per figure, in `components/figures/`, following the rules the
design depends on:

1. `'use client'` — they use `requestAnimationFrame`, `IntersectionObserver`,
   and `matchMedia`, none of which run during static export.
2. Data in as props. No fetching at runtime, so figures stay diffable in git.
3. **Correct at frame zero.** Static export ships HTML with no JS executed, so
   the first paint must already be the finished diagram. Animate away from the
   resting state and back, never into it.
4. Honour `prefers-reduced-motion` — render the resting state and stop.
5. Pause when off-screen.
6. Take the accent from `var(--accent)` or a prop; it is user-configurable.

`StackEvolution` renders every column at once — that is the complete, correct
diagram, and it is what print, crawlers, and a failed hydration all get. The
animation only moves a highlight along the columns and returns to rest.

Reveal-on-scroll follows the same rule: elements are visible in CSS, and
`RevealRuntime` arms the transition only after confirming the animation clock is
advancing. Never invert this.

## Layout

```
app/                 routes: index, /about, /writing/<slug>
components/          element components + figures/
content/writing/     the posts
lib/                 frontmatter loading, the fenced-code-meta remark plugin
styles/              tokens.css (the palette), prose.css (element styles)
reference/           the original design handoff — see below
```

`reference/` holds the source design: two `.dc.html` prototypes, the handoff
notes, and `support.js`. **`support.js` is a design-tool runtime — do not port
it.** It is only there so the prototypes open in a browser.
