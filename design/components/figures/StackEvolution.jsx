'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

// Static — no JS, print, reduced motion — every state renders side by side,
// which is the complete diagram and the correct first paint. Each state carries
// its own copy of the store underneath.
//
// With JS and motion allowed the states become a history: the stack being
// executed sits in the middle of the strip, everything already executed trails
// off to the left, and what has not happened yet is not shown. The store then
// moves out of the strip into a panel on the right, showing the whole store as
// it stands at the active state — the record that outlives the frames.
//
// A store row is written 'id STATE'; the last word is the state.
const parseRow = (row) => {
  const i = row.lastIndexOf(' ');
  return [row.slice(0, i).trim(), row.slice(i + 1)];
};

const snapshot = (rows = []) => Object.fromEntries(rows.map(parseRow));

export default function StackEvolution({ columns, accentFrames = [], interval = 3000 }) {
  const stripRef = useRef(null);
  const trackRef = useRef(null);
  const [animated, setAnimated] = useState(false);
  const [active, setActive] = useState(0);
  // The store lags the stack by half a step: the frame that creates or settles
  // a promise is on screen first, and the store changes while it is showing.
  const [shown, setShown] = useState(0);

  const hasStore = columns.some((c) => c.store);
  const last = columns[columns.length - 1];

  // One trailing empty cell, only while animating, so the cycle ends on nothing
  // and wraps back through it. It keeps the final store: the stack is gone, the
  // record is not. There is deliberately no leading empty cell: at rest the
  // figure must show a real state, not a blank frame.
  const cells = animated ? [...columns, { label: '', frames: [], store: last.store }] : columns;

  // every id that ever appears, in order of first appearance
  const ids = [];
  columns.forEach((c) => {
    (c.store || []).forEach((row) => {
      const [id] = parseRow(row);
      if (!ids.includes(id)) ids.push(id);
    });
  });

  const current = snapshot(cells[shown]?.store);
  const previous = snapshot(cells[shown - 1]?.store);

  const centre = useCallback((index) => {
    const strip = stripRef.current;
    const track = trackRef.current;
    const cell = track?.children[index];
    if (!strip || !track || !cell) return;
    // offsetLeft is layout, so it ignores the transform already applied — but it
    // is measured against a shared offsetParent, hence the subtraction.
    const x = cell.offsetLeft - track.offsetLeft;
    track.style.transform = `translateX(${strip.clientWidth / 2 - (x + cell.offsetWidth / 2)}px)`;
  }, []);

  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    setAnimated(true);
  }, []);

  useEffect(() => {
    if (!animated) return;
    const strip = stripRef.current;
    if (!strip) return;

    let timer = null;
    let lag = null;
    let i = 0;

    const step = () => {
      i = (i + 1) % cells.length;
      setActive(i);
      centre(i);
      const at = i;
      clearTimeout(lag);
      lag = setTimeout(() => setShown(at), interval / 2);
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

    io.observe(strip);
    centre(i);

    const onResize = () => centre(i);
    window.addEventListener('resize', onResize);
    // web fonts land after first layout and change the cell metrics
    document.fonts?.ready.then(() => centre(i));

    return () => {
      io.disconnect();
      if (timer) clearInterval(timer);
      clearTimeout(lag);
      window.removeEventListener('resize', onResize);
    };
  }, [animated, cells.length, interval, centre]);

  return (
    <div className={`stack${animated ? ' is-animated' : ''}${hasStore ? ' has-store' : ''}`}>
      <div className="stack-strip" ref={stripRef}>
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
              data-store={col.store ? JSON.stringify(col.store.map(parseRow)) : undefined}
            >
              <div className="stack-num">{col.label}</div>
              <div className="stack-frames">
                {[...col.frames].reverse().map((frame, fi) => (
                  <div
                    key={`${frame}-${fi}`}
                    className={`stack-frame${accentFrames.includes(frame) ? ' is-accent' : ''}`}
                  >
                    {frame}
                  </div>
                ))}
              </div>
              {col.store && (
                <div className="stack-store">
                  <div className="stack-store-label">store</div>
                  {col.store.length === 0 ? (
                    <div className="stack-store-row is-empty">
                      <span>—</span>
                    </div>
                  ) : (
                    col.store.map(parseRow).map(([id, state]) => (
                      <div key={id} className="stack-store-row">
                        <span className="stack-store-id">{id}</span>
                        <span className="stack-store-state">{state}</span>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
      {hasStore && (
        <div className="stack-store-panel">
          <div className="stack-store-label">store</div>
          {ids.map((id) => {
            const state = current[id];
            return (
              <div
                key={id}
                data-id={id}
                className={
                  'stack-store-row' +
                  (state ? '' : ' is-absent') +
                  (state && state !== previous[id] ? ' is-changed' : '')
                }
              >
                <span className="stack-store-id">{id}</span>
                <span className="stack-store-state">{state || ''}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
