// Drafts, submission receipts, and a bounded cache of originals Hermes may omit.
const MAX_IMAGE = 2 * 1024 * 1024;
export const MAX_IMAGES_TOTAL = 6 * 1024 * 1024;
const TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
let database;
function openDatabase() {
  if (!database)
    database = new Promise((resolve, reject) => {
      const request = indexedDB.open("talaria-drafts", 1);
      request.onupgradeneeded = () =>
        request.result.createObjectStore("pending");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () =>
        reject(
          new Error(
            "Browser storage is unavailable. Try again or enable storage for Talaria.",
          ),
        );
      request.onblocked = () =>
        reject(
          new Error(
            "Close other Talaria tabs and try saving this attachment again.",
          ),
        );
    }).catch((error) => {
      database = null;
      throw error;
    });
  return database;
}
export async function pendingStorage(key, value) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const write = arguments.length > 1;
    const transaction = db.transaction(
      "pending",
      write ? "readwrite" : "readonly",
    );
    const store = transaction.objectStore("pending");
    const request = write
      ? value === null
        ? store.delete(key)
        : store.put(value, key)
      : store.get(key);
    transaction.oncomplete = () => resolve(request.result);
    transaction.onabort = transaction.onerror = () =>
      reject(
        new Error(
          "The images could not be saved in this browser. Free some browser storage and try again.",
        ),
      );
  });
}

const CACHE_LIMIT = 32 * 1024 * 1024;
export async function cacheMessageImages(
  session,
  messageId,
  images,
  receipt = null,
) {
  const db = await openDatabase();
  const key = `image.${session}.${messageId}`;
  return new Promise((resolve, reject) => {
    const transaction = db.transaction("pending", "readwrite");
    const records = transaction.objectStore("pending");
    const request = records.get("image-index");
    request.onsuccess = () => {
      try {
        const previous = Array.isArray(request.result) ? request.result : [];
        const index = previous.filter((item) => item.key !== key);
        index.push({
          key,
          session,
          messageId,
          bytes: images.reduce((n, image) => n + image.url.length, 0),
        });
        let size = index.reduce((n, item) => n + item.bytes, 0);
        while (index.length > 100 || size > CACHE_LIMIT) {
          const oldest = index.shift();
          size -= oldest.bytes;
          records.delete(oldest.key);
        }
        if (receipt) records.delete(receipt);
        records.put(images, key);
        records.put(index, "image-index");
      } catch {
        transaction.abort();
      }
    };
    transaction.oncomplete = resolve;
    transaction.onabort = transaction.onerror = () =>
      reject(
        new Error(
          "This browser could not retain the image. Download a copy to keep it.",
        ),
      );
  });
}
export async function browserAttachments(session, ids = null) {
  const index = await pendingStorage("image-index");
  if (!Array.isArray(index)) return [];
  const rows = index
    .filter(
      (item) => item.session === session && (!ids || ids.has(item.messageId)),
    )
    .slice(-100);
  return (
    await Promise.all(
      rows.map(async (item) => ({
        message_id: item.messageId,
        images: await pendingStorage(item.key),
      })),
    )
  ).filter((item) => Array.isArray(item.images) && item.images.length);
}
export async function forgetImages(session) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction("pending", "readwrite");
    const records = transaction.objectStore("pending");
    const request = records.get("image-index");
    const receipts = records.getAllKeys(
      IDBKeyRange.bound(`run.${session}.`, `run.${session}.\uffff`),
    );
    receipts.onsuccess = () =>
      receipts.result.forEach((key) => records.delete(key));
    request.onsuccess = () => {
      const index = Array.isArray(request.result) ? request.result : [];
      for (const item of index)
        if (item.session === session) records.delete(item.key);
      records.put(
        index.filter((item) => item.session !== session),
        "image-index",
      );
    };
    transaction.oncomplete = resolve;
    transaction.onabort = transaction.onerror = reject;
  });
}
export async function withCachedImages(session, messages) {
  if (!messages.some((message) => placeholderCount(message?.content)))
    return messages;
  const records = await browserAttachments(
    session,
    new Set(messages.map((message) => message.id)),
  ).catch(() => []);
  const images = new Map(records.map((item) => [item.message_id, item.images]));
  return messages.map((message) =>
    images.has(message.id)
      ? { ...message, browserImages: images.get(message.id) }
      : message,
  );
}
export function placeholderCount(content) {
  if (typeof content !== "string") return 0;
  return (
    content
      .trimEnd()
      .match(/(?:^|\n)(?:\[screenshot\]\n?)+$/)?.[0]
      .match(/\[screenshot\]/g)?.length || 0
  );
}
export function withoutImagePlaceholders(content) {
  return typeof content === "string"
    ? content.trimEnd().replace(/(?:^|\n)(?:\[screenshot\]\n?)+$/, "")
    : content;
}
export function currentImageMessage(history, live) {
  if (!live?.userImages?.length || typeof live.imageBoundary !== "number")
    return null;
  const projected = [
    live.userText,
    ...live.userImages.map(() => "[screenshot]"),
  ]
    .filter(Boolean)
    .join("\n");
  const candidates = history.filter((message) => {
    if (
      message.role !== "user" ||
      typeof message.id !== "number" ||
      message.id <= live.imageBoundary
    )
      return false;
    if (message.content === projected) return true;
    const images = imageParts(message.content);
    return (
      images.length === live.userImages.length &&
      images.every((image, i) => image.url === live.userImages[i].url)
    );
  });
  return candidates.length === 1 ? candidates[0] : null;
}
export function messageImages(message) {
  const native = imageParts(message.content);
  if (native.length) return native;
  if (Array.isArray(message.browserImages))
    return message.browserImages.map((image) => ({ ...image, cached: true }));
  return Array.from(
    { length: Math.min(4, placeholderCount(message.content)) },
    (_, i) => ({
      name: `Image ${i + 1}`,
      unavailable:
        "Hermes saved an image placeholder. The original is not available in this browser.",
    }),
  );
}
export function imageParts(content) {
  if (!Array.isArray(content)) return [];
  return content
    .filter((p) => p && ["image_url", "input_image", "image"].includes(p.type))
    .map((p, i) => {
      let url =
        typeof p.image_url === "string" ? p.image_url : p.image_url?.url;
      if (p.type === "image" && p.source?.type === "base64")
        url = `data:${p.source.media_type};base64,${p.source.data}`;
      const safe =
        typeof url === "string" &&
        /^data:image\/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$/.test(url) &&
        url.length < 3 * 1024 * 1024;
      // External images remain explicit links; never load arbitrary hosts or server paths.
      const external = typeof url === "string" && /^https?:\/\//i.test(url);
      return {
        url: safe ? url : null,
        href: external ? url : null,
        name:
          typeof p.name === "string" ? p.name.slice(0, 160) : `Image ${i + 1}`,
      };
    });
}
function dataURL(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () =>
      reject(new Error("This image could not be read. Attach it again."));
    reader.readAsDataURL(blob);
  });
}
export async function prepareImage(file) {
  if (!TYPES.has(file.type))
    throw new Error("Choose a PNG, JPEG, WebP, or GIF image.");
  if (file.size > 10 * 1024 * 1024)
    throw new Error("Choose an image smaller than 10 MB.");
  const objectURL = URL.createObjectURL(file);
  let blob = file,
    resized = false;
  try {
    const image = new Image();
    image.src = objectURL;
    try {
      await image.decode();
    } catch {
      throw new Error("This image could not be opened. Try another file.");
    }
    if (
      !image.naturalWidth ||
      image.naturalWidth * image.naturalHeight > 40_000_000
    )
      throw new Error(
        "This image is too large. Choose one with fewer than 40 million pixels.",
      );
    if (
      file.size > MAX_IMAGE ||
      Math.max(image.naturalWidth, image.naturalHeight) > 2560
    ) {
      const scale = Math.min(
        1,
        2560 / Math.max(image.naturalWidth, image.naturalHeight),
      );
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      const context = canvas.getContext("2d");
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      for (const quality of [0.9, 0.75, 0.55]) {
        blob = await new Promise((resolve) =>
          canvas.toBlob(resolve, "image/jpeg", quality),
        );
        if (blob && blob.size <= MAX_IMAGE) break;
      }
      resized = true;
    }
    if (!blob || blob.size > MAX_IMAGE)
      throw new Error("This image is still too large. Try a smaller version.");
    return {
      id: crypto.randomUUID(),
      name: file.name || "Pasted image",
      url: await dataURL(blob),
      size: blob.size,
      resized,
    };
  } finally {
    URL.revokeObjectURL(objectURL);
  }
}
