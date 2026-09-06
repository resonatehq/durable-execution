import Link from 'next/link';
import { getAllPosts, stamp } from '@/lib/posts';

export default function Index() {
  const posts = getAllPosts();

  return (
    <article className="article">
      <h1 className="h1-page rise">Writing</h1>
      <p className="standfirst-page rise rise-1">
        Notes taken while building a durable execution engine, in order.
      </p>

      <div className="index-list">
        {posts.map((p) => (
          <Link key={p.slug} href={`/writing/${p.slug}/`} className="index-entry" data-reveal>
            <span className="index-date">{stamp(p.date)}</span>
            <span>
              <span className="index-title">{p.title}</span>
              <span className="index-dek">{p.dek}</span>
            </span>
          </Link>
        ))}
      </div>
    </article>
  );
}
