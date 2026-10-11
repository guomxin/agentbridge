// Candidate ranking only; the caller still checks applicability and access.
// Keep Python skill_ranking.py in sync through tests/fixtures/skill_ranking.json.
const text = value => typeof value === "string" ? value.toLowerCase().trim().replace(/\s+/gu, " ") : "";
function grams(value) {
  const chars = Array.from(text(value).replace(/\s+/gu, ""));
  return new Set(chars.slice(0, -1).map((char, index) => char + chars[index + 1]));
}
function mentions(query, value) {
  const label = text(value);
  if (!label) return false;
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const left = /^[a-z0-9_-]/.test(label) ? "(?<![a-z0-9_-])" : "";
  const right = /[a-z0-9_-]$/.test(label) ? "(?![a-z0-9_-])" : "";
  return new RegExp(left + escaped + right, "u").test(query);
}

export function rankSkillCandidates(items, prompt = "") {
  const query = text(prompt), queryGrams = grams(query);
  return items.map((item, index) => {
    const selection = item.selection || {};
    const explicit = mentions(query, item.id) || mentions(query, item.name);
    const positive = new Set([item.name, item.description, selection.use_when, selection.output]
      .flatMap(value => [...grams(value)]));
    const overlap = [...queryGrams].filter(gram => positive.has(gram)).length;
    return { item, index, explicit: Number(explicit), overlap };
  }).sort((a, b) => b.explicit - a.explicit || b.overlap - a.overlap || a.index - b.index)
    .map(row => row.item);
}
