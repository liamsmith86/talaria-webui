import { html, useState } from "./lib.js";
import { Dialog } from "./dialogs.js";
import { t } from "./i18n.js";
import { imageName } from "./attachments.js";

function Thumbnail({ image, onOpen }) {
  const [failed, setFailed] = useState(null);
  if (!image.url || failed === image.url)
    return html`<span class="image-unavailable"
      >${image.href
        ? html`<a href=${image.href} target="_blank" rel="noopener noreferrer"
            >${t("Open image")}</a
          >`
        : t(image.unavailable) || t("Image unavailable in this transcript")}</span
    >`;
  return html`<button
    class="message-image"
    aria-label=${t("Open {name}", { name: imageName(image) })}
    onClick=${onOpen}
  >
    <img
      src=${image.url}
      alt=${imageName(image)}
      loading="lazy"
      decoding="async"
      onError=${() => setFailed(image.url)}
    />
  </button>`;
}
export function Images({ images }) {
  const [selected, setSelected] = useState(null);
  if (!images?.length) return null;
  return html`<div class="message-images">
    ${images.map(
      (image, i) =>
        html`<${Thumbnail}
          key=${image.id || i}
          image=${image}
          onOpen=${() => setSelected(image)}
        />`,
    )}
    ${images.some((image) => image.cached) &&
    html`<small class="image-retention-note"
      >${t("Original kept in this browser")}</small
    >`}
    ${selected &&
    html`<${Dialog} title=${imageName(selected)} wide className="image-dialog" onClose=${() => setSelected(null)}><img src=${selected.url} alt=${imageName(selected)}/><a class="text-button image-download" href=${selected.url} download=${selected.name}>${t("Download image")}</a></${Dialog}>`}
  </div>`;
}
