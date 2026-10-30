import { readStorage, writeStorage } from "./lib.js";
import { t, msg } from "./i18n.js";

// Project only supported catalog fields; incomplete discovery must not break chat.
const text = (value) =>
  typeof value === "string" && value.length <= 256 ? value.trim() : "";
const record = (value) =>
  value && typeof value === "object" && !Array.isArray(value);
const providerId = (p) =>
  text(p.slug) || text(p.id) || text(p.provider) || text(p.name);
const providerName = (p) => text(p.name) || text(p.label) || providerId(p);

export function modelInventory(data) {
  data = record(data) ? data : {};
  const providers = Array.isArray(data.providers)
    ? data.providers.filter(record).slice(0, 200)
    : [];
  const models = providers
    .filter((p) => p.authenticated !== false || p.is_current === true)
    .flatMap((p) =>
      (Array.isArray(p.models) ? p.models.slice(0, 2000) : [])
        .map((m) => {
          const id =
            typeof m === "string"
              ? text(m)
              : record(m)
                ? text(m.id) || text(m.model) || text(m.name)
                : "";
          return {
            id,
            provider: providerId(p),
            label: record(m) ? text(m.name) || text(m.label) || id : id,
            providerLabel: providerName(p),
            available: !(
              Array.isArray(p.unavailable_models) &&
              p.unavailable_models.includes(id)
            ),
            featured:
              Array.isArray(p.featured_models) &&
              p.featured_models.includes(id),
            capabilities: record(p.capabilities?.[id])
              ? p.capabilities[id]
              : {},
            pricing: record(p.pricing?.[id])
              ? {
                  input: text(p.pricing[id].input),
                  output: text(p.pricing[id].output),
                  cache: text(p.pricing[id].cache),
                  free: p.pricing[id].free === true,
                }
              : null,
            warning: text(p.warning),
          };
        })
        .filter((m) => m.id),
    );
  const configuredProvider = text(data.provider);
  const provider =
    providers.find((p) => providerId(p) === configuredProvider) ||
    providers.find((p) => p.is_current === true);
  const defaultId = text(data.model);
  const defaultProvider =
    configuredProvider || (provider ? providerId(provider) : "");
  const defaultModel = defaultId
    ? {
        ...models.find(
          (m) => m.id === defaultId && m.provider === defaultProvider,
        ),
        id: defaultId,
        label: defaultId,
        provider: defaultProvider,
        providerLabel: provider ? providerName(provider) : configuredProvider,
        configuredReasoning: text(data.configured_reasoning),
      }
    : null;
  return {
    models,
    defaultModel,
    providers: providers
      .filter((p) => p.authenticated === true || p.is_current === true)
      .map((p) => ({
        id: providerId(p),
        name: providerName(p),
      })),
  };
}

export const reasoningNames = {
  auto: msg("Auto"),
  none: msg("Off"),
  minimal: msg("Minimal"),
  low: msg("Low"),
  medium: msg("Medium"),
  high: msg("High"),
  xhigh: msg("Extra high"),
  max: msg("Maximum"),
  ultra: msg("Ultra"),
};
export function reasoningLabel(value, model) {
  const configured = model?.capabilities?.configured_reasoning || model?.configuredReasoning;
  return t(reasoningNames[value === "auto" && Object.hasOwn(reasoningNames, configured)
    ? configured : value] || reasoningNames.auto);
}
export function reasoningOptions(model) {
  if (model?.capabilities?.reasoning === false) return ["auto"];
  const supported = model?.capabilities?.supported_efforts;
  const efforts = Array.isArray(supported)
    ? supported.filter(
        (v) =>
          typeof v === "string" &&
          Object.hasOwn(reasoningNames, v) &&
          !["auto", "none"].includes(v),
      )
    : ["low", "medium", "high", "xhigh", "max"];
  return [
    "auto",
    ...(model?.capabilities?.can_disable_reasoning === true ? ["none"] : []),
    ...efforts,
  ];
}
// Only the identity of a choice is saved; the catalog supplies the rest.
export function reasoningChoice(value) {
  return Object.hasOwn(reasoningNames, value) ? value : "auto";
}
export function sessionReasoning(app) {
  const choice = app.active
    ? app.reasoningChoices[app.active]
    : app.draftReasoning;
  return reasoningOptions(sessionModel(app) || app.defaultModel).includes(
    choice,
  )
    ? choice
    : "auto";
}

function valid(model) {
  return (
    model &&
    typeof model.id === "string" &&
    model.id.length <= 256 &&
    typeof model.provider === "string" &&
    model.provider.length <= 256
  );
}

export function modelChoice(model) {
  return valid(model) ? { id: model.id, provider: model.provider } : null;
}

// Talaria 0.3.20 and earlier kept these choices, and a running command, in
// each browser's storage for the profile. Hand the choices to Talaria's server
// once, which keeps any choice another device already saved. Its own record of
// commands replaces the rest.
export async function importBrowserChoices(api) {
  const read = (key) => {
    try {
      const value = JSON.parse(readStorage(key, "{}"));
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch {
      return {};
    }
  };
  const models = read("session-models"), reasoning = read("session-reasoning");
  const sessions = {};
  for (const [id, model] of Object.entries(models).slice(-200))
    if (model === null || valid(model)) sessions[id] = { model: modelChoice(model) };
  for (const [id, value] of Object.entries(reasoning).slice(-200))
    if (typeof value === "string" && Object.hasOwn(reasoningNames, value))
      sessions[id] = { ...sessions[id], reasoning: value };
  const imported = Object.keys(sessions).length > 0;
  if (imported) await api("/preferences/import", { method: "POST", body: { sessions } });
  for (const key of ["session-models", "session-reasoning", "command.pending"])
    if (readStorage(key)) writeStorage(key, "");
  return imported;
}

export function sessionModel(app) {
  let selected = app.active ? app.modelChoices[app.active] : app.draftModel;
  if (app.active && !Object.hasOwn(app.modelChoices, app.active) && app.sessionDetails?.id === app.active) {
    const id = text(app.sessionDetails.model);
    if (id && id !== "hermes-agent") selected = { id, provider: "", inherited: true };
  }
  if (!selected) return null;
  return (
    (!selected.inherited && app.models.find(
      (m) => m.id === selected.id && m.provider === selected.provider,
    )) || {
      ...selected,
      label: selected.id,
      providerLabel:
        app.providers.find((p) => p.id === selected.provider)?.name ||
        selected.provider,
    }
  );
}
