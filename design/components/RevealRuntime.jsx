'use client';

import { useEffect } from 'react';

// Elements are visible by default in CSS. This arms the hidden-then-reveal
// transition, and only once we have confirmed the animation clock is actually
// advancing. If JS never runs, or motion is reduced, everything is just visible.
export default function RevealRuntime() {
  useEffect(() => {
    const root = document.documentElement;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    let observer;
    let onScroll;

    const arm = () => {
      root.classList.add('armed');

      const reveal = (el) => {
        el.classList.add('zos-in');
        observer.unobserve(el);
      };

      observer = new IntersectionObserver(
        (entries) => entries.forEach((e) => e.isIntersecting && reveal(e.target)),
        { rootMargin: '0px 0px -12% 0px' },
      );

      const scan = () => {
        document.querySelectorAll('[data-reveal]:not(.zos-in)').forEach((el) => {
          // Anything already at or above 88% of the viewport is shown at once,
          // so nothing above the fold sits hidden waiting for a scroll.
          if (el.getBoundingClientRect().top < window.innerHeight * 0.88) {
            el.classList.add('zos-in');
          } else {
            observer.observe(el);
          }
        });
      };

      scan();
      onScroll = () => scan();
      window.addEventListener('scroll', onScroll, { passive: true });
    };

    // Two frames with a moving clock before we trust it.
    let first = null;
    const f1 = requestAnimationFrame((t) => {
      first = t;
      requestAnimationFrame((t2) => {
        if (t2 > first) arm();
      });
    });

    return () => {
      cancelAnimationFrame(f1);
      observer?.disconnect();
      if (onScroll) window.removeEventListener('scroll', onScroll);
      root.classList.remove('armed');
    };
  }, []);

  return null;
}
