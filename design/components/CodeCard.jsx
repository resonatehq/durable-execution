const LABELS = { python: 'Python', bash: 'Shell', json: 'JSON', sql: 'SQL' };

export default function CodeCard({ name, lang = 'Python', code }) {
  return (
    <div className="card" data-reveal>
      <div className="card-head">
        <span>{name}</span>
        <span className="card-lang">{lang}</span>
      </div>
      <pre>{code.replace(/\n+$/, '')}</pre>
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
