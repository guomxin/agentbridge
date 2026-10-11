import { isCancellation, cancellationError } from "./workspace_lifecycle.mjs";
import { skillTrialTitle, skillTrialBinding } from "./workspace_skill_trial.mjs";

const MAX_COMPOSER_IMAGES = 4;
const MAX_COMPOSER_IMAGE_BYTES = 6 * 1024 * 1024;
const MAX_COMPOSER_IMAGES_TOTAL_BYTES = 12 * 1024 * 1024;
const SUPPORTED_COMPOSER_IMAGE_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
]);


export function createComposer({ document, state, $, toast, getScope, executeChatMessage, FileReader = globalThis.FileReader, crypto = globalThis.crypto }) {
  let attachmentGeneration = 0;
  let trialScope = null;
  function renderComposerTrial() {
    const trial = state.composerSkillTrial;
    const card = $("#composer-skill-trial");
    card.replaceChildren();
    card.hidden = !trial;
    const input = $("#chat-form textarea[name='message']");
    input.maxLength = trial ? 3000 : 20000;
    input.placeholder = trial ? "输入简短的合成材料，例如：计划修复登录问题。" : "输入你的任务或问题";
    $("#attach-image").disabled = Boolean(trial || input.disabled);
    $("#image-input").disabled = Boolean(trial || input.disabled);
    if (!trial) return;
    const title = document.createElement("strong");
    title.textContent = `正在试用：${skillTrialTitle(trial)}`;
    const note = document.createElement("p");
    note.textContent = "仅输入合成文字，最多 3000 字；不支持图片。样例保存后仍需人工复核。";
    const cancel = document.createElement("button");
    cancel.type = "button"; cancel.className = "secondary"; cancel.textContent = "取消试用";
    cancel.disabled = Boolean(input.disabled);
    cancel.addEventListener("click", () => clearComposerTrial(trial));
    card.append(title, note, cancel);
  }
  function clearComposerTrial(expected) {
    if (expected && state.composerSkillTrial !== expected) return;
    state.composerSkillTrial = null;
    renderComposerTrial();
  }
  function beginComposerTrial(trial) {
    const scope = getScope();
    scope.assertCurrent();
    if (trialScope !== scope) {
      trialScope = scope;
      scope.defer(() => { if (trialScope === scope) { trialScope = null; clearComposerTrial(); } });
    }
    state.composerSkillTrial = {...skillTrialBinding(trial), name: trial.name};
    clearComposerAttachments();
    renderComposerTrial();
    const input = $("#chat-form textarea[name='message']");
    input.value = "";
    input.focus();
  }
  async function handleComposerPaste(event) {
    const files = [...(event.clipboardData?.items || [])]
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (files.length === 0) return;
    event.preventDefault();
    await addComposerFiles(files);
  }

  function handleComposerDragOver(event) {
    if (![...(event.dataTransfer?.items || [])].some(
      (item) => item.kind === "file" && item.type.startsWith("image/"),
    )) {
      return;
    }
    event.preventDefault();
    event.currentTarget.classList.add("drag-active");
  }

  function handleComposerDragLeave(event) {
    if (!event.currentTarget.contains(event.relatedTarget)) {
      event.currentTarget.classList.remove("drag-active");
    }
  }

  async function handleComposerDrop(event) {
    event.currentTarget.classList.remove("drag-active");
    const files = [...(event.dataTransfer?.files || [])].filter((file) =>
      file.type.startsWith("image/"),
    );
    if (files.length === 0) return;
    event.preventDefault();
    await addComposerFiles(files);
  }

  async function addComposerFiles(fileList) {
    if (state.composerSkillTrial) {
      toast("短试用仅支持合成文字，请取消试用后再添加图片。", true, "trial-text-only");
      return;
    }
    const scope = getScope();
    const generation = attachmentGeneration;
    const available = MAX_COMPOSER_IMAGES - state.composerAttachments.length;
    if (available <= 0) {
      toast("一次最多添加 4 张图片。", true, "image-limit");
      return;
    }
    const files = [...(fileList || [])];
    if (files.length > available) {
      toast("一次最多添加 4 张图片。", true, "image-limit");
    }
    let totalBytes = state.composerAttachments.reduce(
      (total, attachment) => total + Number(attachment.size || 0),
      0,
    );
    for (const file of files.slice(0, available)) {
      const mimeType = normalizedImageType(file);
      if (!SUPPORTED_COMPOSER_IMAGE_TYPES.has(mimeType)) {
        toast("仅支持 JPEG、PNG 和 WebP 图片。", true, "image-type");
        continue;
      }
      if (!file.size || file.size > MAX_COMPOSER_IMAGE_BYTES) {
        toast("单张图片不能超过 6 MB。", true, "image-size");
        continue;
      }
      if (totalBytes + file.size > MAX_COMPOSER_IMAGES_TOTAL_BYTES) {
        toast("图片总大小不能超过 12 MB。", true, "image-total-size");
        continue;
      }
      try {
        const attachment = await readComposerImage(file, mimeType, scope);
        scope.assertCurrent();
        if (generation !== attachmentGeneration || state.composerSkillTrial) return;
        // Parallel paste/drop events share the same attachment budget.
        const bytes = state.composerAttachments.reduce((total, item) => total + item.size, 0);
        if (state.composerAttachments.length >= MAX_COMPOSER_IMAGES ||
            bytes + file.size > MAX_COMPOSER_IMAGES_TOTAL_BYTES) continue;
        state.composerAttachments.push(attachment);
        totalBytes += file.size;
      } catch (error) {
        if (isCancellation(error)) return;
        toast(error.code === "IMAGE_CONTENT_UNSUPPORTED"
          ? "图片内容不是 JPEG、PNG 或 WebP，请重新导出后添加。"
          : "图片读取失败，请重新选择。", true, "image-read");
      }
    }
    renderComposerAttachments();
  }

  function normalizedImageType(file) {
    const supplied = String(file?.type || "").toLowerCase();
    if (supplied === "image/jpg") return "image/jpeg";
    if (supplied) return supplied;
    const name = String(file?.name || "").toLowerCase();
    if (/\.jpe?g$/.test(name)) return "image/jpeg";
    if (/\.png$/.test(name)) return "image/png";
    if (/\.webp$/.test(name)) return "image/webp";
    return "";
  }

  function readComposerImage(file, mimeType, scope = getScope()) {
    scope.assertCurrent();
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      const release = scope.defer(() => { reader.abort(); reject(cancellationError()); });
      reader.addEventListener("error", () => { release(); reject(reader.error); });
      reader.addEventListener("abort", () => { release(); reject(cancellationError()); });
      reader.addEventListener("load", () => {
        release();
        if (!scope.current()) { reject(cancellationError()); return; }
        const dataUrl = String(reader.result || "");
        const separator = dataUrl.indexOf(",");
        const content = separator >= 0 ? dataUrl.slice(separator + 1) : "";
        if (!content) {
          reject(new Error("image content is empty"));
          return;
        }
        // A file picker's MIME type commonly comes from its suffix. Use the
        // actual signature so a renamed PNG/JPEG remains a valid upload.
        let actualType;
        try {
          const header = atob(content.slice(0, 16));
          if (header.startsWith("\x89PNG\r\n\x1a\n")) actualType = "image/png";
          else if (header.startsWith("\xff\xd8\xff")) actualType = "image/jpeg";
          else if (header.startsWith("RIFF") && header.slice(8, 12) === "WEBP") actualType = "image/webp";
        } catch { /* Invalid base64 is rejected like an unsupported signature. */ }
        if (!actualType) {
          const error = new Error("unsupported image content");
          error.code = "IMAGE_CONTENT_UNSUPPORTED";
          reject(error);
          return;
        }
        resolve({
          id: crypto.randomUUID(),
          fileName: String(file.name || "pasted-image").slice(0, 120),
          mimeType: actualType,
          content,
          dataUrl: `data:${actualType};base64,${content}`,
          size: file.size,
        });
      });
      reader.readAsDataURL(file);
    });
  }

  function renderComposerAttachments() {
    const container = $("#composer-attachments");
    container.replaceChildren();
    state.composerAttachments.forEach((attachment) => {
      const preview = document.createElement("div");
      preview.className = "attachment-preview";
      const image = document.createElement("img");
      image.src = attachment.dataUrl;
      image.alt = attachment.fileName;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "attachment-remove";
      remove.title = `移除 ${attachment.fileName}`;
      remove.setAttribute("aria-label", remove.title);
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        state.composerAttachments = state.composerAttachments.filter(
          (item) => item.id !== attachment.id,
        );
        renderComposerAttachments();
      });
      preview.append(image, remove);
      container.append(preview);
    });
    container.hidden = state.composerAttachments.length === 0;
  }

  function clearComposerAttachments() {
    attachmentGeneration++;
    state.composerAttachments = [];
    renderComposerAttachments();
  }

  async function sendChat(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const textarea = form.elements.message;
    if (textarea.disabled) return;
    const trial = state.composerSkillTrial;
    const attachments = state.composerAttachments.map((item) => ({ ...item }));
    const message =
      textarea.value.trim() ||
      (attachments.length > 0 ? "请处理附加图片中的内容。" : "");
    if (!message) return;
    if (trial && (attachments.length || message.length > 3000)) {
      toast(attachments.length ? "短试用不支持图片，请移除图片。" : "短试用材料不能超过 3000 字。", true, "trial-material");
      return;
    }
    if (trial) {
      await executeChatMessage(message, form, [], {...trial}, () => {
        if (state.composerSkillTrial !== trial) return;
        clearComposerTrial(trial);
        if (textarea.value.trim() === message) textarea.value = "";
      }, () => clearComposerTrial(trial));
      return;
    }
    textarea.value = "";
    clearComposerAttachments();
    await executeChatMessage(message, form, attachments);
  }

  return { handleComposerPaste, handleComposerDragOver, handleComposerDragLeave, handleComposerDrop, addComposerFiles, normalizedImageType, readComposerImage, renderComposerAttachments, clearComposerAttachments, beginComposerTrial, clearComposerTrial, renderComposerTrial, sendChat };
}
