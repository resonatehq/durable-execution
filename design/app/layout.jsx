import { Source_Serif_4, Instrument_Sans, JetBrains_Mono } from 'next/font/google';
import Link from 'next/link';
import RevealRuntime from '@/components/RevealRuntime';
import ThemeToggle from '@/components/ThemeToggle';
import '@/styles/tokens.css';
import '@/styles/prose.css';

const serif = Source_Serif_4({
  subsets: ['latin'],
  weight: ['300', '400', '500'],
  style: ['normal', 'italic'],
  variable: '--font-serif',
  display: 'swap',
});

const sans = Instrument_Sans({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-sans',
  display: 'swap',
});

const mono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata = {
  title: 'Durable Execution in 5,000 Lines',
  description:
    'Building a durable execution engine from first principles. Production-grade. Planet-scale.',
};

const REPO = 'https://github.com/resonatehq/durable-execution';

// Applied before first paint so the theme does not flash.
const themeInit = `(function(){try{var t=localStorage.getItem('theme');
if(!t){t=window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}
document.documentElement.dataset.theme=t;}catch(e){}})();`;

export default function RootLayout({ children }) {
  return (
    <html lang="en" className={`${serif.variable} ${sans.variable} ${mono.variable}`}>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body>
        <header className="header">
          <Link href="/" className="wordmark">
            <span className="diamond" aria-hidden="true" />
            <span>Durable Execution</span>
          </Link>
          <nav className="nav">
            <Link href="/">Writing</Link>
            <a href={REPO}>Code</a>
            <Link href="/about/">About</Link>
            <ThemeToggle />
          </nav>
        </header>

        <div className="hairline-wrap">
          <div className="hairline" />
        </div>

        <main>{children}</main>

        <footer className="footer">
          <div className="footer-inner">
            <span>Durable Execution in 5,000 Lines</span>
            <a href={REPO}>Source</a>
          </div>
        </footer>

        <RevealRuntime />
      </body>
    </html>
  );
}
