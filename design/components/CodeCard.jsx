const LABELS = { python: 'Python', bash: 'Shell', json: 'JSON', sql: 'SQL' };

// Deliberately not a highlighter. Keywords go one step towards the muted grey so
// the control flow recedes and the domain terms — store, settle, create — are
// what the eye lands on. Everything else keeps the ink colour.
const KEYWORDS = new Set([
  'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def',
  'del', 'elif', 'else', 'except', 'False', 'finally', 'for', 'from', 'global',
  'if', 'import', 'in', 'is', 'lambda', 'None', 'nonlocal', 'not', 'or', 'pass',
  'raise', 'return', 'True', 'try', 'while', 'with', 'yield',
]);

// Comments and strings are matched first so a keyword inside either is left alone.
const TOKEN =
  /(#[^\n]*)|([A-Za-z]*(?:"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'))|([A-Za-z_]\w*)/g;

function highlight(code) {
  const out = [];
  let last = 0;
  let key = 0;
  let m;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(code)) !== null) {
    if (m.index > last) out.push(code.slice(last, m.index));
    const [text, comment, string, word] = m;
    if (!comment && !string && KEYWORDS.has(word)) {
      out.push(
        <span className="kw" key={key++}>
          {text}
        </span>,
      );
    } else {
      out.push(text);
    }
    last = m.index + text.length;
  }
  out.push(code.slice(last));
  return out;
}

export default function CodeCard({ name, lang = 'Python', code }) {
  const body = code.replace(/\n+$/, '');
  return (
    <div className="card" data-reveal>
      <div className="card-head">
        <span>{name}</span>
        <span className="card-lang">{lang}</span>
      </div>
      <pre>{lang === 'Python' ? highlight(body) : body}</pre>
    </div>
  );
}

// Every fenced block in an MDX post arrives here as <pre><code>.
export function Pre({ children }) {
  const props = children?.props ?? {};
  const lang = props['data-lang'] || '';
  const code = typeof props.children === 'string' ? props.children : '';
  return (
    <CodeCard
      name={props['data-name'] || ''}
      lang={LABELS[lang] || lang || 'Text'}
      code={code}
    />
  );
}
