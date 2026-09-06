'use client';

import { useEffect, useState } from 'react';

export default function ThemeToggle() {
  const [dark, setDark] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setDark(document.documentElement.dataset.theme === 'dark');
    setReady(true);
  }, []);

  const flip = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.dataset.theme = next ? 'dark' : 'light';
    try {
      localStorage.setItem('theme', next ? 'dark' : 'light');
    } catch {}
  };

  return (
    <button className="toggle" onClick={flip} aria-label="Toggle colour theme">
      {ready ? (dark ? 'Light' : 'Dark') : 'Theme'}
    </button>
  );
}
