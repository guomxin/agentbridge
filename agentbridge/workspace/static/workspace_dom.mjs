// Retain existing controls when a status update only changes their neighbours.
// replaceChildren removes even unchanged nodes and drops DOM focus.
export function reconcileChildren(parent, nodes) {
  const wanted = new Set(nodes);
  for (const child of [...parent.childNodes]) {
    if (!wanted.has(child)) child.remove();
  }
  nodes.forEach((node, index) => {
    const current = parent.childNodes[index];
    if (current !== node) parent.insertBefore(node, current || null);
  });
}
