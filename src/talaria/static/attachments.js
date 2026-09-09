// Browser drafts and pending submission receipts only. Hermes stores sent images.
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
