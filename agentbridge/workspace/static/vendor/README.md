# Workspace Markdown dependencies

Vendored browser builds (no runtime CDN requests):

- Marked 18.0.12: https://www.npmjs.com/package/marked/v/18.0.12
  - `lib/marked.umd.js`, license in `marked.LICENSE`.
- DOMPurify 3.4.15: https://www.npmjs.com/package/dompurify/v/3.4.15
  - `dist/purify.min.js`, license in `DOMPurify.LICENSE`.

Downloaded from the matching version on cdn.jsdelivr.net/npm.
When updating, replace the build and license together and run the Workspace
Markdown checks. Both scripts participate in the Workspace asset version hash.
All parsed output must pass through the restricted DOMPurify configuration in
`renderMarkdown` before insertion. Generated links allow HTTP(S) and mailto only;
images and active HTML are excluded. User messages remain plain text.
