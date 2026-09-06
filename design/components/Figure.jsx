export default function Figure({ number, caption, bleed = false, children }) {
  return (
    <figure className={`figure${bleed ? ' figure-bleed' : ''}`} data-reveal>
      <div className="figure-frame">{children}</div>
      <figcaption className="figcaption">
        <span className="num">{number}</span>
        <span>{caption}</span>
      </figcaption>
    </figure>
  );
}
