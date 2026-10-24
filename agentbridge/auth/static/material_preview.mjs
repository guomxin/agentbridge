// Read-only, demand-loaded material views. Source system URLs never enter DOM.
import {renderPdfMaterial} from "./pdf_preview.mjs";

const FILE_LIMIT = 8 * 1024 * 1024;
const ARCHIVE_LIMIT = 24 * 1024 * 1024;
const RESPONSE_LIMIT = 34 * 1024 * 1024;
const ID = /^[a-f0-9]{64}$/;
const KINDS = new Set(["image", "pdf", "archive", "file"]);

function manifest(review) {
  if (!Array.isArray(review?.materialPreviews)) return [];
  const seen = new Set();
  return review.materialPreviews.slice(0, 128).filter(item => {
    if (!item || !ID.test(item.id) || seen.has(item.id) || !KINDS.has(item.kind) ||
        !["available", "unavailable"].includes(item.state)) return false;
    seen.add(item.id); return true;
  }).map(item => ({id: item.id, name: String(item.name || "未命名材料").slice(0, 500), kind: item.kind,
    state: item.state, reason: String(item.reason || "此材料暂不支持预览，请在 OA 中查看。").slice(0, 500)}));
}

function verifyFile(bytes, contentType, expectedKind) {
  if (!(bytes instanceof Uint8Array) || !bytes.length || bytes.length > FILE_LIMIT) throw new Error("材料为空或超过预览大小限制，请在 OA 中查看。");
  const isPng = [137, 80, 78, 71, 13, 10, 26, 10].every((value, index) => bytes[index] === value);
  const isJpeg = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  const isPdf = [37, 80, 68, 70, 45].every((value, index) => bytes[index] === value);
  if (!((expectedKind === "image" && ((contentType === "image/png" && isPng) || (contentType === "image/jpeg" && isJpeg))) ||
        (expectedKind === "pdf" && contentType === "application/pdf" && isPdf))) throw new Error("材料实际格式不支持安全预览，请在 OA 中查看。");
}

export function decodeMaterialArchive(value, atobFunction = globalThis.atob) {
  if (value?.kind !== "archive" || !Array.isArray(value.items) || value.items.length > 40 || value.totalEntries !== value.items.length) {
    throw new Error("附件包目录不完整，请重新获取。");
  }
  const seen = new Set(); let total = 0;
  return value.items.map(item => {
    if (!item || !ID.test(item.id) || seen.has(item.id) || !["available", "unavailable"].includes(item.state)) throw new Error("附件包目录无效，请重新获取。");
    seen.add(item.id);
    const result = {id: item.id, name: String(item.name || "未命名附件").slice(0, 500), kind: item.kind, state: item.state};
    if (item.state === "unavailable") return {...result, reason: String(item.reason || "此文件类型暂不支持预览，请在 OA 中查看。").slice(0, 500)};
    if (typeof item.bodyBase64 !== "string" || item.bodyBase64.length > Math.ceil(FILE_LIMIT / 3) * 4 ||
        item.bodyBase64.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(item.bodyBase64)) throw new Error("附件内容无效或超过预览大小限制。");
    const decoded = atobFunction(item.bodyBase64);
    const bytes = Uint8Array.from(decoded, char => char.charCodeAt(0));
    total += bytes.length;
    if (total > ARCHIVE_LIMIT || (item.byteLength !== undefined && item.byteLength !== bytes.length)) throw new Error("附件包内容大小不一致或超过预览限制。");
    verifyFile(bytes, item.contentType, item.kind);
    return {...result, bytes, contentType: item.contentType};
  });
}

async function responseBytes(response) {
  const declared = Number(response.headers?.get("content-length") || 0);
  if (declared > RESPONSE_LIMIT) throw new Error("材料超过预览大小限制，请在 OA 中查看。");
  if (!response.body?.getReader) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.length > RESPONSE_LIMIT) throw new Error("材料超过预览大小限制，请在 OA 中查看。");
    return bytes;
  }
  const reader = response.body.getReader(), chunks = []; let size = 0;
  try {
    while (true) {
      const value = await reader.read();
      if (value.done) break;
      size += value.value.length;
      if (size > RESPONSE_LIMIT) throw new Error("材料超过预览大小限制，请在 OA 中查看。");
      chunks.push(value.value);
    }
  } catch (error) { try { await reader.cancel(); } catch {} throw error; }
  const result = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}

export function createMaterialPreviews({document, window, target, apiUrl, csrfToken, timeoutMs = 30000, renderPdf = renderPdfMaterial}) {
  const records = new Map(); let signature = "", epoch = 0;
  const readingPositions = new Map();
  const base = new URL(apiUrl, window.location.origin);
  const allowedBase = base.origin === window.location.origin && !base.search && !base.hash && base.pathname.endsWith("/draft");
  const make = (tag, text, className) => {
    const node = document.createElement(tag); if (text !== undefined) node.textContent = text;
    if (className) node.className = className; return node;
  };
  const button = text => { const node = make("button", text); node.type = "button"; node.className = "secondary"; return node; };
  const createUrl = (record, data, type) => {
    const url = window.URL.createObjectURL(new window.Blob([data], {type})); record.urls.add(url); return url;
  };
  function revoke(record) {
    for (const dispose of record.disposers) { try { Promise.resolve(dispose()).catch(() => {}); } catch {} }
    record.disposers.clear();
    for (const url of record.urls) window.URL.revokeObjectURL(url); record.urls.clear();
  }
  function close(record) {
    record.generation++; record.abort?.abort(); record.abort = null; record.pending = false;
    revoke(record); record.content.replaceChildren(); record.close.hidden = true;
    record.open.disabled = record.item.state !== "available"; record.open.textContent = "重新获取预览";
    record.status.textContent = "预览已关闭；重新打开会再次核验并获取材料。";
  }
  function suspend() {
    epoch++; for (const record of records.values()) close(record);
    records.clear(); readingPositions.clear(); signature = ""; target.replaceChildren(); target.hidden = true;
  }
  function current(record, pageEpoch, generation) { return epoch === pageEpoch && record.generation === generation && records.get(record.item.id) === record; }

  async function displayFile(record, item, destination, status, check) {
    verifyFile(item.bytes, item.contentType, item.kind);
    if (item.kind === "image") {
      const url = createUrl(record, item.bytes, item.contentType);
      const image = make("img"); image.alt = item.name; image.className = "material-preview-image";
      image.style.width = "100%"; image.style.maxWidth = "none";
      image.src = url;
      const viewport = make("div", undefined, "material-preview-image-frame");
      viewport.style.overflow = "auto"; viewport.style.maxWidth = "100%"; viewport.append(image);
      const zoomLabel = make("label", "缩放图片 ", "material-preview-zoom"), zoom = make("input");
      zoom.type = "range"; zoom.min = "50"; zoom.max = "300"; zoom.step = "25"; zoom.value = "100";
      zoom.setAttribute("aria-label", "缩放图片");
      zoom.addEventListener("input", () => { image.style.width = `${Math.max(50, Math.min(300, Number(zoom.value)))}%`; });
      zoomLabel.append(zoom); destination.append(zoomLabel, viewport);
      if (typeof image.decode === "function") await image.decode();
      else await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = () => reject(new Error("图片无法显示，请在 OA 中查看。")); });
      if (check()) status.textContent = "图片已显示，可缩放核对。";
    } else {
      const abort = new window.AbortController();
      record.disposers.add(() => abort.abort());
      const positionKey = `${record.item.id}:${item.id}`;
      const renderer = await renderPdf({document, window, target: destination, bytes: item.bytes, isCurrent: check,
        initialView:readingPositions.get(positionKey),onViewState:value => {
          if (!check()) return;
          readingPositions.set(positionKey,{page:value.page,fitWidth:value.fitWidth,zoom:value.zoom,textOpen:value.textOpen,
            scrollTop:value.scrollTop,scrollLeft:value.scrollLeft});
          while(readingPositions.size>128) readingPositions.delete(readingPositions.keys().next().value);
        },
        signal: abort.signal, onStatus: message => { if (check()) status.textContent = String(message); }});
      if (check()) record.disposers.add(() => renderer.destroy());
      else await renderer.destroy();
    }
  }

  async function load(record) {
    if (record.pending || record.item.state !== "available" || !allowedBase) return;
    close(record);
    const pageEpoch = epoch, generation = record.generation, check = () => current(record, pageEpoch, generation);
    record.pending = true; record.open.disabled = true; record.close.hidden = false;
    record.status.textContent = "正在读取材料…尚未读取完成。";
    const controller = new window.AbortController(); record.abort = controller;
    let timer;
    const timeout = new Promise((_, reject) => { timer = window.setTimeout(() => {
      controller.abort(); reject(new Error("读取材料超时，尚未完成。可以重新获取或在 OA 中查看。"));
    }, timeoutMs); });
    try {
      await Promise.race([timeout, (async () => {
        const response = await window.fetch(`${base.href}/materials/${record.item.id}`, {
          method: "GET", credentials: "same-origin", cache: "no-store", redirect: "error",
          headers: {"X-AgentBridge-Draft-Token": csrfToken}, signal: controller.signal,
        });
        const bytes = await responseBytes(response);
        if (!check()) return;
        const contentType = (response.headers?.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
        if (!response.ok) {
          let message = "材料读取失败，请重新获取或在 OA 中查看。";
          if (contentType === "application/json") {
            try { message = String(JSON.parse(new TextDecoder().decode(bytes))?.error?.message || message).slice(0, 500); } catch {}
          }
          throw new Error(message);
        }
        if (record.item.kind === "archive") {
          if (contentType !== "application/json") throw new Error("附件包返回格式不正确，请重新获取。");
          let archive;
          try { archive = JSON.parse(new TextDecoder().decode(bytes)); }
          catch { throw new Error("附件包返回格式不正确，请重新获取。"); }
          const items = decodeMaterialArchive(archive, value => window.atob(value));
          if (!check()) return;
          const available = items.filter(item => item.state === "available").length;
          record.status.textContent = `附件包已获取，${available} 份可预览，${items.length - available} 份格式暂不支持。请逐项打开核对。`;
          for (const item of items) {
            const section = make("section", undefined, "material-preview-archive-item");
            const label = make("h4", item.name), state = make("p", item.state === "available" ? "尚未打开" : item.reason, "material-preview-status");
            section.append(label, state);
            if (item.state === "available") {
              const open = button("打开此文件"), content = make("div", undefined, "material-preview-content");
              open.setAttribute("aria-label", `预览：${item.name}`);
              open.addEventListener("click", async () => {
                if (open.disabled || !check()) return;
                open.disabled = true; state.textContent = "正在显示…";
                let decodeTimer, itemActive = true;
                const oldUrls = new Set(record.urls), oldDisposers = new Set(record.disposers);
                try {
                  await Promise.race([displayFile(record, item, content, state, () => check() && itemActive), new Promise((_, reject) => {
                    decodeTimer = window.setTimeout(() => reject(new Error("文件显示超时，请关闭后重新获取。")), timeoutMs);
                  })]);
                } catch (error) {
                  itemActive = false;
                  for (const url of record.urls) if (!oldUrls.has(url)) { window.URL.revokeObjectURL(url); record.urls.delete(url); }
                  for (const dispose of record.disposers) if (!oldDisposers.has(dispose)) {
                    try { Promise.resolve(dispose()).catch(() => {}); } catch {} record.disposers.delete(dispose);
                  }
                  if (check()) { content.replaceChildren(); state.textContent = error?.message || "文件无法显示。"; }
                }
                finally { window.clearTimeout(decodeTimer); }
              });
              section.append(open, content);
            }
            record.content.append(section);
          }
        } else {
          await displayFile(record, {...record.item, bytes, contentType}, record.content, record.status, check);
        }
      })()]);
    } catch (error) {
      if (check()) {
        record.generation++;
        revoke(record); record.content.replaceChildren();
        record.status.textContent = error?.message || "材料尚未读取完成，请重新获取。";
        record.pending = false; record.abort = null; record.open.disabled = false; record.open.textContent = "重新获取预览";
      }
    } finally {
      window.clearTimeout(timer);
      if (check()) { record.pending = false; record.abort = null; record.open.disabled = false; record.open.textContent = "重新获取预览"; }
    }
  }

  function render(review) {
    const items = manifest(review);
    const next = JSON.stringify([items, Boolean(review?.reviewChanged)]);
    if (signature === next) return;
    suspend(); signature = next;
    target.hidden = items.length === 0;
    if (!items.length) return;
    target.append(make("h3", "图片与附件预览"), make("p", "按需获取材料。预览不会提交业务，也不会自动将未打开的材料标为已核对。", "material-preview-note"));
    for (const item of items) {
      const section = make("section", undefined, "material-preview-item"), name = make("h4", item.name);
      section.id = `material-preview-${item.id}`; section.tabIndex = -1;
      const status = make("p", item.state === "available" ? "尚未读取" : item.reason, "material-preview-status"); status.setAttribute("role", "status");
      const open = button(item.kind === "archive" ? "获取附件包" : "打开预览"), closeButton = button("关闭预览");
      open.setAttribute("aria-label", `预览：${item.name}`);
      closeButton.setAttribute("aria-label", `关闭预览：${item.name}`);
      open.disabled = item.state !== "available" || !allowedBase; closeButton.hidden = true;
      const actions = make("div", undefined, "material-preview-actions"), content = make("div", undefined, "material-preview-content");
      actions.append(open, closeButton); section.append(name, status, actions, content); target.append(section);
      const record = {item, open, close: closeButton, status, content, urls: new Set(), disposers: new Set(), generation: 0, pending: false, abort: null};
      records.set(item.id, record);
      open.addEventListener("click", () => load(record)); closeButton.addEventListener("click", () => close(record));
    }
  }
  return {render, suspend};
}
