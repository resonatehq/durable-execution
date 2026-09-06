import fs from 'node:fs';
import path from 'node:path';
import matter from 'gray-matter';

const DIR = path.join(process.cwd(), 'content', 'writing');

export function getSlugs() {
  return fs
    .readdirSync(DIR)
    .filter((f) => f.endsWith('.mdx'))
    .map((f) => f.replace(/\.mdx$/, ''));
}

// YAML turns an unquoted 2026-09-06 into a Date; keep dates as ISO strings.
const isoDate = (d) =>
  d instanceof Date ? d.toISOString().slice(0, 10) : String(d);

export function getPost(slug) {
  const raw = fs.readFileSync(path.join(DIR, `${slug}.mdx`), 'utf8');
  const { data, content } = matter(raw);
  return { slug, frontmatter: { ...data, date: isoDate(data.date) }, source: content };
}

export function getAllPosts() {
  return getSlugs()
    .map((slug) => ({ slug, ...getPost(slug).frontmatter }))
    .sort((a, b) => (a.date < b.date ? 1 : -1));
}

// "2026-09-06" -> "2026.09"
export const stamp = (d) => d.slice(0, 7).replace('-', '.');

// "2026-09-06" -> "September 2026"
export function longDate(d) {
  const [y, m] = d.split('-');
  const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
    'August', 'September', 'October', 'November', 'December'];
  return `${months[Number(m) - 1]} ${y}`;
}
