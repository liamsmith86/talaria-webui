import { html, useState } from "./lib.js";
import { Dialog } from "./dialogs.js";

function Thumbnail({ image, onOpen }) {
  const [failed, setFailed] = useState(null);
  if (!image.url || failed === image.url)
    return html`<span class="image-unavailable"
      >${image.href
        ? html`<a href=${image.href} target="_blank" rel="noopener noreferrer"
            >Open image</a
          >`
        : image.unavailable || "Image unavailable in this transcript"}</span
    >`;
  return html`<button
    class="message-image"
    aria-label=${`Open ${image.name}`}
    onClick=${onOpen}
  >
    <img
      src=${image.url}
      alt=${image.name}
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
      >Original kept in this browser</small
    >`}
    ${selected &&
    html`<${Dialog} title=${selected.name} wide className="image-dialog" onClose=${() => setSelected(null)}><img src=${selected.url} alt=${selected.name}/><a class="text-button image-download" href=${selected.url} download=${selected.name}>Download image</a></${Dialog}>`}
  </div>`;
}
