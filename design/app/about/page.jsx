const REPO = 'https://github.com/resonatehq/durable-execution';

export default function About() {
  return (
    <article className="article">
      <h1 className="h1-page rise">About</h1>
      <p className="standfirst-page rise rise-1">
        A durable execution engine, built in the open, in about five thousand lines.
      </p>

      <div className="body">
        <p>
          This is the written companion to the build. Each post takes one part of the
          engine — the promise store, the scheduler, leases and failure detection —
          and works through the reasoning before the code lands.
        </p>
        <p>
          The claim the whole project rests on is that durable execution needs one
          primitive: a promise that outlives the process holding it. Everything else —
          recovery, fan-out, timeouts, retries, remote calls — is a policy on top of
          creating and settling those promises. The posts are where that claim gets
          tested; the repository is where it either holds or does not.
        </p>
        <p>
          The code is written to be read rather than vendored. Clarity beats
          cleverness, and every abstraction has to earn its place against the line
          budget.
        </p>

        <div className="contact">
          <div className="contact-row">
            <span className="label">Code</span>
            <a href={REPO}>github.com/resonatehq/durable-execution</a>
          </div>
          <div className="contact-row">
            <span className="label">Posts</span>
            <a href="/">Writing index</a>
          </div>
        </div>
      </div>
    </article>
  );
}
