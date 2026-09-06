import { compileMDX } from 'next-mdx-remote/rsc';
import remarkCodeMeta from '@/lib/remark-code-meta';
import { getPost, getSlugs, longDate } from '@/lib/posts';
import { mdxComponents } from '@/components/mdx-components';

export function generateStaticParams() {
  return getSlugs().map((slug) => ({ slug }));
}

export async function generateMetadata({ params }) {
  const { slug } = await params;
  const { frontmatter } = getPost(slug);
  return { title: frontmatter.title, description: frontmatter.dek };
}

export default async function Post({ params }) {
  const { slug } = await params;
  const { frontmatter, source } = getPost(slug);
  const { content } = await compileMDX({
    source,
    components: mdxComponents,
    options: { mdxOptions: { remarkPlugins: [remarkCodeMeta] } },
  });

  return (
    <article className="article">
      <div className="eyebrow rise">
        <span>{frontmatter.kind}</span>
        <span className="tick" aria-hidden="true" />
        <span>{longDate(frontmatter.date)}</span>
      </div>

      <h1 className="h1 rise rise-1">{frontmatter.title}</h1>
      <p className="standfirst rise rise-2">{frontmatter.standfirst}</p>

      <div className="meta rise rise-3">
        <span>{frontmatter.readTime}</span>
        <span className="dot">·</span>
        <span>{longDate(frontmatter.date)}</span>
      </div>

      <div className="body">{content}</div>
    </article>
  );
}
