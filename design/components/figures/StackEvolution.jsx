'use client';

import { useEffect, useRef, useState } from 'react';

// Renders every column at once — that is the finished, correct diagram, and it
// is what static export, print, and a failed hydration all show. With JS and
// motion allowed, a highlight steps along the columns and then returns to rest,
// so the animation departs from the resting state rather than arriving at it.
export default function StackEvolution({ columns, accentFrames = [], interval = 1100 }) {
  const ref = useRef(null);
  const [active, setActive] = useState(-1);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    let timer = null;

    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
      setActive(-1);
    };

    const start = () => {
      if (timer) return;
      let i = 0;
      setActive(0);
      timer = setInterval(() => {
        // One extra step past the end is the rest state.
        i = (i + 1) % (columns.length + 1);
        setActive(i === columns.length ? -1 : i);
      }, interval);
    };

    const io = new IntersectionObserver(
      ([entry]) => (entry.isIntersecting ? start() : stop()),
      { threshold: 0.35 },
    );
    io.observe(el);

    return () => {
      io.disconnect();
      stop();
    };
  }, [columns.length, interval]);

  return (
    <div className="stack" ref={ref}>
      {columns.map((col, ci) => (
        <div key={col.label} className={`stack-col${ci === active ? ' is-active' : ''}`}>
          <div className="stack-num">{col.label}</div>
          <div className="stack-frames">
            {[...col.frames].reverse().map((frame) => (
              <div
                key={frame}
                className={`stack-frame${accentFrames.includes(frame) ? ' is-accent' : ''}`}
              >
                {frame}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
