// Fenced blocks carry their card name in the info string:
//
//     ```python name="Store"
//
// Markdown keeps that in `node.meta` and then drops it. Copy it onto the
// element so the <pre> component can render the card header.
export default function remarkCodeMeta() {
  return (tree) => {
    const walk = (node) => {
      if (node.type === 'code') {
        const name = /name="([^"]*)"/.exec(node.meta || '')?.[1] ?? '';
        node.data = node.data || {};
        node.data.hProperties = {
          ...(node.data.hProperties || {}),
          'data-name': name,
          'data-lang': node.lang || '',
        };
      }
      (node.children || []).forEach(walk);
    };
    walk(tree);
  };
}
