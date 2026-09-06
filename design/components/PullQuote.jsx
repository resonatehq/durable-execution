export default function PullQuote({ children }) {
  return (
    <blockquote className="pullquote" data-reveal>
      <p>{children}</p>
    </blockquote>
  );
}
