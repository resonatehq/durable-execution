'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

// Static — no JS, print, reduced motion — every state renders side by side,
// which is the complete diagram and the correct first paint.
//
// With JS and motion allowed the states become a history: the stack being
// executed sits in the middle, everything already executed trails off to the
// left, and what has not happened yet is not shown.
export default function StackEvolution({ columns, accentFrames = [], interval = 2200 }) {
  const wrapRef = useRef(null);
  const trackRef = useRef(null);
  const [animated, setAnimated] = useState(false);
  const [active, setActive] = useState(0);

  // One trailing empty cell, only while animating, so the cycle ends on nothing
  // and wraps back through it. There is deliberately no leading one: at rest the
  // figure must show a real state, not a blank frame.
  const cells = animated ? [...columns, { label: '', frames: [] }] : columns;

  const centre = useCallback((index) => {
    const wrap = wrapRef.current;
    const track = trackRef.current;
    const cell = track?.children[index];
    if (!wrap || !track || !cell) return;
    // offsetLeft is layout, so it ignores the transform already applied — but it
    // is measured against a shared offsetParent, hence the subtraction.
    const x = cell.offsetLeft - track.offsetLeft;
    track.style.transform = `translateX(${wrap.clientWidth / 2 - (x + cell.offsetWidth / 2)}px)`;
  }, []);

  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    setAnimated(true);
  }, []);

  useEffect(() => {
    if (!animated) return;
    const wrap = wrapRef.current;
    if (!wrap) return;

    let timer = null;
    let i = 0;

    const step = () => {
      i = (i + 1) % cells.length;
      setActive(i);
      centre(i);
    };

    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && !timer) timer = setInterval(step, interval);
        else if (!entry.isIntersecting && timer) {
          clearInterval(timer);
          timer = null;
        }
      },
      { threshold: 0.35 },
    );

    io.observe(wrap);
    centre(i);

    const onResize = () => centre(i);
    window.addEventListener('resize', onResize);
    // web fonts land after first layout and change the cell metrics
    document.fonts?.ready.then(() => centre(i));

    return () => {
      io.disconnect();
      if (timer) clearInterval(timer);
      window.removeEventListener('resize', onResize);
    };
  }, [animated, cells.length, interval, centre]);

  return (
    <div className={`stack${animated ? ' is-animated' : ''}`} ref={wrapRef}>
      <div className="stack-track" ref={trackRef}>
        {cells.map((col, ci) => (
          <div
            key={`${col.label}-${ci}`}
            className={
              'stack-col' +
              (col.frames.length === 0 ? ' is-empty' : '') +
              (animated && ci === active ? ' is-active' : '') +
              (animated && ci > active ? ' is-future' : '')
            }
          >
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
    </div>
  );
}
