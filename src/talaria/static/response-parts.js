// Presentation order is separate from the text used for copying and recovery.
// Retain settled parts when appending to the current text block.
export function responseParts(live) {
  if (live.parts) return live.parts;
  return [
    ...(live.reasoning ? [{ kind: "reasoning", text: live.reasoning }] : []),
    ...(live.tools || []).map((tool) => ({
      kind: "tool",
      id: tool.id,
      agent: tool.kind === "agent",
    })),
    ...(live.text ? [{ kind: "text", text: live.text }] : []),
  ];
}

export function appendText(parts, delta) {
  const last = parts.at(-1);
  return last?.kind === "text" && !last.interim
    ? [...parts.slice(0, -1), { ...last, text: last.text + delta }]
    : [...parts, { kind: "text", text: delta }];
}

export function finishText(live, output) {
  const parts = responseParts(live);
  // The terminal output is the final model call, whereas deltas span every
  // tool round. Keep earlier prose when reconciling that final call.
  const last = parts.at(-1);
  if (last?.kind === "text" && !last.interim && last.text === output) return parts;
  if (last?.kind === "text" && !last.interim)
    return [...parts.slice(0, -1), { ...last, text: output }];
  return [...parts, { kind: "text", text: output }];
}

export function interimText(live, text, alreadyStreamed) {
  const parts = responseParts(live);
  const last = parts.at(-1);
  if (!alreadyStreamed) return [...parts, { kind: "text", text, interim: true }];
  if (last?.kind === "text" && last.text.trim() === text.trim())
    return [...parts.slice(0, -1), { ...last, interim: true }];
  return parts;
}
