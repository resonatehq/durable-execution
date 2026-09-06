'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

// Static — no JS, print, reduced motion — every state renders side by side,
// which is the complete diagram and the correct first paint.
//
// With JS and motion allowed the same states become a filmstrip: one stack in
// place, growing a frame at a time as the track slides right, from nothing up
// to the deepest stack and back to nothing.
export default function StackEvolution({ columns, accentFrames = [], interval = 1150 }) {
  const wrapRef = useRef(null);
  const trackRef = useRef(null);
  const [animated, setAnimated] = useState(false);
  const [active, setActive] = useState(0);

  // Empty bookends only exist while animating, so the static diagram stays clean.
  const cells = animated
    ? [{ label: '', frames: [] }, ...columns, { label: '', frames: [] }]
    : columns;

  const centre = useCallback((index) => {
    const wrap = wrapRef.current;
    const track = trackRef.current;
    if (!wrap || !track) return;
    const cell = track.children[index];
    if (!cell) return;
    const offset = cell.offsetLeft + cell.offsetWidth / 2 - wrap.clientWidth / 2;
    track.style.transform = `translateX(${-offset}px)`;
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
        if (entry.isIntersecting && !timer) {
          timer = setInterval(step, interval);
        } else if (!entry.isIntersecting && timer) {
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
            className={`stack-col${animated && ci === active ? ' is-active' : ''}${
              col.frames.length === 0 ? ' is-empty' : ''
            }`}
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
