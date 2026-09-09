let csrf = '';
export const setCSRF = value => { csrf = value; };
export class RequestError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}
export async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`/api${path}`, {
      credentials: 'same-origin', ...options,
      headers: { 'Content-Type': 'application/json', 'X-Talaria-Request': '1', 'X-CSRF-Token': csrf, ...options.headers },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new RequestError('Connection interrupted. Your work is still with Hermes.', 0, 'offline');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new RequestError(data.error || 'Something went wrong. Please try again.', response.status, data.code);
  return data;
}
