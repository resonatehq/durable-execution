const repo = 'durable-execution';
const onPages = process.env.GITHUB_PAGES === 'true';

/** @type {import('next').NextConfig} */
export default {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  basePath: onPages ? `/${repo}` : '',
  assetPrefix: onPages ? `/${repo}/` : '',
};
