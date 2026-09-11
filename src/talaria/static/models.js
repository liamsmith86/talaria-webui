import { readStorage, writeStorage } from "./lib.js";

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
  auto: "Auto",
  none: "Off",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Maximum",
  ultra: "Ultra",
};
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
    : ["low", "medium", "high"];
  return [
    "auto",
    ...(model?.capabilities?.can_disable_reasoning === true ? ["none"] : []),
    ...efforts,
  ];
}
function readChoices(key, valid) {
  try {
    return Object.fromEntries(
      Object.entries(JSON.parse(readStorage(key, "{}")))
        .slice(-200)
        .filter(([, value]) => valid(value)),
    );
  } catch {
    return {};
  }
}
function saveChoice(key, choices, id, value) {
  const next = { ...choices };
  delete next[id];
  next[id] = value;
  const bounded = Object.fromEntries(Object.entries(next).slice(-200));
  writeStorage(key, JSON.stringify(bounded));
  return bounded;
}
export function readReasoningChoices() {
  return readChoices(
    "session-reasoning",
    (value) =>
      typeof value === "string" && Object.hasOwn(reasoningNames, value),
  );
}
export function saveReasoningChoice(choices, id, value) {
  return saveChoice(
    "session-reasoning",
    choices,
    id,
    Object.hasOwn(reasoningNames, value) ? value : "auto",
  );
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

export function readModelChoices() {
  return readChoices(
    "session-models",
    (model) => model === null || valid(model),
  );
}
export function saveModelChoice(choices, id, model) {
  return saveChoice(
    "session-models",
    choices,
    id,
    valid(model) ? { id: model.id, provider: model.provider } : null,
  );
}

export function sessionModel(app) {
  const selected = app.active ? app.modelChoices[app.active] : app.draftModel;
  if (!selected) return null;
  return (
    app.models.find(
      (m) => m.id === selected.id && m.provider === selected.provider,
    ) || {
      ...selected,
      label: selected.id,
      providerLabel:
        app.providers.find((p) => p.id === selected.provider)?.name ||
        selected.provider,
    }
  );
}
