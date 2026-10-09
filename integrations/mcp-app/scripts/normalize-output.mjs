import { readFile, writeFile } from "node:fs/promises";

const outputUrl = new URL(
  "../../../agentbridge/mcp/static/trusted-interaction.html",
  import.meta.url,
);
const html = await readFile(outputUrl, "utf8");
const normalized = `${html.replace(/[ \t]+$/gm, "").trimEnd()}\n`;

await writeFile(outputUrl, normalized, "utf8");
