// A code card whose body is folded away. Native <details>, so it works with no
// JS, and print forces it open.
export default function Details({ summary, children }) {
  return (
    <details className="details">
      <summary>
        <span>{summary}</span>
        <span className="details-mark" aria-hidden="true" />
      </summary>
      <div className="details-body">{children}</div>
    </details>
  );
}
